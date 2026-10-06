"""Validate the admitted ordinary-DOCX completion baseline readbacks."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ordinary-docx-baseline-1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handle(path: Path) -> dict[str, str]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError("baseline evidence handle is not a file")
    return {"path": str(target), "sha256": sha256(target)}


def read_handle(value: Any, label: str) -> tuple[Path | None, str | None]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        return None, f"{label} handle is malformed"
    raw = value.get("path")
    path = Path(raw) if isinstance(raw, str) else Path()
    if not path.is_absolute() or not path.is_file():
        return None, f"{label} is missing"
    if value.get("sha256") != sha256(path):
        return None, f"{label} hash drift"
    return path.resolve(), None


def validate(contract_path: Path, artifacts: list[str]) -> str | None:
    if not contract_path.is_absolute() or not contract_path.is_file():
        return "ordinary-DOCX baseline contract is missing"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "ordinary-DOCX baseline contract is unreadable"
    required = {"schema_version", "artifact", "docx_readback", "render_readback", "rendered_pdf", "representative_pages", "final_output_receipts", "finalization_manifest"}
    if not isinstance(contract, dict) or set(contract) != required or contract.get("schema_version") != SCHEMA_VERSION:
        return "ordinary-DOCX baseline contract schema mismatch"

    artifact_path, error = read_handle(contract.get("artifact"), "artifact")
    if error:
        return error
    if artifact_path is None:
        return "artifact handle is missing"
    declared = [str(artifact_path)]
    expected = [str(Path(value).resolve()) for value in artifacts if isinstance(value, str)]
    if not expected or declared != expected or len(artifacts) != len(expected):
        return "ordinary-DOCX baseline artifact coverage mismatch"
    if artifact_path is None or artifact_path.suffix.lower() != ".docx":
        return "ordinary-DOCX baseline accepts only DOCX artifacts"
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            if archive.testzip() is not None or "word/document.xml" not in archive.namelist():
                return "DOCX package readback failed"
    except (OSError, zipfile.BadZipFile):
        return "DOCX package readback failed"

    docx_readback_path, error = read_handle(contract.get("docx_readback"), "DOCX readback")
    if error:
        return error
    if docx_readback_path is None:
        return "DOCX readback is missing"
    try:
        readback = json.loads(docx_readback_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "DOCX readback is malformed"
    if (not isinstance(readback, dict) or readback.get("status") != "pass"
            or readback.get("package_valid") is not True):
        return "DOCX source/order/text/package baseline did not pass"
    # Generic readback is bound to this artifact and to its actual source.
    checks = readback.get("checks")
    if checks is not None:
        if (readback.get("artifact_sha256") != sha256(artifact_path)
                or not isinstance(checks, dict) or not checks
                or any(value is not True for value in checks.values())):
            return "DOCX baseline checks are missing, failed, or stale"
    else:
        # Compatibility with the original ordered-list readback: verify its
        # reported rows against the actual OOXML, not only a claimed pass.
        import re
        from defusedxml import ElementTree as ET
        actual = readback.get("actual")
        if (readback.get("exact_order_titles_filenames_ranges_allocations") is not True
                or not isinstance(actual, list) or not actual
                or readback.get("count") != len(actual)):
            return "DOCX source/order/text/package baseline did not pass"
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        try:
            with zipfile.ZipFile(artifact_path) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
            rows = []
            for row in root.findall(".//w:tr", ns):
                cells = ["".join(t.text or "" for t in cell.findall(".//w:t", ns))
                         for cell in row.findall("w:tc", ns)]
                if cells and re.fullmatch(r"\d+\.", cells[0]):
                    paragraphs = row.findall("w:tc", ns)[1].findall("w:p", ns)
                    texts = ["".join(t.text or "" for t in p.findall(".//w:t", ns)) for p in paragraphs]
                    rows.append([int(cells[0][:-1]), "".join(texts[:-1]), texts[-1], *cells[2:]])
            normalize = lambda values: [["".join(str(v).split()) for v in row] for row in values]
            if normalize(rows) != normalize(actual):
                return "DOCX actual content/order differs from readback"
        except (IndexError, KeyError, ET.ParseError):
            return "DOCX actual content readback failed"

    final_receipts = contract.get("final_output_receipts")
    if not isinstance(final_receipts, list) or not final_receipts:
        return "canonical final-output receipt handle is missing"
    receipt_artifacts: set[str] = set()
    for index, value in enumerate(final_receipts, 1):
        receipt_path, error = read_handle(value, f"final-output receipt {index}")
        if error:
            return error
        if receipt_path is None:
            return "canonical final-output receipt is missing"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "canonical final-output receipt is malformed"
        entries = receipt.get("entries") if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict) or receipt.get("schema_version") != "docs-final-output-receipt-1"
                or receipt.get("status") != "pass" or not isinstance(entries, list) or not entries):
            return "canonical final-output receipt did not pass"
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("status") != "pass":
                return "canonical final-output receipt entry did not pass"
            path = Path(str(entry.get("artifact_path", "")))
            if not path.is_absolute() or not path.is_file() or entry.get("artifact_sha256") != sha256(path):
                return "canonical final-output receipt artifact is missing or stale"
            receipt_artifacts.add(str(path.resolve()))
    if receipt_artifacts != {str(artifact_path)}:
        return "canonical final-output receipt does not cover exactly the DOCX artifact"

    render_path, error = read_handle(contract.get("render_readback"), "render readback")
    if error:
        return error
    if render_path is None:
        return "render readback is missing"
    try:
        render = json.loads(render_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "render readback is malformed"
    bounds = render.get("bounds") if isinstance(render, dict) else None
    pages = render.get("page_count") if isinstance(render, dict) else None
    if (not isinstance(pages, int) or isinstance(pages, bool) or pages < 1
            or not isinstance(bounds, list) or len(bounds) != pages
            or not isinstance(render.get("fonts"), list) or not render["fonts"]):
        return "representative render baseline is incomplete"
    for index, item in enumerate(bounds, 1):
        if (not isinstance(item, dict) or item.get("page") != index
                or not isinstance(item.get("block_count"), int) or item["block_count"] < 1
                or not isinstance(item.get("last_text_bottom"), (int, float))):
            return "render page readback is incomplete"

    pdf_path, error = read_handle(contract.get("rendered_pdf"), "rendered PDF")
    if error:
        return error
    if pdf_path is None or pdf_path.suffix.lower() != ".pdf" or Path(str(render.get("pdf", ""))).resolve() != pdf_path:
        return "rendered PDF path does not match render readback"
    pages_value = contract.get("representative_pages")
    if not isinstance(pages_value, list) or not pages_value:
        return "representative render evidence is missing"
    try:
        from PIL import Image
        for index, value in enumerate(pages_value):
            image_path, error = read_handle(value, f"representative page {index + 1}")
            if error:
                return error
            if image_path is None:
                return f"representative page {index + 1} is missing"
            with Image.open(image_path) as image:
                image.verify()
    except (OSError, ValueError):
        return "representative page image readback failed"

    finalization_spec = contract.get("finalization_manifest")
    if not isinstance(finalization_spec, dict) or set(finalization_spec) != {"path", "receiver_receipt"}:
        return "finalization manifest and receiver receipt are required"
    manifest_value, receiver_value = finalization_spec.get("path"), finalization_spec.get("receiver_receipt")
    manifest_path = Path(manifest_value) if isinstance(manifest_value, str) else Path()
    receiver_path = Path(receiver_value) if isinstance(receiver_value, str) else Path()
    from quality_rules import finalization
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "finalization manifest is unreadable or malformed"
    if not isinstance(manifest_data, dict):
        return "finalization manifest is unreadable or malformed"
    has_task_id = isinstance(manifest_data.get("created_by_task_id"), str) and bool(manifest_data["created_by_task_id"])
    has_run_id = isinstance(manifest_data.get("created_by_run_id"), int) and not isinstance(manifest_data.get("created_by_run_id"), bool)
    if has_task_id != has_run_id:
        return "finalization producer identity is incomplete"
    has_identity = has_task_id and has_run_id
    manifest, manifest_error = finalization.validate_manifest(manifest_path, expected, require_identity=has_identity)
    if manifest_error or manifest is None:
        return manifest_error or "finalization manifest is invalid"
    if not has_identity:
        for key, label in (("source", "finalization source"), ("checker", "finalization checker")):
            error = finalization.validate_handle(manifest.get(key), label, include_size=True)
            if error:
                return error
    if manifest.get("state") != "accepted":
        return "finalization manifest is not accepted"
    return finalization.validate_receiver_copy(manifest, receiver_path, require_identity=True)
