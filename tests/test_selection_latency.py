#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from measure_selection_latency import collect  # noqa: E402
from report_selection_latency import generate, percentile  # noqa: E402


class SelectionLatencyToolTests(unittest.TestCase):
    def test_collector_never_stores_selection_content_or_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = root / "capture"
            fixture.write_text("#!/bin/sh\nprintf '%s' 'private-selection'\n", encoding="utf-8")
            fixture.chmod(0o755)
            args = argparse.Namespace(
                owner_class="browser",
                application="FixtureBrowser",
                toolkit="fixture",
                compositor="fixture",
                selection="primary",
                scenario="primary-present",
                samples=3,
                probe_timeout_ms=1000,
                command=[str(fixture)],
            )
            report = collect(args)
        encoded = json.dumps(report)
        self.assertNotIn("private-selection", encoded)
        self.assertEqual(report["privacy"], {"content_stored": False, "content_hash_stored": False})
        self.assertTrue(all(sample["byte_count"] == len("private-selection") for sample in report["samples"]))

    def test_report_marks_synthetic_incomplete_data_without_approving_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "raw.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "selection_latency_raw",
                        "owner_class": "browser",
                        "scenario": "primary-present",
                        "privacy": {"content_stored": False, "content_hash_stored": False},
                        "samples": [
                            {
                                "duration_ms": value,
                                "returncode": 0,
                                "timed_out": False,
                                "byte_count": 4,
                                "whitespace_only": False,
                                "outcome": "success_nonempty",
                            }
                            for value in (100, 200, 600)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report = generate([path])
        self.assertFalse(report["dataset_complete"])
        self.assertFalse(report["production_default_approved"])
        self.assertIsNone(report["approved_primary_timeout_ms"])
        candidate = next(item for item in report["candidate_timeouts"] if item["timeout_ms"] == 250)
        self.assertAlmostEqual(candidate["erroneous_clipboard_fallback_rate"], 1 / 3, places=6)

    def test_percentile_uses_nearest_rank_for_raw_recalculation(self):
        self.assertEqual(percentile([1, 2, 3, 100], 50), 2.0)
        self.assertEqual(percentile([1, 2, 3, 100], 99), 100.0)


if __name__ == "__main__":
    unittest.main()
