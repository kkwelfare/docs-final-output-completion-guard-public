import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import check_docs_artifact_quality as checker
from quality_rules.safe_wrap import resolve_cjk_font


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(source: Path) -> dict:
    return {
        "schema_version": "docs-safe-wrap-receipt-1",
        "status": "pass",
        "source_sha256": _sha(source),
        "safe_ratio": 0.90,
        "font": resolve_cjk_font(),
        "frames": [{
            "frame_id": "frame-1",
            "status": "pass",
            "rule_ids": [],
            "frame_width": 120,
            "margins": [10, 10],
            "font_size": 12,
            "safe_width": 90,
            "line_height": 12.75,
            "total_height": 12.75,
            "line_count": 1,
            "max_lines": 2,
            "max_height": 30,
            "measured_widths": [80],
            "implicit_wrap_candidates": [],
        }],
    }


def test_safe_wrap_rule_is_pdf_only_and_accepts_bound_deterministic_receipt(tmp_path):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"authoritative-source")
    receipt_path = tmp_path / "safe-wrap.json"
    receipt_path.write_text(json.dumps(_receipt(source)), encoding="utf-8")
    contract = {"artifact_quality": {"safe_wrap": {
        "required": True,
        "receipt_path": str(receipt_path),
    }}}

    status, evidence, reasons = checker._safe_wrap_rule(contract, source, tmp_path / "output.pdf")
    assert status == "pass" and reasons == []
    assert evidence and evidence["path"] == str(receipt_path.resolve())

    status, evidence, reasons = checker._safe_wrap_rule(contract, source, tmp_path / "output.pptx")
    assert (status, evidence, reasons) == ("not_applicable", None, [])


def test_safe_wrap_rule_blocks_implicit_overflow_and_ratio_drift_with_rule_ids(tmp_path):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"authoritative-source")
    receipt = _receipt(source)
    receipt["frames"][0]["measured_widths"] = [91]
    receipt["frames"][0]["implicit_wrap_candidates"] = [{"id": "implicit-wrap-1"}]
    receipt["safe_ratio"] = 0.92
    receipt_path = tmp_path / "unsafe-wrap.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    contract = {"artifact_quality": {"safe_wrap": {
        "required": True,
        "receipt_path": str(receipt_path),
        "safe_ratio": 0.90,
    }}}

    status, _, reasons = checker._safe_wrap_rule(contract, source, tmp_path / "output.pdf")
    assert status == "fail"
    assert "SAFE-WRAP-RATIO-01" in reasons
    assert "SAFE-WRAP-IMPLICIT-OVERFLOW-01" in reasons


def test_safe_wrap_rule_blocks_height_limit_and_shrink_directive(tmp_path):
    source = tmp_path / "source.pptx"
    source.write_bytes(b"authoritative-source")
    receipt = _receipt(source)
    receipt["frames"][0]["total_height"] = 40
    receipt["frames"][0]["max_height"] = 30
    receipt["frames"][0]["auto_fit"] = True
    receipt_path = tmp_path / "height-wrap.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    contract = {"artifact_quality": {"safe_wrap": {
        "required": True,
        "receipt_path": str(receipt_path),
    }}}

    status, _, reasons = checker._safe_wrap_rule(contract, source, tmp_path / "output.pdf")
    assert status == "fail"
    assert "SAFE-WRAP-MAX-HEIGHT-01" in reasons
    assert "SAFE-WRAP-NO-FONT-SHRINK-01" in reasons
