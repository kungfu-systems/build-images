#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Write Buildchain publish evidence for image digests.")
    parser.add_argument("--summary", required=True, help="Image digest summary from build-image-family.sh")
    parser.add_argument("--output", default=os.environ.get("BUILDCHAIN_PUBLISH_EVIDENCE", ""))
    args = parser.parse_args()

    if not args.output:
        raise SystemExit("--output or BUILDCHAIN_PUBLISH_EVIDENCE is required")

    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    artifacts = []
    for image in summary.get("images", []):
        repository = image.get("repository") or image.get("image", "").split(":", 1)[0]
        digest = image.get("digest", "")
        tag = image.get("tag", "")
        if not repository or not digest or not tag:
            raise SystemExit(f"incomplete image summary record: {image!r}")
        artifacts.append(
            {
                "group": "image",
                "kind": "oci",
                "name": repository,
                "ref": tag,
                "digest": digest,
            }
        )

    if not artifacts:
        raise SystemExit("image digest summary has no artifacts")

    evidence = {
        "schema": 1,
        "version": required_env("BUILDCHAIN_VERSION"),
        "channel": required_env("BUILDCHAIN_CHANNEL"),
        "source_sha": required_env("BUILDCHAIN_SOURCE_SHA"),
        "release_sha": required_env("BUILDCHAIN_RELEASE_SHA"),
        "target_ref": required_env("BUILDCHAIN_TARGET_REF"),
        "release_material_sha": os.environ.get("BUILDCHAIN_RELEASE_MATERIAL_SHA") or required_env("BUILDCHAIN_RELEASE_SHA"),
        "publish_tooling_sha": os.environ.get("BUILDCHAIN_PUBLISH_TOOLING_SHA") or required_env("BUILDCHAIN_RELEASE_SHA"),
        "artifacts": artifacts,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
