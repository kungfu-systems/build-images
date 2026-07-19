#!/usr/bin/env python3
"""Stage every frozen input and emit an unscored formal-performance receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PILOT_DIR = SCRIPT_DIR.parent
REPO_ROOT = PILOT_DIR.parents[1]
KUNGFU_PACKAGE_DESTINATION = (
    PILOT_DIR / "images/kungfu/kungfu-episodes-cli-linux-x64.tar.gz"
)
FROZEN_IMAGES = (
    "postgres:18.4-bookworm@sha256:d9c83446333daec3f0588cc709adb80c26090b7f9f0f7ec8d43c243385d79818",
    "ghcr.io/kungfu-systems/build-images/clickhouse-server@sha256:964dfcdf7f33ed509f50757061456618e1a13d99340e86c678130f9bd235cdc8",
    "ghcr.io/kungfu-systems/build-images/aeron-native-kit@sha256:684be4100866a3908020d1f368fe1100225a1101a65e9a0ee6d4da6a94963c76",
)
SOURCE_SHA = "d6fb3879c8f495b6b4e4a1a619ce78358291a6e1"
PACKAGE_SHA256 = "8bb1a8933ed053e17eabe09795340d90ff143043a35b719d5e6b951377dc3e4f"
EXACT_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


class PreparationError(RuntimeError):
    pass


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(
    command: list[str],
    *,
    cwd: pathlib.Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    timeout: int = 7200,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreparationError(
            f"command failed: {' '.join(command)}: {error}"
        ) from error
    if result.returncode != 0:
        raise PreparationError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr}"
        )
    return result


def exact_image(value: str, repository: str) -> str:
    prefix = f"ghcr.io/kungfu-systems/build-images/{repository}@sha256:"
    if not value.startswith(prefix) or not EXACT_IMAGE.fullmatch(value):
        raise PreparationError(f"{repository} must use its exact published digest")
    return value


def require_empty(path: pathlib.Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise PreparationError(f"staging directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def canonical_repo_digest(image: str) -> str:
    reference, digest = image.rsplit("@", 1)
    last_colon = reference.rfind(":")
    if last_colon > reference.rfind("/"):
        reference = reference[:last_colon]
    return f"{reference}@{digest}"


def inspect_image(image: str) -> dict[str, Any]:
    result = run(["docker", "image", "inspect", image])
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PreparationError(f"docker image inspect is invalid: {error}") from error
    if not isinstance(values, list) or len(values) != 1:
        raise PreparationError("docker image inspect did not return one image")
    value = values[0]
    repo_digests = value.get("RepoDigests", [])
    if canonical_repo_digest(image) not in repo_digests:
        raise PreparationError(f"local image does not retain exact digest: {image}")
    return {
        "identity": image,
        "image_id": value["Id"],
        "repo_digests": repo_digests,
    }


def source_identity(source: pathlib.Path) -> dict[str, Any]:
    head = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    status = run(["git", "-C", str(source), "status", "--porcelain"]).stdout
    if head != SOURCE_SHA or status:
        raise PreparationError("Kungfu source must be clean at the frozen SHA")
    common = pathlib.Path(
        run(
            [
                "git",
                "-C",
                str(source),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ]
        ).stdout.strip()
    )
    return {"source_sha": head, "git_common_dir": str(common)}


def copy_kit(runner_image: str, destination: pathlib.Path) -> dict[str, Any]:
    container = run(["docker", "create", "--platform", "linux/amd64", runner_image])
    container_id = container.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise PreparationError("runner extraction container id is invalid")
    try:
        run(
            [
                "docker",
                "cp",
                f"{container_id}:/opt/formal-performance/.",
                str(destination),
            ]
        )
    finally:
        run(["docker", "rm", container_id])
    required = (
        destination / "bin/formal-performance",
        destination / "bin/formal-performance-provider",
        destination / "bin/compile-kungfu-formal-driver",
        destination / "drivers/aeron/formal-performance-aeron.jar",
        destination / "drivers/kungfu/formal_performance_fixture.cpp",
    )
    if any(not path.is_file() for path in required):
        raise PreparationError("extracted runner kit is incomplete")
    return {
        path.relative_to(destination).as_posix(): sha256_file(path)
        for path in required
    }


def build_kungfu(
    source: pathlib.Path,
    common_git: pathlib.Path,
    cache_home: pathlib.Path,
    staging: pathlib.Path,
    builder_image: str,
    kit: pathlib.Path,
) -> dict[str, Any]:
    cache_home.mkdir(parents=True, exist_ok=True)
    evidence = staging / "kungfu-build"
    evidence.mkdir()
    output = (
        source
        / "framework/core/build/Release/kungfu_formal_performance_fixture"
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/home/kungfu",
        "-e",
        "USER=kungfu",
        "-e",
        f"KUNGFU_SOURCE_SHA={SOURCE_SHA}",
        "-e",
        "KUNGFU_BUILD_EVIDENCE_DIR=/evidence",
        "-e",
        f"KUNGFU_BUILD_JOBS={os.environ.get('KUNGFU_BUILD_JOBS', '12')}",
        "-e",
        "GIT_OPTIONAL_LOCKS=0",
        "-v",
        f"{source}:/work",
        "-v",
        f"{common_git}:{common_git}:ro",
        "-v",
        f"{cache_home}:/home/kungfu",
        "-v",
        f"{evidence}:/evidence",
        "-v",
        f"{kit}:/formal-performance:ro",
        builder_image,
        "bash",
        "-c",
        "/opt/kungfu-native-source-build/bin/build-kungfu-core"
        " && python3 /formal-performance/bin/compile-kungfu-formal-driver"
        " --source-root /work"
        " --driver-source /formal-performance/drivers/kungfu/formal_performance_fixture.cpp"
        " --output /work/framework/core/build/Release/kungfu_formal_performance_fixture"
        " --receipt /evidence/formal-driver-build.json",
    ]
    environment = dict(os.environ)
    for name in (
        "COREPACK_NPM_REGISTRY",
        "NPM_CONFIG_REGISTRY",
        "NODEJS_ORG_MIRROR",
        "UV_PYTHON_INSTALL_MIRROR",
        "KUNGFU_CONAN_REMOTE_URL",
        "KF_LIBWASM_CARGO_REGISTRY",
    ):
        if name in environment:
            command[command.index(builder_image) : command.index(builder_image)] = [
                "-e",
                f"{name}={environment[name]}",
            ]
    started = time.monotonic_ns()
    result = run(command)
    elapsed_ns = time.monotonic_ns() - started
    (evidence / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (evidence / "stderr.log").write_text(result.stderr, encoding="utf-8")
    if not output.is_file():
        raise PreparationError("Kungfu formal driver build did not produce a binary")
    identity = source_identity(source)
    return {
        "source_sha": identity["source_sha"],
        "builder_image": builder_image,
        "elapsed_ns": elapsed_ns,
        "scored": False,
        "formal_driver": {
            "path": str(output),
            "sha256": sha256_file(output),
        },
        "receipt": {
            "path": "kungfu-build/formal-driver-build.json",
            "sha256": sha256_file(evidence / "formal-driver-build.json"),
        },
    }


def build_package_image(
    package: pathlib.Path,
    version: str,
    staging: pathlib.Path,
) -> dict[str, Any]:
    if sha256_file(package) != PACKAGE_SHA256:
        raise PreparationError("Kungfu CLI package SHA-256 drifted")
    KUNGFU_PACKAGE_DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package, KUNGFU_PACKAGE_DESTINATION)
    tag = f"kungfu-formal-package:{PACKAGE_SHA256[:24]}"
    started = time.monotonic_ns()
    result = run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--build-arg",
            f"KUNGFU_CLI_PACKAGE_SHA256={PACKAGE_SHA256}",
            "--build-arg",
            f"KUNGFU_CLI_VERSION={version}",
            "--build-arg",
            f"KUNGFU_CLI_SOURCE_SHA={SOURCE_SHA}",
            "-t",
            tag,
            str(PILOT_DIR / "images/kungfu"),
        ]
    )
    (staging / "kungfu-package-build.stdout.log").write_text(
        result.stdout, encoding="utf-8"
    )
    (staging / "kungfu-package-build.stderr.log").write_text(
        result.stderr, encoding="utf-8"
    )
    inspected = json.loads(
        run(["docker", "image", "inspect", tag]).stdout
    )[0]
    labels = inspected.get("Config", {}).get("Labels", {}) or {}
    if (
        labels.get("org.opencontainers.image.version") != version
        or labels.get("org.opencontainers.image.revision") != SOURCE_SHA
    ):
        raise PreparationError("Kungfu package runtime image labels drifted")
    return {
        "artifact": str(package),
        "artifact_sha256": PACKAGE_SHA256,
        "version": version,
        "image": tag,
        "image_id": inspected["Id"],
        "elapsed_ns": time.monotonic_ns() - started,
        "scored": False,
    }


def write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-image", required=True)
    parser.add_argument("--builder-image", required=True)
    parser.add_argument("--kungfu-source", type=pathlib.Path, required=True)
    parser.add_argument("--kungfu-package", type=pathlib.Path, required=True)
    parser.add_argument("--kungfu-version", required=True)
    parser.add_argument("--kungfu-evidence-url", required=True)
    parser.add_argument("--cache-home", type=pathlib.Path, required=True)
    parser.add_argument("--scratch-root", type=pathlib.Path, required=True)
    parser.add_argument("--staging", type=pathlib.Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        if not args.execute:
            raise PreparationError("preparation requires explicit --execute")
        runner_image = exact_image(
            args.runner_image, "comparator-formal-runner"
        )
        builder_image = exact_image(
            args.builder_image, "kungfu-native-linux-x64"
        )
        source = args.kungfu_source.resolve()
        package = args.kungfu_package.resolve()
        scratch_root = args.scratch_root.resolve()
        if not scratch_root.is_dir():
            raise PreparationError("--scratch-root must be an existing directory")
        staging = args.staging.resolve()
        require_empty(staging)
        source_record = source_identity(source)
        pulls = []
        for image in (*FROZEN_IMAGES, builder_image, runner_image):
            started = time.monotonic_ns()
            run(["docker", "pull", "--platform", "linux/amd64", image])
            pulls.append(
                {
                    **inspect_image(image),
                    "elapsed_ns": time.monotonic_ns() - started,
                    "scored": False,
                }
            )
        kit = staging / "kit"
        kit.mkdir()
        kit_hashes = copy_kit(runner_image, kit)
        kungfu_build = build_kungfu(
            source,
            pathlib.Path(source_record["git_common_dir"]),
            args.cache_home.resolve(),
            staging,
            builder_image,
            kit,
        )
        package_image = build_package_image(
            package, args.kungfu_version, staging
        )
        receipt = {
            "schema": (
                "urn:kungfu-systems:build-images:"
                "formal-performance-preparation:v1"
            ),
            "all_inputs_staged": True,
            "network_disabled_before_measurement": True,
            "measurement_network_policy": (
                "docker --pull never with internal or none networks"
            ),
            "image_transfer": {"scored": False, "images": pulls},
            "kungfu_source_build": kungfu_build,
            "kungfu_package": package_image,
            "runner": {
                "image": runner_image,
                "kit": str(kit),
                "files": kit_hashes,
            },
            "consumer_environment": {
                "FORMAL_PERFORMANCE_REPO_ROOT": str(REPO_ROOT),
                "FORMAL_PERFORMANCE_KUNGFU_SOURCE": str(source),
                "FORMAL_PERFORMANCE_KUNGFU_MATCHED_BINARY": kungfu_build[
                    "formal_driver"
                ]["path"],
                "FORMAL_PERFORMANCE_KUNGFU_BUILDER_IMAGE": builder_image,
                "FORMAL_PERFORMANCE_KUNGFU_IMAGE": package_image["image"],
                "FORMAL_PERFORMANCE_KUNGFU_IMAGE_ID": package_image["image_id"],
                "FORMAL_PERFORMANCE_KUNGFU_VERSION": args.kungfu_version,
                "FORMAL_PERFORMANCE_KUNGFU_EVIDENCE_URL": (
                    args.kungfu_evidence_url
                ),
                "FORMAL_PERFORMANCE_SCRATCH_ROOT": str(scratch_root),
            },
            "authority": "unscored-preparation-only",
        }
        write_json(staging / "preparation.json", receipt)
        print(
            json.dumps(
                {
                    "status": "prepared",
                    "preparation": str(staging / "preparation.json"),
                    "runner": str(kit / "bin/formal-performance"),
                },
                indent=2,
                sort_keys=True,
            )
        )
    except (
        PreparationError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"formal-performance preparation error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
