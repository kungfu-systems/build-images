#!/usr/bin/env python3
"""Exercise the builder boundary with the real publish planner's family."""
import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("builder", ROOT / "scripts/build-oci-candidate.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class CandidatePlanTests(unittest.TestCase):
    def test_real_full_family_plan_preserves_oci_platforms(self):
        plan = json.loads(subprocess.check_output([
            sys.executable, str(ROOT / "scripts/plan-image-publish.py"),
            "--current-source", "a" * 40, "--changed-path", "package.json",
        ], cwd=ROOT, text=True))
        self.assertEqual(len(plan["images"]), 11)
        original = copy.deepcopy(plan)
        builder.validate_plan(plan)
        self.assertEqual(plan, original)
        self.assertEqual({image["platform"] for image in plan["images"]}, {"linux/amd64"})

    def test_rejects_runner_ids_and_unsupported_platforms(self):
        for platform in ["linux-x64", "windows/amd64", "linux/386", ""]:
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                builder.validate_plan({"images": [{"platform": platform}]})


if __name__ == "__main__":
    unittest.main()
