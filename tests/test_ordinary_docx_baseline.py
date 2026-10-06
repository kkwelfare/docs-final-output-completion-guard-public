from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import ordinary_docx_baseline


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha(path)}


def write_minimal_docx(path: Path) -> None:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Portable fixture</w:t></w:r></w:p>'
        '<w:sectPr/></w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", document)


def make_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    artifact = tmp_path / "fixture.docx"
    write_minimal_docx(artifact)
    received = tmp_path / "received-copy.docx"
    shutil.copyfile(artifact, received)
    source = tmp_path / "source.txt"
    source.write_text("Portable synthetic source.\n", encoding="utf-8")

    docx_readback = tmp_path / "docx-readback.json"
    docx_readback.write_text(json.dumps({
        "status": "pass", "package_valid": True, "artifact_sha256": sha(artifact),
        "checks": {"synthetic_source_order_text_package_fixture": True},
    }), encoding="utf-8")

    pdf = tmp_path / "rendered.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Portable render fixture")
    document.save(pdf)
    document.close()
    render_readback = tmp_path / "render-readback.json"
    render_readback.write_text(json.dumps({
        "pdf": str(pdf.resolve()), "page_count": 1,
        "bounds": [{"page": 1, "block_count": 1, "last_text_bottom": 82.0}],
        "fonts": ["Helvetica"],
    }), encoding="utf-8")
    pixmap = fitz.open(pdf)[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    png = tmp_path / "representative-page.png"
    pixmap.save(png)
    with Image.open(png) as image:
        image.verify()

    final_receipt = tmp_path / "final-output-receipt.json"
    command = [
        sys.executable, str(ROOT / "check_docs_artifact.py"),
        "--artifact", str(artifact),
        "--no-applicable-reason", "Synthetic DOCX fixture has no applicable source text rewrite.",
        "--receipt", str(final_receipt),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(final_receipt.read_text(encoding="utf-8"))
    assert receipt["status"] == "pass"
    assert receipt["entries"][0]["artifact_sha256"] == sha(artifact)

    manifest = tmp_path / "finalization-manifest.json"
    receiver_receipt = tmp_path / "receiver-receipt.json"
    contract = tmp_path / "ordinary-docx-contract.json"
    command = [
        sys.executable, str(ROOT / "scripts/prepare_ordinary_docx_baseline.py"),
        "--artifact", str(artifact), "--receiver-copy", str(received),
        "--source", str(source), "--docx-readback", str(docx_readback),
        "--render-readback", str(render_readback), "--rendered-pdf", str(pdf),
        "--representative-page", str(png), "--final-output-receipt", str(final_receipt),
        "--manifest", str(manifest), "--receiver-receipt", str(receiver_receipt),
        "--contract", str(contract), "--producer-task-id", "t_fixture_producer",
        "--producer-run-id", "7", "--receiver-task-id", "t_fixture_receiver",
        "--receiver-run-id", "8",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["completion"] == "not_submitted"
    value = json.loads(contract.read_text(encoding="utf-8"))
    assert ordinary_docx_baseline.validate(contract, [str(artifact)]) is None
    return artifact, final_receipt, value


def test_helper_does_not_require_unknown_producer_identity(tmp_path):
    artifact, final_receipt, _ = make_fixture(tmp_path)
    # The helper preserves unknown provenance by omitting producer identity.
    output = tmp_path / "unknown-producer-identity"
    output.mkdir()
    manifest = output / "finalization-manifest.json"
    receiver_receipt = output / "receiver-receipt.json"
    contract = output / "ordinary-docx-contract.json"
    source = tmp_path / "source.txt"
    readback = tmp_path / "docx-readback.json"
    render_readback = tmp_path / "render-readback.json"
    pdf = tmp_path / "rendered.pdf"
    png = tmp_path / "representative-page.png"
    received = tmp_path / "received-copy.docx"
    command = [
        sys.executable, str(ROOT / "scripts/prepare_ordinary_docx_baseline.py"),
        "--artifact", str(artifact), "--receiver-copy", str(received),
        "--source", str(source), "--docx-readback", str(readback),
        "--render-readback", str(render_readback), "--rendered-pdf", str(pdf),
        "--representative-page", str(png), "--final-output-receipt", str(final_receipt),
        "--manifest", str(manifest), "--receiver-receipt", str(receiver_receipt),
        "--contract", str(contract), "--receiver-task-id", "t_fixture_receiver",
        "--receiver-run-id", "8",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ordinary_docx_baseline.validate(contract, [str(artifact)]) is None
    generated = json.loads(manifest.read_text(encoding="utf-8"))
    assert "created_by_task_id" not in generated
    assert "created_by_run_id" not in generated


def test_synthetic_docx_preparation_and_real_hook_route(tmp_path):
    artifact, final_receipt, contract = make_fixture(tmp_path)
    spec = importlib.util.spec_from_file_location("ordinary_docx_fixture_guard", ROOT / "__init__.py")
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    spec.loader.exec_module(guard)
    args = {
        "artifacts": [str(artifact)],
        "metadata": {"verification": {
            "final_output_filter": {"receipts": [str(final_receipt)]},
            "artifact_quality": {"contract": str(tmp_path / "ordinary-docx-contract.json")},
        }},
    }
    result = guard._pre_tool_call(tool_name="kanban_complete", args=args, task_id="t_fixture")
    assert result is None
    assert contract["schema_version"] == "ordinary-docx-baseline-1"


def test_wrong_artifact_and_bad_package_are_blocked(tmp_path):
    artifact, _, _ = make_fixture(tmp_path)
    other = tmp_path / "other.docx"
    write_minimal_docx(other)
    contract_path = tmp_path / "ordinary-docx-contract.json"
    assert ordinary_docx_baseline.validate(contract_path, [str(other)]) == "ordinary-DOCX baseline artifact coverage mismatch"
    other.write_bytes(b"not a DOCX zip")
    value = json.loads(contract_path.read_text(encoding="utf-8"))
    value["artifact"] = handle(other)
    contract_path.write_text(json.dumps(value), encoding="utf-8")
    assert ordinary_docx_baseline.validate(contract_path, [str(other)]) == "DOCX package readback failed"
    assert artifact.exists()


def test_missing_render_readback_and_receiver_copy_drift_are_blocked(tmp_path):
    artifact, _, _ = make_fixture(tmp_path)
    contract_path = tmp_path / "ordinary-docx-contract.json"
    value = json.loads(contract_path.read_text(encoding="utf-8"))
    value["render_readback"] = {"path": str(tmp_path / "missing-render.json"), "sha256": "0" * 64}
    contract_path.write_text(json.dumps(value), encoding="utf-8")
    assert ordinary_docx_baseline.validate(contract_path, [str(artifact)]) == "render readback is missing"

    # Rebuild a clean baseline, then mutate only the distinct received copy.
    second = tmp_path / "fresh"
    second.mkdir()
    artifact, _, _ = make_fixture(second)
    receiver = second / "received-copy.docx"
    receiver.write_bytes(b"receiver drift")
    assert ordinary_docx_baseline.validate(second / "ordinary-docx-contract.json", [str(artifact)]) == "receiver-copy-hash-drift"


def test_generic_failed_check_is_not_accepted(tmp_path):
    artifact, _, _ = make_fixture(tmp_path)
    contract_path = tmp_path / "ordinary-docx-contract.json"
    value = json.loads(contract_path.read_text(encoding="utf-8"))
    readback_path = tmp_path / "docx-readback.json"
    readback = json.loads(readback_path.read_text(encoding="utf-8"))
    readback["checks"]["synthetic_source_order_text_package_fixture"] = False
    readback_path.write_text(json.dumps(readback), encoding="utf-8")
    value["docx_readback"] = handle(readback_path)
    contract_path.write_text(json.dumps(value), encoding="utf-8")
    assert ordinary_docx_baseline.validate(contract_path, [str(artifact)]) == "DOCX baseline checks are missing, failed, or stale"
