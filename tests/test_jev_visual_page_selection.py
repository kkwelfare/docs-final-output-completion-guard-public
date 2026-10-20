"""Page selection (``pages``) for the optional Jev image review.

Offline only: a fixture transport replaces the provider; no network and no API key is used.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import jev_visual_review as vr


def _pdf(tmp_path: Path, pages: int) -> Path:
    path = (tmp_path / "doc.pdf").resolve()
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=595, height=842)
            page.insert_text((60, 80), f"Page {i + 1} heading", fontsize=18)
        doc.save(path)
    return path


def _candidate(cid, page):
    return {"id": cid, "rule": "unclassified_source_overlap_or_gutter", "severity": "review", "page": page,
            "bbox_pdf_points": [50.0, 60.0, 300.0, 150.0], "gutter_pt": 3.5}


class Fixture:
    def __init__(self):
        self.calls = []

    def __call__(self, body, headers, timeout):
        self.calls.append(body)
        answers = {}
        for key, q in body["questions"].items():
            if q["type"] == "choice":
                answers[key] = {"choice": "acceptable", "confidence": 0.9}
            elif q["type"] == "noul":
                answers[key] = {"noul": 0.1}
        return {"model": vr.MODEL, "answers": answers}


def test_selected_pages_only_and_others_recorded(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 6), candidates=[], config={"enabled": True, "pages": [5, 1, 3]},
                    transport=fx)
    assert [p["page"] for p in result["pages"]] == [1, 3, 5]
    assert result["skipped_pages"] == [{"page": n, "reason": "not_selected"} for n in (2, 4, 6)]
    assert len(fx.calls) == 3 and result["provider_calls"] == 3
    assert result["status"] == "evaluated" and result["reason_code"] == "ok"
    assert result["config"]["pages"] == [1, 3, 5]
    summary = vr.summary(result, None)
    assert summary["selected_pages"] == [1, 3, 5] and summary["skipped_pages"] == 3


def test_candidates_limited_to_selected_pages(tmp_path):
    fx = Fixture()
    cands = [_candidate("a", 1), _candidate("b", 2), _candidate("c", 3)]
    result = vr.run(artifact=_pdf(tmp_path, 3), candidates=cands, config={"enabled": True, "pages": [1, 3]},
                    transport=fx)
    assert [c["id"] for c in result["candidates"]] == ["a", "c"]
    assert result["skipped_candidates"] == [{"id": "b", "reason": "page_not_selected"}]
    assert len(fx.calls) == 4  # 2 pages + 2 candidates


def test_max_pages_counts_selected_pages(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 6), candidates=[],
                    config={"enabled": True, "pages": [2, 4, 6], "max_pages": 2}, transport=fx)
    assert [p["page"] for p in result["pages"]] == [2, 4]
    reasons = {s["page"]: s["reason"] for s in result["skipped_pages"]}
    assert reasons == {1: "not_selected", 3: "not_selected", 5: "not_selected", 6: "max_pages"}


def test_unspecified_pages_keeps_all_pages(tmp_path):
    fx = Fixture()
    cfg, err = vr.normalize_config({"enabled": True})
    assert err is None and cfg["pages"] is None
    result = vr.run(artifact=_pdf(tmp_path, 3), candidates=[_candidate("a", 2)], config={"enabled": True},
                    transport=fx)
    assert [p["page"] for p in result["pages"]] == [1, 2, 3]
    assert result["skipped_pages"] == [] and result["skipped_candidates"] == []
    assert [c["id"] for c in result["candidates"]] == ["a"]
    assert vr.summary(result, None)["selected_pages"] is None


@pytest.mark.parametrize("pages", [[], [0], [-1], [1, 1], [1.0], ["1"], [True], 3, "1,3", {"a": 1}])
def test_invalid_pages_is_config_error(pages):
    assert vr.normalize_config({"enabled": True, "pages": pages}) == (None, "invalid_config")


def test_invalid_pages_never_calls_provider(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 2), candidates=[], config={"enabled": True, "pages": [2, 2]},
                    transport=fx)
    assert result["reason_code"] == "invalid_config" and result["status"] == "not_run" and fx.calls == []


def test_out_of_range_page_is_config_error(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 3), candidates=[], config={"enabled": True, "pages": [1, 4]},
                    transport=fx)
    assert result["reason_code"] == "invalid_config" and result["config_error"] == "pages_out_of_range"
    assert result["status"] == "not_run" and result["provider_calls"] == 0 and fx.calls == []
    assert result["pages"] == [] and result["blocks_completion"] is False


def test_pages_does_not_enable_route(tmp_path):
    fx = Fixture()
    result = vr.run(artifact=_pdf(tmp_path, 2), candidates=[], config={"enabled": False, "pages": [1]},
                    transport=fx)
    assert result["reason_code"] == "disabled" and fx.calls == []
    off = vr.run(artifact=_pdf(tmp_path, 2), candidates=[], config={"enabled": True, "pages": [1]})
    assert off["reason_code"] == "live_provider_not_allowed" and off["provider_calls"] == 0
