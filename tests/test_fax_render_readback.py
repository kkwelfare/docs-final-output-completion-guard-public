from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import fitz
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import fax


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def one_page_pdf(path: Path, pages: int = 1) -> Path:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=200, height=100)
        page.insert_text((20, 30), "FAX TEST")
    doc.save(path)
    doc.close()
    return path


def test_fax_renders_are_real_pngs_with_half_size_and_one_bit_binary(tmp_path):
    checker = load("fax_quality_recovery", "check_docs_artifact_quality.py")
    pdf = one_page_pdf(tmp_path / "fax.pdf")
    renders = checker._render_fax(pdf, tmp_path / "evidence", 128)
    with Image.open(renders["FAX-RENDER-01"]) as original, Image.open(renders["FAX-REDUCED-01"]) as reduced, Image.open(renders["FAX-BINARY-01"]) as binary:
        assert original.format == reduced.format == binary.format == "PNG"
        assert reduced.size == (original.width // 2, original.height // 2)
        assert binary.size == reduced.size and binary.mode == "1"


def test_fax_requires_one_pdf_page(tmp_path):
    pdf = one_page_pdf(tmp_path / "two-pages.pdf", pages=2)
    contract = {"artifact_kind": "fax_dm", "artifact_quality": {"extensions": ["fax"]}, "render_contract": {"fax": {"original_size": True, "reduced_scale": 0.5, "binary": {"mode": "1-bit", "threshold": 128}}}}
    errors = fax.validate_entry(contract, {"artifact_path": str(pdf), "rules": []})
    assert any("one page" in error for error in errors)


def test_fax_contract_requires_half_scale_and_explicit_one_bit_threshold():
    contract = {"artifact_kind": "fax_dm", "artifact_quality": {"extensions": ["fax"]}, "render_contract": {"fax": {"original_size": False, "reduced_scale": 1, "binary": {"mode": "gray"}}}}
    _, errors = fax._render_contract(contract)
    assert errors and any(error.startswith(("FAX-REDUCED-01", "FAX-BINARY-META-01")) for error in errors)


def test_legibility_requires_structured_pass_review_and_hash_bound_binary(tmp_path):
    pdf = one_page_pdf(tmp_path / "fax.pdf")
    binary = tmp_path / "binary.png"
    Image.new("1", (10, 10)).save(binary)
    review = tmp_path / "review.json"
    review.write_text("{}", encoding="utf-8")
    errors = fax._validate_legibility(review, pdf, binary)
    assert any(error.startswith("FAX-LEGIBILITY-01") for error in errors)


def test_binary_hash_drift_is_rejected(tmp_path):
    pdf = one_page_pdf(tmp_path / "fax.pdf")
    binary = tmp_path / "binary.png"
    Image.new("1", (10, 10)).save(binary)
    evidence = {"path": str(binary), "sha256": "0" * 64}
    _, errors = fax._evidence_path({"evidence": evidence}, "FAX-BINARY-01")
    assert any("drift" in error for error in errors)


def test_rendered_readback_module_is_available_for_quality_enabled_execution():
    module = load("rendered_readback_recovery", "quality_rules/rendered_readback.py")
    assert callable(module.build_manifest)
