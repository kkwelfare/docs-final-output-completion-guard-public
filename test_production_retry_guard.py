"""Focused tests for docs artifact-ready production retry control."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("production_retry_guard", ROOT / "production_retry_guard.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def _artifact(tmp_path: Path, name: str = "primary.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"primary artifact fixture")
    return path


def _state(tmp_path: Path, *, status: str = "artifact_ready", baseline: str = "pass", delivery: str = "attachment", retry_count: int = 0, allow_new: bool = False, artifact: Path | None = None) -> dict:
    artifact = artifact or _artifact(tmp_path)
    return {
        "schema_version": MODULE.SCHEMA_VERSION,
        "policy_version": MODULE.POLICY_VERSION,
        "task_id": "t_fixture",
        "status": status,
        "baseline_status": baseline,
        "primary_delivery": delivery,
        "effective_max_retries": 2,
        "retry_limit_source": "task.max_retries",
        "retry_count": retry_count,
        "production_fingerprint": "a" * 64,
        "allow_new_fingerprint": allow_new,
        "primary_artifacts": [{"path": str(artifact.resolve()), "sha256": MODULE._sha256(artifact)}],
    }


def test_same_fingerprint_after_artifact_ready_is_blocked(tmp_path):
    state = _state(tmp_path)
    args = {"production_fingerprint": "a" * 64}
    result = MODULE.evaluate_production_call("image_generate", args, state, task_id="t_fixture")
    assert result["decision"] == "block"
    assert result["same_condition"] is True


def test_verify_only_and_complete_from_primary_readback_are_allowed(tmp_path):
    state = _state(tmp_path)
    for mode in ("verify-only", "complete-from-primary-readback"):
        result = MODULE.evaluate_production_call("image_generate", {"production_mode": mode}, state, task_id="t_fixture")
        assert result["decision"] == "allow"


def test_transient_retry_remains_available_but_broad_hard_fail_is_blocked(tmp_path):
    transient = _state(tmp_path, status="transient_retry", delivery="none", baseline="unavailable", retry_count=0)
    hard = _state(tmp_path, status="hard_fail", delivery="none", baseline="fail", retry_count=0)
    assert MODULE.evaluate_production_call("image_generate", {}, transient, task_id="t_fixture")["decision"] == "allow"
    result = MODULE.evaluate_production_call("image_generate", {}, hard, task_id="t_fixture")
    assert result["decision"] == "block"
    assert "hash-bound repair plan" in result["reason"]


def test_review_only_never_recreates(tmp_path):
    state = _state(tmp_path, status="review_only", delivery="none", baseline="fail")
    result = MODULE.evaluate_production_call("image_generate", {}, state, task_id="t_fixture")
    assert result["decision"] == "block"
    assert "review-only" in result["reason"]


def test_hash_drift_blocks_state(tmp_path):
    artifact = _artifact(tmp_path)
    state = _state(tmp_path, artifact=artifact)
    artifact.write_bytes(b"changed")
    result = MODULE.evaluate_production_call("image_generate", {}, state, task_id="t_fixture")
    assert result["decision"] == "block"
    assert "hash drift" in result["reason"]
