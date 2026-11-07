#!/usr/bin/env python3
"""Standalone-first offline dictionary lookup core.

This module has no UI or DMS dependency. It invokes the local ``sdcv`` binary
with metadata-only argv and a bounded stdin protocol, reads local configuration,
and returns JSON-serialisable data.
"""

from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import os
import pathlib
import re
import selectors
import signal
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from dank_lookup_detectors import detect as detect_data
from dank_lookup_sdcv_stdin import (
    DEFAULT_STDOUT_CAP_BYTES,
    MAIN_PROMPT,
    STDERR_CAP_BYTES,
    SdcvProtocolError,
    SdcvSession,
    parse_frame,
    protocol_argv,
)


SCHEMA_VERSION = 2
MAX_QUERY_CHARS = 4096
TOKENIZER_STDOUT_CAP_BYTES = 1024 * 1024
TOKENIZER_STDERR_CAP_BYTES = 1024 * 1024
MAX_HOMOGRAPH_CHOICES = 8
# Internal marker set by the v1 config mapping; never read from user config.
V1_FUZZY_FALLBACK_KEY = "_v1_fuzzy_fallback_only"
# How many fuzzy candidates SdcvClient.query() fetches; see its docstring.
CANDIDATES_ALL = "all"
CANDIDATES_VARIANTS = "variants"
CANDIDATES_FALLBACK = "fallback"
INCOMPLETE_SOURCE_WARNINGS = frozenset({"lookup_budget_exhausted", "sdcv_source_timeout"})
# Four UTF-8 bytes per Unicode scalar keeps every inline body within the CLI's
# 8 MiB open-text stdin limit. Larger complete bodies must use open-ref.
MAX_INLINE_FULL_CHARS = 2 * 1024 * 1024
_MIN_SHARED_PREVIEW_CHARS = 600
TRUNCATION_MARKER = "[… preview truncated; Open full for complete entry …]"
CANTONESE_MARKERS = frozenset("嘅喺唔冇咗啲嗰佢哋噉咁乜嘢嚟喎囉㗎")
CANTONESE_PHRASES = (
    "點解", "係咪", "屋企", "瞓覺", "返工", "放工", "得閒", "幾多", "乜嘢",
)


def builtin_config() -> dict[str, Any]:
    """Return a dependency-free four-mode fallback configuration.

    Dictionary files are never bundled. A mode is reported unavailable until
    at least one named local StarDict source is actually visible to sdcv.
    """

    return {
        "version": 2,
        "stardict_data_dir": [
            os.path.expanduser("~/.local/share/stardict/dic"),
            "/usr/share/stardict/dic",
        ],
        "definition_format_policy": {
            "automatic": "ifo_exact_bookname_sametypesequence",
            "fallback": "plain",
        },
        "preview": {
            "max_entry_chars": 4000,
            "max_body_chars": 12000,
            "max_inline_full_chars": 262144,
        },
        "lookup_timeout_ms": 3000,
        "source_timeout_ms": 1500,
        "modes": {
            "han": {
                "label": "漢語",
                "enabled": True,
                "default_for": ["han"],
                "show_related": False,
                "sources": [
                    {"name": "萌典重編國語辭典", "when": "han", "exact_only": True, "allow_related": False},
                    {"name": "CC-CEDICT 漢英辭典", "when": "han", "exact_only": True, "allow_related": False},
                ],
            },
            "en": {
                "label": "English",
                "enabled": True,
                "default_for": ["latin"],
                "show_related": False,
                "sources": [
                    {"name": "dictd_www.dict.org_gcide", "when": "latin", "exact_only": False, "allow_related": True},
                    {"name": "WordNet", "when": "latin", "exact_only": False, "allow_related": True},
                ],
            },
            "ja": {
                "label": "日本語",
                "enabled": True,
                "default_for": ["kana", "han"],
                "priority_boost": ["kana"],
                "show_related": False,
                "sources": [
                    {"name": "org.edrdg.jmdict-jpn-eng", "when": ["kana", "han"], "exact_only": True, "allow_related": False},
                    {"name": "reader.dict JA", "when": ["kana", "han"], "exact_only": True, "allow_related": False},
                ],
            },
            "yue": {
                "label": "粵語",
                "enabled": True,
                "default_for": ["han"],
                "priority_boost": ["cantonese_marker"],
                "show_related": False,
                "sources": [
                    {"name": "words.hk 粵語詞典", "when": "han", "exact_only": True, "allow_related": False},
                ],
            },
        },
        "fallback": {"unfiltered": False, "mock": False},
    }


class _PlainTextParser(HTMLParser):
    """Text of an HTML definition, one ``_emit`` call per piece.

    Besides element text this keeps text that some dictionaries carry only in
    attributes, because dropping it loses dictionary content. 王力古漢語字典
    writes the headword inside quotations as ``<w w="春"></w>`` and sense
    numbers as ``<p n="❶">``; its stylesheet shows them with ``attr()``.
    """

    _BLOCK_TAGS = frozenset({"br", "p", "div", "li", "tr", "hr", "section"})
    # Attributes whose value is shown in front of a paragraph (王力 sense
    # numbers ❶ and ①, the 備考 section label).
    _LABEL_ATTRIBUTES = {"p": ("n", "num"), "div": ("t",)}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._lists: list[list[Any]] = []
        self._attribute_word: list[str | None] = []
        self._header_depth = 0

    # Subclasses record styling here; flags name the extra styles of a piece.
    def _emit(self, text: str, flags: frozenset[str] = frozenset()) -> None:
        self.parts.append(text)

    def _ends_with(self, characters: str) -> bool:
        for part in reversed(self.parts):
            if part:
                return part[-1] in characters
        return True

    def _ends_with_newline(self) -> bool:
        return self._ends_with("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        values = {key.casefold(): (value or "") for key, value in attrs}
        self._open(name, values)
        if name in self._BLOCK_TAGS and not (name == "li" and self._ends_with_newline()):
            self._emit("\n")
        if name in {"ol", "ul"}:
            style = values.get("style", "").casefold().replace(" ", "")
            alpha = "lower-alpha" in style or values.get("type") == "a"
            self._lists.append([name, 0, alpha])
        elif name == "li":
            self._emit(self._list_marker(), frozenset({"b", "d"}) if self._lists and self._lists[-1][0] == "ol" else frozenset({"d"}))
        for attribute in self._LABEL_ATTRIBUTES.get(name, ()):
            label = values.get(attribute, "").strip()
            if label:
                self._emit(label + " ", frozenset({"b", "d"}))
                break
        if name in {"tou", "yin"}:
            # 王力 headword line and reading lines: start a new line, and let
            # the following sense paragraph start right below them.
            if not self._ends_with_newline() or name == "yin":
                self._emit("\n")
            self._header_depth += 1
        elif self._header_depth and (
            name in {"py", "mc", "gu"} or (name == "a" and ("pg" in values or "rs" in values))
        ):
            # Separate the page, radical, pinyin and Middle Chinese columns
            # of the 王力 headword line instead of running them together.
            if not self._ends_with("\n "):
                self._emit(" ")
        if name == "w":
            self._attribute_word.append(values.get("w", "").strip() or None)

    def _open(self, name: str, values: Mapping[str, str]) -> None:
        """Hook for subclasses, called before anything is emitted."""

    def _list_marker(self) -> str:
        if not self._lists or self._lists[-1][0] != "ol":
            return "• "
        self._lists[-1][1] += 1
        number = self._lists[-1][1]
        if self._lists[-1][2] and number <= 26:
            return f"{chr(ord('a') + number - 1)}. "
        return f"{number}. "

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name == "w" and self._attribute_word:
            word = self._attribute_word.pop()
            if word is not None:
                self._emit(word, frozenset({"b"}))
        if name in self._BLOCK_TAGS:
            self._emit("\n")
        if name in {"ol", "ul"}:
            for index in range(len(self._lists) - 1, -1, -1):
                if self._lists[index][0] == name:
                    del self._lists[index:]
                    break
        if name in {"tou", "yin"} and self._header_depth:
            self._header_depth -= 1
        self._close(name)

    def _close(self, name: str) -> None:
        """Hook for subclasses, called after the end tag is handled."""

    def handle_data(self, data: str) -> None:
        if self._attribute_word and self._attribute_word[-1] is not None and data:
            # The element has its own text after all; keep that instead.
            self._attribute_word[-1] = None
        self._emit(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_TRAILING_HORIZONTAL_SPACE = re.compile(r"[ \t]+(?=\n|$)")
_IFO_MAX_BYTES = 1024 * 1024
_IFO_MAX_FILES = 2048
_IFO_MAX_DIRECTORIES = 4096


def _normalise_definition_text(dictionary_name: str, text: str) -> str:
    if _CONTROL_CHARACTERS.search(text):
        text = _CONTROL_CHARACTERS.sub("", text)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if _TRAILING_HORIZONTAL_SPACE.search(text):
        text = _TRAILING_HORIZONTAL_SPACE.sub("", text)
    name = dictionary_name.casefold()
    if "gcide" in name or "webster" in name:
        text = re.sub(r"(?m)^\s*\[1913 Webster\]\s*$", "", text)
        text = re.sub(r"(?m)^(\d+\.)", r"\n\1", text)
    if "\n\n\n" in text:
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_definition(
    dictionary_name: str,
    value: str,
    *,
    definition_format: str = "plain",
) -> str:
    """Clean one definition according to reliable source metadata.

    Plain text is the conservative default. In particular, angle-bracketed
    prose such as ``vector<T>`` is retained unless the source was explicitly
    classified as HTML.
    """

    raw = str(value)
    if definition_format != "html":
        return _normalise_definition_text(dictionary_name, raw)

    parser = _PlainTextParser()
    try:
        parser.feed(raw)
        parser.close()
        text = html.unescape("".join(parser.parts))
    except Exception:
        # Do not guess at markup when even an explicitly HTML-labelled source
        # cannot be parsed. Preserving literal text is the lossless fallback.
        text = raw
    return _normalise_definition_text(dictionary_name, text)


# ---- Display styling ---------------------------------------------------------
# The card can show light typographic hierarchy (bold, italic, dimmed text) the
# way a dictionary page does. Styling is advisory: it never changes the text.
# ``style_runs`` returns runs whose concatenated text is exactly the plain
# body, so Copy/Open and every plain-text consumer are unaffected. Flags are
# "b" (bold), "i" (italic) and "d" (dimmed/secondary).

_STYLE_BOLD_TAGS = frozenset({"b", "strong", "hw", "r", "ci"})
_STYLE_ITALIC_TAGS = frozenset({"i", "em"})
_STYLE_DIM_TAGS = frozenset({"rt", "rp", "sup", "sub", "py", "gu", "mc"})
_STYLE_MAX_ALIGN_CHARS = 20000


class _StyledTextParser(_PlainTextParser):
    """_PlainTextParser that also records a flag set per emitted part."""

    def __init__(self) -> None:
        super().__init__()
        self.flags: list[frozenset[str]] = []
        self._stack: list[tuple[str, frozenset[str]]] = []

    def _current(self) -> frozenset[str]:
        return self._stack[-1][1] if self._stack else frozenset()

    def _emit(self, text: str, flags: frozenset[str] = frozenset()) -> None:
        self.parts.append(text)
        self.flags.append(frozenset() if text == "\n" else self._current() | flags)

    def _open(self, name: str, values: Mapping[str, str]) -> None:
        if name in self._BLOCK_TAGS and name not in {"p", "div", "li", "section"}:
            return
        added: set[str] = set()
        if name in _STYLE_BOLD_TAGS:
            added.add("b")
        if name in _STYLE_ITALIC_TAGS:
            added.add("i")
        if name in _STYLE_DIM_TAGS or (name == "a" and ("pg" in values or "rs" in values)):
            added.add("d")
        lowered = {key: value.casefold() for key, value in values.items()}
        if lowered.get("color") or "color:" in lowered.get("style", ""):
            added.add("d")
        if lowered.get("class") == "grammar":
            added.update({"i", "d"})
        self._stack.append((name, self._current() | added))

    def _close(self, name: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == name:
                del self._stack[index:]
                break


def _html_flags(raw: str, plain: str) -> list[set[str]] | None:
    """Per-character flags of ``plain`` taken from the HTML markup."""

    if len(raw) > _STYLE_MAX_ALIGN_CHARS:
        return None
    parser = _StyledTextParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return None
    source_chars: list[str] = []
    source_flags: list[frozenset[str]] = []
    for text, flags in zip(parser.parts, parser.flags):
        source_chars.extend(text)
        source_flags.extend([flags] * len(text))
    source = "".join(source_chars)
    result: list[set[str]] = [set() for _ in plain]
    # Fast path: the cleaned text usually differs from the parser output only
    # by surrounding whitespace.
    lead = len(source) - len(source.lstrip())
    if source.strip() == plain:
        for index in range(len(plain)):
            result[index].update(source_flags[lead + index])
        return result
    # Cleaning only deletes characters (control characters, trailing spaces,
    # surplus blank lines), so plain is normally a subsequence of source and a
    # linear walk aligns them. Anything else falls back to difflib.
    cursor = 0
    for index, char in enumerate(plain):
        found = source.find(char, cursor)
        if found < 0 or found - cursor > 64:
            break
        result[index].update(source_flags[found])
        cursor = found + 1
    else:
        return result
    result = [set() for _ in plain]
    import difflib  # rare fallback; kept off the one-shot import path

    matcher = difflib.SequenceMatcher(None, source, plain, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(a1 - a0):
            result[b0 + offset].update(source_flags[a0 + offset])
    return result


_HAN = re.compile(r"[㐀-鿿]")
# Line-level rules for plain-text sources. Each pattern names the span to
# style; none of them changes text.
_LINE_RULES: list[tuple[re.Pattern[str], str]] = [
    # Sense numbers: "1. ", "2) ", "1 ▶ ", "(3) ", circled numbers.
    (re.compile(r"^\s*(\(?\d{1,3}[.)]?(?=\s)|[①-⑳❶-❿])"), "bd"),
    # Romanisation/pronunciation in square brackets at line start: [hao3].
    (re.compile(r"^\s*(\[[^\]\n㐀-鿿]{1,60}\])"), "d"),
    # Part-of-speech tags in brackets with Han text: [形], [動詞].
    (re.compile(r"^\s*(\[[㐀-鿿]{1,6}\])"), "d"),
    # Latin part-of-speech abbreviations at line start or after a sense marker.
    (re.compile(r"^(?:\s*\d{1,3}\s*▶\s*|\s*)((?:vt|vi|vb|v|n|a|adj|adv|int|interj|prep|conj|pron|abbr|abbrev|aux|num|art|det|phr|pref|prefix|suffix|anagr|alt)\.(?:\s*[a-z]+\.)*)(?=\s|$)"), "id"),
    # WordNet senses: "adj 1:", "n 2:", "v :" and continued "3:".
    (re.compile(r"^\s*(n|v|adj|adv)(?=\s+(?:\d{1,3})?\s*:)"), "id"),
    (re.compile(r"^\s*(?:(?:n|v|adj|adv)\s+)?(\d{1,3}):"), "bd"),
    # words.hk readings with Han variant prefixes: [hang4,衡:hang4].
    (re.compile(r"^\s*(\[[a-z]+[1-6](?:(?:[ ,:]|[㐀-鿿]{1,6}:)+[a-z]+[1-6])*\])"), "d"),
    # Example / quotation / note lines (Chinese dictionaries).
    (re.compile(r"^\s*((?:例|引|如|英|註|注|參見|見)：.*)$"), "d"),
    (re.compile(r"^\s*((?:部首|總筆畫|筆畫|部外筆畫)：.*)$"), "d"),
    # words.hk example: sentence followed by a jyutping gloss in parentheses.
    (re.compile(r"^(.+\s\((?:[a-z]+[1-6][ ,.!?;:'\-]*)+[^()\n]*\))$"), "d"),
    # GCIDE quotation attribution lines ("--T. Burnet.").
    (re.compile(r"^(\s+.*--\s?[A-Z][^\n]*)$"), "id"),
]
_INLINE_RULES: list[tuple[re.Pattern[str], str]] = [
    # IPA between slashes: either one unbroken token or text with a non-ASCII
    # (IPA) letter, so prose such as "and/or ... and/" stays undimmed.
    (re.compile(r"(/(?:[^/\s㐀-鿿]{1,60}|(?=[^/\n]*[^\x00-\x7f])[^/\n㐀-鿿]{1,60})/)"), "d"),
    # GCIDE pronunciation between backslashes.
    (re.compile(r"(\\[^\\\n]{1,60}\\)"), "d"),
    # GCIDE etymology/inflection brackets.
    (re.compile(r"(\[(?:Etym|imp\.|p\. p\.|pl\.|R\.)[^\]\n]{0,200}\])"), "d"),
    # Sense separators in CC-CEDICT and JMdict, JMdict arrows and markers.
    (re.compile(r"(\s[|¦](?:\s|$))"), "d"),
    # JMdict sense numbers after a "‖" separator.
    (re.compile(r"‖\s*(\d{1,3})(?=\s*▶)"), "bd"),
    (re.compile(r"(→|█|▊|▶)"), "d"),
]


# Rules that may span hard-wrapped lines. Brackets allow one nested level
# because GCIDE writes accents as [e^] inside [imp. ...].
_SPAN_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\[(?:Etym|imp\.|p\. p\.|pl\.|R\.)(?:[^\[\]]|\[[^\[\]\n]{0,8}\]){0,400}\])"), "d"),
    # WordNet synonym/antonym/also lists.
    (re.compile(r"(\[(?:syn|ant|also):(?:[^\[\]]|\[[^\[\]\n]{0,8}\]){0,800}\])"), "d"),
    # WordNet quoted examples.
    (re.compile(r'((?<=[;:] )"[^"]{1,400}"|(?<=[;:])"[^"]{1,400}")'), "d"),
]


# Line rules a per-dictionary formatter may switch off (see _FORMATTERS).
_POS_LINE_RULE = 3
_ATTRIBUTION_LINE_RULE = len(_LINE_RULES) - 1


def _rule_flags(
    plain: str, flags: list[set[str]], skip_line_rules: frozenset[int] = frozenset()
) -> None:
    for pattern, styles in _SPAN_RULES:
        for match in pattern.finditer(plain):
            for index in range(match.start(1), match.end(1)):
                if plain[index] != "\n":
                    flags[index].update(styles)
    start = 0
    for line in plain.split("\n"):
        for number, (pattern, styles) in enumerate(_LINE_RULES):
            if number in skip_line_rules:
                continue
            match = pattern.search(line)
            if match:
                italic_ok = not _HAN.search(match.group(1))
                for index in range(start + match.start(1), start + match.end(1)):
                    flags[index].update(s for s in styles if s != "i" or italic_ok)
        for pattern, styles in _INLINE_RULES:
            for match in pattern.finditer(line):
                for index in range(start + match.start(1), start + match.end(1)):
                    flags[index].update(styles)
        start += len(line) + 1


# ---- Per-dictionary formatters --------------------------------------------------
# A few plain-text sources have a house notation that the generic rules cannot
# read safely. Their formatters add (or, for a generic rule that misfires on
# that source, skip) style flags; like every rule they never change text.


def _mark(flags: list[set[str]], plain: str, start: int, end: int, styles: str) -> None:
    for index in range(start, end):
        if plain[index] != "\n":
            flags[index].update(styles)


def _mark_groups(
    pattern: re.Pattern[str], plain: str, flags: list[set[str]], styles: dict[int, str]
) -> None:
    for match in pattern.finditer(plain):
        for group, style in styles.items():
            if match.start(group) >= 0:
                _mark(flags, plain, match.start(group), match.end(group), style)


def _blocks(plain: str, separator: re.Pattern[str]) -> Iterable[tuple[int, int]]:
    """(start, end) offsets of the blocks between separator matches."""

    start = 0
    for match in separator.finditer(plain):
        if match.start() > start:
            yield start, match.start()
        start = match.start()
    if start < len(plain):
        yield start, len(plain)


# Wiktionary (wikt-en-en): Lua template calls survive as "(lb en computing)",
# "(senseid en Q14579)", "(tcl de Linux id=Q14579)", "{{lb|en|...}}", and
# wikitext italics as ''X''. Entries for the same spelling in other languages
# follow the English ones without a language name; their labels carry the
# language code, which is how they are found and greyed.
_WIKT_LABEL_NAMES = (
    r"lb|lbl|label|tlb|senseid|tcl|cln|anagrams|alt form|alt|alternative form of"
    r"|alternative spelling of|initialism of|abbreviation of|acronym of|&amp;lit|&lit"
    # Form-of templates: "(noun form of et rebane gen s)", "(plural of en x)".
    r"|(?:[a-z]+ ){0,2}(?:form|forms|plural|inflection|tense|participle|comparative"
    r"|superlative|singular|spelling|genitive|dative|accusative|nominative|diminutive"
    r"|feminine|masculine|contraction|clipping|synonym) of"
)
_WIKT_LABEL = re.compile(
    r"(\((?:" + _WIKT_LABEL_NAMES + r"|\+obj|q|qual|qualifier|gl|gloss|i|sense|non-gloss"
    r"|[a-z]{2,3}-[a-z-]+(?: of)?):?\s[^()\n]*(?:\([^()\n]*\)[^()\n]*)*\))"
)
_WIKT_LINK = re.compile(r"(\((?:w:\s*|[lm] [a-z]{2,3}(?:-[a-z]{2,3})? ))([^()\n]+)(\))")
_WIKT_BRACES = re.compile(r"(\{\{[^{}\n]*\}\})")
_WIKT_ITALIC = re.compile(r"('')([^'\n]{1,200})('')")
_WIKT_HASH = re.compile(r"(?:^|(?<=\s))(#)(?=\s|[A-Za-z])", re.M)
_WIKT_COMMENT = re.compile(r"(&lt;!--.*?--&gt;|<!--.*?-->)")
_WIKT_POS_LINE = re.compile(r"(?m)^[a-z]{1,10}\.$")
_WIKT_LANGUAGE = re.compile(
    r"\((?:" + _WIKT_LABEL_NAMES + r")\s+([a-z]{2,3}(?:-[a-z]{2,3})?)\b"
    r"|\(([a-z]{2,3})-[a-z-]+[\s:]|\{\{[^|{}\n]+\|([a-z]{2,3}(?:-[a-z]{2,3})?)\|"
)


def _format_wiktionary(plain: str, flags: list[set[str]]) -> None:
    _mark_groups(_WIKT_LABEL, plain, flags, {1: "id"})
    _mark_groups(_WIKT_LINK, plain, flags, {1: "d", 3: "d"})
    _mark_groups(_WIKT_BRACES, plain, flags, {1: "d"})
    _mark_groups(_WIKT_ITALIC, plain, flags, {1: "d", 2: "i", 3: "d"})
    _mark_groups(_WIKT_HASH, plain, flags, {1: "d"})
    _mark_groups(_WIKT_COMMENT, plain, flags, {1: "d"})
    _mark_groups(re.compile(r"(?m)^([a-z]{1,10}\.)$"), plain, flags, {1: "id"})
    # A part-of-speech block whose labels name only other languages is
    # another language's entry: grey all of it.
    for start, end in _blocks(plain, _WIKT_POS_LINE):
        codes = {
            next(group for group in match.groups() if group)
            for match in _WIKT_LANGUAGE.finditer(plain, start, end)
        }
        if codes and "en" not in codes:
            _mark(flags, plain, start, end, "d")


# GCIDE (dict.org Webster 1913): "Word \Pron\ (respelling), pos. [Etym.]",
# numbered senses with (a) sub-senses, {cross references}, indented verse
# quotations with "--Author" attributions, and ASCII accent escapes such as
# [e^] or ["o] that are kept as written.
_GCIDE_HEAD = re.compile(
    r"(?m)^([^\s\\][^\\\n]{0,80}?) (\\[^\\\n]{1,80}\\)"
    r"(?:,? ?(\((?:[^()\n]|\([^()\n]*\)){1,80}\)))?"
    r"(?:,\s*((?:[a-z]{1,5}\.\s?){1,4}))?"
)
_GCIDE_BRACKET = re.compile(
    r"(\[(?!(?:[a-z\"=.^'`~-]{1,4}|root|sum)\])(?:[^\[\]]|\[[^\[\]\n]{0,8}\]){1,600}\])"
)
_GCIDE_XREF = re.compile(r"(\{)([^{}]{1,80})(\})")
_GCIDE_SUBSENSE = re.compile(r"(?m)^\s*(\([a-z]\))(?=\s)")
_GCIDE_FIELD = re.compile(
    r"(\((?:[A-Z][a-z]*(?:\[[^\]\n]{1,4}\][a-z]*)*\.)(?:\s?(?:&\s)?[A-Z][a-z]*\.){0,3}\))"
)
_GCIDE_INLINE_QUOTE = re.compile(r"(``[^`'\n]{1,300}'')")
_GCIDE_ATTRIBUTION = re.compile(r"(?m)(--\s?[A-Z][^\n]*)$")
_GCIDE_QUOTE_INDENT = 8


def _format_gcide(plain: str, flags: list[set[str]]) -> None:
    _mark_groups(_GCIDE_HEAD, plain, flags, {1: "b", 3: "d", 4: "id"})
    _mark_groups(_GCIDE_BRACKET, plain, flags, {1: "d"})
    for match in _GCIDE_XREF.finditer(plain):
        line_start = plain.rfind("\n", 0, match.start()) + 1
        leading = plain[line_start:match.start()]
        # "{Ephemeral fly} (Zool.), one of ..." opens a sub-entry; elsewhere,
        # including a wrapped line inside an [Etym.] bracket, braces mark a
        # cross reference.
        opens_entry = (leading.strip() == "" and len(leading) <= 3
                       and "d" not in flags[match.start()])
        inner = "b" if opens_entry else "i"
        _mark(flags, plain, match.start(1), match.end(1), "d")
        _mark(flags, plain, match.start(2), match.end(2), inner)
        _mark(flags, plain, match.start(3), match.end(3), "d")
    _mark_groups(_GCIDE_SUBSENSE, plain, flags, {1: "bd"})
    _mark_groups(_GCIDE_FIELD, plain, flags, {1: "id"})
    _mark_groups(_GCIDE_INLINE_QUOTE, plain, flags, {1: "id"})
    _mark_groups(_GCIDE_ATTRIBUTION, plain, flags, {1: "id"})
    # Verse and prose quotations are paragraphs indented past the sense text.
    for start, end in _blocks(plain, re.compile(r"\n[ \t]*\n")):
        paragraph = plain[start:end].lstrip("\n")
        first = paragraph.split("\n", 1)[0]
        if first.strip() and _indent(first) >= _GCIDE_QUOTE_INDENT:
            _mark(flags, plain, start, end, "id")


# JMdict (koaeH/JMdict-sdcv): headword blocks ("行₁", reading lines with
# frequency marks) then senses "1 ▶ pos → gloss ¦ gloss ‖ 2 ▶ ...", and an
# optional "━━━━" footnote block.
_JMDICT_SENSE_POS = re.compile(r"▶([^→▶‖]{1,240}?)→")
_JMDICT_LEAD_POS = re.compile(r"^([^→▶‖\n\d][^→▶‖\n]{0,160}?)\s*→")
_JMDICT_BRACKET = re.compile(r"(\[[^\[\]\n]{1,40}\])")
_JMDICT_MARKS = re.compile(r"(‖|(?<=\s)\*(?=\s|$)|[₀-₉]+)", re.M)
# A sense number wrapped away from its "▶" ("‖ 2\n▶ n. → act").
_JMDICT_NUMBER = re.compile(r"‖\s*(\d{1,3})(?=\s*▶)")


def _format_jmdict(plain: str, flags: list[set[str]]) -> None:
    _mark_groups(_JMDICT_SENSE_POS, plain, flags, {1: "id"})
    _mark_groups(_JMDICT_BRACKET, plain, flags, {1: "d"})
    _mark_groups(_JMDICT_MARKS, plain, flags, {1: "d"})
    _mark_groups(_JMDICT_NUMBER, plain, flags, {1: "bd"})
    footnote = plain.find("━━")
    if footnote >= 0:
        _mark(flags, plain, footnote, len(plain), "d")
    else:
        footnote = len(plain)
    for start, end in _blocks(plain[:footnote], re.compile(r"\n[ \t\u3000]*\n")):
        block = plain[start:end]
        offset = start + len(block) - len(block.lstrip("\n"))
        block = block.strip("\n")
        if not block:
            continue
        if "→" not in block:
            # Headword block: the spelling, then reading lines.
            head_end = offset + len(block.split("\n", 1)[0])
            _mark(flags, plain, offset, head_end, "b")
            _mark(flags, plain, head_end, offset + len(block), "d")
            continue
        lead = _JMDICT_LEAD_POS.match(block)
        if lead and "▶" not in block[: lead.end()]:
            _mark(flags, plain, offset + lead.start(1), offset + lead.end(1), "id")


_FORMATTERS: list[tuple[re.Pattern[str], Any, frozenset[int]]] = [
    (re.compile(r"wikt|wiktionary", re.I), _format_wiktionary, frozenset()),
    # GCIDE continuation lines such as "n. {Running}" are not part-of-speech
    # tags; the head line formatter marks the real ones.
    # Its own quotation rules replace the generic whole-line attribution rule.
    (re.compile(r"gcide|webster", re.I), _format_gcide,
     frozenset({_POS_LINE_RULE, _ATTRIBUTION_LINE_RULE})),
    (re.compile(r"jmdict", re.I), _format_jmdict, frozenset()),
]


def _formatter_for(dictionary_name: str) -> tuple[Any, frozenset[int]]:
    for pattern, formatter, skip in _FORMATTERS:
        if pattern.search(dictionary_name):
            return formatter, skip
    return None, frozenset()


# ---- Display layout -----------------------------------------------------------
# Plain sources often arrive hard-wrapped (GCIDE and WordNet at 72 columns with
# hanging indents, JMdict with "‖"-joined senses). Layout flags let the card
# reflow them without touching the text that Copy and Open use:
#   "w" a line break (plus the next line's indentation) shown as one space,
#   "j" the same shown as nothing (CJK or a word broken after a hyphen),
#   "l" a space shown as a line break,
#   "e" an HTML entity left in plain text, shown decoded.
_LAYOUT_FLAGS = frozenset("wjl")
# Lines that start structure and never continue the line above.
_BLOCK_START = re.compile(
    r"(?:\(?\d{1,3}[.):]?(?=\s)|\d{1,3}:|\([a-z]\)\s|[①-⑳❶-❿•‖]|--|Syn\.|Note:"
    r"|(?:n|v|adj|adv)\s+(?:\d{1,3})?\s*:)"
)
_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d{1,7}|#x[0-9a-fA-F]{1,6});")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _layout_flags(plain: str, flags: list[set[str]]) -> None:
    lines = plain.split("\n")
    starts = []
    position = 0
    for line in lines:
        starts.append(position)
        position += len(line) + 1
    paragraph_indent = _indent(lines[0]) if lines else 0
    sense_paragraph = "▶" in lines[0] if lines else False
    for number in range(1, len(lines)):
        above, line = lines[number - 1], lines[number]
        body = line.lstrip(" ")
        joined = False
        if above.rstrip().endswith("‖") and body[:1].isdigit():
            # The break already goes before the "‖" (see "l" below).
            joined = True
        elif above.strip() and body and not _BLOCK_START.match(body):
            # A hanging indent continues the paragraph; JMdict senses wrap
            # without one.
            joined = _indent(line) > paragraph_indent or (
                sense_paragraph and _indent(line) == paragraph_indent
            )
        if joined:
            tail, head = above.rstrip()[-1:], body[:1]
            glue = "j" if (
                _HAN.match(tail) or _HAN.match(head) or re.match(r"[぀-ヿ]", tail + head)
                or re.search(r"[A-Za-z]-$", above.rstrip())
            ) else "w"
            newline = starts[number] - 1
            for index in range(newline, starts[number] + _indent(line)):
                flags[index].clear()
                flags[index].add(glue)
        else:
            paragraph_indent = _indent(line)
            sense_paragraph = "▶" in line
    for match in re.finditer(r" (?=‖)", plain):
        if match.start() > 0 and plain[match.start() - 1] != "\n":
            flags[match.start()].clear()
            flags[match.start()].add("l")
    for match in _ENTITY.finditer(plain):
        for index in range(match.start(), match.end()):
            flags[index].add("e")


def style_flags(
    raw: str, plain: str, *, definition_format: str = "plain", dictionary_name: str = ""
) -> list[set[str]]:
    flags: list[set[str]] | None = None
    if definition_format == "html":
        flags = _html_flags(raw, plain)
    if flags is None:
        flags = [set() for _ in plain]
    formatter, skip = (
        _formatter_for(dictionary_name) if definition_format != "html" else (None, frozenset())
    )
    _rule_flags(plain, flags, skip)
    if formatter is not None:
        formatter(plain, flags)
    if definition_format != "html":
        _layout_flags(plain, flags)
    # Italic CJK reads badly; show it dimmed instead.
    for index, char in enumerate(plain):
        if "i" in flags[index] and _HAN.match(char):
            flags[index].discard("i")
            flags[index].add("d")
    return flags


def runs_from_flags(text: str, flags: list[set[str]]) -> list[list[str]] | None:
    """Group ``text`` into [text, flags] runs; None when nothing is styled."""

    runs: list[list[str]] = []
    styled = False
    for index, char in enumerate(text):
        key = "".join(sorted(flags[index])) if index < len(flags) else ""
        if char == "\n":
            key = "".join(sorted(flags[index] & _LAYOUT_FLAGS)) if index < len(flags) else ""
        styled = styled or key != ""
        if runs and runs[-1][1] == key:
            runs[-1][0] += char
        else:
            runs.append([char, key])
    return runs if styled else None


def _configured_data_directories(
    source: Mapping[str, Any], config: Mapping[str, Any]
) -> list[pathlib.Path]:
    configured = source.get("data_dir")
    if configured in (None, ""):
        configured = config.get("stardict_data_dir", [])
    if isinstance(configured, (str, os.PathLike)):
        values: Sequence[Any] = [configured]
    elif isinstance(configured, Sequence):
        values = configured
    else:
        values = []
    directories: list[pathlib.Path] = []
    seen: set[str] = set()
    for value in values:
        path = pathlib.Path(os.path.expanduser(os.fspath(value)))
        for candidate in (path, path / "dic"):
            key = os.path.abspath(os.fspath(candidate))
            if key not in seen:
                seen.add(key)
                directories.append(candidate)
    return directories


def _ifo_files(directory: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield ``*.ifo`` files under ``directory`` the way sdcv finds them.

    sdcv loads dictionaries from every subdirectory of a data dir, at any
    depth and through symlinked directories (verified on sdcv 0.5.5), so a
    dictionary in ``dic/wangli/wangli.ifo`` is found. Unlike sdcv, a symlink
    loop is visited once, and the walk is bounded.
    """

    visited: set[str] = set()
    walked = 0
    for current, subdirectories, filenames in os.walk(directory, followlinks=True):
        real = os.path.realpath(current)
        if real in visited:
            subdirectories[:] = []
            continue
        visited.add(real)
        walked += 1
        if walked > _IFO_MAX_DIRECTORIES:
            return
        subdirectories.sort()
        for filename in sorted(filenames):
            if filename.endswith(".ifo"):
                yield pathlib.Path(current) / filename


def _read_ifo_metadata(path: pathlib.Path) -> dict[str, str] | None:
    try:
        if not path.is_file() or path.stat().st_size > _IFO_MAX_BYTES:
            return None
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    metadata: dict[str, str] = {}
    for line in data.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        metadata[key.strip().casefold()] = value.strip()
    return metadata


def resolve_definition_format(
    source: Mapping[str, Any], config: Mapping[str, Any]
) -> str:
    """Resolve ``html`` or conservative ``plain`` for a StarDict source."""

    override = source.get("definition_format")
    if override is not None:
        return str(override).strip().casefold() if str(override).strip().casefold() in {"html", "plain"} else "plain"

    dictionary_name = str(source.get("name", ""))
    if not dictionary_name:
        return "plain"
    sequences: list[str] = []
    inspected = 0
    seen_paths: set[str] = set()
    for directory in _configured_data_directories(source, config):
        try:
            candidates = list(_ifo_files(directory))
        except OSError:
            continue
        for path in candidates:
            identity = os.path.realpath(os.fspath(path))
            if identity in seen_paths:
                continue
            seen_paths.add(identity)
            inspected += 1
            if inspected > _IFO_MAX_FILES:
                return "plain"
            metadata = _read_ifo_metadata(path)
            if metadata is None or metadata.get("bookname") != dictionary_name:
                continue
            sequences.append(metadata.get("sametypesequence", ""))
    if sequences and all(sequence == "h" for sequence in sequences):
        return "html"
    # Explicit m, conflicts, mixed/unknown sequences, and missing metadata all
    # intentionally converge on the lossless plain-text path.
    return "plain"


def detect_features(text: str) -> dict[str, Any]:
    """Detect script features without depending on character order."""

    value = str(text)
    has_han = any(
        "\u3400" <= ch <= "\u4dbf"
        or "\u4e00" <= ch <= "\u9fff"
        or "\U00020000" <= ch <= "\U0002fa1f"
        for ch in value
    )
    has_hiragana = any("\u3040" <= ch <= "\u309f" for ch in value)
    has_katakana = any("\u30a0" <= ch <= "\u30ff" or "\uff66" <= ch <= "\uff9f" for ch in value)
    has_kana = has_hiragana or has_katakana
    has_latin = any("LATIN" in unicodedata.name(ch, "") and ch.isalpha() for ch in value)
    has_cantonese_marker = any(ch in CANTONESE_MARKERS for ch in value) or any(
        phrase in value for phrase in CANTONESE_PHRASES
    )
    scripts = [
        name
        for name, present in (("han", has_han), ("kana", has_kana), ("latin", has_latin))
        if present
    ]
    classes = list(scripts)
    if has_han:
        classes.append("cjk")  # Version-1 config compatibility.
    if len(scripts) > 1:
        classes.append("mixed")
    if not scripts:
        classes.append("unknown")
    if has_cantonese_marker:
        classes.append("cantonese_marker")
    return {
        "has_han": has_han,
        "has_kana": has_kana,
        "has_hiragana": has_hiragana,
        "has_katakana": has_katakana,
        "has_latin": has_latin,
        "has_cantonese_marker": has_cantonese_marker,
        "scripts": scripts,
        "classes": classes,
    }


def _apply_v1_related_policy(config: dict[str, Any]) -> bool:
    """Map version-1 related semantics onto the version-2 source policy.

    Up to v0.7.1 the main body was always exact-only and `allow_related`
    alone decided whether fuzzy entries formed a Related row; `exact_only`
    was not read. Version 2 makes `exact_only: true` drop related entries,
    so a v1 source with `exact_only: true, allow_related: true` (the
    shipped GCIDE/WordNet sources) silently lost its Related row.
    """
    if config.get("version", 1) != 1:
        return False
    changed = False
    for mode in config.get("modes", {}).values():
        if not isinstance(mode, dict):
            continue
        for source in mode.get("sources", []):
            if isinstance(source, dict) and "allow_related" in source:
                wanted = not bool(source["allow_related"])
                if source.get("exact_only") != wanted:
                    source["exact_only"] = wanted
                    changed = True
                if source["allow_related"]:
                    source[V1_FUZZY_FALLBACK_KEY] = True
    return changed


def load_config(path: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    configured = path or os.environ.get("DANK_LOOKUP_CONFIG")
    if configured is None:
        config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        configured = os.path.join(config_home, "dank-lookup", "config.json")
    config_path = pathlib.Path(os.path.expanduser(os.fspath(configured)))
    metadata: dict[str, Any] = {"path": str(config_path), "loaded": False, "warning": None}
    if not config_path.is_file():
        metadata["warning"] = "config_not_found_using_builtin"
        return builtin_config(), metadata
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("modes"), dict):
            raise ValueError("config must contain an object named modes")
        metadata["loaded"] = True
        if _apply_v1_related_policy(value):
            metadata["legacy_related_policy"] = "v1_allow_related"
        return value, metadata
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        metadata["warning"] = "config_invalid_using_builtin"
        return builtin_config(), metadata


def _when_values(source: Mapping[str, Any]) -> list[str]:
    value = source.get("when", "any")
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence):
        values = [str(item) for item in value]
    else:
        values = ["any"]
    return ["han" if item == "cjk" else item for item in values]


def source_matches(source: Mapping[str, Any], features: Mapping[str, Any]) -> bool:
    wanted = _when_values(source)
    classes = {"han" if item == "cjk" else item for item in features.get("classes", [])}
    return "any" in wanted or bool(classes.intersection(wanted))


def candidate_modes(config: Mapping[str, Any], features: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key, mode in config.get("modes", {}).items():
        if not isinstance(mode, Mapping) or not mode.get("enabled", True):
            continue
        sources = [item for item in mode.get("sources", []) if isinstance(item, Mapping)]
        if any(source_matches(source, features) for source in sources):
            candidates.append(str(key))
    return candidates


def _parse_dictionary_list(output: str) -> set[str]:
    names: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.casefold().startswith("dictionary's name"):
            continue
        match = re.match(r"^(.*?)\s+(\d+)$", line)
        if match and match.group(1).strip():
            names.add(match.group(1).strip())
    return names


class _OneShotSession:
    """Session adapter over a one-shot protocol runner.

    Every ``ask`` runs its own process through ``run_bounded_protocol`` (or
    an injected fake) and strips the surrounding prompts into a frame, so
    callers written against :class:`SdcvSession` behave identically.
    """

    def __init__(self, protocol_runner: Callable[..., tuple[dict[str, Any], bytes]], **kwargs: Any) -> None:
        self.protocol_runner = protocol_runner
        self.kwargs = kwargs
        self.frames = 0

    def ask(
        self,
        query: str,
        *,
        choice: int | None = None,
        timeout_seconds: float = 5.0,
        stdout_cap_bytes: int = DEFAULT_STDOUT_CAP_BYTES,
    ) -> tuple[dict[str, Any], bytes]:
        run, stdout = self.protocol_runner(
            query=query,
            choice=choice,
            timeout_seconds=timeout_seconds,
            stdout_cap_bytes=stdout_cap_bytes,
            **self.kwargs,
        )
        self.frames += 1
        terminal = MAIN_PROMPT + b"\n"
        if stdout.startswith(MAIN_PROMPT) and stdout.endswith(terminal) and len(stdout) >= len(MAIN_PROMPT) + len(terminal):
            stdout = stdout[len(MAIN_PROMPT) : -len(terminal)]
        return run, stdout

    def close(self) -> dict[str, Any]:
        return {"returncode": 0, "termination_reason": None, "process_group_reaped": True, "session_frames": self.frames}


class SdcvClient:
    def __init__(
        self,
        executable: str | None = None,
        timeout: float = 5.0,
        data_dirs: Sequence[str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        clock_ns: Callable[[], int] | None = None,
        protocol_runner: Callable[..., tuple[dict[str, Any], bytes]] | None = None,
        cancel_event: threading.Event | None = None,
        stdout_cap_bytes: int = DEFAULT_STDOUT_CAP_BYTES,
        stderr_cap_bytes: int = STDERR_CAP_BYTES,
        session_factory: Callable[..., Any] | None = None,
        fuzzy_in_exact_session: bool = True,
        session_scope: str = "lookup",
    ) -> None:
        self.executable = executable or os.environ.get("DANK_LOOKUP_SDCV", "sdcv")
        self.timeout = timeout
        self.runner = runner or subprocess.run
        self.clock_ns = clock_ns or time.monotonic_ns
        self.protocol_runner = protocol_runner
        # One sdcv process per (dictionary, data_dir, exact flag) for the
        # duration of query(); a legacy protocol_runner is wrapped so each
        # frame still runs as its own one-shot process (tests, probes).
        if session_factory is not None:
            self.session_factory = session_factory
        elif protocol_runner is not None:
            self.session_factory = lambda **kwargs: _OneShotSession(protocol_runner, **kwargs)
        else:
            self.session_factory = SdcvSession
        # sdcv routes a "/query" to its fuzzy search regardless of
        # --exact-search (libwrapper.cpp process_phrase, case qtFUZZY), so one
        # --exact-search session serves exact and fuzzy frames alike; proven
        # on sdcv 0.5.5 by tests/probe_sdcv_session.py
        # (session_fuzzy_under_exact_search). False keeps a separate
        # non-exact session per source.
        self.fuzzy_in_exact_session = bool(fuzzy_in_exact_session)
        # "lookup": sessions stay open across query() calls (every token of a
        # segmented lookup reuses them) until lookup() calls close();
        # "query": each query() call closes its sessions on return.
        self.session_scope = session_scope
        self._sessions: dict[tuple[str, str | None, bool], Any] = {}
        self.session_log: list[dict[str, Any]] = []
        self.cancel_event = cancel_event
        self.stdout_cap_bytes = max(1024, int(stdout_cap_bytes))
        self.stderr_cap_bytes = max(1024, int(stderr_cap_bytes))
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "SDCV_HISTFILE": "/dev/null",
                "SDCV_HISTSIZE": "0",
                "LC_ALL": "C.UTF-8",
            }
        )
        if isinstance(data_dirs, str):
            data_dirs = [data_dirs]
        if data_dirs:
            self.environment["STARDICT_DATA_DIR"] = os.pathsep.join(
                os.path.expanduser(str(path)) for path in data_dirs
            )
        self._dict_cache: dict[str | None, set[str] | None] = {}
        self.backend_ns = 0
        self.deadline_ns: int | None = None
        self.budget_exhausted = False

    @property
    def available(self) -> bool:
        if os.path.sep in self.executable:
            return pathlib.Path(self.executable).is_file() and os.access(self.executable, os.X_OK)
        return shutil.which(self.executable) is not None

    def _run(self, argv: list[str], env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str] | None:
        started = self.clock_ns()
        timeout = self.timeout
        if self.deadline_ns is not None:
            remaining = (self.deadline_ns - started) / 1_000_000_000
            if remaining <= 0:
                self.budget_exhausted = True
                return None
            timeout = min(timeout, remaining)
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=dict(env) if env is not None else self.environment,
                check=False,
            )
            if self.deadline_ns is not None and self.clock_ns() > self.deadline_ns:
                self.budget_exhausted = True
                return None
            return result
        except (OSError, subprocess.SubprocessError, UnicodeError):
            if self.deadline_ns is not None and self.clock_ns() >= self.deadline_ns:
                self.budget_exhausted = True
            return None
        finally:
            self.backend_ns += max(0, self.clock_ns() - started)

    def dictionaries(self, data_dir: str | None = None) -> set[str] | None:
        expanded = os.path.expanduser(data_dir) if data_dir else None
        if expanded in self._dict_cache:
            return self._dict_cache[expanded]
        argv = [self.executable, "--list-dicts"]
        if expanded:
            if not pathlib.Path(expanded).is_dir():
                self._dict_cache[expanded] = set()
                return set()
            argv += ["--data-dir", expanded, "--only-data-dir"]
        result = self._run(argv)
        if result is None or result.returncode != 0:
            answer = None
        else:
            answer = _parse_dictionary_list(result.stdout)
        self._dict_cache[expanded] = answer
        return answer

    def source_installed(self, source: Mapping[str, Any]) -> bool | None:
        names = self.dictionaries(str(source["data_dir"]) if source.get("data_dir") else None)
        if names is None:
            return None
        return str(source.get("name", "")) in names

    def _protocol_timeout(self) -> float | None:
        now = self.clock_ns()
        timeout = self.timeout
        if self.deadline_ns is not None:
            remaining = (self.deadline_ns - now) / 1_000_000_000
            if remaining <= 0:
                self.budget_exhausted = True
                return None
            timeout = min(timeout, remaining)
        return max(0.01, timeout)

    def _stdin_query(
        self,
        *,
        query: str,
        dictionary: str,
        data_dir: str | None,
        exact: bool,
        choice: int | None,
        stdout_cap_bytes: int,
    ) -> tuple[dict[str, Any] | None, str | None, int]:
        timeout = self._protocol_timeout()
        if timeout is None:
            return None, "lookup_budget_exhausted", 0
        started = self.clock_ns()
        try:
            session = self._session(dictionary=dictionary, data_dir=data_dir, exact=exact)
            run, stdout = session.ask(
                query,
                choice=choice,
                timeout_seconds=timeout,
                stdout_cap_bytes=max(1, stdout_cap_bytes),
            )
        except (OSError, RuntimeError, ValueError, UnicodeError):
            return None, "sdcv_execution_failed", 0
        finally:
            self.backend_ns += max(0, self.clock_ns() - started)
        termination = run.get("termination_reason")
        if termination == "cancelled":
            return None, "lookup_cancelled", int(run.get("stdout_bytes", 0))
        if termination == "stdout_cap":
            return None, "sdcv_stdout_cap", int(run.get("stdout_bytes", 0))
        if termination == "stderr_cap":
            return None, "sdcv_stderr_cap", int(run.get("stdout_bytes", 0))
        if termination == "timeout":
            if self.deadline_ns is not None and self.clock_ns() >= self.deadline_ns:
                self.budget_exhausted = True
                return None, "lookup_budget_exhausted", int(run.get("stdout_bytes", 0))
            return None, "sdcv_source_timeout", int(run.get("stdout_bytes", 0))
        # A live session has no return code yet (None); a finished process
        # must have exited cleanly and its group must be gone.
        if termination is not None or run.get("returncode") not in (None, 0):
            return None, "sdcv_execution_failed", int(run.get("stdout_bytes", 0))
        if run.get("process_group_reaped") is False:
            return None, "sdcv_reap_failed", int(run.get("stdout_bytes", 0))
        parser_choice = choice if int(run.get("choice_prompt_count", 0)) else None
        if parser_choice is None and run.get("choice_aborted"):
            parser_choice = -1
        try:
            parsed = parse_frame(stdout, query=query, choice=parser_choice)
        except SdcvProtocolError as exc:
            warning = "sdcv_invalid_json" if str(exc) == "invalid_result_json" else "sdcv_invalid_protocol"
            return None, warning, int(run.get("stdout_bytes", 0))
        return parsed, None, int(run.get("stdout_bytes", 0))

    def _exact_query(
        self,
        *,
        query: str,
        dictionary: str,
        data_dir: str | None,
        stdout_cap_bytes: int,
    ) -> tuple[dict[str, Any] | None, str | None, int]:
        """Exact lookup that also returns every homograph entry.

        sdcv lists homographs (e.g. words.hk 為 wai4/wai6) behind a choice
        prompt; the aborted first run yields the option list, and each index
        is then fetched with its own exact run.
        """
        result, warning, used = self._stdin_query(
            query=query,
            dictionary=dictionary,
            data_dir=data_dir,
            exact=True,
            choice=None,
            stdout_cap_bytes=stdout_cap_bytes,
        )
        if result is None or result["entries"] or not result["options"]:
            return result, warning, used
        entries: list[dict[str, Any]] = []
        for option in result["options"][:MAX_HOMOGRAPH_CHOICES]:
            remaining_cap = stdout_cap_bytes - used
            if remaining_cap <= 0 or self.budget_exhausted:
                warning = warning or "sdcv_stdout_cap"
                break
            selected, selected_warning, selected_used = self._stdin_query(
                query=query,
                dictionary=dictionary,
                data_dir=data_dir,
                exact=True,
                choice=int(option["index"]),
                stdout_cap_bytes=remaining_cap,
            )
            used += selected_used
            if selected_warning:
                warning = warning or selected_warning
                if selected_warning in {"lookup_budget_exhausted", "lookup_cancelled"}:
                    break
            if selected is not None:
                entries.extend(selected["entries"])
        return {"entries": entries, "options": [], "decisions": result["decisions"]}, warning, used

    def query(
        self,
        source: Mapping[str, Any],
        query: str,
        *,
        candidates: str = CANDIDATES_ALL,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return exact entries plus bounded fuzzy candidates.

        `candidates` limits how much of the fuzzy list is fetched:
        CANDIDATES_ALL fetches every listed candidate; CANDIDATES_VARIANTS
        fetches only candidates whose headword is NFKC-casefold equal to the
        query (sdcv's exact search is case-sensitive) and skips the fuzzy
        search when the query has no such variants; CANDIDATES_FALLBACK acts
        like CANDIDATES_ALL when the exact search found nothing, else like
        CANDIDATES_VARIANTS. A caller that would discard every non-exact
        entry passes CANDIDATES_VARIANTS instead of paying one sdcv process
        per discarded candidate.
        """
        if self.session_scope != "query":
            return self._query(source, query, candidates=candidates)
        close_warning: str | None = None
        try:
            entries, warning = self._query(source, query, candidates=candidates)
        finally:
            close_warning = self._close_sessions()
        return entries, warning or close_warning

    def close(self) -> str | None:
        """End every open sdcv session; return a warning if one ended badly."""
        return self._close_sessions()

    def _session(self, *, dictionary: str, data_dir: str | None, exact: bool) -> Any:
        if self.fuzzy_in_exact_session:
            exact = True
        key = (dictionary, data_dir, exact)
        session = self._sessions.get(key)
        if session is None:
            session = self.session_factory(
                name="sdcv",
                argv=protocol_argv(
                    self.executable,
                    data_dir=data_dir,
                    dictionaries=[dictionary],
                    exact=exact,
                ),
                environment=self.environment,
                cancellation_event=self.cancel_event,
                stderr_cap_bytes=self.stderr_cap_bytes,
            )
            self._sessions[key] = session
        return session

    def _close_sessions(self) -> str | None:
        """Close every open sdcv session; return a warning if one ended badly."""
        warning: str | None = None
        sessions, self._sessions = self._sessions, {}
        for session in sessions.values():
            started = self.clock_ns()
            try:
                closed = session.close()
            except (OSError, RuntimeError, ValueError):
                closed = {"termination_reason": "close_failed", "process_group_reaped": False}
            finally:
                self.backend_ns += max(0, self.clock_ns() - started)
            self.session_log.append(closed)
            if closed.get("process_group_reaped") is False:
                warning = warning or "sdcv_reap_failed"
            elif closed.get("returncode") not in (None, 0) or closed.get("termination_reason") not in (
                None,
                "cancelled",
                "timeout",
                "stdout_cap",
                "stderr_cap",
            ):
                warning = warning or "sdcv_execution_failed"
        return warning

    def _query(
        self,
        source: Mapping[str, Any],
        query: str,
        *,
        candidates: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        data_dir = os.path.expanduser(str(source["data_dir"])) if source.get("data_dir") else None
        dictionary = str(source.get("name", ""))
        remaining_cap = self.stdout_cap_bytes
        exact_result, warning, used = self._exact_query(
            query=query,
            dictionary=dictionary,
            data_dir=data_dir,
            stdout_cap_bytes=remaining_cap,
        )
        remaining_cap = max(0, remaining_cap - used)
        if warning in {
            "lookup_budget_exhausted",
            "lookup_cancelled",
            "sdcv_stdout_cap",
            "sdcv_stderr_cap",
        }:
            return [], warning
        entries = list(exact_result["entries"]) if exact_result is not None else []
        warnings = [warning] if warning else []

        if candidates == CANDIDATES_FALLBACK:
            candidates = CANDIDATES_VARIANTS if entries else CANDIDATES_ALL
        variants_only = candidates == CANDIDATES_VARIANTS
        have_identical = any(str(entry.get("word", "")) == query for entry in entries)

        # Exact search is case-sensitive in sdcv. The explicit fuzzy protocol
        # supplies a bounded candidate list; definitions are then fetched by
        # exact stdin lookups and policy remains in _lookup_modes().
        # Whether a non-exact headword is accepted is a lookup policy choice;
        # `candidates` only avoids fetching entries that policy will discard.
        if not self.budget_exhausted and not (variants_only and not _has_term_variants(query)):
            fuzzy_result, fuzzy_warning, used = self._stdin_query(
                query="/" + query,
                dictionary=dictionary,
                data_dir=data_dir,
                exact=False,
                choice=0,
                stdout_cap_bytes=remaining_cap,
            )
            remaining_cap = max(0, remaining_cap - used)
            if fuzzy_warning:
                warnings.append(fuzzy_warning)
            if fuzzy_result is not None:
                for option in fuzzy_result["options"][:16]:
                    if remaining_cap <= 0 or self.budget_exhausted:
                        warnings.append("sdcv_stdout_cap")
                        break
                    option_dictionary = str(option.get("dictionary", ""))
                    option_word = str(option.get("word", ""))
                    if option_dictionary != dictionary or not option_word:
                        continue
                    if variants_only and (
                        (option_word == query and have_identical)
                        or _normalised_term(option_word) != _normalised_term(query)
                    ):
                        # The exact search already returned the identical
                        # headword; other non-variants would be discarded.
                        continue
                    candidate, candidate_warning, used = self._exact_query(
                        query=option_word,
                        dictionary=option_dictionary,
                        data_dir=data_dir,
                        stdout_cap_bytes=remaining_cap,
                    )
                    remaining_cap = max(0, remaining_cap - used)
                    if candidate_warning:
                        warnings.append(candidate_warning)
                        if candidate_warning == "sdcv_stderr_cap":
                            break
                    if candidate is not None:
                        entries.extend(candidate["entries"])

        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = (
                str(entry.get("dict", "")),
                str(entry.get("word", "")),
                str(entry.get("definition", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(entry)
        return deduplicated, next((item for item in warnings if item), None)


def _normalised_term(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _has_term_variants(query: str) -> bool:
    """Whether a headword other than `query` itself can be NFKC-casefold equal."""

    value = str(query).strip()
    return unicodedata.normalize("NFKC", value) != value or any(
        character.casefold() != character or character.upper() != character
        for character in value
    )


def is_exact(entry: Mapping[str, Any], query: str) -> bool:
    return _normalised_term(str(entry.get("word", ""))) == _normalised_term(query)


def source_exact_only(source: Mapping[str, Any]) -> bool:
    """Return the source policy, with allow_related as a legacy-only fallback."""

    configured = source.get("exact_only")
    if isinstance(configured, bool):
        return configured
    return not bool(source.get("allow_related", False))


def _update_text_digest(digest: Any, value: str) -> None:
    for offset in range(0, len(value), 1024 * 1024):
        digest.update(value[offset : offset + 1024 * 1024].encode("utf-8", errors="replace"))


def _text_hash(value: str, length: int | None = None) -> str:
    digest = hashlib.sha256()
    _update_text_digest(digest, value)
    result = digest.hexdigest()
    return result if length is None else result[:length]


def _entry_id(source: str, word: str, cleaned_definition: str) -> str:
    digest = hashlib.sha256()
    _update_text_digest(digest, source)
    digest.update(b"\0")
    _update_text_digest(digest, word)
    digest.update(b"\0")
    _update_text_digest(digest, cleaned_definition)
    result = digest.hexdigest()
    return result[:24]


def _prepare_entries(
    entries: Iterable[Mapping[str, Any]], *, retain_entry_vault: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    vault: dict[str, Any] = {}
    seen: set[str] = set()
    for entry in entries:
        source = str(entry.get("dict") or entry.get("source") or "").strip()
        word = str(entry.get("word") or "").strip()
        raw = str(entry.get("definition") or "")
        definition_format = str(entry.get("_definition_format") or "plain")
        cleaned = clean_definition(source, raw, definition_format=definition_format)
        if not source or not cleaned:
            continue
        identifier = _entry_id(source, word, cleaned)
        if identifier in seen:
            continue
        seen.add(identifier)
        prepared.append(
            {
                "entry_id": identifier,
                "word": word,
                "source": source,
                "full_body": cleaned,
                "raw_ref": identifier,
                "_raw": raw,
                "_definition_format": definition_format,
            }
        )
        if retain_entry_vault:
            vault[identifier] = {
                "word": word,
                "source": source,
                "full_body": cleaned,
            }
    return prepared, vault


def _section(entry: Mapping[str, Any], body: str) -> str:
    word = str(entry.get("word", "")).strip()
    heading = f"[{entry['source']}]"
    if word:
        heading += f"\n{word}"
    return f"{heading}\n{body}".strip()


def render_entries(
    raw_entries: Iterable[Mapping[str, Any]],
    *,
    max_entry_chars: int = 4000,
    max_body_chars: int = 12000,
    max_inline_full_chars: int = 262144,
    retain_entry_vault: bool = False,
    retain_full_blobs: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    entries, entry_vault = _prepare_entries(
        raw_entries, retain_entry_vault=retain_entry_vault
    )
    preview_parts: list[str] = []
    full_parts: list[str] = []
    public_entries: list[dict[str, Any]] = []
    preview_total = 0
    overall_truncated = False
    for entry in entries:
        full = entry["full_body"]
        full_section = _section(entry, full)
        full_parts.append(full_section)
        entry_limit = max_entry_chars
        if preview_parts:
            # A later source gets a shorter preview instead of none when the
            # sources before it used most of the body budget.
            room = (max_body_chars - preview_total - 2 - len(_section(entry, ""))
                    - len(TRUNCATION_MARKER) - 2)
            if len(full) > room >= _MIN_SHARED_PREVIEW_CHARS:
                entry_limit = min(entry_limit, room)
        entry_truncated = len(full) > entry_limit
        preview = full
        if entry_truncated:
            preview = full[:entry_limit].rstrip() + "\n" + TRUNCATION_MARKER
        section = _section(entry, preview)
        flags = style_flags(
            entry["_raw"],
            full,
            definition_format=entry["_definition_format"],
            dictionary_name=entry["source"],
        )
        if entry_truncated:
            kept = len(preview) - len(TRUNCATION_MARKER) - 1
            flags = flags[:kept] + [set()] + [{"d"}] * len(TRUNCATION_MARKER)
        runs = runs_from_flags(preview, flags)
        if preview_parts and preview_total + 2 + len(section) > max_body_chars:
            overall_truncated = True
            continue
        if not preview_parts and len(section) > max_body_chars:
            section = section[: max(0, max_body_chars - len(TRUNCATION_MARKER) - 1)].rstrip()
            section += "\n" + TRUNCATION_MARKER
            entry_truncated = True
        preview_parts.append(section)
        preview_total += len(section) + (2 if len(preview_parts) > 1 else 0)
        overall_truncated = overall_truncated or entry_truncated
        public_entries.append(
            {
                "entry_id": entry["entry_id"],
                "word": entry["word"],
                "source": entry["source"],
                "body": preview,
                "truncated": entry_truncated,
                "raw_ref": entry["raw_ref"],
            }
        )
        if runs is not None:
            public_entries[-1]["runs"] = runs
    if overall_truncated and preview_parts and not preview_parts[-1].endswith(TRUNCATION_MARKER):
        marker = TRUNCATION_MARKER
        if preview_total + 1 + len(marker) <= max_body_chars:
            preview_parts.append(marker)
        else:
            last = preview_parts[-1]
            preview_parts[-1] = last[: max(0, len(last) - len(marker) - 1)].rstrip() + "\n" + marker
    sources = sorted({entry["source"] for entry in entries}, key=str.casefold)
    complete_body = "\n\n".join(full_parts)
    full_ref = None
    full_blobs: dict[str, str] = {}
    if len(complete_body) > max_inline_full_chars:
        full_ref = _text_hash(complete_body, 32)
        inline_full_body = None
        if retain_full_blobs:
            full_blobs[full_ref] = complete_body
    else:
        inline_full_body = complete_body
    rendered = {
        "subtitle": ", ".join(sources),
        "body": "\n\n".join(preview_parts),
        "full_body": inline_full_body,
        "full_ref": full_ref,
        "full_chars": len(complete_body),
        "truncated": overall_truncated,
        "entries": public_entries,
        "entry_count": len(entries),
        "preview_entry_count": len(public_entries),
    }
    return rendered, {"entries": entry_vault, "full_blobs": full_blobs}


def _mode_score(mode_key: str, mode: Mapping[str, Any], features: Mapping[str, Any]) -> int:
    classes = {str(item) for item in features.get("classes", [])}
    defaults = {"han" if item == "cjk" else str(item) for item in mode.get("default_for", [])}
    score = 20 if defaults.intersection(classes) else 0
    boosts = mode.get("priority_boost", [])
    if isinstance(boosts, str):
        boosts = [boosts]
    if not boosts:
        if mode_key == "ja":
            boosts = ["kana"]
        elif mode_key == "yue":
            boosts = ["cantonese_marker"]
    if "kana" in boosts and features.get("has_kana"):
        score += 100
    if "cantonese_marker" in boosts and features.get("has_cantonese_marker"):
        score += 100
    return score


def _preview_limits(config: Mapping[str, Any]) -> tuple[int, int, int]:
    preview = config.get("preview", {})
    try:
        per_entry = min(100_000, max(256, int(preview.get("max_entry_chars", 4000))))
        total = min(500_000, max(per_entry, int(preview.get("max_body_chars", 12000))))
        inline_full = min(
            MAX_INLINE_FULL_CHARS,
            max(total, int(preview.get("max_inline_full_chars", 262144))),
        )
    except (TypeError, ValueError):
        return 4000, 12000, 262144
    return per_entry, total, inline_full


def _lookup_modes(
    query: str,
    config: Mapping[str, Any],
    client: SdcvClient,
    features: Mapping[str, Any],
    *,
    retain_entry_vault: bool = False,
    retain_full_blobs: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = candidate_modes(config, features)
    results_by_mode: dict[str, Any] = {}
    related_by_mode: dict[str, Any] = {}
    mode_preferences: dict[str, Any] = {}
    mode_status: dict[str, Any] = {}
    vault_entries: dict[str, Any] = {}
    full_blobs: dict[str, str] = {}
    warnings: set[str] = set()
    max_entry, max_body, max_inline_full = _preview_limits(config)

    for mode_key, raw_mode in config.get("modes", {}).items():
        if not isinstance(raw_mode, Mapping) or not raw_mode.get("enabled", True):
            continue
        mode_key = str(mode_key)
        mode_preferences[mode_key] = {"show_related": bool(raw_mode.get("show_related", False))}
        all_sources = [source for source in raw_mode.get("sources", []) if isinstance(source, Mapping)]
        matching_sources = [source for source in all_sources if source_matches(source, features)]
        installed_sources: list[str] = []
        unknown_sources: list[str] = []
        source_states: dict[int, bool | None] = {}
        for source in all_sources:
            state = client.source_installed(source)
            source_states[id(source)] = state
            if state is True:
                installed_sources.append(str(source.get("name", "")))
            elif state is None:
                # If discovery itself fails, query explicitly rather than hiding a valid source.
                installed_sources.append(str(source.get("name", "")))
                unknown_sources.append(str(source.get("name", "")))
        eligible_installed_sources = [
            source
            for source in matching_sources
            if source_states.get(id(source)) is not False
        ]
        mode_status[mode_key] = {
            "candidate": mode_key in candidates,
            "configured_sources": [str(source.get("name", "")) for source in all_sources],
            "eligible_sources": [str(source.get("name", "")) for source in matching_sources],
            "installed_sources": list(dict.fromkeys(installed_sources)),
            "eligible_installed_sources": [
                str(source.get("name", "")) for source in eligible_installed_sources
            ],
            "availability_unknown": unknown_sources,
            "available": bool(installed_sources),
        }
        if mode_key not in candidates or not eligible_installed_sources:
            continue

        exact_entries: list[dict[str, Any]] = []
        related_entries: list[dict[str, Any]] = []
        for source in eligible_installed_sources:
            if getattr(client, "budget_exhausted", False):
                warnings.add("lookup_budget_exhausted")
                mode_status[mode_key]["incomplete"] = True
                break
            name = str(source.get("name", ""))
            exact_only = source_exact_only(source)
            definition_format = resolve_definition_format(source, config)
            if source.get(V1_FUZZY_FALLBACK_KEY):
                candidate_policy = CANDIDATES_FALLBACK
            elif exact_only:
                candidate_policy = CANDIDATES_VARIANTS
            else:
                candidate_policy = CANDIDATES_ALL
            entries, warning = client.query(source, query, candidates=candidate_policy)
            if warning:
                warnings.add(warning)
                if warning in INCOMPLETE_SOURCE_WARNINGS:
                    mode_status[mode_key]["incomplete"] = True
            # v1 sources only showed sdcv's fuzzy matches when the headword
            # itself had no entry in that dictionary.
            fuzzy_fallback_only = bool(source.get(V1_FUZZY_FALLBACK_KEY)) and any(
                is_exact(entry, query) for entry in entries
            )
            for entry in entries:
                item = dict(entry)
                item.setdefault("dict", name)
                item["_definition_format"] = definition_format
                if is_exact(item, query):
                    exact_entries.append(item)
                elif not exact_only and not fuzzy_fallback_only:
                    related_entries.append(item)

        exact_rendered, exact_vault = render_entries(
            exact_entries,
            max_entry_chars=max_entry,
            max_body_chars=max_body,
            max_inline_full_chars=max_inline_full,
            retain_entry_vault=retain_entry_vault,
            retain_full_blobs=retain_full_blobs,
        )
        if exact_rendered["body"]:
            exact_rendered["label"] = str(raw_mode.get("label", mode_key))
            results_by_mode[mode_key] = exact_rendered
            vault_entries.update(exact_vault["entries"])
            full_blobs.update(exact_vault["full_blobs"])
        related_rendered, related_vault = render_entries(
            related_entries,
            max_entry_chars=max_entry,
            max_body_chars=max_body,
            max_inline_full_chars=max_inline_full,
            retain_entry_vault=retain_entry_vault,
            retain_full_blobs=retain_full_blobs,
        )
        if related_rendered["body"]:
            related_rendered["label"] = "Related"
            related_by_mode[mode_key] = related_rendered
            vault_entries.update(related_vault["entries"])
            full_blobs.update(related_vault["full_blobs"])

    installed_modes = [key for key, status in mode_status.items() if status["available"]]
    unavailable_modes = [key for key, status in mode_status.items() if not status["available"]]
    # Modes whose sources were cut short (time budget or per-source timeout)
    # must stay visible instead of silently disappearing from the result.
    incomplete_modes = [key for key in candidates if mode_status.get(key, {}).get("incomplete")]
    available_modes = [key for key in candidates if key in results_by_mode]
    ranked_modes = sorted(
        available_modes,
        key=lambda key: (-_mode_score(key, config["modes"][key], features), candidates.index(key)),
    )
    return (
        {
            "candidate_modes": candidates,
            "installed_modes": installed_modes,
            "unavailable_modes": unavailable_modes,
            "incomplete_modes": incomplete_modes,
            "available_modes": available_modes,
            "default_mode": ranked_modes[0] if ranked_modes else None,
            "results_by_mode": results_by_mode,
            "related_by_mode": related_by_mode,
            "mode_preferences": mode_preferences,
            "mode_status": mode_status,
            "warnings": sorted(warnings),
        },
        {"entries": vault_entries, "full_blobs": full_blobs},
    )


def _tokenizer_path(config: Mapping[str, Any]) -> pathlib.Path:
    configured = os.environ.get("DANK_LOOKUP_TOKENIZER") or config.get("tokenizer")
    if configured:
        return pathlib.Path(os.path.expanduser(str(configured)))
    return pathlib.Path(os.path.expanduser("~/.local/lib/dank-lookup/dank-lookup-tokenize"))


def _fallback_segment(query: str) -> list[str]:
    """Conservative script/codepoint fallback, not a linguistic tokenizer."""

    def kind(character: str) -> str:
        feature = detect_features(character)
        if feature["has_han"]:
            return "han"
        if feature["has_kana"]:
            return "kana"
        if feature["has_latin"]:
            return "latin"
        return "separator"

    runs: list[tuple[str, str]] = []
    for character in query:
        current_kind = kind(character)
        if current_kind == "separator":
            continue
        if runs and runs[-1][0] == current_kind:
            runs[-1] = (current_kind, runs[-1][1] + character)
        else:
            runs.append((current_kind, character))
    if len(runs) > 1:
        tokens = [text for _kind, text in runs if text]
    elif runs:
        run_kind, text = runs[0]
        if run_kind in {"han", "kana"} and len(text) == 2:
            tokens = list(text)
        elif run_kind in {"han", "kana"} and len(text) > 2:
            tokens = [text[index : index + 2] for index in range(len(text) - 1)]
        else:
            tokens = [text]
    else:
        tokens = []
    return tokens[:24]


def _kill_tokenizer_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_bounded_tokenizer_output(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[bytes, bytes, str | None]:
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {
        "stdout": TOKENIZER_STDOUT_CAP_BYTES,
        "stderr": TOKENIZER_STDERR_CAP_BYTES,
    }
    termination: str | None = None

    def terminate(reason: str) -> None:
        nonlocal termination
        if termination is None:
            termination = reason
        _kill_tokenizer_process_group(process)

    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if termination is None and cancel_event is not None and cancel_event.is_set():
                terminate("cancelled")
            if termination is None and now >= deadline:
                terminate("timeout")
            for key, _mask in selector.select(0.01):
                label = str(key.data)
                buffer = buffers[label]
                remaining = max(0, caps[label] - len(buffer))
                try:
                    chunk = os.read(
                        key.fileobj.fileno(),
                        min(64 * 1024, remaining + 1),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(chunk) > remaining:
                    buffer.extend(chunk[:remaining])
                    terminate(f"{label}_cap")
                else:
                    buffer.extend(chunk)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            terminate("wait_timeout")
            process.wait(timeout=1.0)
    finally:
        selector.close()
        if process.poll() is None:
            terminate("runner_error")
            process.wait(timeout=1.0)
        process.stdout.close()
        process.stderr.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), termination


def _segment(
    query: str,
    features: Mapping[str, Any],
    config: Mapping[str, Any],
    timeout_seconds: float = 2.0,
    cancel_event: threading.Event | None = None,
) -> tuple[list[str], str | None, str | None]:
    path = _tokenizer_path(config)
    if not (path.is_file() and os.access(path, os.X_OK)) or len(query) <= 1:
        tokens = _fallback_segment(query) if len(query) > 1 else []
        return tokens, "fallback-script-ngram" if len(tokens) > 1 else None, (
            "fallback_segmentation_nonlinguistic" if len(tokens) > 1 else None
        )
    language = "ja" if features.get("has_kana") else "zh" if features.get("has_han") else None
    if language is None:
        return [], None, None
    if cancel_event is not None and cancel_event.is_set():
        return [], None, "lookup_cancelled"
    process: subprocess.Popen[bytes] | None = None
    stderr = b""
    try:
        process = subprocess.Popen(
            [str(path), "--lang", language, "--json", "--stdin"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None
        process.stdin.write(query.encode("utf-8") + b"\n")
        process.stdin.close()
        process.stdin = None
        deadline = time.monotonic() + max(0.05, min(2.0, timeout_seconds))
        stdout, stderr, termination = _read_bounded_tokenizer_output(
            process,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if termination == "cancelled":
            return [], None, "lookup_cancelled"
        if termination is not None:
            raise subprocess.SubprocessError(termination)
        value = json.loads(stdout.decode("utf-8"))
        tokens = value.get("tokens", [])
        if process.returncode == 0 and isinstance(tokens, list):
            return [str(token) for token in tokens if str(token).strip()][:24], "external-tokenizer", None
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError, json.JSONDecodeError):
        if process is not None:
            if process.poll() is None:
                _kill_tokenizer_process_group(process)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _kill_tokenizer_process_group(process)
                process.wait(timeout=1.0)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
    tokens = _fallback_segment(query)
    if b"--stdin" in stderr:
        # A pre-0.8 tokenizer (positional-text CLI) is installed. It cannot
        # receive the query on stdin, so name the problem instead of hiding it
        # behind the generic n-gram fallback warning.
        return tokens, "fallback-script-ngram" if len(tokens) > 1 else None, "tokenizer_incompatible"
    return tokens, "fallback-script-ngram" if len(tokens) > 1 else None, (
        "fallback_segmentation_nonlinguistic" if len(tokens) > 1 else None
    )


def lookup(
    query: str,
    *,
    config_path: str | os.PathLike[str] | None = None,
    request_id: str | None = None,
    sequence: int | None = None,
    client: SdcvClient | None = None,
    cancel_event: threading.Event | None = None,
    retain_entry_vault: bool = False,
    retain_full_blobs: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one explicit local lookup and return (public result, private vault)."""

    clock_ns = getattr(client, "clock_ns", time.monotonic_ns)
    started_ns = clock_ns()
    request_id = str(request_id or uuid.uuid4())
    sequence = int(sequence if sequence is not None else started_ns)
    query = str(query).strip()
    query_too_long = len(query) > MAX_QUERY_CHARS
    if query_too_long:
        query = query[:MAX_QUERY_CHARS]
    config, config_meta = load_config(config_path)
    features = detect_features(query)
    try:
        source_timeout = min(5.0, max(0.1, int(config.get("source_timeout_ms", 1500)) / 1000))
        lookup_timeout_ms = min(30_000, max(250, int(config.get("lookup_timeout_ms", 3000))))
    except (TypeError, ValueError):
        source_timeout = 1.5
        lookup_timeout_ms = 3000
    client = client or SdcvClient(
        timeout=source_timeout,
        data_dirs=config.get("stardict_data_dir"),
        cancel_event=cancel_event,
    )
    if cancel_event is not None and hasattr(client, "cancel_event"):
        client.cancel_event = cancel_event
    clock_ns = getattr(client, "clock_ns", time.monotonic_ns)
    if hasattr(client, "deadline_ns"):
        client.deadline_ns = started_ns + lookup_timeout_ms * 1_000_000
    backend_started_ns = client.backend_ns

    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "backend": "sdcv" if client.available else "unavailable",
        "query": query,
        "request_id": request_id,
        "sequence": sequence,
        "features": features,
        "title": query,
        "segmented": False,
        "tokens": [],
        "token_items": [],
        "results_by_token": {},
        "segmentation": {"engine": None, "warning": None},
        "incomplete_tokens": [],
        "error": None,
        "query_truncated": query_too_long,
        "config": config_meta,
        "detections": [],
    }
    detector_warning = None
    if query and not query_too_long:
        # Pure local computation on the query; never fatal to the lookup.
        try:
            base["detections"] = detect_data(query, config)
        except Exception:  # noqa: BLE001 - a detector bug must not hide definitions
            detector_warning = "detector_error"
    empty_modes = {
        "candidate_modes": candidate_modes(config, features),
        "installed_modes": [],
        "unavailable_modes": [
            str(key)
            for key, mode in config.get("modes", {}).items()
            if isinstance(mode, Mapping) and mode.get("enabled", True)
        ],
        "incomplete_modes": [],
        "available_modes": [],
        "default_mode": None,
        "results_by_mode": {},
        "related_by_mode": {},
        "mode_preferences": {},
        "mode_status": {},
        "warnings": [],
    }
    vault: dict[str, Any] = {"entries": {}, "full_blobs": {}}

    if query_too_long:
        base.update(empty_modes)
        base["error"] = {
            "code": "query_too_long",
            "message": f"Query exceeds the {MAX_QUERY_CHARS}-character safety limit",
        }
    elif not query:
        base.update(empty_modes)
        base["error"] = {"code": "empty_query", "message": "No text was provided"}
    elif not client.available:
        base.update(empty_modes)
        base["error"] = {"code": "sdcv_missing", "message": "Install sdcv to use local StarDict dictionaries"}
    else:
        try:
            base, vault = _lookup_with_backend(
                base,
                query,
                config,
                client,
                features,
                clock_ns=clock_ns,
                retain_entry_vault=retain_entry_vault,
                retain_full_blobs=retain_full_blobs,
            )
        finally:
            # Session-scoped backends keep one sdcv process per source alive
            # across every token of this lookup; end them here so no process
            # outlives the request, and surface a bad exit as a warning.
            close = getattr(client, "close", None)
            close_warning = close() if callable(close) else None
        if close_warning:
            base["warnings"] = sorted(set(base.get("warnings", [])) | {close_warning})

    if detector_warning:
        base["warnings"] = sorted(set(base.get("warnings", [])) | {detector_warning})
    default_mode = base.get("default_mode")
    default_result = base.get("results_by_mode", {}).get(default_mode, {}) if default_mode else {}
    base["subtitle"] = default_result.get("subtitle", "")
    base["body"] = default_result.get("body", "")
    base["truncated"] = bool(default_result.get("truncated", False))
    finished_ns = clock_ns()
    base["timing"] = {
        "started_monotonic_ns": started_ns,
        "backend_ms": round((client.backend_ns - backend_started_ns) / 1_000_000, 3),
        "total_ms": round((finished_ns - started_ns) / 1_000_000, 3),
    }
    return base, vault


def _lookup_with_backend(
    base: dict[str, Any],
    query: str,
    config: Mapping[str, Any],
    client: Any,
    features: Mapping[str, Any],
    *,
    clock_ns: Callable[[], int],
    retain_entry_vault: bool,
    retain_full_blobs: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the mode lookups (and segmentation fallback) against the backend."""
    mode_data, vault = _lookup_modes(
        query,
        config,
        client,
        features,
        retain_entry_vault=retain_entry_vault,
        retain_full_blobs=retain_full_blobs,
    )
    base.update(mode_data)
    if not mode_data["available_modes"]:
        deadline_ns = getattr(client, "deadline_ns", None)
        remaining_seconds = (
            (deadline_ns - clock_ns()) / 1_000_000_000
            if deadline_ns is not None
            else 2.0
        )
        if getattr(client, "budget_exhausted", False) or remaining_seconds <= 0:
            if hasattr(client, "budget_exhausted"):
                client.budget_exhausted = True
            tokens, segmentation_engine, segmentation_warning = [], None, None
        else:
            tokens, segmentation_engine, segmentation_warning = _segment(
                query,
                features,
                config,
                timeout_seconds=remaining_seconds,
                cancel_event=getattr(client, "cancel_event", None),
            )
        if len(tokens) > 1:
            results_by_token: dict[str, Any] = {}
            token_items: list[dict[str, Any]] = []
            aggregate_warnings = set(base.get("warnings", []))
            for index, token in enumerate(tokens):
                token_features = detect_features(token)
                token_data, token_vault = _lookup_modes(
                    token,
                    config,
                    client,
                    token_features,
                    retain_entry_vault=retain_entry_vault,
                    retain_full_blobs=retain_full_blobs,
                )
                token_key = f"{index}:{token}"
                token_items.append({"key": token_key, "index": index, "token": token})
                results_by_token[token_key] = {
                    "token": token,
                    "index": index,
                    **token_data,
                }
                vault["entries"].update(token_vault["entries"])
                vault["full_blobs"].update(token_vault["full_blobs"])
                aggregate_warnings.update(token_data.get("warnings", []))
            base["warnings"] = sorted(aggregate_warnings)
            # A token cut short by the budget must stay visible: fold its
            # modes into the top-level list the card reads.
            incomplete_tokens = [
                item["key"]
                for item in token_items
                if results_by_token[item["key"]].get("incomplete_modes")
            ]
            if incomplete_tokens:
                token_modes = {
                    mode
                    for key in incomplete_tokens
                    for mode in results_by_token[key]["incomplete_modes"]
                }
                ordered_modes = [str(key) for key in config.get("modes", {})]
                base["incomplete_modes"] = [
                    mode
                    for mode in ordered_modes
                    if mode in token_modes or mode in base.get("incomplete_modes", [])
                ]
            if any(item["available_modes"] for item in results_by_token.values()):
                base["segmented"] = True
                base["incomplete_tokens"] = incomplete_tokens
                base["tokens"] = tokens
                base["token_items"] = token_items
                base["results_by_token"] = results_by_token
                base["segmentation"] = {
                    "engine": segmentation_engine,
                    "warning": segmentation_warning,
                }

    if not base.get("available_modes") and not base.get("segmented") and base.get("warnings"):
        warnings = set(base["warnings"])
        if "lookup_budget_exhausted" in warnings:
            base["error"] = {
                "code": "lookup_timeout",
                "message": "Local dictionary lookup exceeded its configured time budget",
            }
        else:
            base["error"] = {
                "code": "backend_error",
                "message": "One or more local dictionary sources could not be read",
            }
    return base, vault
