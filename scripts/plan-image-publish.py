#!/usr/bin/env python3
import argparse
import importlib.util
import json
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "images.lock.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_dag_module():
    module_path = ROOT / "scripts" / "resolve-image-dag.py"
    spec = importlib.util.spec_from_file_location("resolve_image_dag", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load DAG resolver: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DAG = load_dag_module()


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def fetch_git_history(current_source: str) -> None:
    if not SHA_RE.fullmatch(current_source):
        raise ValueError("current source is not a full commit SHA")
    if git("rev-parse", "--is-shallow-repository") != "true":
        return
    git(
        "fetch",
        "--unshallow",
        "--filter=blob:none",
        "--no-tags",
        "origin",
        current_source,
    )
    if git("rev-parse", "--is-shallow-repository") == "true":
        raise RuntimeError("source checkout remains shallow after history fetch")


def read_git_json(ref: str, path: str) -> dict:
    return json.loads(git("show", f"{ref}:{path}"))


def remove_path(value: dict, keys: tuple[str, ...]) -> dict:
    normalized = deepcopy(value)
    current = normalized
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            return normalized
        current = child
    current.pop(keys[-1], None)
    return normalized


def image_neutral_change(base: str, current: str, path: str) -> bool:
    if path.startswith(".buildchain/kfd/"):
        return True
    fields = {
        "package.json": ("version",),
        ".buildchain/release-impact.json": ("release", "version"),
    }
    keys = fields.get(path)
    if not keys:
        return False
    try:
        before = remove_path(read_git_json(base, path), keys)
        after = remove_path(read_git_json(current, path), keys)
    except (json.JSONDecodeError, RuntimeError):
        return False
    return before == after


def changed_paths(base: str, current: str) -> list[str]:
    output = git("diff", "--name-only", "--diff-filter=ACDMRTUXB", base, current, "--")
    paths = [line for line in output.splitlines() if line]
    return [path for path in paths if not image_neutral_change(base, current, path)]


def resolve_git_changes(lock: dict, current_source: str) -> tuple[list[str], dict]:
    lock_source = str(lock.get("source", ""))
    if not SHA_RE.fullmatch(lock_source):
        return [".buildchain/untrusted-baseline"], {
            "eligible": False,
            "reason": "lock-source-invalid",
            "acceptance_sha": "",
        }
    for sha, label in ((lock_source, "lock source"), (current_source, "current source")):
        if not SHA_RE.fullmatch(sha) or not git_ok("cat-file", "-e", f"{sha}^{{commit}}"):
            return [".buildchain/untrusted-baseline"], {
                "eligible": False,
                "reason": f"{label.replace(' ', '-')}-missing",
                "acceptance_sha": "",
            }

    acceptance_sha = git("log", "-1", "--format=%H", current_source, "--", "images.lock.json")
    if not SHA_RE.fullmatch(acceptance_sha):
        return [".buildchain/untrusted-baseline"], {
            "eligible": False,
            "reason": "lock-acceptance-missing",
            "acceptance_sha": "",
        }
    if not git_ok("merge-base", "--is-ancestor", lock_source, acceptance_sha):
        return [".buildchain/untrusted-baseline"], {
            "eligible": False,
            "reason": "lock-source-not-ancestor-of-acceptance",
            "acceptance_sha": acceptance_sha,
        }
    if not git_ok("merge-base", "--is-ancestor", acceptance_sha, current_source):
        return [".buildchain/untrusted-baseline"], {
            "eligible": False,
            "reason": "acceptance-not-ancestor-of-current-source",
            "acceptance_sha": acceptance_sha,
        }

    trust_delta = changed_paths(lock_source, acceptance_sha)
    disallowed = [
        path
        for path in trust_delta
        if path != "images.lock.json" and not path.startswith(".buildchain/kfd/")
    ]
    if disallowed:
        return [".buildchain/untrusted-baseline"], {
            "eligible": False,
            "reason": "unreviewed-content-between-release-and-lock-acceptance",
            "acceptance_sha": acceptance_sha,
            "unexpected_paths": disallowed,
        }
    return changed_paths(acceptance_sha, current_source), {
        "eligible": True,
        "reason": "reviewed-image-lock-acceptance",
        "acceptance_sha": acceptance_sha,
    }


def oci_platform(platform: str) -> str:
    mapping = {
        "linux-x64": "linux/amd64",
        "linux-arm64": "linux/arm64",
    }
    if platform not in mapping:
        raise ValueError(f"unsupported image platform: {platform}")
    return mapping[platform]


def validate_reuse_baseline(lock: dict, manifests: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    lock_tag = lock.get("tag")
    lock_source = lock.get("source")
    entries = {entry.get("name"): entry for entry in lock.get("images", []) if isinstance(entry, dict)}
    if not isinstance(lock_tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-alpha\.\d+)?", lock_tag):
        errors.append("baseline tag is not an exact v-prefixed release")
    if not isinstance(lock_source, str) or not SHA_RE.fullmatch(lock_source):
        errors.append("baseline source is not a full commit SHA")

    for name, manifest in manifests.items():
        entry = entries.get(name)
        if not entry:
            errors.append(f"{name}: baseline entry is missing")
            continue
        expected_repository = f"ghcr.io/kungfu-systems/build-images/{name}"
        if entry.get("image") != expected_repository:
            errors.append(f"{name}: baseline repository mismatch")
        if not DIGEST_RE.fullmatch(str(entry.get("digest", ""))):
            errors.append(f"{name}: baseline digest is invalid")
        expected_platform = oci_platform(manifest["platform"])
        if entry.get("platform") != expected_platform:
            errors.append(f"{name}: baseline platform mismatch")
        if entry.get("contract_major") != manifest.get("contract_major"):
            errors.append(f"{name}: baseline contract major mismatch")
        if entry.get("test_commands") != manifest.get("test_commands"):
            errors.append(f"{name}: baseline smoke policy mismatch")

        content = entry.get("content")
        if not isinstance(content, dict):
            errors.append(f"{name}: baseline content provenance is missing")
            continue
        if content.get("ref") != f"v{content.get('version', '')}":
            errors.append(f"{name}: baseline content version/ref mismatch")
        if not SHA_RE.fullmatch(str(content.get("source_sha", ""))):
            errors.append(f"{name}: baseline content source SHA is invalid")
        if not SHA_RE.fullmatch(str(content.get("material_sha", ""))):
            errors.append(f"{name}: baseline content material SHA is invalid")

        release = entry.get("release")
        if not isinstance(release, dict):
            errors.append(f"{name}: baseline release provenance is missing")
        else:
            expected_release = {
                "version": str(lock_tag or "").removeprefix("v"),
                "ref": lock_tag,
                "source_sha": lock_source,
            }
            for key, expected in expected_release.items():
                if release.get(key) != expected:
                    errors.append(f"{name}: baseline release {key} mismatch")
            if not SHA_RE.fullmatch(str(release.get("material_sha", ""))):
                errors.append(f"{name}: baseline release material SHA is invalid")
            if not isinstance(release.get("target_ref"), str) or not release["target_ref"]:
                errors.append(f"{name}: baseline release target ref is missing")

        parent = manifest.get("base")
        expected_parent = entries.get(parent, {}).get("digest") if parent else None
        if entry.get("parent_digest") != expected_parent:
            errors.append(f"{name}: baseline parent digest mismatch")
    return errors


def force_full(selection: dict, ordered: list[dict], reason: str, details: list[str]) -> dict:
    result = deepcopy(selection)
    result["mode"] = "full"
    result["full_rebuild"] = True
    result["selected_images"] = [image["name"] for image in ordered]
    result.setdefault("invalidators", []).append(
        {"path": "images.lock.json", "reason": reason, "details": details}
    )
    for image in ordered:
        result.setdefault("reasons", {}).setdefault(image["name"], []).append(
            {"kind": "global", "path": "images.lock.json", "reason": reason}
        )
    return result


def trusted_empty_selection() -> dict:
    return {
        "mode": "selective",
        "full_rebuild": False,
        "changed_paths": [],
        "direct_images": [],
        "selected_images": [],
        "invalidators": [],
        "reasons": {},
    }


def build_plan(lock: dict, paths: list[str], current_source: str, baseline: dict) -> dict:
    manifests = DAG.load_manifests()
    ordered = DAG.topo_sort(manifests)
    if (
        not paths
        and baseline.get("eligible") is True
        and baseline.get("reason") == "reviewed-image-lock-acceptance"
    ):
        selection = trusted_empty_selection()
    else:
        selection = DAG.plan_changed_paths(manifests, paths)
    baseline_errors = validate_reuse_baseline(lock, manifests)
    if not selection["full_rebuild"] and (not baseline.get("eligible", True) or baseline_errors):
        details = baseline_errors or [str(baseline.get("reason", "baseline-ineligible"))]
        selection = force_full(selection, ordered, "baseline-provenance-incomplete", details)

    entries = {entry["name"]: entry for entry in lock.get("images", []) if isinstance(entry, dict) and entry.get("name")}
    selected = set(selection["selected_images"])
    images = []
    for manifest in ordered:
        image = deepcopy(manifest)
        image["platform"] = oci_platform(str(manifest["platform"]))
        image["action"] = "built" if image["name"] in selected else "reused"
        if image["action"] == "reused":
            image["baseline"] = deepcopy(entries[image["name"]])
        images.append(image)

    return {
        "schema": 1,
        "current_source": current_source,
        "baseline": {
            **baseline,
            "tag": lock.get("tag", ""),
            "source": lock.get("source", ""),
            "lock_path": "images.lock.json",
        },
        "selection": selection,
        "images": images,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan built and cross-version reused image artifacts.")
    parser.add_argument("--baseline-lock", default=str(LOCK_PATH))
    parser.add_argument("--current-source", required=True)
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--fetch-history", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()

    try:
        lock = json.loads(Path(args.baseline_lock).read_text(encoding="utf-8"))
        if args.changed_path:
            paths = args.changed_path
            baseline = {"eligible": True, "reason": "explicit-test-paths", "acceptance_sha": ""}
        else:
            if args.fetch_history:
                fetch_git_history(args.current_source)
            paths, baseline = resolve_git_changes(lock, args.current_source)
        payload = build_plan(lock, paths, args.current_source, baseline)
    except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
