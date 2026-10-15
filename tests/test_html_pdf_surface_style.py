from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from jsonschema import Draft202012Validator
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import html_pdf_harness

LEGACY_SHAPE_PAINT = "background:#e6e9ed;border:0.5pt solid #c5cbd3;}"
LEGACY_IMAGE_PAINT = "background:#e6e9ed;}"
# FAX-safe example: mid-gray line, 1.5pt (Chrome snaps to 2 CSS px), white fill.
FAX_STYLE = {"border_color": "#595959", "border_width_pt": 1.5, "background_color": "#ffffff"}
THRESHOLD = 128
SCALE_100DPI = 100 / 72


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chrome() -> str:
    binary = shutil.which("google-chrome") or shutil.which("chromium")
    assert binary, "headless Chrome is required for the surface style E2E fixture"
    return binary


def _render(html_path: Path, pdf_path: Path) -> None:
    completed = subprocess.run([_chrome(), "--headless", "--no-sandbox", "--disable-gpu",
                                "--print-to-pdf=" + str(pdf_path), html_path.as_uri()],
                               capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _frame(index: int, x: float, y: float, style=None, kind: str = "shape", target_id: str | None = None) -> dict:
    relation = {"target_id": target_id or f"surface-{index}", "target_kind": kind,
                "target_bbox_pt": [x - 4, y - 4, 68, 32], "relation": "inside", "min_gap_pt": 0}
    if style is not None:
        relation["surface_style"] = style
    return {"text": "確認", "frame_id": f"frame-{index}", "page": 1, "frame_bbox_pt": (x, y, 60, 24),
            "margins_pt": (4, 4), "font_size_pt": 12, "max_lines": 1, "relations": [relation]}


def _grid(style) -> list[dict]:
    # eight frames at different sub-pixel phases so one lucky pixel alignment cannot hide a lost line
    # column pitch 80pt (frames are 68pt wide -> 12pt gaps): same-size bordered frames closer than 8pt are the
    # HTML-PDF-ADJACENT-PARALLEL-FRAMES-01 defect; this test is about the border surviving binarization, not the gap
    return [_frame(k, 24 + (k % 4) * 80 + k * 0.09, 24 + (k // 4) * 44 + k * 0.09, style) for k in range(8)]


def _gray_100dpi(pdf_path: Path) -> Image.Image:
    with pymupdf.open(pdf_path) as document:
        pixmap = document[0].get_pixmap(dpi=100, colorspace=pymupdf.csGRAY, alpha=False)
        return Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)


def _side_black_runs(image: Image.Image, target_bbox_pt: list[float], width_pt: float) -> dict[str, dict[str, float]]:
    """Per side: share of positions with a black (<128) pixel in the border band and the longest unbroken run."""
    x0, y0 = target_bbox_pt[0] * SCALE_100DPI, target_bbox_pt[1] * SCALE_100DPI
    x1 = (target_bbox_pt[0] + target_bbox_pt[2]) * SCALE_100DPI
    y1 = (target_bbox_pt[1] + target_bbox_pt[3]) * SCALE_100DPI
    band = width_pt * SCALE_100DPI
    result: dict[str, dict[str, float]] = {}
    for side in ("top", "bottom", "left", "right"):
        if side in {"top", "bottom"}:
            base = y0 if side == "top" else y1 - band
            lo, hi = int(base - 1.5), int(base + band + 1.5) + 1
            hits = [any(image.getpixel((c, r)) < THRESHOLD for r in range(lo, hi + 1))
                    for c in range(int(x0) + 3, int(x1) - 3)]
        else:
            base = x0 if side == "left" else x1 - band
            lo, hi = int(base - 1.5), int(base + band + 1.5) + 1
            hits = [any(image.getpixel((c, r)) < THRESHOLD for c in range(lo, hi + 1))
                    for r in range(int(y0) + 3, int(y1) - 3)]
        longest = run = 0
        for hit in hits:
            run = run + 1 if hit else 0
            longest = max(longest, run)
        result[side] = {"hit_ratio": sum(hits) / len(hits), "longest_run_ratio": longest / len(hits)}
    return result


def _schema_errors(receipt: dict) -> list:
    schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-4.json").read_text(encoding="utf-8"))
    return list(Draft202012Validator(schema).iter_errors(receipt))


def test_unspecified_surface_style_keeps_legacy_paint_and_adds_no_receipt_key(tmp_path):
    html_path, receipt_path = tmp_path / "plain.html", tmp_path / "plain.json"
    receipt = html_pdf_harness.write_controlled_html_frames(
        [_frame(1, 36, 36), _frame(2, 36, 100, kind="image")], html_path, receipt_path)
    source = html_path.read_text(encoding="utf-8")
    assert source.count(LEGACY_SHAPE_PAINT) == 1 and source.count(LEGACY_IMAGE_PAINT) == 1
    assert all("surface_style" not in relation for frame in receipt["frames"] for relation in frame["relations"])
    assert _schema_errors(receipt) == []
    # an explicit None means "unspecified": byte-identical HTML and receipt to omitting the key
    none_html, none_receipt = tmp_path / "none.html", tmp_path / "none.json"
    frames = [_frame(1, 36, 36), _frame(2, 36, 100, kind="image")]
    for frame in frames:
        frame["relations"][0]["surface_style"] = None
    html_pdf_harness.write_controlled_html_frames(frames, none_html, none_receipt)
    assert none_html.read_bytes() == html_path.read_bytes()
    assert none_receipt.read_bytes() == receipt_path.read_bytes()


def test_explicit_surface_style_is_written_normalized_and_recorded_in_plan(tmp_path):
    html_path, receipt_path = tmp_path / "styled.html", tmp_path / "styled.json"
    requested = {"border_color": "#595959", "border_width_pt": 2, "background_color": "#FFFFFF"}
    receipt = html_pdf_harness.write_controlled_html_frames([_frame(1, 36, 36, requested)], html_path, receipt_path)
    source = html_path.read_text(encoding="utf-8")
    assert "background:#ffffff;border:2.0pt solid #595959;}" in source
    assert "#e6e9ed" not in source and "#c5cbd3" not in source
    recorded = receipt["frames"][0]["relations"][0]["surface_style"]
    assert recorded == {"border_color": "#595959", "border_width_pt": 2.0, "background_color": "#ffffff"}
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["frames"][0]["relations"][0]["surface_style"] == recorded
    assert receipt["source_sha256"] == _sha(html_path)
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path) == []
    assert _schema_errors(receipt) == []
    # partial declarations only override what was named; the rest keeps the legacy value
    only_bg = tmp_path / "only-bg.html"
    html_pdf_harness.write_controlled_html_frames(
        [_frame(1, 36, 36, {"background_color": "#ffffff"})], only_bg, tmp_path / "only-bg.json")
    assert "background:#ffffff;border:0.5pt solid #c5cbd3;}" in only_bg.read_text(encoding="utf-8")
    only_width = tmp_path / "only-width.html"
    html_pdf_harness.write_controlled_html_frames(
        [_frame(1, 36, 36, {"border_width_pt": 1.5})], only_width, tmp_path / "only-width.json")
    assert "background:#e6e9ed;border:1.5pt solid #c5cbd3;}" in only_width.read_text(encoding="utf-8")


def test_surface_style_survives_100dpi_threshold_128_binarization_on_real_pdf(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "fax.html", tmp_path / "fax.json", tmp_path / "fax.pdf"
    frames = _grid(FAX_STYLE)
    html_pdf_harness.write_controlled_html_frames(frames, html_path, receipt_path)
    _render(html_path, pdf_path)
    bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    # the boundary / relation verdicts are not weakened by the explicit style
    assert bound["status"] == "pass" and bound["reason_codes"] == []
    assert bound["layout_contract"]["relation_pass"] is True and bound["layout_contract"]["alignment_pass"] is True
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == []
    assert _schema_errors(bound) == []
    image = _gray_100dpi(pdf_path)
    for frame in frames:
        sides = _side_black_runs(image, frame["relations"][0]["target_bbox_pt"], FAX_STYLE["border_width_pt"])
        for side, measured in sides.items():
            assert measured["hit_ratio"] >= 0.99 and measured["longest_run_ratio"] >= 0.99, (frame["frame_id"], side, measured)


def test_default_paint_loses_its_border_at_100dpi_threshold_128_as_control(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "default.html", tmp_path / "default.json", tmp_path / "default.pdf"
    frames = _grid(None)
    html_pdf_harness.write_controlled_html_frames(frames, html_path, receipt_path)
    _render(html_path, pdf_path)
    assert html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)["status"] == "pass"
    image = _gray_100dpi(pdf_path)
    for frame in frames:
        sides = _side_black_runs(image, frame["relations"][0]["target_bbox_pt"], 0.5)
        assert all(measured["hit_ratio"] <= 0.02 for measured in sides.values()), (frame["frame_id"], sides)


@pytest.mark.parametrize("bad", [
    {"border_color": "red"}, {"border_color": "#fff"}, {"border_color": "#12345g"}, {"border_color": "#1234567"},
    {"border_color": 0x595959}, {"border_color": None}, {"background_color": "rgb(0,0,0)"},
    {"background_color": " #ffffff"}, {"background_color": "#ffffff\n"}, {"background_color": "#ffffff;position:fixed"},
    {"border_width_pt": 0}, {"border_width_pt": -1}, {"border_width_pt": float("nan")},
    {"border_width_pt": float("inf")}, {"border_width_pt": True}, {"border_width_pt": "1"},
    {"border_width_pt": None}, {"border_width_pt": 1e-9}, {}, {"border_style": "dotted"},
    {"border_color": "#000000", "extra": 1}, "none", ["#000000"], 3,
])
def test_invalid_surface_style_values_are_rejected(bad):
    with pytest.raises(ValueError, match="HTML-PDF-RELATION-CONTRACT-01"):
        html_pdf_harness.plan_html_frame(
            "確認", frame_id="bad", page=1, frame_bbox_pt=(40, 40, 60, 24), margins_pt=(4, 4), font_size_pt=12, max_lines=1,
            relations=[{"target_id": "x", "target_kind": "shape", "target_bbox_pt": (36, 36, 68, 32),
                        "relation": "inside", "min_gap_pt": 0, "surface_style": bad}])


def test_conflicting_or_tampered_surface_styles_fail_closed(tmp_path):
    other = {"border_color": "#000000", "border_width_pt": 1.5, "background_color": "#ffffff"}
    with pytest.raises(ValueError, match="HTML-PDF-RELATION-CONTRACT-01"):
        html_pdf_harness.write_controlled_html_frames(
            [_frame(1, 36, 36, FAX_STYLE, target_id="shared"), _frame(2, 36, 36, other, target_id="shared")],
            tmp_path / "conflict.html", tmp_path / "conflict.json")
    shared_html = tmp_path / "shared.html"
    html_pdf_harness.write_controlled_html_frames(
        [_frame(1, 36, 36, FAX_STYLE, target_id="shared"), _frame(2, 36, 36, FAX_STYLE, target_id="shared")],
        shared_html, tmp_path / "shared.json")
    assert shared_html.read_text(encoding="utf-8").count('id="safe-wrap-surface-') == 1
    html_path, receipt_path = tmp_path / "tamper.html", tmp_path / "tamper.json"
    html_pdf_harness.write_controlled_html_frames([_frame(1, 36, 36, FAX_STYLE)], html_path, receipt_path)
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path) == []
    for field, value in (("border_color", "#ABCDEF"), ("border_width_pt", 0), ("border_width_pt", "1.5"), ("unknown", 1)):
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        data["frames"][0]["relations"][0]["surface_style"][field] = value
        mutated = tmp_path / f"tamper-{field}.json"
        mutated.write_text(json.dumps(data), encoding="utf-8")
        assert html_pdf_harness.validate_html_receipt(mutated, html_path) == ["HTML-PDF-RELATION-CONTRACT-01"], (field, value)
