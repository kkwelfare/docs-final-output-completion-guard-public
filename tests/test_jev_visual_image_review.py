"""Optional advisory Jev 1.14 image review (whole page + candidate crop + gated Box).

Offline only: a fixture transport replaces the provider; no network and no API key is used.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import jev_visual_review as vr


def _pdf(tmp_path: Path, pages: int = 2, size=(595, 842)) -> Path:
    path = (tmp_path / "doc.pdf").resolve()
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=size[0], height=size[1])
            page.insert_text((60, 80 + i * 10), f"Page {i + 1} heading", fontsize=18)
            page.insert_text((60, 120), "Body line one", fontsize=11)
            page.insert_text((60, 136), "Body line two", fontsize=11)
        doc.save(path)
    return path


def _candidate(cid="cand-1", page=1, bbox=(50.0, 60.0, 300.0, 150.0), rule="unclassified_source_overlap_or_gutter"):
    return {"id": cid, "rule": rule, "severity": "review", "page": page,
            "bbox_pdf_points": list(bbox), "gutter_pt": 3.5}


class Fixture:
    """Records every body/header; answers per question type."""

    def __init__(self, verdict="acceptable", placement=0.1, breakage=0.05, model=vr.MODEL, fail=None, confidence=0.9):
        self.calls, self.verdict, self.placement, self.breakage, self.model, self.fail = [], verdict, placement, breakage, model, fail
        self.confidence = confidence

    def __call__(self, body, headers, timeout):
        self.calls.append((body, headers, timeout))
        if self.fail:
            raise self.fail
        answers = {}
        for key, q in body["questions"].items():
            if q["type"] == "choice":
                answers[key] = {"type": "choice", "choice": self.verdict, "confidence": self.confidence}
            elif q["type"] == "noul":
                answers[key] = {"type": "noul", "noul": self.placement if key == "placement_unnatural" else self.breakage}
            else:
                answers[key] = {"type": "box", "box": {"left": 0.1, "top": 0.05, "right": 0.6, "bottom": 0.2}, "confidence": 0.7}
        return {"model": self.model, "answers": answers, "usage": {"input_tokens": 4000, "output_tokens": 50}}


def _cfg(**extra):
    return {"enabled": True, **extra}


# ---------------------------------------------------------------- request shape

def test_request_shape_literal_model_header_and_assets(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 1), candidates=[_candidate()], config=_cfg(), transport=fx)
    assert result["status"] == "evaluated" and result["reason_code"] == "ok"
    assert len(fx.calls) == 2  # one whole-page + one candidate, no Box
    for body, headers, _ in fx.calls:
        assert body["model"] == "jev-1.14.0"
        assert headers["TypeSafe-Danger-Zone-Images"] == "true"
        assert body["assets"] and all(a["type"] == "image" and a["format"] == "jpg" for a in body["assets"].values())
        # every asset uuid is referenced from state
        assert set(body["assets"]) <= set(body["state"].values())
        assert "Authorization" not in headers  # key is attached only by the live transport
    page_body = fx.calls[0][0]
    assert set(page_body["questions"]) == {"layout_verdict", "placement_unnatural", "visible_breakage"}
    assert page_body["questions"]["layout_verdict"]["choices"] == ["repair_required", "acceptable", "insufficient_information"]
    cand_body = fx.calls[1][0]
    assert len(cand_body["assets"]) == 2 and '"gutter_pt": 3.5' in cand_body["state"]["context"]
    page = result["pages"][0]
    assert page["judgment"]["response_model"] == "jev-1.14.0" and page["judgment"]["usage"]["input_tokens"] == 4000
    assert len(page["judgment"]["request_sha256"]) == 64 and page["judgment"]["attempts"] == 1
    assert "data_base64" not in json.dumps(result)  # no image bytes retained in evidence


def test_module_never_uses_alias_or_fallback():
    src = (ROOT / "quality_rules/jev_visual_review.py").read_text()
    assert 'MODEL = "jev-1.14.0"' in src
    assert "openrouter" not in src.lower() and "jev-latest" not in src
    assert vr.ENDPOINT == "https://api.typesafe.ai/v1/systemone"


# ---------------------------------------------------------------- image limits

@pytest.mark.parametrize("size", [(4000, 3000), (3000, 100), (100, 50), (1568, 1568), (300, 2000), (20, 20)])
def test_prepare_image_fits_api_limits(size):
    data, geo = vr.prepare_image(Image.new("RGB", size, (200, 10, 10)))
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG" and not img.info.get("progressive")
    w, h = img.size
    assert 280 <= w <= 1568 and 280 <= h <= 1568 and (w, h) == (geo["width"], geo["height"])
    x0, y0, x1, y1 = geo["content_box_px"]
    assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h
    # aspect ratio preserved inside the content box (padding, not stretching)
    assert abs((x1 - x0) / (y1 - y0) - size[0] / size[1]) < 0.05 * max(1, size[0] / size[1])


def test_whole_page_and_crop_render_within_limits(tmp_path):
    with pymupdf.open(_pdf(tmp_path, 1, size=(960, 540))) as doc:
        page = doc[0]
        data, geo = vr.render_whole_page(page)
        assert max(geo["width"], geo["height"]) == 1568 and geo["width"] > geo["height"]
        data, geo = vr.render_crop(page, [900, 500, 950, 530])  # tiny edge region -> context + pad
        assert 280 <= geo["width"] <= 1568 and 280 <= geo["height"] <= 1568
        assert geo["clip_pt"][2] <= 960 and geo["clip_pt"][3] <= 540


def test_rotated_page_rendered_upright(tmp_path):
    path = (tmp_path / "rot.pdf").resolve()
    with pymupdf.open() as doc:
        doc.new_page(width=595, height=842).set_rotation(90)
        doc.save(path)
    with pymupdf.open(path) as doc:
        _, geo = vr.render_whole_page(doc[0])
    assert geo["width"] > geo["height"]  # displayed landscape orientation


# ---------------------------------------------------------------- whole page, page cap

def test_every_page_judged_independent_of_candidates(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 3), candidates=[], config=_cfg(), transport=fx)
    assert [p["page"] for p in result["pages"]] == [1, 2, 3]
    assert len(fx.calls) == 3 and result["skipped_pages"] == []
    assert all(p["label"] == "acceptable" for p in result["pages"])


def test_page_cap_records_skipped_pages(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 5), candidates=[], config=_cfg(max_pages=2), transport=fx)
    assert [p["page"] for p in result["pages"]] == [1, 2]
    assert result["skipped_pages"] == [{"page": 3, "reason": "max_pages"}, {"page": 4, "reason": "max_pages"},
                                       {"page": 5, "reason": "max_pages"}]
    assert len(fx.calls) == 2


def test_default_page_cap_is_explicit():
    cfg, err = vr.normalize_config({"enabled": True})
    assert err is None and cfg["max_pages"] == 40 and cfg["max_candidate_calls"] == 8
    assert cfg["allow_live_provider"] is False


def test_candidate_call_cap_records_skipped(tmp_path):
    fx = Fixture()
    cands = [_candidate(f"c{i}") for i in range(4)]
    result = vr.run(artifact=_pdf(tmp_path, 1), candidates=cands,
                    config=_cfg(max_pages=0, max_candidate_calls=2), transport=fx)
    assert [c["id"] for c in result["candidates"]] == ["c0", "c1"]
    assert [s["id"] for s in result["skipped_candidates"]] == ["c2", "c3"]
    assert len(result["skipped_pages"]) == 1 and len(fx.calls) == 2


# ---------------------------------------------------------------- Box gating

def test_box_not_called_when_page_acceptable(tmp_path):
    fx = Fixture(verdict="acceptable", placement=0.2, breakage=0.1)
    result = vr.run(artifact=_pdf(tmp_path, 1), candidates=[_candidate()], config=_cfg(), transport=fx)
    assert not any(q["type"] == "box" for body, _, _ in fx.calls for q in body["questions"].values())
    assert "box" not in result["pages"][0] and "box" not in result["candidates"][0]


@pytest.mark.parametrize("kwargs", [{"verdict": "repair_required"}, {"breakage": 0.9},
                                    {"verdict": "repair_required", "confidence": 0.5}])
def test_box_after_whole_page_verdict_with_numeric_cross_reference(tmp_path, kwargs):
    fx = Fixture(**kwargs)
    result = vr.run(artifact=_pdf(tmp_path, 1), candidates=[_candidate()], config=_cfg(max_candidate_calls=0),
                    transport=fx)
    page = result["pages"][0]
    assert page["label"] == "repair_required"
    box_calls = [b for b, _, _ in fx.calls if any(q["type"] == "box" for q in b["questions"].values())]
    assert len(box_calls) == 1 and len(box_calls[0]["questions"]) == 1  # Box asked alone, after the verdict
    assert fx.calls[0][0]["questions"]["layout_verdict"]  # verdict request came first
    box = page["box"]
    assert box["status"] == "ok" and len(box["region_pt"]) == 4
    # normalized (0.1,0.05)-(0.6,0.2) on a 595x842 page
    assert box["region_pt"][0] == pytest.approx(59.5, abs=1.5) and box["region_pt"][3] == pytest.approx(168.4, abs=1.5)
    xref = box["numeric_cross_reference"]
    assert xref["text_line_count"] >= 1 and "page_margins_pt" in xref
    assert [c["id"] for c in xref["intersecting_candidates"]] == ["cand-1"]
    assert result["labels"]["pages_repair_required"] == [1]


def test_box_cap(tmp_path):
    fx = Fixture(verdict="repair_required")
    result = vr.run(artifact=_pdf(tmp_path, 3), candidates=[], config=_cfg(max_box_calls=1), transport=fx)
    assert result["pages"][0]["box"]["status"] == "ok"
    assert result["pages"][1]["box"] == {"status": "skipped_box_cap"}
    assert len(fx.calls) == 4



# ---------------------------------------------------------------- two-tier labels

def _answers(choice, confidence, placement, breakage):
    return {"layout_verdict": {"choice": choice, "confidence": confidence},
            "placement_unnatural": {"noul": placement}, "visible_breakage": {"noul": breakage}}


@pytest.mark.parametrize("answers, label", [
    (_answers("repair_required", 0.5, 0.1, 0.1), "repair_required"),     # confident verdict
    (_answers("repair_required", 0.9, 0.1, 0.1), "repair_required"),
    (_answers("acceptable", 0.9, 0.1, 0.5), "repair_required"),          # visible breakage alone
    (_answers("repair_required", 0.49, 0.1, 0.1), "differs_from_surroundings"),  # low-confidence verdict
    (_answers("acceptable", 0.9, 0.5, 0.1), "differs_from_surroundings"),        # placement alone
    (_answers("acceptable", 0.9, 0.49, 0.49), "acceptable"),
    (_answers("insufficient_information", 0.9, 0.1, 0.1), "insufficient_information"),
])
def test_two_tier_label(answers, label):
    assert vr.advisory_label(answers, 0.5, 0.5, 0.5) == label
    assert vr.advisory_label(answers, 0.5) == label  # defaults


def test_threshold_config_defaults_and_validation():
    cfg, err = vr.normalize_config({"enabled": True})
    assert err is None and cfg["repair_confidence_threshold"] == 0.5 and cfg["intent_threshold"] == 0.5
    assert cfg["likely_threshold"] == 0.5
    cfg, err = vr.normalize_config({"enabled": True, "repair_confidence_threshold": 0.7, "intent_threshold": 0.4})
    assert err is None and cfg["repair_confidence_threshold"] == 0.7 and cfg["intent_threshold"] == 0.4
    cfg, _ = vr.normalize_config({"enabled": True, "likely_threshold": 0.3})
    assert cfg["intent_threshold"] == 0.3  # intent tier defaults to at most the repair tier
    for bad in ({"repair_confidence_threshold": 0}, {"repair_confidence_threshold": 1.5},
                {"repair_confidence_threshold": True}, {"repair_confidence_threshold": "0.5"},
                {"intent_threshold": 0.6}, {"intent_threshold": 0}, {"intent_threshold": float("nan")}):
        assert vr.normalize_config({"enabled": True, **bad}) == (None, "invalid_config")


def test_repair_confidence_threshold_is_configurable(tmp_path):
    fx = Fixture(verdict="repair_required", confidence=0.6)
    result = vr.run(artifact=_pdf(tmp_path, 1), candidates=[], config=_cfg(repair_confidence_threshold=0.7),
                    transport=fx)
    assert result["pages"][0]["label"] == "differs_from_surroundings" and "box" not in result["pages"][0]
    assert len(fx.calls) == 1


@pytest.mark.parametrize("kwargs", [{"verdict": "repair_required", "confidence": 0.3}, {"placement": 0.8}])
def test_differs_from_surroundings_never_asks_box(tmp_path, kwargs):
    fx = Fixture(**kwargs)
    result = vr.run(artifact=_pdf(tmp_path, 1), candidates=[_candidate()], config=_cfg(), transport=fx)
    assert not any(q["type"] == "box" for body, _, _ in fx.calls for q in body["questions"].values())
    assert len(fx.calls) == 2
    assert result["pages"][0]["label"] == "differs_from_surroundings" and "box" not in result["pages"][0]
    assert result["candidates"][0]["label"] == "differs_from_surroundings" and "box" not in result["candidates"][0]
    labels = result["labels"]
    assert labels["pages_repair_required"] == [] and labels["candidates_repair_required"] == []
    assert labels["pages_differs_from_surroundings"] == [1]
    assert labels["candidates_differs_from_surroundings"] == ["cand-1"]
    assert vr.summary(result, None)["labels"] == labels


RECORDED = ROOT / "tests" / "fixtures" / "jev-visual-review-recorded-answers.json"


def test_recorded_pages_4_and_7_become_differs_without_box(tmp_path):
    """Offline regression on recorded live answers (pages 1-8; no provider call)."""
    recorded = {p["page"]: p["answers"] for p in json.loads(RECORDED.read_text(encoding="utf-8"))["whole_pages"]}
    assert sorted(recorded) == list(range(1, 9))
    cfg, _ = vr.normalize_config({"enabled": True})
    labels = {n: vr.advisory_label(a, cfg["likely_threshold"], cfg["repair_confidence_threshold"],
                                   cfg["intent_threshold"]) for n, a in recorded.items()}
    assert labels[4] == labels[7] == "differs_from_surroundings"
    assert [n for n, l in labels.items() if l == "repair_required"] == []
    assert all(l == "acceptable" for n, l in labels.items() if n not in (4, 7))

    def replay(body, headers, timeout):  # answers each page request with its recorded answers
        replay.calls.append(body)
        if any(q["type"] == "box" for q in body["questions"].values()):
            raise AssertionError("Box must not be asked")
        number = int(body["state"]["context"].split()[2])
        answers = {k: {"type": "choice", **v} if k == "layout_verdict" else {"type": "noul", **v}
                   for k, v in recorded[number].items()}
        return {"model": vr.MODEL, "answers": answers}
    replay.calls = []
    result = vr.run(artifact=_pdf(tmp_path, 8, size=(960, 540)), candidates=[], config=_cfg(), transport=replay)
    assert result["status"] == "evaluated" and len(replay.calls) == 8 and result["provider_calls"] == 8
    assert result["labels"]["pages_repair_required"] == []
    assert result["labels"]["pages_differs_from_surroundings"] == [4, 7]
    assert all("box" not in p for p in result["pages"])

# ---------------------------------------------------------------- failure states

@pytest.mark.parametrize("error, reason", [
    (vr.VisualReviewError("provider_error", 400), "provider_error"),
    (vr.VisualReviewError("timeout"), "timeout"),
    (TimeoutError(), "timeout"),
    (RuntimeError("boom"), "provider_error"),
])
def test_failures_recorded_once_no_retry(tmp_path, error, reason):
    fx = Fixture(fail=error)
    result = vr.run(artifact=_pdf(tmp_path, 2), candidates=[_candidate()], config=_cfg(), transport=fx)
    assert len(fx.calls) == 3  # one attempt per request, no retry, no Box
    assert result["status"] == "not_run" and result["reason_code"] == reason
    assert all(e["label"] == "not_judged" and e["judgment"]["attempts"] == 1 for e in result["pages"] + result["candidates"])
    assert result["advisory_only"] is True and result["blocks_completion"] is False


def test_malformed_and_model_mismatch(tmp_path):
    bad = vr.run(artifact=_pdf(tmp_path, 1), candidates=[], config=_cfg(), transport=lambda b, h, t: {"answers": {}})
    assert bad["reason_code"] == "malformed_response" and bad["pages"][0]["label"] == "not_judged"
    other = vr.run(artifact=_pdf(tmp_path, 1), candidates=[], config=_cfg(), transport=Fixture(model="jev-1.13.0"))
    assert other["pages"][0]["judgment"]["status"] == "model_mismatch"
    assert other["pages"][0]["judgment"]["response_model"] == "jev-1.13.0"
    assert other["reason_code"] == "model_mismatch"


def test_off_by_default_and_live_requires_opt_in_and_key(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path, 1)
    assert vr.run(artifact=pdf, candidates=[], config=None)["reason_code"] == "disabled"
    assert vr.run(artifact=pdf, candidates=[], config={"enabled": False})["reason_code"] == "disabled"
    assert vr.run(artifact=pdf, candidates=[], config={"enabled": True})["reason_code"] == "live_provider_not_allowed"
    monkeypatch.delenv(vr.API_KEY_ENV, raising=False)
    r = vr.run(artifact=pdf, candidates=[], config={"enabled": True, "allow_live_provider": True})
    assert r["reason_code"] == "provider_unavailable" and r["provider_calls"] == 0
    assert vr.run(artifact=pdf, candidates=[], config={"enabled": True, "bogus": 1})["reason_code"] == "invalid_config"
    assert vr.run(artifact=pdf, candidates=[], config={"enabled": True, "max_pages": 999})["reason_code"] == "invalid_config"


def test_live_transport_sends_header_and_bearer(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b'{"model":"jev-1.14.0","answers":{}}'

    def fake_urlopen(req, timeout):
        captured.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout)
        return Resp()

    monkeypatch.setattr(vr, "urlopen", fake_urlopen)
    monkeypatch.setenv(vr.API_KEY_ENV, "test-only-no-network")
    vr.live_transport({"model": vr.MODEL}, vr.request_headers(), 5.0)
    assert captured["url"] == vr.ENDPOINT
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["typesafe-danger-zone-images"] == "true" and headers["authorization"].startswith("Bearer ")


# ---------------------------------------------------------------- advisory-only in the checker

def _checker_setup(tmp_path, image_cfg):
    pytest.importorskip("pptx")  # baseline fixture helpers need python-pptx (absent from the runtime venv)

    def load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    checker = load(ROOT / "check_docs_artifact_quality.py", "jev_visual_image_checker")
    baseline = load(ROOT / "tests/test_composition_receipt_e2e.py", "jev_visual_image_baseline")
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact = (tmp_path / "badge.pdf").resolve()
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=180)
        page.draw_circle((50, 50), 12, color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        page.insert_text((50, 53), "1", fontsize=8)
        page.insert_text((62, 53), "A", fontsize=9)
        doc.save(artifact)
    source = tmp_path / "source.txt"
    source.write_text("fixture source", encoding="utf-8")
    layout = {"required": True, "measurement_basis": "pdf_points", "minimum_gap_pt": 0,
              "dead_space": {"bottom_blank_ratio_max": 1}, "narrow_card_exemption_ratio": 0}
    if image_cfg is not None:
        layout["jev_visual_image_review"] = image_cfg
    contract = {"artifact_kind": "pdf", "artifacts": [str(artifact)],
                "artifact_quality": {"required": True, "layout_typography": layout}}
    base = baseline._contract(tmp_path, source, artifact)
    for key in ("schema_version", "authoritative_source", "visible_criteria", "rule_contracts"):
        contract[key] = base[key]
    contract["authoritative_source"]["readback_mode"] = "text"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for rule in checker.common.CONTRACT_RULES:
        (evidence / f"{artifact.name}.{rule}.json").write_text(json.dumps(baseline._check(rule, contract, artifact)),
                                                               encoding="utf-8")
    return checker, contract_path, evidence


def _layout(receipt):
    rule = next(r for r in receipt["entries"][0]["rules"] if r["id"] == "AQ-LAYOUT-01")
    return rule, json.loads(Path(rule["evidence"]["path"]).read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", [Fixture(verdict="repair_required", placement=0.99, breakage=0.99),
                                     Fixture(fail=RuntimeError("down"))])
def test_checker_result_unchanged_by_image_review(tmp_path, monkeypatch, fixture):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)  # existing text routes stay offline
    checker, path, ev = _checker_setup(tmp_path / "off", None)
    off_receipt, off_code = checker.build_receipt(path, evidence_dir=ev)
    checker, path, ev = _checker_setup(tmp_path / "on", {"enabled": True})
    on_receipt, on_code = checker.build_receipt(path, evidence_dir=ev, jev_visual_transport=fixture)
    off_rule, off_scan = _layout(off_receipt)
    on_rule, on_scan = _layout(on_receipt)
    assert on_code == off_code and on_rule["status"] == off_rule["status"]
    def verdict(scan):  # path-independent decision fields only (issue ids hash the tmp path)
        lvr, gen = scan["limited_visual_review"], scan.get("generated_artifact_jev") or {}
        return (scan["status"], scan.get("hard_issue_count"), scan.get("review_candidate_count"),
                sorted((i.get("rule"), i.get("severity"), i.get("page")) for i in scan["issues"]),
                {k: lvr.get(k) for k in ("required", "status", "candidate_count", "candidate_pages", "review_mode")},
                {k: gen.get(k) for k in ("status", "reason_code", "provider_invoked", "execution_mode")})
    assert verdict(on_scan) == verdict(off_scan)
    summary = on_scan["jev_visual_image_review"]
    assert summary["advisory_only"] is True and summary["blocks_completion"] is False
    assert summary["satisfies_plan_c"] is False and summary["model"] == "jev-1.14.0"
    assert summary["provider_calls"] >= 1 and Path(summary["evidence"]["path"]).is_file()
    assert "jev_visual_image_review" not in off_scan
