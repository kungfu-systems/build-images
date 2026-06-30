#!/usr/bin/env python3
import argparse
import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "images"


def load_manifest(path: Path) -> dict:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_manifest(image_dir: Path, data: dict, image_names: set[str], errors: list[str]) -> dict:
    rel = image_dir.relative_to(ROOT)
    name = data.get("name")
    build = data.get("build", {})
    runner = data.get("runner", {})
    base = data.get("base")

    require(data.get("schema") == 1, f"{rel}: schema must be 1", errors)
    require(name == image_dir.name, f"{rel}: name must match directory name", errors)
    require(isinstance(data.get("contract_major"), int) and data["contract_major"] >= 1, f"{rel}: contract_major must be a positive integer", errors)
    require(isinstance(data.get("platform"), str) and data["platform"], f"{rel}: platform is required", errors)
    require(isinstance(data.get("publish"), bool), f"{rel}: publish must be boolean", errors)

    if base is not None:
        parent = base.get("image")
        require(parent in image_names, f"{rel}: base.image must reference a known image", errors)
        require(parent != name, f"{rel}: image must not reference itself as base", errors)

    require(isinstance(runner.get("profile"), str) and runner["profile"], f"{rel}: runner.profile is required", errors)
    require(isinstance(runner.get("self_hosted_required"), bool), f"{rel}: runner.self_hosted_required must be boolean", errors)

    context = build.get("context")
    dockerfile = build.get("dockerfile")
    require(context == ".", f"{rel}: build.context must be '.' for now", errors)
    require(isinstance(dockerfile, str) and dockerfile, f"{rel}: build.dockerfile is required", errors)
    if isinstance(dockerfile, str):
        require((image_dir / dockerfile).is_file(), f"{rel}: Dockerfile path does not exist", errors)

    test_commands = build.get("test_commands")
    require(isinstance(test_commands, list) and test_commands, f"{rel}: build.test_commands must be a non-empty list", errors)
    if isinstance(test_commands, list):
        for index, command in enumerate(test_commands):
            require(isinstance(command, str) and command.strip(), f"{rel}: test_commands[{index}] must be a non-empty string", errors)

    return {
        "name": name,
        "contract_major": data.get("contract_major"),
        "platform": data.get("platform"),
        "base": base.get("image") if isinstance(base, dict) else None,
        "publish": data.get("publish"),
        "test_commands": test_commands,
    }


def assert_acyclic(nodes: dict[str, dict], errors: list[str]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in visited:
            return
        if name in visiting:
            errors.append("image graph has a cycle: " + " -> ".join(path + [name]))
            return
        visiting.add(name)
        parent = nodes[name].get("base")
        if parent:
            visit(parent, path + [name])
        visiting.remove(name)
        visited.add(name)

    for name in nodes:
        visit(name, [])


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Kungfu build image manifests.")
    parser.add_argument("--summary-json", type=Path, help="Write a normalized manifest summary.")
    args = parser.parse_args()

    manifest_paths = sorted(IMAGES_DIR.glob("*/image.toml"))
    if not manifest_paths:
        print("No image manifests found.", file=sys.stderr)
        return 1

    image_names = {path.parent.name for path in manifest_paths}
    errors: list[str] = []
    nodes: dict[str, dict] = {}

    for path in manifest_paths:
        data = load_manifest(path)
        node = validate_manifest(path.parent, data, image_names, errors)
        if isinstance(node.get("name"), str):
            nodes[node["name"]] = node

    assert_acyclic(nodes, errors)

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    summary = {
        "schema": 1,
        "images": [nodes[name] for name in sorted(nodes)],
    }
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

