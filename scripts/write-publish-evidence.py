#!/usr/bin/env python3
import argparse
import json
import os
from copy import deepcopy
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
    if summary.get("pushed") is not True:
        raise SystemExit("publish evidence requires a pushed image summary")
    artifacts = []
    evidence_name = Path(args.output).name
    for index, image in enumerate(summary.get("images", [])):
        repository = image.get("repository") or image.get("image", "").split(":", 1)[0]
        digest = image.get("digest", "")
        tag = image.get("tag", "")
        action = image.get("action")
        platform = image.get("platform")
        contract_major = image.get("contract_major")
        content = image.get("content")
        release = image.get("release")
        verification = image.get("verification")
        if (
            not repository
            or not digest
            or not tag
            or action not in {"built", "reused"}
            or not platform
            or not isinstance(contract_major, int)
            or not isinstance(content, dict)
            or not isinstance(release, dict)
            or not isinstance(verification, dict)
        ):
            raise SystemExit(f"incomplete image summary record: {image!r}")
        durable_verification = deepcopy(verification)
        durable_verification["evidence"] = f"{evidence_name}#/artifacts/{index}/verification"
        durable_verification["smoke"]["evidence"] = (
            f"{evidence_name}#/artifacts/{index}/verification/smoke"
        )
        artifact = {
            "group": "image",
            "kind": "oci",
            "name": repository,
            "ref": tag,
            "digest": digest,
            "action": action,
            "platform": platform,
            "contract_major": contract_major,
            "content": content,
            "release": release,
            "verification": durable_verification,
        }
        if image.get("parent_digest"):
            artifact["parent_digest"] = image["parent_digest"]
        artifacts.append(artifact)

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
