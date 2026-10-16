"""Completion guard: review candidates that live Jev accepted need no docs-layout-visual-review-2 record.

Offline only: the provider transport is a mock (jev_overlap._post_provider); no network, no real Jev call.
The judged state/receipt is produced by generated_artifact_jev.run. The scan mirrors what the checker writes on its
`live-jev-cjk-appearance` branch, and the guard's decision is the checker's own `_jev_resolves_cjk_candidates`
(re-used, not re-written). Every other outcome must keep requiring the visual record.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import generated_artifact_jev as route, jev_post_render as jev

RULE = jev.jev_glyph_locale.RULE
ARTIFACT_SET_ID = "a" * 64
MISSING = "AQ-LAYOUT-01: review attachment is missing"
CONTRACT_FAILED = "AQ-LAYOUT-01: review identity/artifact-set/tool-use contract failed"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def _guard():
    return _load(ROOT / "__init__.py", "jev_accepts_completion_guard")


@functools.lru_cache(maxsize=None)
def _checker():
    return _load(ROOT / "check_docs_artifact_quality.py", "jev_accepts_checker")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scenario(tmp_path, monkeypatch, mode="live_accepted"):
    """Run the real generated-artifact Jev route offline. mode selects the (mocked) provider outcome."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact = (tmp_path / "page.pdf").resolve()
    with pymupdf.open() as doc:
        doc.new_page(width=595, height=842).insert_text((40, 50), "日本語の文字" * 3, fontname="japan", fontsize=10)
        doc.save(artifact)
    render = (tmp_path / "page.png").resolve()
    with pymupdf.open(artifact) as doc:
        doc[0].get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).save(render)
    issue = {"id": "cjk-region-1", "rule": RULE, "severity": "review", "bucket": "REVIEW_CANDIDATE", "page": 1,
             "bbox_pdf_points": [0.0, 0.0, 595.0, 842.0],
             "render_evidence": {"path": str(render), "sha256": _sha(render)}}
    pre_scan = {"schema_version": "docs-layout-typography-scan-3", "artifact_sha256": _sha(artifact),
                "measurement_basis": "pdf_points", "status": "review", "hard_issue_count": 0,
                "review_candidate_count": 1, "issues": [issue], "REVIEW_CANDIDATE": [issue], "HARD_FAIL": []}
    real_measure = jev.jev_glyph_locale.measure

    def sufficient(*args, **kwargs):
        measured = real_measure(*args, **kwargs)
        measured["evidence_sufficient"] = True  # locale proxies are covered elsewhere; this seam needs a judged receipt
        return measured

    choice, confidence = {
        "repair_required": ("non_japanese_glyph_likely", 0.95),
        "uncertain": ("uncertain", 0.95),
        "low_confidence": ("japanese_glyph_likely", 0.1),
    }.get(mode, ("japanese_glyph_likely", 0.95))
    calls = []

    def transport(endpoint, key_env, model, payload, timeout):  # mock only: never touches a network
        calls.append(payload)
        if mode == "connection_failed":
            raise RuntimeError("mock provider failure")
        return {"answers": {key: {"choice": choice, "confidence": confidence} for key in payload["questions"]}}

    def fixture(wire, timeout):
        reviews = [{"candidate_id": c["id"], "disposition": "accepted",
                    "criteria_results": [{"criterion": k, "status": "japanese_glyph_likely"} for k in c["criteria"]]}
                   for c in wire["candidates"]]
        return {"schema_version": "docs-jev-post-render-response-1",
                "request_sha256": wire["request_sha256"], "reviews": reviews}

    monkeypatch.setattr(jev.jev_glyph_locale, "measure", sufficient)
    # The completion guard loads its own copy of the Jev modules for its existing generated-artifact readback.
    guard_locale = _guard().jev_post_render_rules.jev_glyph_locale
    guard_measure = guard_locale.measure

    def guard_sufficient(*args, **kwargs):
        measured = guard_measure(*args, **kwargs)
        measured["evidence_sufficient"] = True
        return measured

    monkeypatch.setattr(guard_locale, "measure", guard_sufficient)
    monkeypatch.setattr(jev.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(jev.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    state = route.run(artifact=artifact, scan=pre_scan, evidence_dir=tmp_path / "evidence",
                      artifact_set_id=ARTIFACT_SET_ID,
                      producer={"task_id": "t_producer", "run_id": 1}, reviewer={"task_id": "t_owner", "run_id": 2},
                      allow_live_provider=(mode != "not_invoked"),
                      fixture_adapter=fixture if mode == "fixture" else None)
    return SimpleNamespace(artifact=artifact, issue=issue, state=state, calls=calls, tmp_path=tmp_path)


def _claim(s):
    """The resolution handles a producer would record for this state (what the checker's branch writes)."""
    has_request = isinstance(s.state.get("request"), dict)
    request = jev.read_json(Path(s.state["request"]["path"])) if has_request else None
    return {"request": s.state.get("request"), "receipt": s.state.get("receipt"),
            "candidate_set_digest": jev.candidate_digest(request["candidates"]) if request else "f" * 64,
            "artifact_sha256": _sha(s.artifact), "candidate_ids": [s.issue["id"]]}


def _scan(s, *, review_mode="live-jev-cjk-appearance"):
    claim = _claim(s)
    return {"schema_version": "docs-layout-typography-scan-3", "artifact_sha256": _sha(s.artifact),
            "measurement_basis": "pdf_points", "status": "pass", "hard_issue_count": 0, "review_candidate_count": 1,
            "issues": [s.issue], "REVIEW_CANDIDATE": [s.issue], "HARD_FAIL": [],
            "generated_artifact_jev": s.state,
            "limited_visual_review": {
                "required": True, "candidate_count": 1, "candidate_pages": [1], "review_mode": review_mode,
                "vision_model_invoked": False, "evidence": claim["receipt"], "jev_resolution": claim, "status": "pass"}}


def _readback(s, scan, *, review_path=None, required=True):
    return _guard()._validate_scan_readback(
        scan, review_path=review_path, artifact_set_id=ARTIFACT_SET_ID, artifact=s.artifact,
        generated_jev_required=required,
    )


def test_live_accepted_exact_match_passes_without_visual_record(tmp_path, monkeypatch):
    s = _scenario(tmp_path, monkeypatch)
    assert s.state["status"] == "evaluated" and s.state["execution_mode"] == "live"
    assert s.state["provider_invoked"] is True and s.state["reason_code"] == "ok"
    assert s.state["provider_status"] == "accepted" and s.state["formal_provider_evidence"] is True
    # The mirrored scan claim is exactly the checker's own decision for this state.
    assert _checker()._jev_resolves_cjk_candidates(s.state, [s.issue], s.artifact) == _claim(s)
    scan = _scan(s)
    assert _readback(s, scan) is None  # no review attachment at all
    # The receipt-level caller passes the Jev receipt (limited_visual_review.evidence) as review_path.
    assert _readback(s, scan, review_path=Path(s.state["receipt"]["path"])) is None
    assert len(s.calls) == 1  # completion readback never calls Jev again


@pytest.mark.parametrize("mode", [
    "repair_required", "uncertain", "low_confidence", "not_invoked", "connection_failed", "fixture",
])
def test_not_accepted_or_non_live_jev_still_requires_visual_record(tmp_path, monkeypatch, mode):
    s = _scenario(tmp_path, monkeypatch, mode)
    forged = _scan(s)  # claims live-jev-cjk-appearance / pass with whatever handles exist
    if mode in {"not_invoked", "connection_failed", "fixture"}:
        # Plan C: Jev was not run live for this artifact, so completion stops on that first.
        assert _readback(s, forged).startswith("AQ-LAYOUT-01: Jevの評価がないため完了できません（reason_code=")
        assert _readback(s, forged, required=False) == MISSING
        return
    assert _readback(s, forged) == MISSING
    if isinstance(s.state.get("receipt"), dict):
        # A Jev receipt is not a docs-layout-visual-review-2 record.
        assert _readback(s, forged, review_path=Path(s.state["receipt"]["path"])) == CONTRACT_FAILED


def test_artifact_bytes_changed_after_judgement_requires_visual_record(tmp_path, monkeypatch):
    s = _scenario(tmp_path, monkeypatch)
    scan = _scan(s)
    assert _readback(s, scan) is None
    with pymupdf.open() as doc:
        doc.new_page(width=595, height=842).insert_text((40, 50), "changed after judgement", fontsize=10)
        doc.save(s.artifact)
    # A forger refreshes every sha256 it controls; the Jev binding still names the judged bytes.
    scan["artifact_sha256"] = _sha(s.artifact)
    scan["limited_visual_review"]["jev_resolution"]["artifact_sha256"] = _sha(s.artifact)
    assert _readback(s, scan, required=False) == MISSING  # the new decision alone refuses
    assert _readback(s, scan) is not None  # and the existing generated-artifact Jev readback refuses as well


def test_other_artifact_requires_visual_record(tmp_path, monkeypatch):
    s = _scenario(tmp_path, monkeypatch)
    scan = _scan(s)
    other = (tmp_path / "other.pdf").resolve()
    with pymupdf.open() as doc:
        doc.new_page(width=300, height=180).insert_text((20, 40), "a different artifact")
        doc.save(other)
    assert _guard()._validate_scan_readback(
        scan, review_path=None, artifact_set_id=ARTIFACT_SET_ID, artifact=other, generated_jev_required=True,
    ) == MISSING
    assert _guard()._validate_scan_readback(
        scan, review_path=None, artifact_set_id=ARTIFACT_SET_ID, artifact=None, generated_jev_required=True,
    ) == MISSING


@pytest.mark.parametrize("field,value", [
    ("artifact_sha256", "0" * 64),
    ("candidate_set_digest", "f" * 64),
    ("candidate_ids", ["another-candidate"]),
])
def test_claimed_resolution_differing_from_recomputed_requires_visual_record(tmp_path, monkeypatch, field, value):
    s = _scenario(tmp_path, monkeypatch)
    scan = _scan(s)
    scan["limited_visual_review"]["jev_resolution"][field] = value
    assert _readback(s, scan) == MISSING


@pytest.mark.parametrize("mutate", [
    lambda lim, s: lim.update(review_mode="manual-receipt-only"),
    lambda lim, s: lim.update(review_mode="live-jev-standard"),
    lambda lim, s: lim.update(review_mode="live-jev-cjk-appearance "),
    lambda lim, s: lim.update(vision_model_invoked=True),
    lambda lim, s: lim.pop("jev_resolution"),
    lambda lim, s: lim.update(evidence=None),
    lambda lim, s: lim.update(evidence=s.state["request"]),
], ids=["manual-mode", "other-live-mode", "mode-with-suffix", "vision-invoked", "no-resolution", "no-evidence",
        "evidence-is-request"])
def test_review_mode_forgery_or_missing_claim_requires_visual_record(tmp_path, monkeypatch, mutate):
    s = _scenario(tmp_path, monkeypatch)  # live accepted, so only the scan's claim is wrong
    scan = _scan(s)
    mutate(scan["limited_visual_review"], s)
    assert _readback(s, scan) == MISSING


def test_non_cjk_candidate_in_the_set_requires_visual_record(tmp_path, monkeypatch):
    s = _scenario(tmp_path, monkeypatch)
    scan = _scan(s)
    spacing = {"id": "spacing-1", "rule": "line_spacing_candidate", "severity": "review",
               "bucket": "REVIEW_CANDIDATE", "page": 1}
    scan["issues"].append(spacing)
    scan["REVIEW_CANDIDATE"].append(spacing)
    scan["review_candidate_count"] = 2
    scan["limited_visual_review"]["candidate_count"] = 2
    # generated_jev_required=False isolates the new decision from the unrelated Jev candidate-count readback.
    assert _readback(s, scan, required=False) == MISSING


def test_manual_visual_record_path_is_kept(tmp_path, monkeypatch):
    s = _scenario(tmp_path, monkeypatch, "repair_required")
    scan = _scan(s, review_mode="manual-receipt-only")  # honest producer: Jev did not resolve it
    record = tmp_path / "review.json"
    record.write_text(json.dumps({
        "schema_version": "docs-layout-visual-review-2", "artifact_set_id": ARTIFACT_SET_ID,
        "producer_task_id": "t_producer", "producer_run_id": 1, "reviewer_task_id": "t_owner", "reviewer_run_id": 2,
        "llm_invoked": False, "vision_invoked": False,
        "reviews": [{"issue_id": s.issue["id"], "llm_invoked": False, "vision_invoked": False}],
    }), encoding="utf-8")
    assert _readback(s, scan) == MISSING
    assert _readback(s, scan, review_path=record) is None
    same_identity = json.loads(record.read_text(encoding="utf-8"))
    same_identity["reviewer_task_id"] = "t_producer"
    record.write_text(json.dumps(same_identity), encoding="utf-8")
    assert _readback(s, scan, review_path=record) == CONTRACT_FAILED
