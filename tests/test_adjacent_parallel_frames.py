"""Adjacent parallel bordered frames (HTML-PDF-ADJACENT-PARALLEL-FRAMES-01), rendered-PDF geometry."""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import html_pdf_harness

REASON = "HTML-PDF-ADJACENT-PARALLEL-FRAMES-01"
GRAY = (0.349, 0.349, 0.349)


def _pdf(path: Path, rects, *, width: float = 1.5, fill_only=()) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    for rect in rects:
        page.draw_rect(pymupdf.Rect(*rect), color=GRAY, width=width)
    for rect in fill_only:   # frames without a line
        page.draw_rect(pymupdf.Rect(*rect), color=None, fill=(0.9, 0.9, 0.9), width=0)
    document.save(path)
    document.close()
    return path


def _stack(gap: float, count: int = 2, x0: float = 38.25, x1: float = 557.25, top: float = 100.0, height: float = 20.0):
    return [(x0, top + k * (height + gap), x1, top + k * (height + gap) + height) for k in range(count)]


def _scan(path: Path) -> dict:
    return html_pdf_harness.scan_rendered_pdf_boundaries(path, [])


def test_fax_form_rows_with_4_to_5pt_gaps_fail_with_frame_ids_and_gap(tmp_path):
    rows = [(38.25, 665.25, 557.25, 685.5), (38.25, 690.0, 557.25, 710.25), (38.25, 714.75, 557.25, 735.75),
            (38.25, 740.25, 557.25, 760.5), (38.25, 765.0, 557.25, 785.25)]
    result = _scan(_pdf(tmp_path / "fax.pdf", rows))
    assert result["status"] == "block" and REASON in result["reason_codes"]
    check = result["adjacent_parallel_frames"]
    assert check["bucket"] == "HARD_FAIL" and check["min_gap_pt"] == 8.0 == html_pdf_harness.ADJACENT_PARALLEL_FRAME_MIN_GAP_PT
    assert check["adjacent_parallel_frames_pass"] is False and check["frame_count"] == 5
    assert [item["gap_pt"] for item in check["findings"]] == [4.5, 4.5, 4.5, 4.5]
    assert all(item["orientation"] == "vertical" and item["frame_id_a"] and item["frame_id_b"] for item in check["findings"])
    assert {item["frame_id_a"] for item in check["findings"]} | {item["frame_id_b"] for item in check["findings"]} == {
        f"pdf-p1-frame-{index}" for index in range(1, 6)}


@pytest.mark.parametrize("gap", [8.0, 11.0, 27.0, 100.0, 0.0, -3.0])
def test_gaps_at_or_above_8pt_touching_or_overlapping_pass(tmp_path, gap):
    result = _scan(_pdf(tmp_path / "ok.pdf", _stack(gap, 3)))
    assert result["status"] == "pass" and REASON not in result["reason_codes"]
    assert result["adjacent_parallel_frames"]["adjacent_parallel_frames_pass"] is True
    assert result["adjacent_parallel_frames"]["findings"] == []


@pytest.mark.parametrize("gap", [0.5, 4.0, 5.0, 7.9])
def test_gaps_between_0_and_8pt_fail(tmp_path, gap):
    result = _scan(_pdf(tmp_path / "bad.pdf", _stack(gap)))
    assert result["status"] == "block" and result["reason_codes"] == [REASON]
    (finding,) = result["adjacent_parallel_frames"]["findings"]
    assert finding["gap_pt"] == pytest.approx(gap, abs=0.01) and finding["page"] == 1


def test_side_by_side_frames_of_equal_height_are_checked_on_the_horizontal_axis(tmp_path):
    side = [(40, 100, 140, 160), (144, 100, 244, 160)]
    result = _scan(_pdf(tmp_path / "side.pdf", side))
    assert result["reason_codes"] == [REASON]
    assert result["adjacent_parallel_frames"]["findings"][0]["orientation"] == "horizontal"
    assert _scan(_pdf(tmp_path / "side-ok.pdf", [(40, 100, 140, 160), (148, 100, 248, 160)]))["status"] == "pass"


def test_non_parallel_frameless_and_nested_frames_are_out_of_scope(tmp_path):
    different_width = [(40, 100, 540, 120), (40, 124, 400, 144)]                 # left aligned, widths differ
    misaligned = [(40, 100, 540, 120), (60, 124, 560, 144)]                        # same width, edges not aligned
    nested = [(40, 100, 540, 300), (40, 120, 540, 140)]                            # child inside parent (overlap)
    for name, rects in (("width", different_width), ("shift", misaligned), ("nested", nested)):
        assert _scan(_pdf(tmp_path / f"{name}.pdf", rects))["status"] == "pass", name
    frameless = _pdf(tmp_path / "frameless.pdf", [(40, 100, 540, 120)], fill_only=[(40, 124, 540, 144)])
    result = _scan(frameless)
    assert result["status"] == "pass" and result["adjacent_parallel_frames"]["frame_count"] == 1


def test_pure_geometry_helper_threshold_is_the_named_constant():
    boxes = [("a", (0, 0, 100, 20)), ("b", (0, 24, 100, 44)), ("c", (0, 52, 100, 72))]
    findings = html_pdf_harness.find_adjacent_parallel_frames(boxes)
    assert [(item["frame_id_a"], item["frame_id_b"], item["gap_pt"]) for item in findings] == [("a", "b", 4.0)]   # b-c is exactly 8pt
    assert html_pdf_harness.find_adjacent_parallel_frames(boxes, min_gap_pt=8.5)[-1]["gap_pt"] == 8.0
