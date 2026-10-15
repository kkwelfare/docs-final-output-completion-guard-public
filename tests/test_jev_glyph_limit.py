"""CJK glyph cap: regions over 600 glyphs reach Jev within the wire cap; over the new cap is insufficient_evidence.

Offline only. build_request/build_wire/build_provider_payload are pure; no provider call, no network.
The wire carries one aggregate per glyph array; its size depends on the line count (<= 64), not on the glyph count.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import jev_glyph_locale as locale, jev_post_render as jev


def _request(tmp_path: Path, glyph_count: int, per_line: int = 50) -> dict:
    """A4 page holding exactly glyph_count CJK characters; whole-page appearance region -> validated request."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact = (tmp_path / "page.pdf").resolve()
    text = ("日本語の文字" * 500)[:glyph_count]
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        for row, start in enumerate(range(0, glyph_count, per_line)):
            page.insert_text((40, 50 + row * 12), text[start:start + per_line], fontname="japan", fontsize=8)
        doc.save(artifact)
    render = (tmp_path / "page.png").resolve()
    with pymupdf.open(artifact) as doc:
        doc[0].get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).save(render)
    issue = {"id": "cjk-region-1", "rule": locale.RULE, "severity": "review", "page": 1,
             "bbox_pdf_points": [0.0, 0.0, 595.0, 842.0],
             "render_evidence": {"path": str(render), "sha256": hashlib.sha256(render.read_bytes()).hexdigest()}}
    scan = {"schema_version": "docs-layout-typography-scan-3",
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "measurement_basis": "pdf_points", "status": "review", "hard_issue_count": 0,
            "review_candidate_count": 1, "issues": [issue], "REVIEW_CANDIDATE": [issue], "HARD_FAIL": []}
    raw_scan = (tmp_path / "scan.json").resolve()
    raw_scan.write_text(json.dumps(scan), encoding="utf-8")
    return jev.build_request(artifact=artifact, raw_scan=raw_scan, artifact_set_id="a" * 64,
                             producer={"task_id": "t_producer", "run_id": 1},
                             reviewer={"task_id": "t_owner", "run_id": 2})


def _sizes(request: dict) -> tuple[int, int]:
    wire, _ = jev.build_wire(request)  # re-validates the request and enforces MAX_WIRE_BYTES
    payload, _ = jev.build_provider_payload(wire)
    return len(jev.canonical(wire)), len(jev.canonical(payload))


def test_schema_limit_matches_code_limit_and_wire_cap_is_kept():
    schema = json.loads((ROOT / "schemas" / "docs-jev-post-render-request-1.json").read_text(encoding="utf-8"))
    glyphs = (schema["properties"]["candidates"]["items"]["properties"]["metrics"]
              ["properties"]["glyph_locale"]["properties"]["glyphs"])
    assert glyphs["maxItems"] == locale.MAX_GLYPHS and locale.MAX_GLYPHS > 600
    assert jev.MAX_WIRE_BYTES == 32_000  # the transmission-size cap itself is not relaxed


@pytest.mark.parametrize("count", [640, locale.MAX_GLYPHS])
def test_region_over_600_glyphs_builds_request_within_wire_size(tmp_path, count):
    request = _request(tmp_path, count)
    measured = request["candidates"][0]["metrics"]["glyph_locale"]
    assert count > 600 and measured["cjk_glyph_count"] == count == len(measured["glyphs"])
    assert len(jev.canonical(request)) <= jev.MAX_JSON_BYTES
    wire_bytes, payload_bytes = _sizes(request)
    assert wire_bytes <= jev.MAX_WIRE_BYTES and payload_bytes <= jev.MAX_WIRE_BYTES
    wire, _ = jev.build_wire(request)
    sent = wire["candidates"][0]["metrics"]["glyph_locale"]
    assert "glyphs" not in sent and sent["glyph_summary"]["glyph_count"] == count  # aggregate of all glyphs, no sampling


def test_wire_size_follows_line_count_not_glyph_count(tmp_path):
    small = _sizes(_request(tmp_path / "small", 640, per_line=16))[0]       # 40 lines
    large = _sizes(_request(tmp_path / "large", locale.MAX_GLYPHS, per_line=50))[0]  # 40 lines
    assert abs(large - small) <= 256


def test_worst_case_64_lines_at_new_limit_stays_within_wire_size(tmp_path):
    request = _request(tmp_path, 1984, per_line=31)  # 64 lines (the measure_pdf line cap) x 31 glyphs
    assert len(request["candidates"][0]["metrics"]["lines"]) == 64
    wire_bytes, payload_bytes = _sizes(request)
    assert wire_bytes <= jev.MAX_WIRE_BYTES and payload_bytes <= jev.MAX_WIRE_BYTES
    (tmp_path / "worst_case_wire.json").write_text(json.dumps({"wire_bytes": wire_bytes, "payload_bytes": payload_bytes,
        "glyphs": 1984, "lines": 64, "max_wire_bytes": jev.MAX_WIRE_BYTES}), encoding="utf-8")


def test_over_new_limit_is_insufficient_evidence(tmp_path):
    with pytest.raises(jev.PostRenderError, match="insufficient_evidence"):
        _request(tmp_path, locale.MAX_GLYPHS + 1)
