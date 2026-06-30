#!/usr/bin/env python3
import argparse
import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "images"


def load_manifests() -> dict[str, dict]:
    manifests: dict[str, dict] = {}
    for manifest_path in sorted(IMAGES_DIR.glob("*/image.toml")):
        with manifest_path.open("rb") as handle:
            data = tomllib.load(handle)
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{manifest_path}: name is required")
        manifests[name] = {
            "name": name,
            "path": str(manifest_path.parent.relative_to(ROOT)),
            "base": data.get("base", {}).get("image") if isinstance(data.get("base"), dict) else None,
            "platform": data.get("platform"),
            "contract_major": data.get("contract_major"),
            "publish": data.get("publish"),
            "test_commands": data.get("build", {}).get("test_commands", []),
        }
    return manifests


def topo_sort(manifests: dict[str, dict]) -> list[dict]:
    ordered: list[dict] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, chain: list[str]) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError("image graph has a cycle: " + " -> ".join(chain + [name]))
        if name not in manifests:
            raise ValueError(f"unknown image referenced by graph: {name}")
        visiting.add(name)
        parent = manifests[name].get("base")
        if parent:
            visit(parent, chain + [name])
        visiting.remove(name)
        visited.add(name)
        ordered.append(manifests[name])

    for name in sorted(manifests):
        visit(name, [])
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve the Kungfu build image DAG.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of image names.")
    parser.add_argument("--github-output", help="Write JSON matrix to this GitHub output file.")
    args = parser.parse_args()

    try:
        ordered = topo_sort(load_manifests())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = {"schema": 1, "images": ordered}
    if args.github_output:
        output_path = Path(args.github_output)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write("image_plan=" + json.dumps(payload, separators=(",", ":")) + "\n")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for image in ordered:
            parent = f" <- {image['base']}" if image.get("base") else ""
            print(f"{image['name']}{parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

