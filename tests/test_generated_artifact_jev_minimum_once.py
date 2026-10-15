"""Minimum-once provider seam, failure and identity regressions; no live calls."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import generated_artifact_jev as route, jev_post_render as jev


def pdf_fixture(tmp_path):
    artifact = tmp_path / "all-pages.pdf"
    with pymupdf.open() as doc:
        for index in range(3):
            doc.new_page(width=300, height=180).insert_text((35, 70), f"Private body {index}", fontsize=14)
        doc.save(artifact)
    scan = {"schema_version": "docs-layout-typography-scan-3",
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "measurement_basis": "pdf_points", "status": "pass", "hard_issue_count": 0,
            "review_candidate_count": 0, "issues": [], "REVIEW_CANDIDATE": [], "HARD_FAIL": []}
    inputs = dict(artifact=artifact, scan=scan, evidence_dir=tmp_path / "evidence",
                  artifact_set_id="a" * 64, producer={"task_id": "t_producer", "run_id": 1},
                  reviewer={"task_id": "t_owner", "run_id": 2}, allow_live_provider=True)
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
    inputs["producer"] = None  # No usable identity at all: stop before any Jev call.
    inputs["reviewer"] = None
    result = route.run(**inputs)
    assert result["status"] == "not_invoked"
    assert result["status"] != "connection_failed"
    assert result["evaluation_state"] == "error"
    assert result["reason_code"] == "evaluation_identity_unavailable"
    assert result["formal_provider_evidence"] is False
    assert route.validate_readback(scan, result) is None


def test_live_provider_permission_is_explicit_and_defaults_false(tmp_path, monkeypatch):
    artifact, scan, inputs = pdf_fixture(tmp_path)
    inputs.pop("allow_live_provider")
    calls = []

    def transport(endpoint, key_env, model, payload, timeout):
        calls.append(payload)
        return {"answers": {"artifact_q0": {"choice": "pass", "confidence": 0.95}}}

    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    without_flag = route.run(**inputs)
    missing_identity = route.run(**{**inputs, "evidence_dir": tmp_path / "missing-evidence"},
                                  allow_live_provider=False)
    explicit_false = route.run(**{**inputs, "evidence_dir": tmp_path / "false-evidence"},
                               allow_live_provider=False)
    explicit_true = route.run(**{**inputs, "evidence_dir": tmp_path / "true-evidence"},
                              allow_live_provider=True)
    assert without_flag["provider_invoked"] is False
    assert missing_identity["provider_invoked"] is False
    assert explicit_false["provider_invoked"] is False
    assert explicit_true["provider_invoked"] is True
    assert [len(calls)] == [1]


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


def _fixture_accept(wire, timeout):
    return {"schema_version": "docs-jev-post-render-response-1", "request_sha256": wire["request_sha256"],
            "reviews": [], "whole_artifact_review": {
                "disposition": "accepted", "confidence": 0.95,
                "criteria_results": [{"criterion": "whole_artifact_structure", "status": "pass"}]}}


def test_same_task_producer_and_reviewer_is_not_stopped_for_identity(tmp_path):
    _, scan, inputs = pdf_fixture(tmp_path)
    inputs["producer"] = {"task_id": "t_same", "run_id": 1}
    inputs["reviewer"] = {"task_id": "t_same", "run_id": 2}
    result = route.run(**inputs, fixture_adapter=_fixture_accept)
    assert result["status"] == "evaluated" and result["evaluation_state"] == "executed"
    assert result["execution_mode"] == "fixture" and result["provider_invoked"] is True
    assert result["formal_provider_evidence"] is False  # fixture is never formal evidence
    stored = jev.read_json(Path(result["request"]["path"]))
    assert stored["reviewer"]["task_id"] == stored["binding"]["producer"]["task_id"] == "t_same"
    jev.validate_request(stored)  # the request validator no longer raises identity_drift for this
    assert route.validate_readback(scan, result) is None


def test_missing_reviewer_falls_back_to_producer_identity(tmp_path):
    _, scan, inputs = pdf_fixture(tmp_path)
    inputs["reviewer"] = None
    result = route.run(**inputs, fixture_adapter=_fixture_accept)
    assert result["status"] == "evaluated"
    stored = jev.read_json(Path(result["request"]["path"]))
    assert stored["reviewer"] == stored["binding"]["producer"] == inputs["producer"]
    assert route.validate_readback(scan, result) is None


def test_stops_before_jev_call_are_not_connection_failed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", lambda *a, **k: calls.append(a))
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    # (a) review candidates exist but none is a supported candidate type
    _, scan, inputs = pdf_fixture(tmp_path / "a")
    scan.update(status="review", review_candidate_count=1, issues=[
        {"id": "x1", "rule": "unsupported_candidate", "severity": "review", "page": 1}])
    unsupported = route.run(**inputs)
    assert unsupported["status"] == "not_invoked" and unsupported["reason_code"] == "no_supported_candidate_type"
    assert route.validate_readback(scan, unsupported) is None
    # (b) usable identity unavailable (invalid artifact set id)
    _, scan_b, inputs_b = pdf_fixture(tmp_path / "b")
    inputs_b["artifact_set_id"] = "not-a-sha256"
    no_identity = route.run(**inputs_b)
    assert no_identity["status"] == "not_invoked" and no_identity["reason_code"] == "evaluation_identity_unavailable"
    assert route.validate_readback(scan_b, no_identity) is None
    # (c) identity is fine but live provider was not authorised: provider never invoked
    _, scan_c, inputs_c = pdf_fixture(tmp_path / "c")
    inputs_c["allow_live_provider"] = False
    not_authorised = route.run(**inputs_c)
    assert not_authorised["provider_invoked"] is False and not_authorised["status"] == "not_invoked"
    assert route.validate_readback(scan_c, not_authorised) is None
    for result in (unsupported, no_identity, not_authorised):
        assert result["status"] != "connection_failed" and result["formal_provider_evidence"] is False
    assert calls == []  # no provider/transport call on any of these branches
    # a not_invoked record can never claim that the provider was invoked
    forged = {**not_authorised, "provider_invoked": True}
    assert "not_invoked" in route.validate_readback(scan_c, forged)


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_real_provider_failure_stays_connection_failed(tmp_path, monkeypatch, failure):
    _, scan, inputs = pdf_fixture(tmp_path)

    def transport(endpoint, key_env, model, payload, timeout):  # mock only: never touches a network
        raise TimeoutError("mock timeout") if failure == "timeout" else RuntimeError("mock provider failure")

    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    result = route.run(**inputs)
    assert result["provider_invoked"] is True
    assert result["status"] == "connection_failed"
    assert result["reason_code"] == ("timeout" if failure == "timeout" else "provider_error")
    assert result["formal_provider_evidence"] is False
    assert route.validate_readback(scan, result) is None


def test_exception_before_jev_call_is_not_invoked_with_its_own_reason(tmp_path, monkeypatch):
    """jev_glyph_locale.measure raising ValueError('insufficient_evidence') happens while the request is
    built, before any Jev/provider call: it is recorded as not_invoked (provider_invoked=False)."""
    _, scan, inputs = pdf_fixture(tmp_path)
    candidate = {"id": "g1", "rule": jev.jev_glyph_locale.RULE, "severity": "review", "page": 1,
                 "bbox_pdf_points": [30, 55, 200, 80],
                 "render_evidence": {"path": str(tmp_path / "render.png"), "sha256": "0" * 64}}
    scan.update(status="review", review_candidate_count=1, issues=[candidate], REVIEW_CANDIDATE=[candidate])
    calls = []
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", lambda *a, **k: calls.append(a))
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")

    def insufficient(*args, **kwargs):
        raise ValueError("insufficient_evidence")

    monkeypatch.setattr(jev.jev_glyph_locale, "measure", insufficient)
    result = route.run(**inputs)
    assert result["status"] == "not_invoked" and result["status"] != "connection_failed"
    assert result["provider_invoked"] is False
    assert result["reason_code"] == "insufficient_evidence"
    assert result["evaluation_state"] == "error" and result["formal_provider_evidence"] is False
    assert result["candidate_count"] == 1
    assert calls == []  # the provider transport was never reached
    assert route.validate_readback(scan, result) is None
    forged = {**result, "provider_invoked": True}  # a not_invoked record can never claim an invocation
    assert "not_invoked" in route.validate_readback(scan, forged)


def test_exception_after_jev_call_stays_connection_failed(tmp_path, monkeypatch):
    """A failure raised after the provider was called (receipt validation here) stays connection_failed."""
    _, scan, inputs = pdf_fixture(tmp_path)
    calls = []

    def transport(endpoint, key_env, model, payload, timeout):  # mock only: never touches a network
        calls.append(payload)
        return {"answers": {"artifact_q0": {"choice": "pass", "confidence": 0.95}}}

    real_validate = jev.validate_schema

    def fail_on_receipt(value, kind):
        if kind == "receipt":
            raise jev.PostRenderError("malformed_response")
        return real_validate(value, kind)

    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    monkeypatch.setattr(jev, "validate_schema", fail_on_receipt)
    result = route.run(**inputs)
    monkeypatch.setattr(jev, "validate_schema", real_validate)
    assert len(calls) == 1  # the provider was reached before the failure
    assert result["status"] == "connection_failed" and result["status"] != "not_invoked"
    assert result.get("provider_invoked") is not False
    assert result["reason_code"] == "malformed_response"
    assert result["evaluation_state"] == "error" and result["formal_provider_evidence"] is False
    assert route.validate_readback(scan, result) is None
