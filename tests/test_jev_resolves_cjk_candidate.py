"""AQ-LAYOUT-01: a live Jev 'accepted' for exactly the CJK appearance candidates resolves them without a manual
visual record; every other outcome keeps them review-required.

Offline only: the provider transport is a mock (jev_overlap._post_provider); no network, no real Jev call.
The judged state/receipt is produced by generated_artifact_jev.run and fed to the checker's resolution helper.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import generated_artifact_jev as route, jev_post_render as jev

RULE = jev.jev_glyph_locale.RULE


def _checker():
    spec = importlib.util.spec_from_file_location("jev_resolves_candidate_checker", ROOT / "check_docs_artifact_quality.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(tmp_path, monkeypatch, *, choice="japanese_glyph_likely", confidence=0.95, allow=True,
         transport=None, fixture=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact = (tmp_path / "page.pdf").resolve()
    with pymupdf.open() as doc:
        doc.new_page(width=595, height=842).insert_text((40, 50), "日本語の文字" * 3, fontname="japan", fontsize=10)
        doc.save(artifact)
    render = (tmp_path / "page.png").resolve()
    with pymupdf.open(artifact) as doc:
        doc[0].get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).save(render)
    issue = {"id": "cjk-region-1", "rule": RULE, "severity": "review", "page": 1,
             "bbox_pdf_points": [0.0, 0.0, 595.0, 842.0],
             "render_evidence": {"path": str(render), "sha256": hashlib.sha256(render.read_bytes()).hexdigest()}}
    scan = {"schema_version": "docs-layout-typography-scan-3",
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "measurement_basis": "pdf_points", "status": "review", "hard_issue_count": 0,
            "review_candidate_count": 1, "issues": [issue], "REVIEW_CANDIDATE": [issue], "HARD_FAIL": []}
    real_measure = jev.jev_glyph_locale.measure

    def sufficient(*args, **kwargs):
        measured = real_measure(*args, **kwargs)
        measured["evidence_sufficient"] = True  # locale proxies are covered elsewhere; this seam needs a judged receipt
        return measured

    calls = []

    def default_transport(endpoint, key_env, model, payload, timeout):
        calls.append(payload)
        return {"answers": {key: {"choice": choice, "confidence": confidence} for key in payload["questions"]}}

    monkeypatch.setattr(jev.jev_glyph_locale, "measure", sufficient)
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport or default_transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    state = route.run(artifact=artifact, scan=scan, evidence_dir=tmp_path / "evidence", artifact_set_id="a" * 64,
                      producer={"task_id": "t_producer", "run_id": 1}, reviewer={"task_id": "t_owner", "run_id": 2},
                      allow_live_provider=allow, fixture_adapter=fixture)
    return _checker(), artifact, issue, state, calls


def test_live_accepted_resolves_exact_candidates_without_visual_record(tmp_path, monkeypatch):
    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch)
    assert state["status"] == "evaluated" and state["execution_mode"] == "live"
    assert state["provider_invoked"] is True and state["reason_code"] == "ok" and state["provider_status"] == "accepted"
    resolved = checker._jev_resolves_cjk_candidates(state, [issue], artifact)
    assert resolved is not None and len(calls) == 1
    request = jev.read_json(Path(state["request"]["path"]))
    assert resolved["candidate_ids"] == [issue["id"]]
    assert resolved["candidate_set_digest"] == jev.candidate_digest(request["candidates"])
    assert resolved["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert resolved["request"] == state["request"] and resolved["receipt"] == state["receipt"]


@pytest.mark.parametrize("choice,confidence,provider_status", [
    ("non_japanese_glyph_likely", 0.95, "repair_required"),
    ("uncertain", 0.95, "uncertain"),
    ("japanese_glyph_likely", 0.1, "uncertain"),
])
def test_repair_required_and_uncertain_stay_review_required(tmp_path, monkeypatch, choice, confidence, provider_status):
    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch, choice=choice, confidence=confidence)
    assert state["provider_invoked"] is True and state["provider_status"] == provider_status and len(calls) == 1
    assert checker._jev_resolves_cjk_candidates(state, [issue], artifact) is None


def test_not_invoked_stays_review_required(tmp_path, monkeypatch):
    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch, allow=False)
    assert state["status"] == "not_invoked" and state["provider_invoked"] is False and calls == []
    assert checker._jev_resolves_cjk_candidates(state, [issue], artifact) is None


def test_connection_failed_stays_review_required(tmp_path, monkeypatch):
    def boom(endpoint, key_env, model, payload, timeout):  # mock only: never touches a network
        raise RuntimeError("mock provider failure")

    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch, transport=boom)
    assert state["status"] == "connection_failed" and state["provider_invoked"] is True
    assert checker._jev_resolves_cjk_candidates(state, [issue], artifact) is None


def test_fixture_execution_stays_review_required(tmp_path, monkeypatch):
    def fixture(wire, timeout):
        reviews = [{"candidate_id": c["id"], "disposition": "accepted",
                    "criteria_results": [{"criterion": k, "status": "japanese_glyph_likely"} for k in c["criteria"]]}
                   for c in wire["candidates"]]
        return {"schema_version": "docs-jev-post-render-response-1",
                "request_sha256": wire["request_sha256"], "reviews": reviews}

    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch, fixture=fixture)
    assert state["execution_mode"] == "fixture" and state["provider_status"] == "accepted"
    assert state["formal_provider_evidence"] is False and calls == []
    assert checker._jev_resolves_cjk_candidates(state, [issue], artifact) is None


def test_candidate_mismatch_stays_review_required(tmp_path, monkeypatch):
    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch)
    resolve = checker._jev_resolves_cjk_candidates
    assert resolve(state, [issue], artifact) is not None
    assert resolve(state, [{**issue, "id": "another-candidate"}], artifact) is None  # id differs from the judged one
    assert resolve(state, [{**issue, "bbox_pdf_points": [1.0, 1.0, 100.0, 100.0]}], artifact) is None  # descriptor differs
    spacing = {"id": "spacing-1", "rule": "line_spacing_candidate", "severity": "review", "page": 1}
    assert resolve(state, [issue, spacing], artifact) is None  # a non-CJK candidate is never resolved by this route
    assert resolve(state, [], artifact) is None


def test_tampered_receipt_or_changed_artifact_stays_review_required(tmp_path, monkeypatch):
    checker, artifact, issue, state, calls = _run(tmp_path, monkeypatch)
    resolve = checker._jev_resolves_cjk_candidates
    receipt_path = Path(state["receipt"]["path"])
    original = receipt_path.read_bytes()
    receipt_path.write_bytes(original + b" ")
    assert resolve(state, [issue], artifact) is None  # receipt bytes no longer match the recorded sha256
    receipt_path.write_bytes(original)
    assert resolve(state, [issue], artifact) is not None
    other = tmp_path / "other.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=300, height=180).insert_text((20, 40), "a different artifact")
        doc.save(other)
    assert resolve(state, [issue], other) is None  # artifact sha256 differs from the judged artifact
    with pymupdf.open() as doc:
        doc.new_page(width=595, height=842).insert_text((40, 50), "changed after judgement", fontsize=10)
        doc.save(artifact)
    assert resolve(state, [issue], artifact) is None  # judged artifact bytes changed on disk
