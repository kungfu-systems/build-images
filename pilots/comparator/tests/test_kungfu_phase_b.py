from __future__ import annotations

import copy
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest

PILOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_DIR = PILOT_DIR / "scripts"
ADAPTER_DIR = PILOT_DIR / "workload-adapters"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ADAPTER_DIR))

import comparator_qualification as qualification  # noqa: E402
import kungfu_phase_b_v1 as adapter  # noqa: E402
import kungfu_phase_b_semantics as semantics  # noqa: E402
import materialize_kungfu_phase_b as materialize  # noqa: E402


class KungfuPackageMaterializationTests(unittest.TestCase):
    source_sha = "a" * 40
    version = "4.0.0-alpha.0"

    def package(
        self,
        root: pathlib.Path,
        *,
        source_sha: str | None = None,
        unsafe_link: bool = False,
    ) -> pathlib.Path:
        path = root / materialize.PACKAGE_NAME
        prefix = "kungfu-episodes-cli-linux-x64/"
        product = {
            "schema": "kungfu.product.cli/v1",
            "product": "cli",
            "platform": "linux-x64",
            "archive": materialize.PACKAGE_NAME,
            "entries": {
                "kungfu": "kungfu",
                "compatibility": "runtime/product-compatibility.json",
                "upgradeManifest": "upgrade/kungfu-release-manifest.json",
            },
        }
        compatibility = {
            "schema": "kungfu.product.compatibility/v1",
            "source_commit": source_sha or self.source_sha,
            "versions": {"product": self.version},
        }
        upgrade = {
            "schema": "kungfu.product-upgrade.manifest/v1",
            "productVersion": self.version,
            "sourceCommit": source_sha or self.source_sha,
            "platform": "linux",
            "architecture": "x64",
        }
        with tarfile.open(path, "w:gz") as archive:
            for relative, value in (
                ("product.json", product),
                ("runtime/product-compatibility.json", compatibility),
                ("upgrade/kungfu-release-manifest.json", upgrade),
            ):
                encoded = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
                info = tarfile.TarInfo(prefix + relative)
                info.size = len(encoded)
                archive.addfile(info, io.BytesIO(encoded))
            if unsafe_link:
                link = tarfile.TarInfo(prefix + "unsafe-link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside-package"
                archive.addfile(link)
        return path

    def test_exact_package_materializes_complete_three_by_twenty_one_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(pathlib.Path(temporary))
            digest = materialize.sha256_file(package)
            identity = materialize.verify_package(
                package, digest, self.version, self.source_sha
            )
            plan = materialize.build_plan(
                materialize.load_json(materialize.TEMPLATE_PATH),
                identity,
                "https://github.com/kungfu-systems/kungfu/actions/runs/1",
            )
        qualification.validate_plan_document(plan)
        self.assertEqual(len(plan["scenarios"]), 21)
        self.assertEqual(plan["repetitions"] * len(plan["scenarios"]), 63)
        self.assertEqual(plan["subject"]["package_sha256"], digest)
        self.assertEqual(plan["subject"]["package_size_bytes"], identity["package_size_bytes"])
        self.assertIn(
            "raw/**/cleanup.resources.json",
            plan["artifact_retention"]["required_patterns"],
        )
        self.assertTrue(
            all(
                not pattern.startswith("repetition-")
                for pattern in plan["artifact_retention"]["required_patterns"]
            )
        )

    def test_package_sha_source_and_https_evidence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(pathlib.Path(temporary))
            digest = materialize.sha256_file(package)
            with self.assertRaisesRegex(materialize.MaterializationError, "SHA-256"):
                materialize.verify_package(package, "0" * 64, self.version, self.source_sha)
            with self.assertRaisesRegex(materialize.MaterializationError, "source/version"):
                materialize.verify_package(package, digest, self.version, "b" * 40)
            identity = materialize.verify_package(package, digest, self.version, self.source_sha)
            with self.assertRaisesRegex(materialize.MaterializationError, "HTTPS"):
                materialize.build_plan(
                    materialize.load_json(materialize.TEMPLATE_PATH),
                    identity,
                    "agent-120:/tmp/package.log",
                )

    def test_archive_links_are_rejected_before_docker_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(pathlib.Path(temporary), unsafe_link=True)
            with self.assertRaisesRegex(materialize.MaterializationError, "unsupported archive member"):
                materialize.verify_package(
                    package,
                    materialize.sha256_file(package),
                    self.version,
                    self.source_sha,
                )

    def test_incomplete_or_relabelled_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(pathlib.Path(temporary))
            identity = materialize.verify_package(
                package,
                materialize.sha256_file(package),
                self.version,
                self.source_sha,
            )
            plan = materialize.build_plan(
                materialize.load_json(materialize.TEMPLATE_PATH),
                identity,
                "https://github.com/kungfu-systems/kungfu/actions/runs/1",
            )
        incomplete = copy.deepcopy(plan)
        incomplete["scenarios"].pop()
        with self.assertRaisesRegex(qualification.QualificationError, "complete adapter"):
            qualification.validate_plan_document(incomplete)
        relabelled = copy.deepcopy(plan)
        relabelled["scenarios"][0]["tier"] = "concurrent"
        with self.assertRaisesRegex(qualification.QualificationError, "duplicate production"):
            qualification.validate_plan_document(relabelled)
        repetitions = copy.deepcopy(plan)
        repetitions["repetitions"] = 4
        with self.assertRaisesRegex(qualification.QualificationError, "exactly three"):
            qualification.validate_plan_document(repetitions)


class KungfuSemanticTests(unittest.TestCase):
    fixture = qualification.load_json(
        ADAPTER_DIR / "fixtures" / "kungfu-phase-b-v1.json"
    )
    oracle = qualification.load_json(
        ADAPTER_DIR / "oracles" / "kungfu-phase-b-v1.json"
    )

    def evidence(self, tier: str) -> dict[str, object]:
        values: dict[str, dict[str, object]] = {
            "normal": {
                "operation": "fact-library-current-query",
                "facts_observed": True,
                "canonical_count": 1,
            },
            "concurrent": {
                "operation": "two-agent-concurrent-material-write",
                "facts_observed": True,
                "writers": 2,
                "canonical_count": 3,
            },
            "crash-recovery": {
                "operation": "sigkill-restart-fact-query",
                "facts_observed": True,
                "crash_exit_code": 137,
            },
            "whole-root-restore": {
                "operation": "whole-volume-recreate-and-library-import",
                "facts_observed": True,
                "imported": True,
                "backup_sha256": "1" * 64,
            },
            "historical-query": {
                "operation": "fact-history-and-head-query",
                "facts_observed": True,
                "history_count": 2,
                "canonical_count": 1,
            },
            "schema-evolution": {
                "operation": "non-overlapping-v1-v2-schema-evolution",
                "facts_observed": True,
                "versions": ["v1", "v2"],
                "admitted_count": 2,
            },
            "new-agent-takeover": {
                "operation": "fresh-agent-process-query",
                "facts_observed": True,
                "reader": "agent-b",
            },
        }
        return values[tier]

    def test_fixture_is_answer_free_and_every_tier_matches_independent_oracle(self) -> None:
        semantics.validate_execution_fixture(self.fixture)
        self.assertNotIn("expected", json.dumps(self.fixture).lower())
        for job_id, job in self.fixture["jobs"].items():
            for tier in materialize.TIERS:
                self.assertEqual(
                    semantics.derive_job_verdict(job_id, job["facts"], tier, self.evidence(tier)),
                    self.oracle["jobs"][job_id],
                )

    def test_ambiguous_schema_evolution_evidence_is_rejected(self) -> None:
        evidence = self.evidence("schema-evolution")
        evidence["versions"] = ["v1", "v1", "v2"]
        with self.assertRaisesRegex(semantics.SemanticError, "schema-evolution"):
            semantics.derive_job_verdict(
                "J1-multi-session-progress-triage",
                self.fixture["jobs"]["J1-multi-session-progress-triage"]["facts"],
                "schema-evolution",
                evidence,
            )

    def test_adapter_returns_facts_read_back_from_kungfu(self) -> None:
        observed = {"status": "observed-from-product"}

        class Project:
            def kungfu(self, *arguments: str, **_kwargs: object) -> dict[str, object]:
                if arguments[:3] == ("facts", "type", "create"):
                    return {"ok": True, "status": "created"}
                if arguments[:3] == ("facts", "material", "put"):
                    return {"ok": True, "receipt": {"admission": {"outcome": "admitted"}}}
                if arguments[:3] == ("facts", "material", "list"):
                    return {
                        "schema": "kungfu.facts.material-catalog/v1",
                        "state": {
                            "canonical_facts": [
                                {"subject_key": "J1-multi-session-progress-triage", "payload_hash": "p1"}
                            ]
                        },
                        "payloads": {"p1": {"facts": observed}},
                    }
                raise AssertionError(arguments)

        facts, evidence = adapter.exercise_tier(
            Project(),  # type: ignore[arg-type]
            "J1-multi-session-progress-triage",
            "normal",
            {"status": "fixture-input"},
            "a" * 64,
        )
        self.assertEqual(facts, observed)
        self.assertFalse(evidence["facts_observed"])


if __name__ == "__main__":
    unittest.main()
