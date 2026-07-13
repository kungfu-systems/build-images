#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "selective-publish-plan.json"
PLANNER = ROOT / "scripts" / "resolve-image-dag.py"


def run_planner(changed_paths: list[str]) -> dict:
    command = [sys.executable, str(PLANNER), "--json"]
    if changed_paths:
        for path in changed_paths:
            command.extend(["--changed-path", path])
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            changed_paths_file = Path(tmp_dir) / "changed-paths.txt"
            changed_paths_file.write_text("", encoding="utf-8")
            command.extend(["--changed-paths-file", str(changed_paths_file)])
            result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main() -> int:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    failures = []

    for fixture in fixtures["cases"]:
        payload = run_planner(fixture["changed_paths"])
        result = payload["selection"]
        expected = fixture["selected_images"]
        matrix_names = [image["name"] for image in payload["images"]]
        if result["selected_images"] != expected:
            failures.append(
                f"{fixture['name']}: selected {result['selected_images']!r}, expected {expected!r}"
            )
        if matrix_names != expected:
            failures.append(f"{fixture['name']}: matrix {matrix_names!r}, expected {expected!r}")
        if result["full_rebuild"] is not fixture["full_rebuild"]:
            failures.append(
                f"{fixture['name']}: full_rebuild={result['full_rebuild']!r}, "
                f"expected {fixture['full_rebuild']!r}"
            )
        if not all(result["reasons"].get(name) for name in expected):
            failures.append(f"{fixture['name']}: every selected image must include a reason")

    if failures:
        for failure in failures:
            print(f"error: {failure}")
        return 1

    print(f"selective publish planner fixtures passed: {len(fixtures['cases'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
