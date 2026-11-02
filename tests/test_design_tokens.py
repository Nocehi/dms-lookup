#!/usr/bin/env python3
"""Design values live only in qml/LookupTheme.qml.

Fails on hard-coded radii, colour literals and font-family strings in any
other QML file, and on matugen templates that miss a palette token.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "qml/LookupTheme.qml"
QML_FILES = sorted(
    path
    for directory in ("qml", "plugin", "standalone")
    for path in (ROOT / directory).rglob("*.qml")
    if path != TOKEN_FILE
)
TEMPLATES = [ROOT / "qml/matugen-dark-template.json", ROOT / "qml/matugen-light-template.json"]

RULES = {
    "hard-coded radius": re.compile(
        r"\b(?:radius|topLeftRadius|topRightRadius|bottomLeftRadius|bottomRightRadius)"
        r"\s*:\s*-?\d"
    ),
    "hex colour literal": re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    "named colour literal": re.compile(r"\bcolor\s*:\s*\"(?!transparent\")[^\"]*\""),
    "inline Qt colour": re.compile(r"\bQt\.(?:rgba|hsla|hsva|lighter|darker)\s*\("),
    "font family literal": re.compile(r"\bfont\.famil(?:y|ies)\s*:\s*[\"\[]"),
}


def code_lines(path: pathlib.Path):
    """Yield (line number, text) with // comments removed."""

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        yield number, re.sub(r"(^|\s)//.*$", "", line)


def violations(paths):
    found = []
    for path in paths:
        for number, line in code_lines(path):
            for rule, pattern in RULES.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {rule}: {line.strip()}")
    return found


def palette_keys():
    text = TOKEN_FILE.read_text(encoding="utf-8")
    block = re.search(r'"dark"\s*:\s*\{(.*?)\}', text, re.S)
    assert block, "fallbackPalettes.dark not found in LookupTheme.qml"
    return re.findall(r'"([a-z_]+)"\s*:', block.group(1))


class DesignTokenLintTests(unittest.TestCase):
    def test_qml_outside_the_token_file_uses_tokens_only(self):
        self.assertTrue(QML_FILES, "no QML files found")
        self.assertEqual(violations(QML_FILES), [])

    def test_lint_rules_catch_each_kind_of_literal(self):
        samples = {
            "hard-coded radius": "radius: 9",
            "hex colour literal": 'color: "#ff5252"',
            "named colour literal": 'color: "white"',
            "inline Qt colour": "color: Qt.rgba(0, 0, 0, 0.3)",
            "font family literal": 'font.family: "Noto Serif"',
        }
        for rule, sample in samples.items():
            with self.subTest(rule=rule):
                self.assertTrue(RULES[rule].search(sample))
        for allowed in ("radius: root.theme.radiusSmall", 'color: "transparent"',
                        "font.family: root.theme.fontUi"):
            with self.subTest(allowed=allowed):
                self.assertFalse(any(pattern.search(allowed) for pattern in RULES.values()))

    def test_token_file_defines_both_fallback_palettes_with_the_same_keys(self):
        text = TOKEN_FILE.read_text(encoding="utf-8")
        light = re.search(r'"light"\s*:\s*\{(.*?)\}', text, re.S)
        self.assertIsNotNone(light)
        self.assertEqual(re.findall(r'"([a-z_]+)"\s*:', light.group(1)), palette_keys())
        for key in palette_keys():
            self.assertRegex(text, rf"readonly property color {key}: paletteColor\(\"{key}\"\)")

    def test_matugen_templates_cover_every_palette_token(self):
        keys = palette_keys()
        for template in TEMPLATES:
            with self.subTest(template=template.name):
                text = template.read_text(encoding="utf-8")
                document = json.loads(text.replace("{{", "").replace("}}", ""))
                for key in keys:
                    self.assertEqual(text.count(f'"{key}": "{{{{colors.{key}.default.hex}}}}"'), 1, key)
                self.assertEqual(document["mode"], "mode")
                self.assertIsInstance(document["dark"], bool)


if __name__ == "__main__":
    unittest.main()
