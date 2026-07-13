#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def load_inspect(args) -> dict:
    if args.inspect_json:
        value = json.loads(Path(args.inspect_json).read_text(encoding="utf-8"))
    else:
        result = subprocess.run(
            ["docker", "image", "inspect", args.image],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("docker image inspect must return exactly one image")
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError("docker image inspect output must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify immutable image labels and platform provenance.")
    parser.add_argument("--image", required=True)
    parser.add_argument("--inspect-json")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--contract-major", required=True, type=int)
    parser.add_argument("--content-version", required=True)
    parser.add_argument("--content-source-sha", required=True)
    parser.add_argument("--content-material-sha", required=True)
    parser.add_argument("--parent-digest", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        inspect = load_inspect(args)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"image inspection failed: {exc}") from exc

    os_name = str(inspect.get("Os", ""))
    architecture = str(inspect.get("Architecture", ""))
    observed_platform = f"{os_name}/{architecture}"
    labels = inspect.get("Config", {}).get("Labels") or {}
    expected_labels = {
        "org.opencontainers.image.version": args.content_version,
        "org.opencontainers.image.revision": args.content_source_sha,
        "io.kungfu.buildchain.release-material-sha": args.content_material_sha,
        "io.kungfu.image.contract-major": str(args.contract_major),
        "io.kungfu.image.platform": args.platform,
    }
    if args.parent_digest:
        expected_labels["io.kungfu.image.parent-digest"] = args.parent_digest
    elif labels.get("io.kungfu.image.parent-digest"):
        raise SystemExit("root image unexpectedly declares a parent digest")

    errors = []
    if observed_platform != args.platform:
        errors.append(f"platform expected {args.platform}, got {observed_platform or '<empty>'}")
    for key, expected in expected_labels.items():
        actual = labels.get(key, "")
        if actual != expected:
            errors.append(f"label {key} expected {expected}, got {actual or '<empty>'}")
    if errors:
        raise SystemExit("; ".join(errors))

    canonical = json.dumps(inspect, sort_keys=True, separators=(",", ":")).encode()
    payload = {
        "schema": 1,
        "image": args.image,
        "platform": observed_platform,
        "contract_major": args.contract_major,
        "parent_digest": args.parent_digest or None,
        "content": {
            "version": args.content_version,
            "source_sha": args.content_source_sha,
            "material_sha": args.content_material_sha,
        },
        "inspect_sha256": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
