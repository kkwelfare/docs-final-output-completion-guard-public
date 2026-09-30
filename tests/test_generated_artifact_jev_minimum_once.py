"""Minimum-once provider seam, failure and identity regressions; no live calls."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import generated_artifact_jev as route, jev_post_render as jev


def pdf_fixture(tmp_path):
    artifact = tmp_path / "all-pages.pdf"
    with fitz.open() as doc:
        for index in range(3):
            doc.new_page(width=300, height=180).insert_text((35, 70), f"Private body {index}", fontsize=14)
        doc.save(artifact)
    scan = {"schema_version": "docs-layout-typography-scan-3",
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "measurement_basis": "pdf_points", "status": "pass", "hard_issue_count": 0,
            "review_candidate_count": 0, "issues": [], "REVIEW_CANDIDATE": [], "HARD_FAIL": []}
    inputs = dict(artifact=artifact, scan=scan, evidence_dir=tmp_path / "evidence",
                  artifact_set_id="a" * 64, producer={"task_id": "t_producer", "run_id": 1},
                  reviewer={"task_id": "t_owner", "run_id": 2})
    return artifact, scan, inputs


@pytest.mark.parametrize("case,expected,reason", [
    ("pass", "accepted", "ok"),
    ("low", "uncertain", "ok"),
    ("fail", "repair_required", "ok"),
    ("uncertain", "uncertain", "ok"),
    ("malformed", "unresolved", "malformed_response"),
    ("bad_probabilities", "unresolved", "malformed_response"),
    ("exception", "unresolved", "provider_error"),
    ("timeout", "unresolved", "timeout"),
])
def test_zero_candidate_transport_outcomes_and_no_retry(tmp_path, monkeypatch, case, expected, reason):
    artifact, scan, inputs = pdf_fixture(tmp_path)
    calls = []

    def transport(endpoint, key_env, model, payload, timeout):
        calls.append(payload)
        assert payload["questions"] and set(payload["questions"]) == {"artifact_q0"}
        wire = json.loads(payload["state"])
        assert wire["candidates"] == [] and wire["review_scope"] == "whole_artifact"
        assert wire["whole_artifact"]["metrics"]["page_count"] == 3
        assert wire["whole_artifact"]["metrics"]["line_count_total"] == 3
        assert "Private body" not in payload["state"]
        if case == "exception":
            raise RuntimeError("mock provider failure")
        if case == "timeout":
            raise TimeoutError("mock timeout")
        if case == "malformed":
            return {"answers": {}}
        answer = {"choice": "fail" if case == "fail" else "uncertain" if case == "uncertain" else "pass",
                  "confidence": 0.1 if case == "low" else 0.95}
        if case == "bad_probabilities":
            answer["probabilities"] = {"pass": True}
        return {"answers": {"artifact_q0": answer}}

    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    first = route.run(**inputs)
    assert first["candidate_count"] == 0
    assert first["evaluation_state"] == "executed"
    assert first["provider_invoked"] is True
    assert first["provider_status"] == expected and first["reason_code"] == reason
    assert route.validate_readback(scan, first) is None
    stored_receipt = Path(first["receipt"]["path"]).read_bytes()
    second = route.run(**inputs)
    assert second["evaluation_state"] == "evidence_reused"
    assert len(calls) == 1 and Path(second["receipt"]["path"]).read_bytes() == stored_receipt
    assert route.validate_readback(scan, second) is None
    if expected != "accepted":
        assert first["provider_status"] != "accepted"
    if expected == "unresolved":
        assert first["formal_provider_evidence"] is False
    if case == "pass":
        config = {"enabled": True, "request": first["request"], "receipt": first["receipt"]}
        consumed, error = jev.consume(config, artifact=artifact, artifact_set_id=inputs["artifact_set_id"],
                                      producer=inputs["producer"])
        assert error is None and consumed is not None
        inputs["reviewer"] = {"task_id": "t_second_owner", "run_id": 3}
        third = route.run(**inputs)
        assert third["evaluation_state"] == "executed" and len(calls) == 2


def test_zero_candidates_missing_identity_never_means_reviewed(tmp_path):
    _, scan, inputs = pdf_fixture(tmp_path)
    inputs["reviewer"] = None
    result = route.run(**inputs)
    assert result["status"] == "connection_failed"
    assert result["evaluation_state"] == "error"
    assert result["reason_code"] == "evaluation_identity_unavailable"
    assert result["formal_provider_evidence"] is False
    assert route.validate_readback(scan, result) is None


def test_legacy_no_candidates_is_compatibility_not_provider_evidence(tmp_path):
    _, scan, _ = pdf_fixture(tmp_path)
    legacy = {"status": "scanned_no_candidates", "candidate_count": 0, "formal_provider_evidence": False}
    assert route.validate_readback(scan, legacy) is None
    assert legacy.get("provider_invoked") is not True


def test_fixture_is_not_formal_provider_evidence(tmp_path):
    _, scan, inputs = pdf_fixture(tmp_path)
    def fixture(wire, timeout):
        return {"schema_version": "docs-jev-post-render-response-1", "request_sha256": wire["request_sha256"],
                "reviews": [], "whole_artifact_review": {
                    "disposition": "accepted", "confidence": 0.95,
                    "criteria_results": [{"criterion": "whole_artifact_structure", "status": "pass"}]}}
    result = route.run(**inputs, fixture_adapter=fixture)
    assert result["status"] == "evaluated" and result["execution_mode"] == "fixture"
    assert result["formal_provider_evidence"] is False
    assert route.validate_readback(scan, result) is None


def test_hard_failure_remains_independent(tmp_path):
    _, scan, inputs = pdf_fixture(tmp_path)
    scan.update(hard_issue_count=1, status="block")
    result = route.run(**inputs)
    assert result["status"] == "not_applicable"
    assert result["reason_code"] == "deterministic_hard_fail"
    assert result["formal_provider_evidence"] is False


def test_candidate_low_confidence_is_uncertain(monkeypatch):
    wire = {"candidates": [{"id": "candidate1", "criteria": ["geometry_consistency"]}],
            "request_sha256": "a" * 64}
    def transport(endpoint, key_env, model, payload, timeout):
        return {"answers": {"c0_q0": {"choice": "pass", "confidence": 0.1}}}
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    result = jev.text_provider(wire, 1)
    assert result["reviews"][0]["disposition"] == "uncertain"
    assert result["reviews"][0]["criteria_results"][0]["status"] == "uncertain"
