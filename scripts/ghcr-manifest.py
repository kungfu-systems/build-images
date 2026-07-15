#!/usr/bin/env python3
import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
    return urllib.request.urlopen(
        urllib.request.Request(url, method=method, headers=headers or {}),
        timeout=30,
    )


def missing_manifest(repository: str, ref: str, *, reason: str, public_manifest: bool) -> dict:
    return {
        "schema": 1,
        "public_manifest": public_manifest,
        "exists": False,
        "repository": repository,
        "ref": ref,
        "digest": "",
        "missing_reason": reason,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def inspect_manifest(repository: str, ref: str, *, allow_missing_package: bool = False) -> dict:
    prefix = "ghcr.io/"
    if not repository.startswith(prefix) or repository == prefix:
        raise ValueError(f"repository must start with {prefix}")
    path = repository[len(prefix) :]
    scope = urllib.parse.quote(f"repository:{path}:pull", safe=":")
    try:
        with request(f"https://ghcr.io/token?scope={scope}") as response:
            token = json.load(response).get("token", "")
    except urllib.error.HTTPError as exc:
        if allow_missing_package and exc.code in {403, 404}:
            return missing_manifest(
                repository,
                ref,
                reason="package-not-yet-publicly-addressable",
                public_manifest=False,
            )
        raise RuntimeError(f"GHCR token request failed for {repository}: HTTP {exc.code}") from exc
    if not token:
        raise RuntimeError(f"GHCR did not issue an anonymous pull token for {path}")

    url = f"https://ghcr.io/v2/{path}/manifests/{urllib.parse.quote(ref, safe=':@') }"
    try:
        with request(
            url,
            method="HEAD",
            headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        ) as response:
            digest = response.headers.get("Docker-Content-Digest", "")
            if not DIGEST_RE.fullmatch(digest):
                raise RuntimeError(f"GHCR returned an invalid manifest digest for {repository}:{ref}")
            return {
                "schema": 1,
                "public_manifest": True,
                "exists": True,
                "repository": repository,
                "ref": ref,
                "digest": digest,
                "media_type": response.headers.get("Content-Type", ""),
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return missing_manifest(
                repository,
                ref,
                reason="manifest-not-found",
                public_manifest=True,
            )
        raise RuntimeError(f"GHCR manifest request failed for {repository}:{ref}: HTTP {exc.code}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a GHCR manifest through anonymous pull auth.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--expected-digest", default="")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--allow-missing-package", action="store_true")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.attempts < 1:
        raise SystemExit("--attempts must be positive")
    if args.allow_missing_package and not args.allow_missing:
        raise SystemExit("--allow-missing-package requires --allow-missing")

    payload = None
    last_error = None
    for attempt in range(1, args.attempts + 1):
        try:
            payload = inspect_manifest(
                args.repository,
                args.ref,
                allow_missing_package=args.allow_missing_package,
            )
            if payload["exists"] or args.allow_missing:
                break
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
        if attempt < args.attempts:
            time.sleep(min(2 ** (attempt - 1), 8))

    if payload is None:
        raise SystemExit(str(last_error or "GHCR manifest inspection failed"))
    if not payload["exists"] and not args.allow_missing:
        raise SystemExit(f"GHCR manifest is missing: {args.repository}:{args.ref}")
    if args.expected_digest and payload.get("digest") != args.expected_digest:
        raise SystemExit(
            f"GHCR manifest digest mismatch for {args.repository}:{args.ref}: "
            f"expected {args.expected_digest}, got {payload.get('digest') or '<missing>'}"
        )

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
