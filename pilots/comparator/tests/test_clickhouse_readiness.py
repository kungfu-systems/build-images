from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PILOT_DIR = pathlib.Path(__file__).resolve().parents[1]
ADAPTER_DIR = PILOT_DIR / "workload-adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import clickhouse_phase_a_v1 as clickhouse  # noqa: E402


class ClickHouseReadinessTests(unittest.TestCase):
    def test_requires_three_consecutive_successes_after_transient_refusal(self) -> None:
        refused = subprocess.CompletedProcess(
            ["clickhouse-client"], 210, "", "Connection refused (localhost:9000)"
        )
        passed = subprocess.CompletedProcess(["clickhouse-client"], 0, "1\n", "")
        with tempfile.TemporaryDirectory() as temporary:
            project = clickhouse.ComposeProject("qualification-test", pathlib.Path(temporary))
            project.command = mock.Mock(side_effect=[refused, passed, refused, passed, passed, passed])
            with mock.patch.object(clickhouse.time, "sleep", return_value=None):
                project.wait_ready()
            self.assertEqual(project.command.call_count, 6)
            evidence = json.loads(
                (pathlib.Path(temporary) / "clickhouse-readiness-01.json").read_text(encoding="utf-8")
            )
            self.assertTrue(evidence["passed"])
            self.assertEqual(evidence["required_consecutive_successes"], 3)
            self.assertEqual(evidence["attempts"][-1]["consecutive_successes"], 3)
            self.assertEqual(evidence["attempts"][2]["consecutive_successes"], 0)


if __name__ == "__main__":
    unittest.main()
