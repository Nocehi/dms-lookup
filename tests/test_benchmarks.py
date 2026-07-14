#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import benchmark_lookup  # noqa: E402
import benchmark_ui  # noqa: E402
import recalculate_benchmark_percentiles  # noqa: E402
from verify_wayland_presentation import (  # noqa: E402
    WaylandVerificationError,
    build_record,
)


class BenchmarkRetentionTests(unittest.TestCase):
    def test_ui_summary_uses_renamed_qml_tick_fields_and_has_p99(self):
        raw = [
            {
                "phase": "ready",
                "first_qml_frame_tick_ms": value,
                "result_qml_frame_tick_ms": value + 2,
            }
            for value in (1.0, 2.0, 4.0)
        ]
        summary = benchmark_ui.summarise(raw)
        self.assertNotIn("trigger_to_first_frame_ms", summary)
        self.assertEqual(summary["first_qml_frame_tick_ms"]["p99"], 4.0)
        self.assertEqual(summary["result_qml_frame_tick_ms"]["p50"], 4.0)

    def test_backend_summary_is_recomputed_from_raw_samples_with_p99(self):
        raw = [
            {
                "wall_ms": value,
                "backend_ms": value / 2,
                "core_total_ms": value,
                "response_bytes": int(value * 10),
            }
            for value in (1.0, 3.0, 9.0)
        ]
        summary = benchmark_lookup.summarise(raw)
        self.assertEqual(summary["samples"], len(raw))
        self.assertEqual(summary["wall_ms"]["p99"], 9.0)

    def test_independent_recalculation_matches_retained_raw_array(self):
        raw = [
            {
                "wall_ms": value,
                "backend_ms": value / 2,
                "core_total_ms": value,
                "response_bytes": int(value * 10),
            }
            for value in (2.0, 4.0)
        ]
        report = {
            "schema_version": 2,
            "scope": {"offscreen": False},
            "phase": {
                "raw_samples": raw,
                "failures": [],
                "summary": benchmark_lookup.summarise(raw),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "backend.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            recalculated = recalculate_benchmark_percentiles.recalculate(path)
        self.assertTrue(recalculated["all_match"])
        self.assertEqual(recalculated["phases"][0]["sample_count"], 2)


class WaylandPresentationTests(unittest.TestCase):
    def test_offscreen_environment_is_rejected(self):
        with self.assertRaises(WaylandVerificationError):
            build_record(
                owner="owner",
                loading_pass=True,
                result_pass=True,
                environment={
                    "QT_QPA_PLATFORM": "offscreen",
                    "XDG_SESSION_TYPE": "wayland",
                    "WAYLAND_DISPLAY": "wayland-1",
                    "XDG_RUNTIME_DIR": "/tmp",
                },
                require_socket=False,
            )

    def test_owner_check_without_compositor_timestamps_emits_no_latency(self):
        record = build_record(
            owner="owner",
            loading_pass=True,
            result_pass=True,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-1",
                "XDG_RUNTIME_DIR": "/tmp",
            },
            require_socket=False,
        )
        self.assertFalse(record["presentation_timestamp_evidence"])
        self.assertNotIn("presentation_timestamps", record)

    def test_compositor_timestamp_evidence_is_explicit(self):
        record = build_record(
            owner="owner",
            loading_pass=True,
            result_pass=True,
            environment={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-1",
                "XDG_RUNTIME_DIR": "/tmp",
            },
            timestamp_source="compositor trace",
            trigger_ns=1_000_000,
            loading_presented_ns=3_000_000,
            result_presented_ns=6_000_000,
            require_socket=False,
        )
        self.assertEqual(
            record["presentation_timestamps"]["result_presentation_ms"],
            5.0,
        )


if __name__ == "__main__":
    unittest.main()
