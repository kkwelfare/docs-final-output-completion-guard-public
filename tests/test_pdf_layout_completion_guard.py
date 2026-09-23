from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_guard():
    spec = importlib.util.spec_from_file_location("docs_pdf_layout_guard", ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(pdf: Path, contract: Path, quality_receipts):
    return {
        "artifacts": [str(pdf)],
        "metadata": {"verification": {
            "final_output_filter": {"receipts": ["/synthetic/final-receipt.json"]},
            "artifact_quality": {"contract": str(contract), "receipts": quality_receipts},
        }},
    }


def test_completion_guard_blocks_pdf_contract_without_layout_config(tmp_path, monkeypatch):
    guard = _load_guard()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    pdf = tmp_path / "output.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "artifact_kind": "pdf",
        "artifacts": [str(pdf)],
        "artifact_quality": {"required": True},
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "_validate_receipt", lambda _: ({str(pdf.resolve())}, None))
    result = guard._pre_tool_call(tool_name="kanban_complete", args=_args(pdf, contract, ["/synthetic/quality.json"]))
    assert result == {
        "action": "block",
        "message": "docs artifact-quality guard: required target must declare finalization_manifest with manifest and receiver receipt",
    }


def test_completion_guard_hook_blocks_required_contract_without_finalization_manifest(tmp_path, monkeypatch):
    """Exercise the real completion pre-tool hook, not the validator directly."""
    guard = _load_guard()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    pdf = tmp_path / "output.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "artifact_kind": "pdf",
        "artifacts": [str(pdf)],
        "artifact_quality": {"required": True, "layout_typography": {"required": True}},
        "finalization_manifest": {},
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "_validate_receipt", lambda _: ({str(pdf.resolve())}, None))
    result = guard._pre_tool_call(tool_name="kanban_complete", args=_args(pdf, contract, ["/synthetic/quality.json"]))
    assert result == {
        "action": "block",
        "message": "docs artifact-quality guard: strict finalization_manifest requires path and receiver_receipt",
    }


def test_completion_guard_blocks_missing_artifact_quality_receipt(tmp_path, monkeypatch):
    guard = _load_guard()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    pdf = tmp_path / "output.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "artifact_kind": "pdf",
        "artifacts": [str(pdf)],
        "artifact_quality": {"required": True, "layout_typography": {"required": True}},
        "finalization_manifest": {"path": str(tmp_path / "manifest.json"), "receiver_receipt": str(tmp_path / "receiver.json")},
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "_validate_receipt", lambda _: ({str(pdf.resolve())}, None))
    result = guard._pre_tool_call(tool_name="kanban_complete", args=_args(pdf, contract, []))
    assert result and result["action"] == "block" and "contract and receipts" in result["message"]
