"""Plan B (split send over MAX_WIRE_BYTES) and Plan C (block completion when Jev did not run).

Offline only: the provider transport is replaced by a mock and API keys are removed or set to a
test-only value that never reaches a network.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import generated_artifact_jev as route, jev_glyph_locale as locale, jev_post_render as jev

BLOCK_PREFIX = "AQ-LAYOUT-01: Jevの評価がないため完了できません（reason_code="


def _guard():
    name = "jev_split_enforce_guard"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _multi_candidate_pdf(tmp_path: Path, pages: int, lines: int = 40, per_line: int = 6):
    """One CJK glyph-locale candidate per page; each candidate's wire is several KB."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact = (tmp_path / "multi.pdf").resolve()
    text = "日本語の文字列です" * 400
    with pymupdf.open() as doc:
        for _ in range(pages):
            page = doc.new_page(width=595, height=842)
            for row in range(lines):
                page.insert_text((40, 40 + row * 12), text[row * per_line:(row + 1) * per_line],
                                 fontname="japan", fontsize=8)
        doc.save(artifact)
    issues = []
    with pymupdf.open(artifact) as doc:
        for index in range(pages):
            render = (tmp_path / f"page-{index + 1}.png").resolve()
            doc[index].get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).save(render)
            issues.append({"id": f"cjk-region-{index + 1}", "rule": locale.RULE, "severity": "review",
                           "page": index + 1, "bbox_pdf_points": [0.0, 0.0, 595.0, 842.0],
                           "render_evidence": {"path": str(render), "sha256": _sha(render)}})
    scan = {"schema_version": "docs-layout-typography-scan-3", "artifact_sha256": _sha(artifact),
            "measurement_basis": "pdf_points", "status": "review", "hard_issue_count": 0,
            "review_candidate_count": len(issues), "issues": issues, "REVIEW_CANDIDATE": issues, "HARD_FAIL": []}
    inputs = dict(artifact=artifact, scan=scan, evidence_dir=tmp_path / "evidence",
                  artifact_set_id="a" * 64, producer={"task_id": "t_producer", "run_id": 1},
                  reviewer={"task_id": "t_producer", "run_id": 1}, allow_live_provider=True)
    return artifact, scan, inputs


@pytest.fixture
def sufficient_glyphs(monkeypatch):
    """The builtin test font is not embedded; mark only the sufficiency flag so the provider path runs."""
    real = locale.measure

    def measure(*args, **kwargs):
        value = real(*args, **kwargs)
        value["evidence_sufficient"] = True
        return value

    monkeypatch.setattr(jev.jev_glyph_locale, "measure", measure)


def _answering_transport(calls, choice="japanese_glyph_likely"):
    def transport(endpoint, key_env, model, payload, timeout):  # mock only: never touches a network
        calls.append(payload)
        assert len(jev.canonical(payload)) <= jev.MAX_WIRE_BYTES
        return {"answers": {key: {"choice": choice, "confidence": 0.95} for key in payload["questions"]}}
    return transport


def _split_request(tmp_path: Path, pages: int = 4) -> dict:
    artifact, scan, _ = _multi_candidate_pdf(tmp_path, pages)
    raw = (tmp_path / "scan.json").resolve()
    raw.write_text(json.dumps(scan), encoding="utf-8")
    return jev.build_request(artifact=artifact, raw_scan=raw, artifact_set_id="a" * 64,
                             producer={"task_id": "t_producer", "run_id": 1},
                             reviewer={"task_id": "t_producer", "run_id": 1}, allow_live_provider=True)


# ---------------------------------------------------------------- Plan B: split send

def test_oversize_request_is_split_into_bound_chunks_within_the_unchanged_cap(tmp_path):
    assert jev.MAX_WIRE_BYTES == 32_000
    request = _split_request(tmp_path)
    with pytest.raises(jev.PostRenderError, match="invalid_request"):
        jev.build_wire(request)  # the single-wire form still refuses an oversize request
    wires, records = jev.build_wire_chunks(request)
    assert len(wires) >= 2
    assert [cid for w in wires for cid in w["chunk"]["candidate_ids"]] == [c["id"] for c in request["candidates"]]
    for position, wire in enumerate(wires):
        assert wire["chunk"]["index"] == position and wire["chunk"]["count"] == len(wires)
        assert wire["request_sha256"] == jev.digest(request)
        assert wire["candidate_set_digest"] == request["binding"]["candidate_set_digest"]
        by_id = {c["id"]: c for c in request["candidates"]}
        assert wire["chunk"]["candidate_set_digest"] == jev.candidate_digest(
            [by_id[i] for i in wire["chunk"]["candidate_ids"]])
        payload, _ = jev.build_provider_payload(wire)
        assert len(jev.canonical(wire)) <= jev.MAX_WIRE_BYTES
        assert len(jev.canonical(payload)) <= jev.MAX_WIRE_BYTES
    assert records == jev.measurement_records(request)
    assert [r["candidate_id"] for r in records] == [c["id"] for c in request["candidates"]]


def test_small_request_is_not_split_and_keeps_its_wire_shape(tmp_path):
    request = _split_request(tmp_path, pages=1)
    wires, _ = jev.build_wire_chunks(request)
    single, _ = jev.build_wire(request)
    assert wires == [single] and "chunk" not in single
    assert jev.expected_wire_chunks(request) is None


def test_single_candidate_over_the_cap_stays_invalid_request(tmp_path, monkeypatch):
    request = _split_request(tmp_path, pages=1)
    monkeypatch.setattr(jev, "MAX_WIRE_BYTES", 2_000)  # simulate one candidate larger than the cap
    with pytest.raises(jev.PostRenderError, match="invalid_request"):
        jev.build_wire_chunks(request)
    receipt = jev.evaluate(request)
    assert receipt["provider_invoked"] is False and receipt["reason_code"] == "invalid_request"
    assert "wire_chunks" not in receipt


def test_split_live_send_calls_every_chunk_once_and_reuses_evidence(tmp_path, monkeypatch, sufficient_glyphs):
    _, scan, inputs = _multi_candidate_pdf(tmp_path, 4)
    calls = []
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", _answering_transport(calls))
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    first = route.run(**inputs)
    chunk_count = first["wire_chunk_count"]
    assert chunk_count >= 2 and len(calls) == chunk_count
    assert first["status"] == "evaluated" and first["provider_invoked"] is True
    assert first["reason_code"] == "ok" and first["provider_status"] == "accepted"
    assert first["formal_provider_evidence"] is True
    receipt = json.loads(Path(first["receipt"]["path"]).read_text())
    assert [c["provider_invoked"] for c in receipt["wire_chunks"]] == [True] * chunk_count
    assert [r["candidate_id"] for r in receipt["reviews"]] == [i["id"] for i in scan["issues"]]
    assert route.validate_readback(scan, first) is None
    assert route.jev_required_block_reason(scan, first) is None
    stored = Path(first["receipt"]["path"]).read_bytes()
    second = route.run(**inputs)
    assert second["evaluation_state"] == "evidence_reused" and len(calls) == chunk_count
    assert Path(second["receipt"]["path"]).read_bytes() == stored
    assert route.validate_readback(scan, second) is None
    assert route.jev_required_block_reason(scan, second) is None
    config = {"enabled": True, "request": first["request"], "receipt": first["receipt"]}
    consumed, error = jev.consume(config, artifact=inputs["artifact"], artifact_set_id=inputs["artifact_set_id"],
                                  producer=inputs["producer"])
    assert error is None and consumed["provider_invoked"] is True


def test_tampered_chunk_record_is_rejected_on_readback_and_not_reused(tmp_path, monkeypatch, sufficient_glyphs):
    _, scan, inputs = _multi_candidate_pdf(tmp_path, 4)
    calls = []
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", _answering_transport(calls))
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    first = route.run(**inputs)
    receipt_path = Path(first["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["wire_chunks"][-1]["provider_invoked"] = False  # claims a chunk was skipped
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tampered = {**first, "receipt": {"path": str(receipt_path), "sha256": _sha(receipt_path)}}
    assert "readback" in route.validate_readback(scan, tampered)
    request = json.loads(Path(first["request"]["path"]).read_text())
    with pytest.raises(jev.PostRenderError, match="identity_drift"):
        jev.validate_receipt_chunks(request, receipt)
    before = len(calls)
    again = route.run(**inputs)  # the tampered receipt is not reused; this run sends again
    assert again["evaluation_state"] == "executed" and len(calls) > before


def test_split_failure_midway_is_connection_failed_and_blocks(tmp_path, monkeypatch, sufficient_glyphs):
    _, scan, inputs = _multi_candidate_pdf(tmp_path, 4)
    calls = []

    def transport(endpoint, key_env, model, payload, timeout):
        calls.append(payload)
        if len(calls) == 2:
            raise RuntimeError("mock provider failure on chunk 2")
        return {"answers": {k: {"choice": "japanese_glyph_likely", "confidence": 0.95} for k in payload["questions"]}}

    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    result = route.run(**inputs)
    assert len(calls) == 2  # stops at the failing chunk; no retry
    assert result["status"] == "connection_failed" and result["reason_code"] == "provider_error"
    receipt = json.loads(Path(result["receipt"]["path"]).read_text())
    assert [c["provider_invoked"] for c in receipt["wire_chunks"]][:2] == [True, True]
    assert route.validate_readback(scan, result) is None
    assert route.jev_required_block_reason(scan, result).startswith(BLOCK_PREFIX + "provider_error")


# ---------------------------------------------------------------- Plan C: block when Jev did not run

def _zero_candidate(tmp_path: Path):
    artifact = (tmp_path / "plain.pdf").resolve()
    with pymupdf.open() as doc:
        doc.new_page(width=300, height=180).insert_text((35, 70), "Body", fontsize=14)
        doc.save(artifact)
    scan = {"schema_version": "docs-layout-typography-scan-3", "artifact_sha256": _sha(artifact),
            "measurement_basis": "pdf_points", "status": "pass", "hard_issue_count": 0,
            "review_candidate_count": 0, "issues": [], "REVIEW_CANDIDATE": [], "HARD_FAIL": []}
    inputs = dict(artifact=artifact, scan=scan, evidence_dir=tmp_path / "evidence",
                  artifact_set_id="a" * 64, producer={"task_id": "t_producer", "run_id": 1},
                  reviewer=None, allow_live_provider=True)
    return scan, inputs


def _readback(scan, state, artifact):
    full = {**scan, "generated_artifact_jev": state,
            "limited_visual_review": {"candidate_count": 0, "candidate_pages": [], "status": "pass",
                                      "required": False}}
    return _guard()._validate_scan_readback(full, artifact=artifact, generated_jev_required=True)


@pytest.mark.parametrize("case,code", [
    ("no_key", "provider_unavailable"),
    ("run_id_zero", "evaluation_identity_unavailable"),
    ("not_allowed", "live_provider_not_allowed"),
])
def test_not_invoked_cases_block_completion_with_reason(tmp_path, monkeypatch, case, code):
    scan, inputs = _zero_candidate(tmp_path)
    calls = []
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", _answering_transport(calls))
    monkeypatch.delenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, raising=False)
    if case != "no_key":
        monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    if case == "run_id_zero":
        inputs["producer"] = {"task_id": "t_producer", "run_id": 0}
    if case == "not_allowed":
        inputs["allow_live_provider"] = False
    state = route.run(**inputs)
    assert calls == [] and state.get("provider_invoked") is not True
    assert route.validate_readback(scan, state) is None  # the record itself stays consistent
    assert route.effective_reason_code(state) == code
    error = _readback(scan, state, inputs["artifact"])
    assert error.startswith(BLOCK_PREFIX + code + "）: ")
    assert len(error) > len(BLOCK_PREFIX + code) + 10  # carries a readable next step


@pytest.mark.parametrize("choice,disposition", [
    ("pass", "accepted"), ("uncertain", "uncertain"), ("fail", "repair_required"),
])
def test_live_evaluation_of_any_disposition_passes_the_jev_requirement(tmp_path, monkeypatch, choice, disposition):
    scan, inputs = _zero_candidate(tmp_path)
    calls = []

    def transport(endpoint, key_env, model, payload, timeout):
        calls.append(payload)
        return {"answers": {"artifact_q0": {"choice": choice, "confidence": 0.95}}}

    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    state = route.run(**inputs)
    assert len(calls) == 1 and state["provider_status"] == disposition
    assert route.jev_required_block_reason(scan, state) is None  # Jev result handling is unchanged
    assert _readback(scan, state, inputs["artifact"]) is None


def test_not_eligible_routes_are_unchanged(tmp_path):
    scan, inputs = _zero_candidate(tmp_path)
    assert route.jev_required_block_reason(scan, {"status": "not_applicable", "candidate_count": 0,
                                                  "formal_provider_evidence": False}) is None
    hard = {"status": "not_applicable", "candidate_count": 0, "formal_provider_evidence": False,
            "evaluation_state": "error", "reason_code": "deterministic_hard_fail"}
    assert route.jev_required_block_reason(scan, hard) is None
    unsupported = {"status": "not_invoked", "candidate_count": 1, "formal_provider_evidence": False,
                   "evaluation_state": "error", "reason_code": "no_supported_candidate_type"}
    assert route.jev_required_block_reason(scan, unsupported) is None
    # Not required by the contract: the guard never asks for Jev.
    state = route.run(**{**inputs, "producer": None})
    full = {**scan, "generated_artifact_jev": state,
            "limited_visual_review": {"candidate_count": 0, "candidate_pages": [], "status": "pass", "required": False}}
    assert _guard()._validate_scan_readback(full, artifact=inputs["artifact"], generated_jev_required=False) is None


def test_missing_route_state_blocks_when_required(tmp_path):
    scan, inputs = _zero_candidate(tmp_path)
    assert route.jev_required_block_reason(scan, None).startswith(BLOCK_PREFIX + "jev_state_missing")
