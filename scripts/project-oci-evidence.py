#!/usr/bin/env python3
"""Project public v4 OCI readback into the retained schema-1 image evidence."""
import argparse
import json
import re
from pathlib import Path


def project(readback, family, release_sha, tooling_sha, target_ref):
    if readback.get("schema") != "kungfu-buildchain-oci-publication-readback/v1":
        raise ValueError("unsupported OCI readback")
    if family.get("schema") != "kungfu-buildchain-oci-family/v1":
        raise ValueError("unsupported OCI family")
    if family.get("repository") != "kungfu-systems/build-images":
        raise ValueError("foreign OCI family")
    for value in [release_sha, tooling_sha, readback.get("sourceSha", "")]:
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("exact Git coordinates required")
    if not re.fullmatch(r"alpha/v\d+/v\d+\.\d+", target_ref):
        raise ValueError("expected protected alpha target")
    if (readback.get("familyRoot") != family.get("root")
            or readback.get("candidateSourceSha") != family.get("sourceSha")
            or readback.get("version") != family.get("version")):
        raise ValueError("readback is not bound to this candidate family")
    images = {i["name"]: i for i in family["images"]}
    observed = {i["name"]: i for i in readback["images"]}
    expected = set(family["expectedImages"])
    if (len(images) != len(family["images"]) or len(observed) != len(readback["images"])
            or set(images) != expected or set(observed) != expected):
        raise ValueError("incomplete or duplicate image family")
    tag = "v" + family["version"]
    artifacts = []
    for name, image in images.items():
        receipt = observed[name]
        for field in ["repository", "digest", "platform", "action", "content", "contractMajor", "parentDigest", "smoke"]:
            if receipt.get(field) != image.get(field):
                raise ValueError(f"{name}: {field} drift")
        if receipt.get("anonymous") is not True or receipt.get("ref") != tag:
            raise ValueError(f"{name}: public exact-tag readback required")
        content = image["content"]
        pointer = f"evidence.json#/artifacts/{len(artifacts)}/verification"
        artifact = {
            "group": "image", "kind": "oci", "name": image["repository"], "ref": tag,
            "digest": image["digest"], "action": image["action"], "platform": image["platform"],
            "contract_major": image["contractMajor"], "parent_digest": image.get("parentDigest"),
            "content": {"version": content["version"], "ref": "v" + content["version"],
                        "source_sha": content["sourceSha"], "material_sha": content["materialSha"]},
            "release": {"version": family["version"], "ref": tag, "target_ref": target_ref,
                        "source_sha": readback["sourceSha"], "material_sha": readback["sourceSha"]},
            "verification": {"public_manifest": True, "ref": tag, "digest": image["digest"],
                             "platform": image["platform"], "contract_major": image["contractMajor"],
                             "parent_digest": image.get("parentDigest"), "evidence": pointer,
                             "smoke": {"passed": True, "policy": "image-manifest-test-commands-v1",
                                       "evidence": pointer + "/smoke"}},
        }
        artifacts.append(artifact)
    return {"schema": 1, "version": family["version"], "channel": "alpha",
            "source_sha": readback["sourceSha"], "release_sha": release_sha,
            "release_material_sha": readback["sourceSha"], "publish_tooling_sha": tooling_sha,
            "target_ref": target_ref, "artifacts": artifacts,
            "v4_evidence": {"family_root": family["root"], "readback": "oci-publication-readback.json"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ["readback", "family", "release-sha", "tooling-sha", "target-ref", "output"]:
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    evidence = project(json.loads(Path(args.readback).read_text()), json.loads(Path(args.family).read_text()),
                       args.release_sha, args.tooling_sha, args.target_ref)
    Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
