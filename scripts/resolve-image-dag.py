#!/usr/bin/env python3
import argparse
import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "images"
GLOBAL_INVALIDATORS = {
    "images.lock.json": "image-lock",
    "scripts/build-image-family.sh": "image-builder",
    "scripts/publish-image-family.sh": "image-publisher",
    "scripts/resolve-image-dag.py": "dag-resolver",
    "scripts/write-publish-evidence.py": "publish-evidence",
}


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


def normalize_changed_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def global_invalidator(path: str) -> str | None:
    if path in GLOBAL_INVALIDATORS:
        return GLOBAL_INVALIDATORS[path]
    if path.startswith(".buildchain/"):
        return "buildchain-config-or-provenance"
    if path.startswith(".github/workflows/"):
        return "workflow"
    if path.startswith("images/") and path.endswith("/image.toml"):
        return "image-manifest"
    return None


def downstream_closure(manifests: dict[str, dict], roots: set[str]) -> tuple[set[str], dict[str, list[str]]]:
    children: dict[str, list[str]] = {name: [] for name in manifests}
    for name, manifest in manifests.items():
        parent = manifest.get("base")
        if parent:
            children[parent].append(name)

    selected = set(roots)
    propagated: dict[str, list[str]] = {name: [] for name in manifests}
    queue = sorted(roots)
    while queue:
        parent = queue.pop(0)
        for child in sorted(children[parent]):
            propagated[child].append(parent)
            if child not in selected:
                selected.add(child)
                queue.append(child)
    return selected, propagated


def plan_changed_paths(manifests: dict[str, dict], changed_paths: list[str]) -> dict:
    normalized_paths = sorted({path for value in changed_paths if (path := normalize_changed_path(value))})
    ordered_names = [image["name"] for image in topo_sort(manifests)]
    direct: dict[str, list[str]] = {name: [] for name in manifests}
    invalidators: list[dict[str, str]] = []

    if not normalized_paths:
        invalidators.append({"path": "", "reason": "no-changed-paths"})

    for path in normalized_paths:
        reason = global_invalidator(path)
        if reason:
            invalidators.append({"path": path, "reason": reason})
            continue

        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "images" and parts[1] in manifests:
            direct[parts[1]].append(path)
            continue

        invalidators.append({"path": path, "reason": "unknown-path"})

    if invalidators:
        selected = set(manifests)
        propagated = {name: [] for name in manifests}
        mode = "full"
    else:
        selected, propagated = downstream_closure(
            manifests,
            {name for name, paths in direct.items() if paths},
        )
        mode = "selective"

    reasons = {}
    for name in ordered_names:
        if name not in selected:
            continue
        image_reasons = []
        image_reasons.extend({"kind": "direct", "path": path} for path in direct[name])
        image_reasons.extend({"kind": "downstream", "parent": parent} for parent in propagated[name])
        image_reasons.extend(
            {"kind": "global", "path": item["path"], "reason": item["reason"]}
            for item in invalidators
        )
        reasons[name] = image_reasons

    return {
        "mode": mode,
        "full_rebuild": selected == set(manifests),
        "changed_paths": normalized_paths,
        "direct_images": [name for name in ordered_names if direct[name]],
        "selected_images": [name for name in ordered_names if name in selected],
        "invalidators": invalidators,
        "reasons": reasons,
    }


def read_changed_paths(values: list[str], file_path: str | None) -> tuple[list[str], bool]:
    paths = list(values)
    selection_requested = bool(values) or file_path is not None
    if file_path:
        paths.extend(Path(file_path).read_text(encoding="utf-8").splitlines())
    return paths, selection_requested


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve the Kungfu build image DAG.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of image names.")
    parser.add_argument("--github-output", help="Write JSON matrix to this GitHub output file.")
    parser.add_argument(
        "--changed-path",
        action="append",
        default=[],
        help="Select the directly changed image and its downstream closure; repeat for multiple paths.",
    )
    parser.add_argument(
        "--changed-paths-file",
        help="Read newline-delimited changed paths. An empty file conservatively selects the full family.",
    )
    args = parser.parse_args()

    try:
        manifests = load_manifests()
        ordered = topo_sort(manifests)
        changed_paths, selection_requested = read_changed_paths(args.changed_path, args.changed_paths_file)
        selection = plan_changed_paths(manifests, changed_paths) if selection_requested else None
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    selected_names = set(selection["selected_images"]) if selection else {image["name"] for image in ordered}
    payload = {"schema": 1, "images": [image for image in ordered if image["name"] in selected_names]}
    if selection:
        payload["selection"] = selection
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
