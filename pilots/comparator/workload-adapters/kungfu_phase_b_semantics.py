"""Deterministic job verdicts for the packaged Kungfu Phase B adapter."""

from __future__ import annotations

import re
from typing import Any

SEMANTICS_MODULE_ID = "kungfu-phase-b-semantics-v1"
DECISION_LOGIC_VERSION = "kungfu-phase-b-decisions-v1"
EXECUTION_FIXTURE_SCHEMA = "urn:kungfu-systems:build-images:kungfu-phase-b-execution-input:v1"
VERIFIER_ORACLE_SCHEMA = "urn:kungfu-systems:build-images:kungfu-phase-b-verifier-oracle:v1"
OBSERVED_FACTS_SCHEMA = "urn:kungfu-systems:build-images:kungfu-observed-facts:v1"
JOB_RECEIPT_SCHEMA = "urn:kungfu-systems:build-images:kungfu-job-receipt:v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
JOB_IDS = (
    "J1-multi-session-progress-triage",
    "J2-cross-repo-delivery-trust",
    "J3-interrupted-go-recovery-handoff",
)
JOB_RECEIPTS = {
    "J1-multi-session-progress-triage": "j1-decision-receipt.json",
    "J2-cross-repo-delivery-trust": "j2-delivery-receipt.json",
    "J3-interrupted-go-recovery-handoff": "j3-handoff-receipt.json",
}
FORBIDDEN_ANSWER_KEYS = {
    "answer",
    "answer_key",
    "expected",
    "expected_outcome",
    "oracle",
    "verdict",
}


class SemanticError(ValueError):
    """Observed facts cannot support an authoritative job verdict."""


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticError(f"{context} must be an object")
    return value


def _find_forbidden_answer_key(value: Any, path: str = "execution fixture") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_ANSWER_KEYS:
                return f"{path}.{key}"
            found = _find_forbidden_answer_key(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_forbidden_answer_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_execution_fixture(fixture: dict[str, Any]) -> None:
    forbidden = _find_forbidden_answer_key(fixture)
    if forbidden:
        raise SemanticError(f"execution fixture contains a forbidden answer field: {forbidden}")
    if fixture.get("schema") != EXECUTION_FIXTURE_SCHEMA:
        raise SemanticError("execution fixture schema is unsupported")
    jobs = _require_object(fixture.get("jobs"), "execution fixture jobs")
    if set(jobs) != set(JOB_IDS):
        raise SemanticError("execution fixture job set is incomplete or unexpected")
    for job_id, record in jobs.items():
        record = _require_object(record, f"execution fixture job {job_id}")
        if set(record) != {"fixture_id", "facts"}:
            raise SemanticError(f"execution fixture job fields are invalid: {job_id}")
        if not isinstance(record["fixture_id"], str) or not record["fixture_id"]:
            raise SemanticError(f"execution fixture id is invalid: {job_id}")
        _require_object(record["facts"], f"execution fixture facts {job_id}")


def validate_tier_evidence(tier: str, evidence: dict[str, Any]) -> None:
    operation = evidence.get("operation")
    facts_observed = evidence.get("facts_observed") is True
    if tier == "normal":
        valid = (
            operation == "fact-library-current-query"
            and facts_observed
            and evidence.get("canonical_count") == 1
        )
    elif tier == "concurrent":
        valid = (
            operation == "two-agent-concurrent-material-write"
            and facts_observed
            and evidence.get("writers") == 2
            and evidence.get("canonical_count") == 3
        )
    elif tier == "crash-recovery":
        valid = (
            operation == "sigkill-restart-fact-query"
            and facts_observed
            and evidence.get("crash_exit_code") == 137
        )
    elif tier == "whole-root-restore":
        valid = (
            operation == "whole-volume-recreate-and-library-import"
            and facts_observed
            and evidence.get("imported") is True
            and isinstance(evidence.get("backup_sha256"), str)
            and SHA256.fullmatch(evidence["backup_sha256"]) is not None
        )
    elif tier == "historical-query":
        valid = (
            operation == "fact-history-and-head-query"
            and facts_observed
            and evidence.get("history_count") == 2
            and evidence.get("canonical_count") == 1
        )
    elif tier == "schema-evolution":
        valid = (
            operation == "non-overlapping-v1-v2-schema-evolution"
            and facts_observed
            and evidence.get("versions") == ["v1", "v2"]
            and evidence.get("admitted_count") == 2
        )
    elif tier == "new-agent-takeover":
        valid = (
            operation == "fresh-agent-process-query"
            and facts_observed
            and evidence.get("reader") == "agent-b"
        )
    else:
        raise SemanticError(f"unsupported tier: {tier}")
    if not valid:
        raise SemanticError(f"tier evidence does not prove the required {tier} postcondition")


def evaluate_j1(facts: dict[str, Any]) -> dict[str, Any]:
    sessions = facts.get("sessions")
    if not isinstance(sessions, list):
        raise SemanticError("J1 sessions must be a list")
    findings: list[str] = []
    false_receipt_rejected = False
    for raw_session in sessions:
        session = _require_object(raw_session, "J1 session")
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise SemanticError("J1 session id is invalid")
        if session.get("process") == "alive" and session.get("attachment") == "detached":
            findings.append(f"reattach {session_id}")
        if session.get("claimed_state") == "done" and session.get("required_validation") != "passed":
            findings.append(f"reject {session_id} as false receipt")
            false_receipt_rejected = True
        stale_minutes = session.get("minutes_since_useful_evidence", 0)
        stale_calls = session.get("tool_calls_since_useful_evidence", 0)
        if (
            session.get("process") == "alive"
            and session.get("claimed_state") == "active"
            and (
                isinstance(stale_minutes, int) and stale_minutes >= 60
                or isinstance(stale_calls, int) and stale_calls >= 40
                or bool(session.get("blocker"))
            )
        ):
            findings.append(f"pause or inspect {session_id} before further spend")
    return {
        "state": "mixed-action-required" if findings else "healthy",
        "required_findings": findings,
        "false_receipt_rejected": false_receipt_rejected,
    }


def evaluate_j2(facts: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    source_sha = facts.get("source_sha")
    runtime = _require_object(facts.get("runtime"), "J2 runtime identity")
    package = _require_object(facts.get("package"), "J2 package identity")
    pull_request = _require_object(facts.get("pull_request"), "J2 pull request")
    receipt = _require_object(facts.get("downstream_runtime_smoke_receipt"), "J2 runtime smoke receipt")
    if pull_request.get("state") != "merged" or pull_request.get("checks") != "green":
        blockers.append("pull request is not merged with green checks")
    if facts.get("static_contract") != "valid":
        blockers.append("static contract is invalid")
    if runtime.get("source_sha") != source_sha:
        blockers.append("runtime identity is not bound to the source")
    if package.get("source_sha") != source_sha or package.get("runtime_sha") != runtime.get("sha"):
        blockers.append("package identity is not bound to the source and runtime")
    if receipt.get("status") != "passed":
        blockers.append("missing downstream runtime smoke receipt bound to source, runtime, and package identity")
    elif (
        receipt.get("source_sha") != source_sha
        or receipt.get("runtime_sha") != runtime.get("sha")
        or receipt.get("package_sha256") != package.get("sha256")
    ):
        blockers.append("downstream runtime smoke receipt identity does not match the delivered artifacts")
    return {
        "state": "completed-trusted" if not blockers else "waiting",
        "completion": not blockers,
        "only_blocker": blockers[0] if len(blockers) == 1 else None,
        "blockers": blockers,
    }


def evaluate_j3(facts: dict[str, Any]) -> dict[str, Any]:
    marker = _require_object(facts.get("latest_marker"), "J3 latest marker")
    worktree = _require_object(facts.get("source_worktree"), "J3 source worktree")
    branch = _require_object(facts.get("source_branch"), "J3 source branch")
    risk = facts.get("remaining_risk")
    no_risk = risk in (None, "", "none")
    validation = facts.get("latest_validation")
    validation_passed = isinstance(validation, str) and "passed" in validation.lower()
    completed = (
        facts.get("goal_status") in ("completed", "merged")
        and marker.get("status") == "merged"
        and branch.get("contained_in_main") is True
        and branch.get("exists") is False
        and worktree.get("path_state") == "removed"
        and worktree.get("dirty_state") == "clean"
        and validation_passed
        and no_risk
    )
    actions: list[str] = []
    if not completed:
        if branch.get("exists") is True and branch.get("contained_in_main") is not True:
            actions.append("preserve the live source branch")
        if worktree.get("path_state") == "stale" or worktree.get("dirty_state") == "unknown":
            actions.append("recreate or relocate only after policy check")
        if not no_risk:
            actions.append("continue from the recorded remaining risk")
        actions.append("do not clean or mark merged")
    return {
        "state": "completed" if completed else "resume-from-stage",
        "final_ready": completed,
        "required_actions": actions,
    }


def derive_job_verdict(
    job_id: str,
    facts: dict[str, Any],
    tier: str,
    tier_evidence: dict[str, Any],
) -> dict[str, Any]:
    _require_object(facts, f"{job_id} observed facts")
    _require_object(tier_evidence, f"{job_id}/{tier} tier evidence")
    validate_tier_evidence(tier, tier_evidence)
    if job_id == JOB_IDS[0]:
        return evaluate_j1(facts)
    if job_id == JOB_IDS[1]:
        return evaluate_j2(facts)
    if job_id == JOB_IDS[2]:
        return evaluate_j3(facts)
    raise SemanticError(f"unsupported job: {job_id}")
