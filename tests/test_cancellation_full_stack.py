#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import tempfile
import unittest


from tests.probe_socket_architecture import PHASES, execute_gate


class ProductionCancellationFullStackTests(unittest.TestCase):
    def test_three_cancellation_phases_use_production_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            report = execute_gate(pathlib.Path(directory) / "evidence")

        self.assertEqual(report["fixed_socket"]["verdict"], "pass")
        self.assertEqual(
            report["implementation_under_test"]["server"],
            "lib.dank_lookup_runtime.PrivateBackendServer",
        )
        cases = {
            case["detail"].get("phase"): case
            for case in report["fixed_socket"]["cases"]
            if case["detail"].get("phase")
        }
        self.assertEqual(set(cases), set(PHASES))
        self.assertTrue(cases["sdcv_running"]["detail"]["process_tree_reaped"])
        self.assertTrue(cases["tokenizer_running"]["detail"]["process_tree_reaped"])
        self.assertTrue(cases["response_queued"]["detail"]["old_sequence_rejected"])
        self.assertTrue(
            all(case["detail"]["active_registry_empty"] for case in cases.values())
        )


if __name__ == "__main__":
    unittest.main()
