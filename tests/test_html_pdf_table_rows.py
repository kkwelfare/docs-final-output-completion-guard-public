"""Table-form rows: one outer frame + one divider line per row boundary (relation ``table`` group)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import os
from pathlib import Path

import pymupdf
import pytest
from jsonschema import Draft202012Validator
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import html_pdf_harness

REASON = "HTML-PDF-ADJACENT-PARALLEL-FRAMES-01"
TABLE_REASON = "HTML-PDF-TABLE-CONTRACT-01"
FAX_STYLE = {"border_color": "#595959", "border_width_pt": 1.5, "background_color": "#ffffff"}
X, TOP, WIDTH, ROW_HEIGHT = 38.0, 100.0, 520.0, 22.0
SCALE_100DPI = 100 / 72
BEFORE_HARNESS = Path(os.environ.get("DOCS_GUARD_BEFORE_HARNESS", "/nonexistent/html_pdf_harness.py"))


def _chrome() -> str:
    binary = shutil.which("google-chrome") or shutil.which("chromium")
    assert binary, "headless Chrome is required for the table form E2E fixture"
    return binary


def _render(html_path: Path, pdf_path: Path) -> None:
    completed = subprocess.run([_chrome(), "--headless", "--no-sandbox", "--disable-gpu",
                                "--print-to-pdf=" + str(pdf_path), html_path.as_uri()],
                               capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _rows(count: int = 5, *, table: bool = True, gap: float = 0.0, style=FAX_STYLE, group: str = "fax-entry") -> list[dict]:
    frames = []
    for k in range(count):
        y = TOP + k * (ROW_HEIGHT + gap)
        relation = {"target_id": f"row-{k}", "target_kind": "shape", "target_bbox_pt": [X, y, WIDTH, ROW_HEIGHT],
                    "relation": "inside", "min_gap_pt": 0}
        if style is not None:
            relation["surface_style"] = dict(style)
        if table:
            relation["table"] = {"group_id": group, "row_index": k}
        frames.append({"text": "確認", "frame_id": f"row-text-{k}", "page": 1, "frame_bbox_pt": (X + 6, y + 2, 200, 18),
                       "margins_pt": (4, 4), "font_size_pt": 12, "max_lines": 1, "relations": [relation]})
    return frames


def _black_runs(pdf_path: Path, x_pt: float, y0_pt: float, y1_pt: float) -> int:
    with pymupdf.open(pdf_path) as document:
        pixmap = document[0].get_pixmap(dpi=100, colorspace=pymupdf.csGRAY, alpha=False)
        image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
    runs, inside = 0, False
    for row in range(int(y0_pt * SCALE_100DPI), int(y1_pt * SCALE_100DPI) + 1):
        black = image.getpixel((int(x_pt * SCALE_100DPI), row)) < 128
        runs += int(black and not inside)
        inside = black
    return runs


def _schema_errors(receipt: dict) -> list:
    schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-4.json").read_text(encoding="utf-8"))
    return list(Draft202012Validator(schema).iter_errors(receipt))


def _produce(tmp_path: Path, name: str, frames: list[dict]):
    html_path, receipt_path, pdf_path = tmp_path / f"{name}.html", tmp_path / f"{name}.json", tmp_path / f"{name}.pdf"
    html_pdf_harness.write_controlled_html_frames(frames, html_path, receipt_path)
    _render(html_path, pdf_path)
    return html_path, receipt_path, pdf_path, html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)


def test_table_form_rows_pass_on_real_chrome_pdf_with_one_outer_frame_and_row_checks(tmp_path):
    html_path, receipt_path, pdf_path, bound = _produce(tmp_path, "table", _rows(5))
    assert bound["status"] == "pass" and bound["reason_codes"] == []
    assert bound["layout_contract"]["relation_pass"] is True and bound["layout_contract"]["alignment_pass"] is True
    rendered = bound["rendered_boundary_check"]
    adjacent = rendered["adjacent_parallel_frames"]
    assert adjacent["adjacent_parallel_frames_pass"] is True and adjacent["findings"] == []
    assert adjacent["frame_count"] == 1                      # one outer frame, not five stacked frames
    checks = [check for frame in rendered["frames"] for check in frame["relation_checks"]]
    assert [check["table_row_index"] for check in checks] == [0, 1, 2, 3, 4]
    assert all(check["relation_pass"] is True and check["sample_count"] == 1 and check["effective_gap_pt"] >= 0 for check in checks)
    tables = [frame["relations"][0]["table"] for frame in bound["frames"]]
    assert all(table["row_count"] == 5 and table["outer_bbox_pt"] == [38.0, 100.0, 520.0, 110.0] for table in tables)
    assert len({table["group_id_hash"] for table in tables}) == 1 and "fax-entry" not in receipt_path.read_text(encoding="utf-8")
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == []
    assert _schema_errors(bound) == []
    source = html_path.read_text(encoding="utf-8")
    assert source.count('id="safe-wrap-table-1"') == 1 and source.count("-divider-") == 8   # 4 dividers x (css + markup)
    assert 'id="safe-wrap-surface-' not in source          # rows are not drawn as individual frames


def test_separate_row_frames_fail_with_two_lines_per_boundary_while_table_form_has_exactly_one(tmp_path):
    _, separate_receipt, separate_pdf, separate = _produce(tmp_path, "separate", _rows(5, table=False, gap=4.5))
    separate_html = tmp_path / "separate.html"
    assert separate["status"] == "block" and separate["reason_codes"] == [REASON]
    adjacent = separate["rendered_boundary_check"]["adjacent_parallel_frames"]
    assert adjacent["adjacent_parallel_frames_pass"] is False and adjacent["frame_count"] == 5 and len(adjacent["findings"]) == 4
    assert all(item["orientation"] == "vertical" and 0 < item["gap_pt"] < 8.0 for item in adjacent["findings"])
    assert html_pdf_harness.validate_html_receipt(separate_receipt, separate_html, separate_pdf) == [REASON]
    _, _, table_pdf, table = _produce(tmp_path, "table2", _rows(5))
    assert table["status"] == "pass"
    separate_boundary = TOP + ROW_HEIGHT + 4.5 / 2
    table_boundary = TOP + ROW_HEIGHT
    assert _black_runs(separate_pdf, 400, separate_boundary - 8, separate_boundary + 8) == 2
    assert _black_runs(table_pdf, 400, table_boundary - 8, table_boundary + 8) == 1


def test_table_row_text_straddling_a_divider_still_fails_the_row_containment_check(tmp_path):
    frames = _rows(3)
    frames[0]["frame_bbox_pt"] = (X + 6, TOP + 14, 200, 18)   # the glyphs run past the row-0 divider
    _, _, _, bound = _produce(tmp_path, "straddle", frames)
    assert bound["status"] == "block" and "HTML-PDF-RELATION-DRIFT-01" in bound["reason_codes"]
    first = bound["rendered_boundary_check"]["frames"][0]["relation_checks"][0]
    assert first["relation_pass"] is False and first["effective_gap_pt"] < 0 and first["table_row_index"] == 0


def test_unspecified_table_keeps_html_and_receipt_unchanged(tmp_path):
    plain_html, plain_receipt = tmp_path / "plain.html", tmp_path / "plain.json"
    html_pdf_harness.write_controlled_html_frames(_rows(3, table=False, gap=26.0), plain_html, plain_receipt)
    assert "safe-wrap-table" not in plain_html.read_text(encoding="utf-8") and '"table"' not in plain_receipt.read_text(encoding="utf-8")
    none_frames = _rows(3, table=False, gap=26.0)
    for frame in none_frames:
        frame["relations"][0]["table"] = None            # explicit None means unspecified
    none_html, none_receipt = tmp_path / "none.html", tmp_path / "none.json"
    html_pdf_harness.write_controlled_html_frames(none_frames, none_html, none_receipt)
    assert none_html.read_bytes() == plain_html.read_bytes() and none_receipt.read_bytes() == plain_receipt.read_bytes()


@pytest.mark.skipif(not BEFORE_HARNESS.is_file(), reason="pre-change harness backup is not available on this machine")
def test_unspecified_table_is_byte_identical_to_the_pre_change_harness(tmp_path):
    spec = importlib.util.spec_from_file_location("html_pdf_harness_before", BEFORE_HARNESS)
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    cases = {"plain": _rows(3, table=False, gap=26.0), "unstyled": _rows(2, table=False, gap=11.0, style=None),
             "gap8": _rows(2, table=False, gap=8.0), "legacy-v2": [{"text": "確認", "frame_id": "a", "page": 1,
                                                                  "frame_bbox_pt": (40, 40, 60, 24), "margins_pt": (4, 4),
                                                                  "font_size_pt": 12, "max_lines": 1}]}
    for name, frames in cases.items():
        old_html, old_receipt = tmp_path / f"{name}-old.html", tmp_path / f"{name}-old.json"
        new_html, new_receipt = tmp_path / f"{name}-new.html", tmp_path / f"{name}-new.json"
        before.write_controlled_html_frames(json.loads(json.dumps(frames)), old_html, old_receipt)
        html_pdf_harness.write_controlled_html_frames(json.loads(json.dumps(frames)), new_html, new_receipt)
        assert new_html.read_bytes() == old_html.read_bytes(), name
        assert new_receipt.read_bytes() == old_receipt.read_bytes(), name


@pytest.mark.parametrize("bad", [
    "t", ["g", 0], 1, {}, {"group_id": "g"}, {"row_index": 0}, {"group_id": "g", "row_index": 0, "extra": 1},
    {"group_id": "", "row_index": 0}, {"group_id": " g", "row_index": 0}, {"group_id": 3, "row_index": 0},
    {"group_id": "g", "row_index": -1}, {"group_id": "g", "row_index": True}, {"group_id": "g", "row_index": "0"},
    {"group_id": "g", "row_index": 1.0}, {"group_id": "g", "row_index": None},
])
def test_invalid_table_declarations_are_rejected(bad):
    with pytest.raises(ValueError, match=TABLE_REASON):
        html_pdf_harness.plan_html_frame(
            "確認", frame_id="bad", page=1, frame_bbox_pt=(40, 40, 60, 24), margins_pt=(4, 4), font_size_pt=12, max_lines=1,
            relations=[{"target_id": "x", "target_kind": "shape", "target_bbox_pt": (36, 36, 68, 32),
                        "relation": "inside", "min_gap_pt": 0, "table": bad}])


@pytest.mark.parametrize("kind, relation", [("image", "inside"), ("shape", "outside"), ("surface", "no_overlap")])
def test_table_rows_must_be_inside_surfaces_or_shapes(kind, relation):
    with pytest.raises(ValueError, match=TABLE_REASON):
        html_pdf_harness.plan_html_frame(
            "確認", frame_id="bad", page=1, frame_bbox_pt=(40, 40, 60, 24), margins_pt=(4, 4), font_size_pt=12, max_lines=1,
            relations=[{"target_id": "x", "target_kind": kind, "target_bbox_pt": (36, 36, 68, 32), "relation": relation,
                        "min_gap_pt": 0, "table": {"group_id": "g", "row_index": 0}}])


def _mutate(frames: list[dict], index: int, **changes) -> list[dict]:
    relation = frames[index]["relations"][0]
    for key, value in changes.items():
        if key == "bbox_width":
            relation["target_bbox_pt"][2] = value
        elif key == "row_index":
            relation["table"]["row_index"] = value
        else:
            relation[key] = value
    return frames


@pytest.mark.parametrize("name, frames", [
    ("single-row", _rows(1)), ("vertical-gap", _rows(3, gap=2.0)), ("overlapping-rows", _rows(3, gap=-2.0)),
    ("index-skips", _mutate(_rows(3), 2, row_index=3)), ("duplicate-index", _mutate(_rows(3), 1, row_index=0)),
    ("different-width", _mutate(_rows(3), 1, bbox_width=500.0)),
    ("different-style", _mutate(_rows(3), 1, surface_style={**FAX_STYLE, "border_width_pt": 2.0})),
    ("different-kind", _mutate(_rows(3), 1, target_kind="surface")),
])
def test_invalid_table_groups_fail_closed(tmp_path, name, frames):
    with pytest.raises(ValueError, match=TABLE_REASON):
        html_pdf_harness.write_controlled_html_frames(frames, tmp_path / f"{name}.html", tmp_path / f"{name}.json")


def test_tampered_table_record_fails_closed(tmp_path):
    html_path, receipt_path = tmp_path / "t.html", tmp_path / "t.json"
    html_pdf_harness.write_controlled_html_frames(_rows(3), html_path, receipt_path)
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path) == []
    for field, value in (("row_count", 5), ("row_count", None), ("outer_bbox_pt", [38.0, 100.0, 520.0, 111.0]),
                         ("outer_bbox_pt", "x"), ("group_id_hash", "short"), ("row_index", 7)):
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        data["frames"][1]["relations"][0]["table"][field] = value
        mutated = tmp_path / f"tamper-{field}.json"
        mutated.write_text(json.dumps(data), encoding="utf-8")
        assert html_pdf_harness.validate_html_receipt(mutated, html_path) == [TABLE_REASON], (field, value)
