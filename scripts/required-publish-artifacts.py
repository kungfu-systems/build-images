#!/usr/bin/env python3
import argparse
import json
import os
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def required_artifacts(*, version: str = "") -> list[dict]:
    artifacts = []
    for manifest_path in sorted((ROOT / "images").glob("*/image.toml")):
        with manifest_path.open("rb") as handle:
            manifest = tomllib.load(handle)
        if manifest.get("publish") is True:
            artifact = {
                "group": "image",
                "kind": "oci",
                "name": f"ghcr.io/kungfu-systems/build-images/{manifest['name']}",
            }
            if version:
                artifact["ref"] = f"v{version}"
            else:
                artifact["ref_template"] = "v{version}"
            artifacts.append(artifact)
    if not artifacts:
        raise SystemExit("no publishable image artifacts are declared")
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Render exact Buildchain OCI family requirements.")
    parser.add_argument("--github-output", default="")
    parser.add_argument("--verify-env", action="store_true")
    args = parser.parse_args()

    artifacts = required_artifacts()
    rendered = json.dumps(artifacts, separators=(",", ":"), sort_keys=True)
    if args.verify_env:
        version = os.environ.get("BUILDCHAIN_VERSION", "")
        if not version:
            raise SystemExit("BUILDCHAIN_VERSION is required with --verify-env")
        expected = required_artifacts(version=version)
        observed = json.loads(os.environ.get("BUILDCHAIN_REQUIRED_ARTIFACTS", "null"))
        observed = [
            {key: value for key, value in artifact.items() if key in {"group", "kind", "name", "ref"}}
            for artifact in observed
        ] if isinstance(observed, list) else observed
        if observed != expected:
            raise SystemExit(
                "BUILDCHAIN_REQUIRED_ARTIFACTS does not match the exact five-image manifest declaration"
            )
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"json={rendered}\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
