"""Deterministic verdicts for the Aeron containerized Phase A adapter."""

from __future__ import annotations

import re
from typing import Any

SEMANTICS_MODULE_ID = "aeron-phase-a-semantics-v1"
DECISION_LOGIC_VERSION = "aeron-phase-a-decisions-v1"
EXECUTION_FIXTURE_SCHEMA = "urn:kungfu-systems:build-images:aeron-phase-a-execution-input:v1"
VERIFIER_ORACLE_SCHEMA = "urn:kungfu-systems:build-images:aeron-phase-a-verifier-oracle:v1"
OBSERVED_FACTS_SCHEMA = "urn:kungfu-systems:build-images:aeron-observed-facts:v1"
JOB_RECEIPT_SCHEMA = "urn:kungfu-systems:build-images:aeron-job-receipt:v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
JOB_IDS = (
    "J1-multi-session-progress-triage",
    "J2-cross-repo-delivery-trust",
    "J3-interrupted-go-recovery-handoff",
)
JOB_RECEIPTS = {
    JOB_IDS[0]: "j1-decision-receipt.json",
    JOB_IDS[1]: "j2-delivery-receipt.json",
    JOB_IDS[2]: "j3-handoff-receipt.json",
}
TIERS = {
    "normal",
    "concurrent",
    "crash-recovery",
    "whole-root-restore",
    "historical-query",
    "schema-evolution",
    "new-agent-takeover",
}
FORBIDDEN_ANSWER_KEYS = {"answer", "answer_key", "expected", "oracle", "verdict"}


class SemanticError(ValueError):
    """Observed evidence cannot support the requested verdict."""


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticError(f"{context} must be an object")
    return value


def _forbidden(value: Any, path: str = "execution fixture") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_ANSWER_KEYS:
                return f"{path}.{key}"
            found = _forbidden(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _forbidden(child, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_execution_fixture(fixture: dict[str, Any]) -> None:
    forbidden = _forbidden(fixture)
    if forbidden:
        raise SemanticError(f"execution fixture contains a forbidden answer field: {forbidden}")
    if fixture.get("schema") != EXECUTION_FIXTURE_SCHEMA:
        raise SemanticError("execution fixture schema is unsupported")
    jobs = _object(fixture.get("jobs"), "execution fixture jobs")
    if set(jobs) != set(JOB_IDS):
        raise SemanticError("execution fixture job set is incomplete or unexpected")
    for job_id, raw in jobs.items():
        record = _object(raw, f"execution fixture job {job_id}")
        if set(record) != {"fixture_id", "facts"}:
            raise SemanticError(f"execution fixture job fields are invalid: {job_id}")
        facts = _object(record["facts"], f"execution fixture facts {job_id}")
        if not isinstance(record["fixture_id"], str) or not record["fixture_id"]:
            raise SemanticError(f"execution fixture id is invalid: {job_id}")
        if facts.get("expected_count") != 5 or facts.get("marker_policy") != "binding-sha256":
            raise SemanticError(f"execution fixture facts are not frozen: {job_id}")


def validate_tier_evidence(tier: str, evidence: dict[str, Any]) -> None:
    if tier not in TIERS:
        raise SemanticError(f"unsupported tier: {tier}")
    records = evidence.get("records")
    replays = evidence.get("replays")
    if (
        not isinstance(records, list)
        or not isinstance(replays, list)
        or not records
        or len(records) != len(replays)
        or evidence.get("live_health") is not True
        or evidence.get("marker_bound") is not True
    ):
        raise SemanticError(f"tier evidence does not close the Aeron lifecycle: {tier}")
    for record, replay in zip(records, replays):
        if (
            not isinstance(record, dict)
            or not isinstance(replay, dict)
            or record.get("observed") != 5
            or replay.get("observed") != 5
            or any(record.get(field) != 0 for field in ("duplicates", "reordered", "marker_mismatches"))
            or any(replay.get(field) != 0 for field in ("duplicates", "reordered", "marker_mismatches"))
            or record.get("marker") != evidence.get("binding_sha256")
            or replay.get("marker") != evidence.get("binding_sha256")
        ):
            raise SemanticError(f"tier evidence contains a failed record/replay oracle: {tier}")
    if tier == "concurrent" and evidence.get("clients") != 2:
        raise SemanticError("concurrent tier must prove two isolated clients")
    if tier == "crash-recovery" and (
        evidence.get("stale_health_rejected") is not True
        or evidence.get("expiry_wait_seconds") != 11
    ):
        raise SemanticError("crash recovery does not prove stale-health rejection")
    if tier == "whole-root-restore" and (
        evidence.get("root_restored") is not True
        or not SHA256.fullmatch(str(evidence.get("backup_sha256", "")))
    ):
        raise SemanticError("whole-root restore evidence is incomplete")
    if tier == "schema-evolution" and evidence.get("envelope_versions") != [1, 2]:
        raise SemanticError("schema evolution did not preserve both envelope versions")
    if tier == "new-agent-takeover" and evidence.get("fresh_client") is not True:
        raise SemanticError("new-agent takeover lacks a fresh replay client")


def derive_job_verdict(
    job_id: str,
    facts: dict[str, Any],
    tier: str,
    tier_evidence: dict[str, Any],
) -> dict[str, Any]:
    _object(facts, f"{job_id} observed facts")
    _object(tier_evidence, f"{job_id}/{tier} tier evidence")
    validate_tier_evidence(tier, tier_evidence)
    if facts.get("expected_count") != 5 or facts.get("marker_policy") != "binding-sha256":
        raise SemanticError("observed facts do not match the frozen execution input")
    if job_id == JOB_IDS[0]:
        return {"state": "ordered-complete", "observed_count": 5, "anomalies": 0}
    if job_id == JOB_IDS[1]:
        return {"state": "digest-bound", "trusted": True, "marker_bound": True}
    if job_id == JOB_IDS[2]:
        return {"state": "replay-ready", "resumable": True, "cleanup_required": False}
    raise SemanticError(f"unsupported job: {job_id}")
