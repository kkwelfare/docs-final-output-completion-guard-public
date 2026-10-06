"""Focused ordinary-HTML baseline regressions using isolated readbacks."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from quality_rules import ordinary_html_baseline


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": digest(path)}


def make_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    artifact = tmp_path / "one.html"
    artifact.write_text("<!doctype html><title>one</title><p>sample</p>", encoding="utf-8")
    receiver = tmp_path / "received.html"
    shutil.copyfile(artifact, receiver)
    render = tmp_path / "render.png"
    from PIL import Image
    Image.new("RGB", (32, 24), "white").save(render)
    browser = tmp_path / "browser.json"
    return artifact, render, browser


def write_evidence(tmp_path: Path, *, artifact: Path, render: Path, browser_path: Path) -> Path:
    from quality_rules import finalization
    receiver = tmp_path / "received.html"
    final_output = tmp_path / "final-output.json"
    final_output.write_text(json.dumps({
        "schema_version": "docs-final-output-receipt-1", "status": "pass",
        "entries": [{"artifact_path": str(artifact.resolve()), "artifact_sha256": digest(artifact), "status": "pass"}],
    }), encoding="utf-8")
    final_output_handle = handle(final_output)
    manifest_path = tmp_path / "manifest.json"
    receiver_path = tmp_path / "receiver.json"
    # Build/read back the exact strict identity manifest using the production validator.
    from quality_rules import finalization
    manifest = finalization.build_manifest(
        [str(artifact)], source=str(final_output), checker=str(ROOT / "check_docs_artifact.py"),
        created_by_task_id="fixture-task", created_by_run_id=7, state="accepted",
    )
    manifest_path.write_text(json.dumps(manifest,sort_keys=True), encoding="utf-8")
    receiver_record = {"status":"accepted","artifact_set_id":manifest["artifact_set_id"],
        "receiver_task_id":"fixture-task","receiver_run_id":7,
        "copies":[{"canonical_path":str(artifact.resolve()),"copy_path":str(receiver.resolve()),
            "sha256":digest(receiver),"size_bytes":receiver.stat().st_size,
            "media_type":finalization.media_type(artifact)}]}
    receiver_path.write_text(json.dumps(receiver_record,sort_keys=True),encoding="utf-8")
    browser_record = {
        "schema_version":"ordinary-html-browser-receipt-1", "artifact_sha256":digest(artifact),
        "url":artifact.resolve().as_uri(), "task_id":"fixture-task", "run_id":7,
        "page_errors":[], "console_errors":[], "external_resources":[],
        "checks":{"page_load":True,"interaction":True,"pause_resume":True,"grounding":True,"finish":True},
        "render":handle(render),
    }
    browser_path.write_text(json.dumps(browser_record,sort_keys=True),encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "schema_version":"ordinary-html-baseline-1", "artifact":handle(artifact),
        "browser":handle(browser_path), "render":handle(render),
        "final_output_receipts":[final_output_handle],
        "finalization_manifest":{"path":str(manifest_path.resolve()),"receiver_receipt":str(receiver_path.resolve())},
        "checks":browser_record["checks"], "applicability":{},
    }),encoding="utf-8")
    return contract, artifact, browser_path


def test_single_html_baseline_passes_real_bound_handles(tmp_path):
    artifact, render, browser = make_case(tmp_path)
    contract, artifact, _ = write_evidence(tmp_path,artifact=artifact,render=render,browser_path=browser)
    assert ordinary_html_baseline.validate(contract,[str(artifact)]) is None


def test_html_baseline_rejects_missing_browser_readback(tmp_path):
    artifact, render, browser = make_case(tmp_path)
    contract, artifact, _ = write_evidence(tmp_path,artifact=artifact,render=render,browser_path=browser)
    value=json.loads(contract.read_text()); value["browser"]["path"]=str(tmp_path/"missing.json")
    contract.write_text(json.dumps(value),encoding="utf-8")
    assert ordinary_html_baseline.validate(contract,[str(artifact)]) == "real-browser receipt is missing"


def test_html_baseline_rejects_artifact_hash_drift(tmp_path):
    artifact, render, browser = make_case(tmp_path)
    contract, artifact, _ = write_evidence(tmp_path,artifact=artifact,render=render,browser_path=browser)
    artifact.write_text("<!doctype html><title>drift</title>",encoding="utf-8")
    assert ordinary_html_baseline.validate(contract,[str(artifact)]) == "HTML artifact hash drift"


def test_html_baseline_rejects_false_interaction_check(tmp_path):
    artifact, render, browser = make_case(tmp_path)
    contract, artifact, browser = write_evidence(tmp_path,artifact=artifact,render=render,browser_path=browser)
    value=json.loads(browser.read_text()); value["checks"]["interaction"]=False
    browser.write_text(json.dumps(value),encoding="utf-8")
    c=json.loads(contract.read_text()); c["browser"]=handle(browser); c["checks"]=value["checks"]
    contract.write_text(json.dumps(c),encoding="utf-8")
    assert ordinary_html_baseline.validate(contract,[str(artifact)]) == "real-browser representative checks are missing or failed"


def test_html_baseline_rejects_non_html_or_multiple_artifacts(tmp_path):
    artifact, render, browser = make_case(tmp_path)
    contract, artifact, _ = write_evidence(tmp_path,artifact=artifact,render=render,browser_path=browser)
    assert ordinary_html_baseline.validate(contract,[str(artifact),str(artifact)]) == "ordinary-HTML baseline artifact coverage mismatch"
    assert ordinary_html_baseline.validate(contract,[str(artifact.with_suffix('.docx'))]) == "ordinary-HTML baseline artifact coverage mismatch"


def test_docx_baseline_route_remains_registered():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert 'contract.get("schema_version") in {"ordinary-docx-baseline-1", "ordinary-html-baseline-1"}' in source
    assert 'from quality_rules import ordinary_docx_baseline' in source
    assert 'baseline_error = ordinary_docx_baseline.validate(contract_path, artifacts)' in source


def test_html_baseline_normal_hook_accepts_synthetic_route_and_blocks_missing_browser(tmp_path):
    artifact, render, browser = make_case(tmp_path)
    contract, artifact, _ = write_evidence(tmp_path, artifact=artifact, render=render, browser_path=browser)
    from quality_rules import finalization
    from canonical_checker import check_document
    spec = importlib.util.spec_from_file_location("ordinary_html_fixture_guard", ROOT / "__init__.py")
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    spec.loader.exec_module(guard)
    final_receipt = contract.with_name("final-output.json")
    checker_root = ROOT / "canonical_checker"
    source_path = contract.with_name("source.txt")
    source_path.write_text("Synthetic fixture source.\n", encoding="utf-8")
    checker = checker_root / "check_document.py"
    core = checker_root / "filter_core.py"
    final_receipt.write_text(json.dumps({
        "schema_version": "docs-final-output-receipt-1", "status": "pass",
        "canonical_checker_path": str(checker), "canonical_core_path": str(core),
        "canonical_checker_sha256": digest(checker), "canonical_core_sha256": digest(core),
        "entries": [{"artifact_path": str(artifact.resolve()), "artifact_sha256": digest(artifact),
                     "status": "pass", "kind": "text", "source_text_path": str(source_path.resolve()),
                     "source_text_sha256": digest(source_path), "final_readback": {"changed": False}}],
    }), encoding="utf-8")
    manifest_path = contract.with_name("manifest.json")
    receiver_path = contract.with_name("receiver.json")
    manifest = finalization.build_manifest(
        [str(artifact)], source=str(final_receipt), checker=str(checker),
        created_by_task_id="fixture-task", created_by_run_id=7, state="accepted",
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    receiver_path.write_text(json.dumps({
        "status": "accepted", "artifact_set_id": manifest["artifact_set_id"],
        "receiver_task_id": "fixture-task", "receiver_run_id": 7,
        "copies": [{"canonical_path": str(artifact.resolve()), "copy_path": str(artifact.resolve()),
                    "sha256": digest(artifact), "size_bytes": artifact.stat().st_size,
                    "media_type": finalization.media_type(artifact)}],
    }, sort_keys=True), encoding="utf-8")
    contract_value = json.loads(contract.read_text(encoding="utf-8"))
    contract_value["final_output_receipts"] = [handle(final_receipt)]
    contract_value["finalization_manifest"] = {"path": str(manifest_path.resolve()), "receiver_receipt": str(receiver_path.resolve())}
    contract.write_text(json.dumps(contract_value), encoding="utf-8")
    args = {
        "artifacts": [str(artifact)],
        "metadata": {"verification": {
            "final_output_filter": {"receipts": [str(final_receipt)]},
            "artifact_quality": {"contract": str(contract)},
        }},
    }
    # This is a synthetic contract/hook integration fixture, not a browser run.
    assert guard._pre_tool_call(tool_name="kanban_complete", args=args, task_id="t_fixture") is None
    value = json.loads(contract.read_text())
    value["browser"]["path"] = str(tmp_path / "missing-browser.json")
    contract.write_text(json.dumps(value), encoding="utf-8")
    result = guard._pre_tool_call(tool_name="kanban_complete", args=args, task_id="t_fixture")
    assert isinstance(result, dict) and result.get("action") == "block"
    assert "real-browser receipt is missing" in result.get("message", "")


def test_html_baseline_rejects_docx_contract_schema(tmp_path):
    contract=tmp_path/"contract.json"
    contract.write_text(json.dumps({"schema_version":"ordinary-docx-baseline-1"}),encoding="utf-8")
    assert ordinary_html_baseline.validate(contract,[str(tmp_path/"one.html")]) == "ordinary-HTML baseline contract schema mismatch"
