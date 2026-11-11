"""Same-named font subsets map to one font group instead of an unmapped glyph.

LibreOffice splits a font into several same-named subsets once 256 glyphs are used,
so text extraction cannot tell which subset a glyph came from. Offline only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import jev_glyph_locale as locale


def _pdf(tmp_path: Path, same_name_twice: bool) -> tuple[Path, Path]:
    artifact = (tmp_path / "page.pdf").resolve()
    buf = pymupdf.Font("japan").buffer
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="JA", fontbuffer=buf)
        # Different bytes force a second font object that keeps the same BaseFont name.
        page.insert_font(fontname="JB", fontbuffer=buf + b"\0" * 4 if same_name_twice else buf)
        page.insert_text((40, 60), "日本語の文字", fontname="JA", fontsize=10)
        page.insert_text((40, 90), "漢字かな", fontname="JB", fontsize=10)
        doc.save(artifact)
    render = (tmp_path / "page.png").resolve()
    with pymupdf.open(artifact) as doc:
        doc[0].get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).save(render)
    return artifact, render


def _measure(tmp_path: Path, same_name_twice: bool) -> dict:
    artifact, render = _pdf(tmp_path, same_name_twice)
    return locale.measure(artifact, 1, [0.0, 0.0, 595.0, 842.0], {"path": str(render)})


def test_same_named_subsets_map_every_glyph_to_one_group(tmp_path):
    result = _measure(tmp_path, same_name_twice=True)
    xrefs = sorted(f["xref"] for f in result["fonts"])
    assert len(xrefs) == 2  # the fixture really has two same-named font objects
    assert result["cjk_glyph_count"] == 10
    assert {g["font_xref"] for g in result["glyphs"]} == {xrefs[0]}  # never 0 (unmapped)


def test_single_font_mapping_is_unchanged(tmp_path):
    result = _measure(tmp_path, same_name_twice=False)
    assert len(result["fonts"]) == 1
    assert {g["font_xref"] for g in result["glyphs"]} == {result["fonts"][0]["xref"]}
