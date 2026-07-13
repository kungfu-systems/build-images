#!/usr/bin/env python3
import argparse
import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_PREFIX = "ghcr.io/kungfu-systems/build-images/"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PLATFORMS = {"linux-x64": "linux/amd64", "linux-arm64": "linux/arm64"}


def load_manifests() -> dict[str, dict]:
    manifests = {}
    for path in sorted((ROOT / "images").glob("*/image.toml")):
        with path.open("rb") as handle:
            manifest = tomllib.load(handle)
        manifests[manifest["name"]] = manifest
    return manifests


def summary_from_evidence(evidence: dict, manifests: dict[str, dict]) -> dict:
    if evidence.get("schema") != 1:
        raise SystemExit("publish evidence schema must be 1")
    version = evidence.get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit("publish evidence version is required")
    for key in ("source_sha", "release_sha", "release_material_sha", "publish_tooling_sha"):
        if not SHA_RE.fullmatch(str(evidence.get(key, ""))):
            raise SystemExit(f"publish evidence {key} must be a full SHA")
    if not isinstance(evidence.get("target_ref"), str) or not evidence["target_ref"]:
        raise SystemExit("publish evidence target_ref is required")
    if evidence.get("channel") not in {"alpha", "release"}:
        raise SystemExit("publish evidence channel must be alpha or release")

    images = []
    for index, artifact in enumerate(evidence.get("artifacts", [])):
        if artifact.get("group") != "image" or artifact.get("kind") != "oci":
            continue
        repository = artifact.get("name", "")
        name = repository.removeprefix(REPOSITORY_PREFIX)
        manifest = manifests.get(name, {})
        release = artifact.get("release", {})
        expected_release = {
            "version": version,
            "ref": f"v{version}",
            "target_ref": evidence["target_ref"],
            "source_sha": evidence["source_sha"],
            "material_sha": evidence["release_material_sha"],
        }
        if any(release.get(key) != value for key, value in expected_release.items()):
            raise SystemExit(f"{name or repository}: artifact release does not match evidence header")
        verification = artifact.get("verification", {})
        expected_pointer = f"evidence.json#/artifacts/{index}/verification"
        if (
            verification.get("evidence") != expected_pointer
            or verification.get("smoke", {}).get("evidence") != f"{expected_pointer}/smoke"
        ):
            raise SystemExit(f"{name or repository}: verification must point into durable evidence.json")
        images.append(
            {
                "name": name,
                "repository": repository,
                "tag": artifact.get("ref"),
                "digest": artifact.get("digest"),
                "action": artifact.get("action"),
                "platform": artifact.get("platform"),
                "contract_major": artifact.get("contract_major"),
                "parent_digest": artifact.get("parent_digest"),
                "content": artifact.get("content"),
                "release": release,
                "verification": verification,
                "test_commands": manifest.get("build", {}).get("test_commands", []),
            }
        )
    return {"schema": 1, "pushed": True, "images": images}


def require_coordinate(value: object, label: str, *, target_ref: bool = False) -> dict:
    if not isinstance(value, dict):
        raise SystemExit(f"{label}: coordinate is required")
    version = value.get("version")
    if not isinstance(version, str) or value.get("ref") != f"v{version}":
        raise SystemExit(f"{label}: version/ref mismatch")
    for key in ("source_sha", "material_sha"):
        if not SHA_RE.fullmatch(str(value.get(key, ""))):
            raise SystemExit(f"{label}: {key} must be a full SHA")
    if target_ref and (not isinstance(value.get("target_ref"), str) or not value["target_ref"]):
        raise SystemExit(f"{label}: target_ref is required")
    return value


def accepted_lock(summary: dict, manifests: dict[str, dict], publish_run: str) -> dict:
    images = summary.get("images", [])
    expected_names = set(manifests)
    names = [image.get("name") for image in images if isinstance(image, dict)]
    if summary.get("pushed") is not True or len(images) != len(expected_names):
        raise SystemExit("accepted image input must contain the complete pushed image family")
    if len(names) != len(set(names)) or set(names) != expected_names:
        raise SystemExit("accepted image input must contain each declared image exactly once")
    if not publish_run.startswith("https://github.com/kungfu-systems/build-images/actions/runs/"):
        raise SystemExit("--publish-run must be a build-images GitHub Actions run URL")

    tags = {image.get("tag") for image in images}
    sources = {image.get("release", {}).get("source_sha") for image in images}
    materials = {image.get("release", {}).get("material_sha") for image in images}
    targets = {image.get("release", {}).get("target_ref") for image in images}
    if len(tags) != 1 or len(sources) != 1 or len(materials) != 1 or len(targets) != 1:
        raise SystemExit("accepted image input must have one release coordinate")
    tag = next(iter(tags))
    source = next(iter(sources))
    if not isinstance(tag, str) or not tag.startswith("v") or not SHA_RE.fullmatch(str(source)):
        raise SystemExit("accepted image input has invalid release coordinates")

    by_name = {image["name"]: image for image in images}
    entries = []
    for name in sorted(expected_names):
        image = by_name[name]
        manifest = manifests[name]
        expected_repository = f"{REPOSITORY_PREFIX}{name}"
        expected_platform = PLATFORMS.get(manifest.get("platform"))
        parent = manifest.get("base", {}).get("image")
        expected_parent_digest = by_name[parent].get("digest") if parent else None
        label = name

        if image.get("repository") != expected_repository:
            raise SystemExit(f"{label}: repository mismatch")
        if not DIGEST_RE.fullmatch(str(image.get("digest", ""))):
            raise SystemExit(f"{label}: invalid digest")
        if image.get("action") not in {"built", "reused"}:
            raise SystemExit(f"{label}: invalid provenance action")
        if image.get("platform") != expected_platform:
            raise SystemExit(f"{label}: platform mismatch")
        if image.get("contract_major") != manifest.get("contract_major"):
            raise SystemExit(f"{label}: contract major mismatch")
        if image.get("parent_digest") != expected_parent_digest:
            raise SystemExit(f"{label}: parent digest does not close over the accepted family")
        if image.get("test_commands") != manifest.get("build", {}).get("test_commands", []):
            raise SystemExit(f"{label}: smoke commands do not match image.toml")

        content = require_coordinate(image.get("content"), f"{label}.content")
        release = require_coordinate(image.get("release"), f"{label}.release", target_ref=True)
        if release.get("ref") != tag or release.get("source_sha") != source:
            raise SystemExit(f"{label}: release coordinate mismatch")

        verification = image.get("verification")
        if not isinstance(verification, dict):
            raise SystemExit(f"{label}: verification is required")
        comparisons = {
            "ref": tag,
            "digest": image["digest"],
            "platform": expected_platform,
            "contract_major": manifest["contract_major"],
            "parent_digest": expected_parent_digest,
        }
        for key, expected in comparisons.items():
            if verification.get(key) != expected:
                raise SystemExit(f"{label}: verification {key} mismatch")
        smoke = verification.get("smoke", {})
        if (
            verification.get("public_manifest") is not True
            or not isinstance(verification.get("evidence"), str)
            or not verification["evidence"]
            or smoke.get("passed") is not True
            or smoke.get("policy") != "image-manifest-test-commands-v1"
            or not isinstance(smoke.get("evidence"), str)
            or not smoke["evidence"]
        ):
            raise SystemExit(f"{label}: public manifest and smoke evidence must pass")

        entries.append(
            {
                "name": name,
                "image": expected_repository,
                "digest": image["digest"],
                "platform": expected_platform,
                "contract_major": manifest["contract_major"],
                "parent_digest": expected_parent_digest,
                "content": content,
                "release": release,
                "test_commands": image["test_commands"],
            }
        )

    return {
        "schema": 1,
        "tag": tag,
        "source": source,
        "publish_run": publish_run,
        "images": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Project reviewed publish evidence into images.lock.json.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--summary", help="Pushed image summary from build-image-family.sh")
    source.add_argument("--evidence", help="Durable Buildchain evidence.json release asset")
    parser.add_argument("--publish-run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifests = load_manifests()
    if args.evidence:
        evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        summary = summary_from_evidence(evidence, manifests)
    else:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    payload = accepted_lock(summary, manifests, args.publish_run)
    output = Path(args.output)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
