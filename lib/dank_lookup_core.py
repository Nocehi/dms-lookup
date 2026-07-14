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
import signal
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from dank_lookup_sdcv_stdin import (
    DEFAULT_STDOUT_CAP_BYTES,
    SdcvProtocolError,
    parse_protocol_stdout,
    protocol_argv,
    run_bounded_protocol,
)


SCHEMA_VERSION = 2
MAX_QUERY_CHARS = 4096
# Four UTF-8 bytes per Unicode scalar keeps every inline body within the CLI's
# 8 MiB open-text stdin limit. Larger complete bodies must use open-ref.
MAX_INLINE_FULL_CHARS = 2 * 1024 * 1024
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
    _BLOCK_TAGS = frozenset({"br", "p", "div", "li", "tr", "hr", "section"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self._BLOCK_TAGS:
            self.parts.append("\n")
        if tag.casefold() == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.parts.append(html.unescape(f"&#{name};"))


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_TRAILING_HORIZONTAL_SPACE = re.compile(r"[ \t]+(?=\n|$)")
_IFO_MAX_BYTES = 1024 * 1024
_IFO_MAX_FILES = 2048


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
            candidates = sorted(directory.glob("*.ifo"), key=lambda item: item.name)
        except OSError:
            continue
        for path in candidates:
            identity = os.path.abspath(os.fspath(path))
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
    ) -> None:
        self.executable = executable or os.environ.get("DANK_LOOKUP_SDCV", "sdcv")
        self.timeout = timeout
        self.runner = runner or subprocess.run
        self.clock_ns = clock_ns or time.monotonic_ns
        self.protocol_runner = protocol_runner or run_bounded_protocol
        self.cancel_event = cancel_event
        self.stdout_cap_bytes = max(1024, int(stdout_cap_bytes))
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
            run, stdout = self.protocol_runner(
                name="sdcv",
                argv=protocol_argv(
                    self.executable,
                    data_dir=data_dir,
                    dictionaries=[dictionary],
                    exact=exact,
                ),
                environment=self.environment,
                query=query,
                choice=choice,
                timeout_seconds=timeout,
                cancellation_event=self.cancel_event,
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
        if termination == "timeout":
            if self.deadline_ns is not None and self.clock_ns() >= self.deadline_ns:
                self.budget_exhausted = True
                return None, "lookup_budget_exhausted", int(run.get("stdout_bytes", 0))
            return None, "sdcv_source_timeout", int(run.get("stdout_bytes", 0))
        if termination is not None or run.get("returncode") != 0:
            return None, "sdcv_execution_failed", int(run.get("stdout_bytes", 0))
        if not run.get("process_group_reaped", False):
            return None, "sdcv_reap_failed", int(run.get("stdout_bytes", 0))
        parser_choice = choice if int(run.get("choice_prompt_count", 0)) else None
        try:
            parsed = parse_protocol_stdout(stdout, query=query, choice=parser_choice)
        except SdcvProtocolError as exc:
            warning = "sdcv_invalid_json" if str(exc) == "invalid_result_json" else "sdcv_invalid_protocol"
            return None, warning, int(run.get("stdout_bytes", 0))
        return parsed, None, int(run.get("stdout_bytes", 0))

    def query(self, source: Mapping[str, Any], query: str) -> tuple[list[dict[str, Any]], str | None]:
        data_dir = os.path.expanduser(str(source["data_dir"])) if source.get("data_dir") else None
        dictionary = str(source.get("name", ""))
        remaining_cap = self.stdout_cap_bytes
        exact_result, warning, used = self._stdin_query(
            query=query,
            dictionary=dictionary,
            data_dir=data_dir,
            exact=True,
            choice=None,
            stdout_cap_bytes=remaining_cap,
        )
        remaining_cap = max(0, remaining_cap - used)
        if warning in {"lookup_budget_exhausted", "lookup_cancelled", "sdcv_stdout_cap"}:
            return [], warning
        entries = list(exact_result["entries"]) if exact_result is not None else []
        warnings = [warning] if warning else []

        # Exact search is case-sensitive in sdcv. The explicit fuzzy protocol
        # supplies a bounded candidate list; definitions are then fetched by
        # exact stdin lookups and policy remains in _lookup_modes().
        # The adapter always supplies a bounded exact+candidate set. Whether a
        # non-exact headword is accepted is exclusively a lookup policy choice.
        if not self.budget_exhausted:
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
                    candidate, candidate_warning, used = self._stdin_query(
                        query=option_word,
                        dictionary=option_dictionary,
                        data_dir=data_dir,
                        exact=True,
                        choice=None,
                        stdout_cap_bytes=remaining_cap,
                    )
                    remaining_cap = max(0, remaining_cap - used)
                    if candidate_warning:
                        warnings.append(candidate_warning)
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
        entry_truncated = len(full) > max_entry_chars
        preview = full
        if entry_truncated:
            preview = full[:max_entry_chars].rstrip() + "\n" + TRUNCATION_MARKER
        section = _section(entry, preview)
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
                break
            name = str(source.get("name", ""))
            exact_only = source_exact_only(source)
            definition_format = resolve_definition_format(source, config)
            entries, warning = client.query(source, query)
            if warning:
                warnings.add(warning)
            for entry in entries:
                item = dict(entry)
                item.setdefault("dict", name)
                item["_definition_format"] = definition_format
                if is_exact(item, query):
                    exact_entries.append(item)
                elif not exact_only:
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
    process: subprocess.Popen[bytes] | None = None
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
        termination = None
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                termination = "cancelled"
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            if time.monotonic() >= deadline:
                termination = "timeout"
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.005)
        stdout, _stderr = process.communicate(timeout=1.0)
        if termination == "cancelled":
            return [], None, "lookup_cancelled"
        if termination is not None or len(stdout) > 1024 * 1024:
            raise subprocess.SubprocessError(termination or "tokenizer output exceeds cap")
        value = json.loads(stdout.decode("utf-8"))
        tokens = value.get("tokens", [])
        if process.returncode == 0 and isinstance(tokens, list):
            return [str(token) for token in tokens if str(token).strip()][:24], "external-tokenizer", None
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError, json.JSONDecodeError):
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
    tokens = _fallback_segment(query)
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
        "error": None,
        "query_truncated": query_too_long,
        "config": config_meta,
    }
    empty_modes = {
        "candidate_modes": candidate_modes(config, features),
        "installed_modes": [],
        "unavailable_modes": [
            str(key)
            for key, mode in config.get("modes", {}).items()
            if isinstance(mode, Mapping) and mode.get("enabled", True)
        ],
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
                if any(item["available_modes"] for item in results_by_token.values()):
                    base["segmented"] = True
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
