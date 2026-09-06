#!/usr/bin/env python3
"""Build or reuse the governed family into a local, smoke-qualified OCI layout."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build/oci-candidate"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def validate_plan(plan):
    for image in plan["images"]:
        if image["platform"] not in {"linux/amd64", "linux/arm64"}:
            raise ValueError(f'unsupported OCI plan platform: {image["platform"]}')


def main():
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    version = json.loads((ROOT / "package.json").read_text())["version"]
    if OUTPUT.exists():
        raise RuntimeError("candidate output already exists; use a clean build workspace")
    OUTPUT.mkdir(parents=True)
    layout = OUTPUT / "oci"
    blobs = layout / "blobs/sha256"
    blobs.mkdir(parents=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n')
    plan_path = OUTPUT / "image-publish-plan.json"
    run("python3", str(ROOT / "scripts/plan-image-publish.py"), "--current-source", source,
        "--baseline-lock", str(ROOT / "images.lock.json"), "--fetch-history", "--output", str(plan_path))
    plan = json.loads(plan_path.read_text())
    validate_plan(plan)
    descriptors, family, digests, local_refs = [], [], {}, {}
    for image in plan["images"]:
        name = image["name"]
        repository = f"ghcr.io/kungfu-systems/build-images/{name}"
        local_ref = f"buildchain-candidate/{name}:{source}"
        manifest = tomllib.loads((ROOT / image["path"] / "image.toml").read_text())
        action = image["action"]
        if action == "reused":
            baseline = image["baseline"]
            immutable = f'{baseline["image"]}@{baseline["digest"]}'
            run("python3", str(ROOT / "scripts/ghcr-manifest.py"), "--repository", baseline["image"],
                "--ref", baseline["digest"], "--expected-digest", baseline["digest"], "--attempts", "3")
            run("docker", "pull", immutable)
            run("docker", "tag", immutable, local_ref)
            content = {"sourceSha": baseline["content"]["source_sha"], "version": baseline["content"]["version"], "materialSha": baseline["content"]["material_sha"]}
            transport = f"docker://{immutable}"
        elif action == "built":
            args = ["docker", "build", "--platform", image["platform"], "--label",
                    "org.opencontainers.image.source=https://github.com/kungfu-systems/build-images",
                    "--label", f"org.opencontainers.image.revision={source}",
                    "--label", f"org.opencontainers.image.version={version}",
                    "--label", f'io.kungfu.image.contract-major={image["contract_major"]}',
                    "--label", f'io.kungfu.image.platform={image["platform"]}',
                    "--label", f"io.kungfu.buildchain.release-material-sha={source}"]
            if image.get("base"):
                parent = image["base"]
                args += ["--build-arg", f"BASE_IMAGE={local_refs[parent]}",
                         "--label", f"io.kungfu.image.parent-digest={digests[parent]}"]
            run(*args, "-t", local_ref, str(ROOT / image["path"]))
            content = {"sourceSha": source, "version": version, "materialSha": source}
        else:
            raise RuntimeError(f"unsupported image action: {action}")
        local_refs[name] = local_ref
        log_path = OUTPUT / f"{name}-smoke.log"
        with log_path.open("wb") as log:
            for command in manifest["build"]["test_commands"]:
                run("docker", "run", "--rm", local_ref, "sh", "-lc", command,
                    stdout=log, stderr=subprocess.STDOUT)
        smoke = {"schema": 1, "image": name, "action": action, "passed": True,
                 "policy": "image-manifest-test-commands-v1", "log_sha256": sha(log_path.read_bytes())}
        smoke_path = OUTPUT / f"{name}-smoke.json"
        smoke_path.write_text(json.dumps(smoke, indent=2) + "\n")
        with tempfile.TemporaryDirectory(prefix="buildchain-image-") as scratch:
            directory = Path(scratch) / "export"
            args = ["skopeo", "copy"]
            if action == "reused":
                args += ["--preserve-digests"]
            else:
                archive = Path(scratch) / "image.tar"
                run("docker", "image", "save", "--output", str(archive), local_ref)
                transport = f"docker-archive:{archive}"
                args += ["--dest-compress"]
            run(*args, transport, f"dir:{directory}")
            raw = (directory / "manifest.json").read_bytes()
            document = json.loads(raw)
            digest = sha(raw)
            if action == "reused" and digest != image["baseline"]["digest"]:
                raise RuntimeError("reused image manifest bytes changed")
            for descriptor in [document["config"], *document["layers"]]:
                filename = descriptor["digest"].removeprefix("sha256:")
                target = blobs / filename
                if not target.exists():
                    shutil.copyfile(directory / filename, target)
            (blobs / digest.removeprefix("sha256:")).write_bytes(raw)
        digests[name] = digest
        descriptors.append({"mediaType": document["mediaType"], "digest": digest, "size": len(raw),
                            "annotations": {"org.opencontainers.image.ref.name": name}})
        family.append({"name": name, "repository": repository, "digest": digest, "layout": "oci",
                       "platform": image["platform"], "action": action, "content": content,
                       "contractMajor": image["contract_major"], "parentDigest": digests.get(image.get("base")),
                       "smoke": {"path": smoke_path.name, "sha256": sha(smoke_path.read_bytes())}})
    (layout / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": descriptors}) + "\n")
    body = {"schema": "kungfu-buildchain-oci-family/v1", "repository": "kungfu-systems/build-images",
            "sourceSha": source, "version": version, "expectedImages": [i["name"] for i in plan["images"]], "images": family}
    (OUTPUT / "oci-family.input.json").write_text(json.dumps(body) + "\n")
    run("node", str(ROOT / "scripts/seal-oci-candidate.mjs"))


if __name__ == "__main__":
    main()
