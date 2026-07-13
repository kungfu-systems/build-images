#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "plan-image-publish.py"
REQUIRED = ROOT / "scripts" / "required-publish-artifacts.py"
WRITE_EVIDENCE = ROOT / "scripts" / "write-publish-evidence.py"
VERIFY_IMAGE = ROOT / "scripts" / "verify-published-image.py"
ACCEPT_IMAGE = ROOT / "scripts" / "accept-image-summary.py"
BUILD_FAMILY = ROOT / "scripts" / "build-image-family.sh"
BUILDCHAIN_PUBLISH_TRANSACTION = (
    ROOT / "node_modules" / "@kungfu-tech" / "buildchain" / "packages" / "core" / "publish-transaction.js"
)
CURRENT_SOURCE = "a" * 40
CURRENT_MATERIAL = "b" * 40
BASELINE_SOURCE = "c" * 40
BASELINE_MATERIAL = "d" * 40


def manifests() -> dict[str, dict]:
    result = {}
    for path in sorted((ROOT / "images").glob("*/image.toml")):
        with path.open("rb") as handle:
            result[path.parent.name] = tomllib.load(handle)
    return result


def complete_lock() -> dict:
    definitions = manifests()
    digests = {
        name: f"sha256:{index + 1:064x}"
        for index, name in enumerate(sorted(definitions))
    }
    entries = []
    for name, manifest in definitions.items():
        parent = manifest.get("base", {}).get("image")
        entries.append(
            {
                "name": name,
                "image": f"ghcr.io/kungfu-systems/build-images/{name}",
                "digest": digests[name],
                "platform": "linux/amd64",
                "contract_major": manifest["contract_major"],
                "parent_digest": digests.get(parent),
                "content": {
                    "version": "1.2.3-alpha.3",
                    "ref": "v1.2.3-alpha.3",
                    "source_sha": BASELINE_SOURCE,
                    "material_sha": BASELINE_MATERIAL,
                },
                "release": {
                    "version": "1.2.3-alpha.3",
                    "ref": "v1.2.3-alpha.3",
                    "target_ref": "alpha/v1/v1.2",
                    "source_sha": BASELINE_SOURCE,
                    "material_sha": BASELINE_MATERIAL,
                },
                "test_commands": manifest["build"]["test_commands"],
            }
        )
    return {
        "schema": 1,
        "tag": "v1.2.3-alpha.3",
        "source": BASELINE_SOURCE,
        "publish_run": "https://github.com/kungfu-systems/build-images/actions/runs/1",
        "images": entries,
    }


def run_plan(lock: dict, changed_path: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "images.lock.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN),
                "--baseline-lock",
                str(lock_path),
                "--current-source",
                CURRENT_SOURCE,
                "--changed-path",
                changed_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        return json.loads(result.stdout)


def assert_selective_plan() -> dict:
    plan = run_plan(complete_lock(), "images/latex-pdf-builder/Dockerfile")
    actions = {image["name"]: image["action"] for image in plan["images"]}
    assert plan["selection"]["selected_images"] == ["latex-pdf-builder"]
    assert actions["latex-pdf-builder"] == "built"
    assert sum(action == "reused" for action in actions.values()) == 4
    return plan


def assert_fail_closed_baselines() -> None:
    missing = complete_lock()
    missing["images"][0].pop("content")
    plan = run_plan(missing, "images/latex-pdf-builder/Dockerfile")
    assert plan["selection"]["full_rebuild"] is True
    assert all(image["action"] == "built" for image in plan["images"])

    drifted = complete_lock()
    child = next(entry for entry in drifted["images"] if entry["name"] == "latex-pdf-builder")
    child["parent_digest"] = "sha256:" + "f" * 64
    plan = run_plan(drifted, "images/latex-pdf-builder/Dockerfile")
    assert plan["selection"]["full_rebuild"] is True


def assert_git_baseline() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    result = subprocess.run(
        [sys.executable, str(PLAN), "--current-source", head],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    plan = json.loads(result.stdout)
    selected = set(plan["selection"]["selected_images"])
    assert all(
        image["action"] == ("built" if image["name"] in selected else "reused")
        for image in plan["images"]
    )
    if plan["selection"]["full_rebuild"]:
        assert selected == {image["name"] for image in plan["images"]}
    if plan["baseline"]["acceptance_sha"]:
        assert plan["baseline"]["eligible"] is True
    else:
        assert plan["baseline"]["eligible"] is False
        assert plan["baseline"]["reason"].endswith("-missing")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_file(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def assert_shallow_history_recovery() -> None:
    spec = importlib.util.spec_from_file_location("plan_image_publish_fixture", PLAN)
    assert spec and spec.loader
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)

    with tempfile.TemporaryDirectory() as tmp:
        fixture_root = Path(tmp)
        origin = fixture_root / "origin"
        shallow = fixture_root / "shallow"
        git(fixture_root, "init", str(origin))
        git(origin, "config", "user.name", "Build Images Test")
        git(origin, "config", "user.email", "build-images-test@kungfu.invalid")
        release_sha = commit_file(origin, "release.txt", "release\n", "release")
        acceptance_sha = commit_file(origin, "images.lock.json", "{}\n", "accept image lock")
        current_sha = commit_file(
            origin,
            "images/latex-pdf-builder/Dockerfile",
            "FROM scratch\n",
            "change latex image",
        )

        subprocess.run(
            ["git", "clone", "--depth", "1", f"file://{origin}", str(shallow)],
            check=True,
            capture_output=True,
            text=True,
        )
        planner.ROOT = shallow
        paths, baseline = planner.resolve_git_changes({"source": release_sha}, current_sha)
        assert paths == [".buildchain/untrusted-baseline"]
        assert baseline["eligible"] is False
        assert baseline["reason"] == "lock-source-missing"

        planner.fetch_git_history(current_sha)
        paths, baseline = planner.resolve_git_changes({"source": release_sha}, current_sha)
        assert paths == ["images/latex-pdf-builder/Dockerfile"]
        assert baseline == {
            "eligible": True,
            "reason": "reviewed-image-lock-acceptance",
            "acceptance_sha": acceptance_sha,
        }


def assert_required_family() -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(REQUIRED)],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    artifacts = json.loads(result.stdout)
    assert len(artifacts) == 5
    assert {artifact["ref_template"] for artifact in artifacts} == {"v{version}"}
    buildchain_resolution = """
import { pathToFileURL } from 'node:url';
const { resolvePublishArtifactRequirements } = await import(pathToFileURL(process.argv[1]).href);
const artifacts = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(resolvePublishArtifactRequirements(artifacts, {
  version: '1.2.3-alpha.4',
})));
"""
    buildchain_result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            buildchain_resolution,
            str(BUILDCHAIN_PUBLISH_TRANSACTION),
            json.dumps(artifacts),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    buildchain_artifacts = json.loads(buildchain_result.stdout)
    assert {artifact["ref"] for artifact in buildchain_artifacts} == {"v1.2.3-alpha.4"}
    assert all("ref_template" not in artifact for artifact in buildchain_artifacts)
    resolved = [
        {**{key: value for key, value in artifact.items() if key != "ref_template"}, "ref": "v1.2.3-alpha.4"}
        for artifact in artifacts
    ]
    env = {
        **os.environ,
        "BUILDCHAIN_VERSION": "1.2.3-alpha.4",
        "BUILDCHAIN_REQUIRED_ARTIFACTS": json.dumps(resolved),
    }
    subprocess.run(
        [sys.executable, str(REQUIRED), "--verify-env"],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )

    unresolved_env = {
        **os.environ,
        "BUILDCHAIN_VERSION": "1.2.3-alpha.4",
        "BUILDCHAIN_REQUIRED_ARTIFACTS": json.dumps(
            [
                {
                    **{key: value for key, value in artifact.items() if key != "ref_template"},
                    "ref": "1.2.3-alpha.4",
                }
                for artifact in artifacts
            ]
        ),
    }
    unresolved = subprocess.run(
        [sys.executable, str(REQUIRED), "--verify-env"],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=unresolved_env,
    )
    assert unresolved.returncode != 0
    assert "does not match the exact five-image" in unresolved.stderr
    return artifacts


def summary_from_plan(plan: dict) -> dict:
    records = []
    digest_by_name = {}
    for index, image in enumerate(plan["images"]):
        digest = (
            image["baseline"]["digest"]
            if image["action"] == "reused"
            else f"sha256:{index + 11:064x}"
        )
        digest_by_name[image["name"]] = digest
        if image["action"] == "reused":
            content = image["baseline"]["content"]
        else:
            content = {
                "version": "1.2.3-alpha.4",
                "ref": "v1.2.3-alpha.4",
                "source_sha": CURRENT_SOURCE,
                "material_sha": CURRENT_MATERIAL,
            }
        parent_digest = digest_by_name.get(image.get("base"))
        verification = {
            "public_manifest": True,
            "ref": "v1.2.3-alpha.4",
            "digest": digest,
            "platform": image["platform"],
            "contract_major": image["contract_major"],
            "evidence": f"image-evidence/{image['name']}/public-manifest.json",
            "smoke": {
                "policy": "image-manifest-test-commands-v1",
                "passed": True,
                "evidence": f"image-evidence/{image['name']}/smoke.json",
            },
        }
        if parent_digest:
            verification["parent_digest"] = parent_digest
        records.append(
            {
                "name": image["name"],
                "repository": f"ghcr.io/kungfu-systems/build-images/{image['name']}",
                "tag": "v1.2.3-alpha.4",
                "digest": digest,
                "action": image["action"],
                "platform": image["platform"],
                "contract_major": image["contract_major"],
                "parent_digest": parent_digest,
                "content": content,
                "release": {
                    "version": "1.2.3-alpha.4",
                    "ref": "v1.2.3-alpha.4",
                    "target_ref": "alpha/v1/v1.2",
                    "source_sha": CURRENT_SOURCE,
                    "material_sha": CURRENT_MATERIAL,
                },
                "verification": verification,
                "test_commands": image["test_commands"],
            }
        )
    return {"schema": 1, "pushed": True, "images": records}


def assert_evidence(plan: dict) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        summary_path = Path(tmp) / "summary.json"
        evidence_path = Path(tmp) / "evidence.json"
        summary_path.write_text(json.dumps(summary_from_plan(plan)), encoding="utf-8")
        env = {
            **os.environ,
            "BUILDCHAIN_VERSION": "1.2.3-alpha.4",
            "BUILDCHAIN_CHANNEL": "alpha",
            "BUILDCHAIN_SOURCE_SHA": CURRENT_SOURCE,
            "BUILDCHAIN_RELEASE_SHA": "e" * 40,
            "BUILDCHAIN_RELEASE_MATERIAL_SHA": CURRENT_MATERIAL,
            "BUILDCHAIN_PUBLISH_TOOLING_SHA": "f" * 40,
            "BUILDCHAIN_TARGET_REF": "alpha/v1/v1.2",
        }
        subprocess.run(
            [sys.executable, str(WRITE_EVIDENCE), "--summary", str(summary_path), "--output", str(evidence_path)],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
        )
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert len(evidence["artifacts"]) == 5
        assert [item["action"] for item in evidence["artifacts"]].count("built") == 1
        assert all(item["verification"]["smoke"]["passed"] for item in evidence["artifacts"])
        assert evidence["artifacts"][0]["verification"]["evidence"] == "evidence.json#/artifacts/0/verification"
        assert evidence["artifacts"][0]["verification"]["smoke"]["evidence"] == (
            "evidence.json#/artifacts/0/verification/smoke"
        )

        requirements_path = Path(tmp) / "requirements.json"
        requirements_path.write_text(
            json.dumps(
                [
                    {key: artifact[key] for key in ("group", "kind", "name", "ref")}
                    for artifact in evidence["artifacts"]
                ]
            ),
            encoding="utf-8",
        )
        buildchain_validation = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const { validatePublishEvidence } = await import(pathToFileURL(process.argv[1]).href);
const evidence = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const requiredArtifacts = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const result = validatePublishEvidence({
  evidence,
  version: evidence.version,
  channel: evidence.channel,
  sourceSha: evidence.source_sha,
  releaseSha: evidence.release_sha,
  targetRef: evidence.target_ref,
  releaseMaterialSha: evidence.release_material_sha,
  publishToolingSha: evidence.publish_tooling_sha,
  requiredArtifacts,
});
if (!result.valid) {
  console.error(JSON.stringify(result));
  process.exit(1);
}
"""
        subprocess.run(
            [
                "node",
                "--input-type=module",
                "--eval",
                buildchain_validation,
                str(BUILDCHAIN_PUBLISH_TRANSACTION),
                str(evidence_path),
                str(requirements_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        lock_path = Path(tmp) / "accepted-images.lock.json"
        subprocess.run(
            [
                sys.executable,
                str(ACCEPT_IMAGE),
                "--evidence",
                str(evidence_path),
                "--publish-run",
                "https://github.com/kungfu-systems/build-images/actions/runs/2",
                "--output",
                str(lock_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        accepted = json.loads(lock_path.read_text(encoding="utf-8"))
        assert accepted["tag"] == "v1.2.3-alpha.4"
        reused = next(item for item in accepted["images"] if item["name"] == "base-linux")
        assert reused["content"]["version"] == "1.2.3-alpha.3"
        assert reused["release"]["version"] == "1.2.3-alpha.4"

        duplicate = json.loads(evidence_path.read_text(encoding="utf-8"))
        duplicate["artifacts"][-1] = json.loads(json.dumps(duplicate["artifacts"][0]))
        duplicate["artifacts"][-1]["verification"]["evidence"] = (
            "evidence.json#/artifacts/4/verification"
        )
        duplicate["artifacts"][-1]["verification"]["smoke"]["evidence"] = (
            "evidence.json#/artifacts/4/verification/smoke"
        )
        duplicate_path = Path(tmp) / "duplicate-evidence.json"
        duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
        rejected = subprocess.run(
            [
                sys.executable,
                str(ACCEPT_IMAGE),
                "--evidence",
                str(duplicate_path),
                "--publish-run",
                "https://github.com/kungfu-systems/build-images/actions/runs/2",
                "--output",
                str(lock_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert rejected.returncode != 0
        assert "each declared image exactly once" in rejected.stderr, rejected.stderr


def assert_image_inspect_fixture() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        inspect_path = Path(tmp) / "inspect.json"
        output_path = Path(tmp) / "verified.json"
        inspect_path.write_text(
            json.dumps(
                [
                    {
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.version": "1.2.3-alpha.4",
                                "org.opencontainers.image.revision": CURRENT_SOURCE,
                                "io.kungfu.buildchain.release-material-sha": CURRENT_MATERIAL,
                                "io.kungfu.image.contract-major": "1",
                                "io.kungfu.image.platform": "linux/amd64",
                            }
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                str(VERIFY_IMAGE),
                "--image",
                "fixture@sha256:test",
                "--inspect-json",
                str(inspect_path),
                "--platform",
                "linux/amd64",
                "--contract-major",
                "1",
                "--content-version",
                "1.2.3-alpha.4",
                "--content-source-sha",
                CURRENT_SOURCE,
                "--content-material-sha",
                CURRENT_MATERIAL,
                "--output",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert json.loads(output_path.read_text(encoding="utf-8"))["platform"] == "linux/amd64"


def assert_digest_preserving_alias() -> None:
    script = BUILD_FAMILY.read_text(encoding="utf-8")
    assert "imagetools create --prefer-index=false" in script


def main() -> int:
    plan = assert_selective_plan()
    assert_fail_closed_baselines()
    assert_git_baseline()
    assert_shallow_history_recovery()
    assert_required_family()
    assert_evidence(plan)
    assert_image_inspect_fixture()
    assert_digest_preserving_alias()
    print("publish provenance fixtures passed: 8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
