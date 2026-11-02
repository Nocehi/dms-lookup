#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from run_backend_fixtures import CONFIG_BY_SET, config_matches, fixture_expectations  # noqa: E402


V1_CJK = {
    "version": 1,
    "modes": {"han": {"sources": [{"name": "萌典重編國語辭典"}, {"name": "CC-CEDICT 漢英辭典"}]}},
}
V2_MOE = {"version": 2, "modes": {"han": {"sources": [{"name": "萌典重編國語辭典"}]}}}


class ConfigVariantTests(unittest.TestCase):
    def test_version_defaults_to_one(self):
        self.assertTrue(config_matches({"modes": {}}, {"version": 1}))
        self.assertFalse(config_matches(V2_MOE, {"version": 1}))

    def test_mode_sources_include_requires_every_named_source(self):
        when = {"mode_sources_include": {"han": ["CC-CEDICT 漢英辭典"]}}
        self.assertTrue(config_matches(V1_CJK, when))
        self.assertFalse(config_matches(V2_MOE, when))
        self.assertFalse(config_matches(V1_CJK, {"mode_sources_include": {"ja": ["CC-CEDICT 漢英辭典"]}}))

    def test_unknown_condition_is_rejected(self):
        with self.assertRaises(ValueError):
            config_matches(V1_CJK, {"default_mode": "han"})

    def test_variant_merges_over_base_and_requires_reason(self):
        fixture = {
            "expect": {"default_mode": "ja", "results_by_mode.ja": {"body_contains": ["はいく"]}},
            "expect_by_config": [
                {
                    "when": {"version": 1},
                    "reason": "v1",
                    "expect": {"default_mode": "han", "results_by_mode.ja": {"subtitle_contains": ["x"]}},
                }
            ],
        }
        expect, applied = fixture_expectations(fixture, V1_CJK)
        self.assertEqual(applied, ["v1"])
        self.assertEqual(expect["default_mode"], "han")
        self.assertEqual(
            expect["results_by_mode.ja"],
            {"body_contains": ["はいく"], "subtitle_contains": ["x"]},
        )
        expect, applied = fixture_expectations(fixture, V2_MOE)
        self.assertEqual((expect["default_mode"], applied), ("ja", []))
        del fixture["expect_by_config"][0]["reason"]
        with self.assertRaises(ValueError):
            fixture_expectations(fixture, V1_CJK)

    def test_repository_fixture_variants_use_known_conditions_and_reasons(self):
        for fixture_set, config_path in CONFIG_BY_SET.items():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            for path in sorted((ROOT / "tests" / fixture_set).glob("*.json")):
                fixture = json.loads(path.read_text(encoding="utf-8"))
                with self.subTest(fixture=f"{fixture_set}/{path.name}"):
                    fixture_expectations(fixture, config)

    def test_both_schema_versions_cover_the_linux_related_row(self):
        path = ROOT / "tests/fixtures-v2-related/linux-related.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        v2_config = json.loads(CONFIG_BY_SET["fixtures-v2-related"].read_text(encoding="utf-8"))
        self.assertEqual(v2_config["version"], 2)
        v2_expect, v2_applied = fixture_expectations(fixture, v2_config)
        v1_expect, v1_applied = fixture_expectations(fixture, {"version": 1, "modes": {}})
        self.assertEqual(v2_applied, [])
        self.assertEqual(len(v1_applied), 1)
        self.assertIn("[WordNet]", v2_expect["related_by_mode.en-en"]["body_contains"])
        self.assertIn("[WordNet]", v1_expect["related_by_mode.en-en"]["body_not_contains"])


if __name__ == "__main__":
    unittest.main()
