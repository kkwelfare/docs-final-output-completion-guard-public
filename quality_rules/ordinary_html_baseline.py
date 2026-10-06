"""Validate a single HTML completion from hash-bound real-browser readbacks.

This is a narrow baseline route, not a substitute for strict artifact-quality
contracts on DOCX/PDF or other formats. The preparer binds evidence; it never
creates browser or render evidence and never asserts a check that did not run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ordinary-html-baseline-1"
REQUIRED_BROWSER_FIELDS = {
    "schema_version", "artifact", "browser", "render", "final_output_receipts",
    "finalization_manifest", "checks", "applicability",
}
REQUIRED_BROWSER_CHECKS = {"page_load"}
OPTIONAL_APPLICABILITY = {"interaction", "pause_resume", "grounding", "finish", "webgl", "reduced_motion", "visibility_handler", "external_resources"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handle(path: Path) -> dict[str, str]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError("HTML baseline evidence handle is not a file")
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


def _json(path: Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"{label} is malformed"
    if not isinstance(value, dict):
        return None, f"{label} is malformed"
    return value, None


def validate(contract_path: Path, artifacts: list[str]) -> str | None:
    if not contract_path.is_absolute() or not contract_path.is_file():
        return "ordinary-HTML baseline contract is missing"
    contract, error = _json(contract_path, "ordinary-HTML baseline contract")
    if error or contract is None:
        return error or "ordinary-HTML baseline contract is malformed"
    if set(contract) != REQUIRED_BROWSER_FIELDS or contract.get("schema_version") != SCHEMA_VERSION:
        return "ordinary-HTML baseline contract schema mismatch"

    artifact, error = read_handle(contract.get("artifact"), "HTML artifact")
    if error:
        return error
    if artifact is None:
        return "HTML artifact is missing"
    expected = [str(Path(value).resolve()) for value in artifacts if isinstance(value, str)]
    if len(artifacts) != 1 or expected != [str(artifact)]:
        return "ordinary-HTML baseline artifact coverage mismatch"
    if artifact.suffix.lower() not in {".html", ".htm"}:
        return "ordinary-HTML baseline accepts only HTML artifacts"

    browser_path, error = read_handle(contract.get("browser"), "real-browser receipt")
    if error:
        return error
    if browser_path is None:
        return "real-browser receipt is missing"
    browser, error = _json(browser_path, "real-browser receipt")
    if error or browser is None:
        return error or "real-browser receipt is malformed"
    if set(browser) != {"schema_version", "artifact_sha256", "url", "task_id", "run_id", "page_errors", "console_errors", "external_resources", "checks", "render"}:
        return "real-browser receipt schema mismatch"
    if browser.get("schema_version") != "ordinary-html-browser-receipt-1":
        return "real-browser receipt schema mismatch"
    if browser.get("artifact_sha256") != sha256(artifact):
        return "real-browser receipt artifact hash mismatch"
    url = browser.get("url")
    if not isinstance(url, str) or not url.startswith("file://") or Path(url.removeprefix("file://")).resolve() != artifact:
        return "real-browser receipt URL does not identify the local HTML artifact"
    if not isinstance(browser.get("task_id"), str) or not browser["task_id"].strip():
        return "real-browser receipt task identity is missing"
    if isinstance(browser.get("run_id"), bool) or not isinstance(browser.get("run_id"), int) or browser["run_id"] < 1:
        return "real-browser receipt run identity is missing"
    for key in ("page_errors", "console_errors", "external_resources"):
        value = browser.get(key)
        if not isinstance(value, list):
            return f"real-browser receipt {key} is malformed"
        if value:
            return f"real-browser receipt reports {key.replace('_', ' ')}"
    checks = browser.get("checks")
    if (not isinstance(checks, dict) or not REQUIRED_BROWSER_CHECKS.issubset(checks)
            or any(value is not True for value in checks.values())):
        return "real-browser representative checks are missing or failed"
    applicability = contract.get("applicability")
    if not isinstance(applicability, dict) or not set(applicability).issubset(OPTIONAL_APPLICABILITY):
        return "HTML baseline applicability declaration is malformed"
    if any(not isinstance(value, bool) for value in applicability.values()):
        return "HTML baseline applicability declaration is malformed"
    for key, applicable in applicability.items():
        if applicable and key == "external_resources" and browser["external_resources"] == []:
            continue
        if applicable and checks.get(key) is not True:
            return f"applicable browser check is missing or failed: {key}"
    render_ref = browser.get("render")
    if not isinstance(render_ref, dict) or set(render_ref) != {"path", "sha256"}:
        return "real-browser render handle is missing"
    if render_ref != contract.get("render"):
        return "HTML baseline render handle differs from browser receipt"
    render_path, error = read_handle(render_ref, "representative browser render")
    if error:
        return error
    if render_path is None or render_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return "representative browser render is missing or unsupported"
    try:
        from PIL import Image
        with Image.open(render_path) as image:
            image.verify()
        with Image.open(render_path) as image:
            if image.width < 1 or image.height < 1:
                return "representative browser render is empty"
    except (OSError, ValueError):
        return "representative browser render readback failed"
    if browser.get("render") != render_ref:
        return "real-browser render receipt mismatch"

    checks = contract.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(browser["checks"]):
        return "HTML baseline checks differ from browser receipt"
    if checks != browser["checks"]:
        return "HTML baseline check readback mismatch"

    final_receipts = contract.get("final_output_receipts")
    if not isinstance(final_receipts, list) or not final_receipts:
        return "canonical final-output receipt handle is missing"
    covered: set[str] = set()
    for index, value in enumerate(final_receipts, 1):
        receipt_path, error = read_handle(value, f"final-output receipt {index}")
        if error:
            return error
        if receipt_path is None:
            return "canonical final-output receipt is missing"
        receipt, error = _json(receipt_path, "canonical final-output receipt")
        if error or receipt is None:
            return error or "canonical final-output receipt is malformed"
        entries = receipt.get("entries")
        if receipt.get("schema_version") != "docs-final-output-receipt-1" or receipt.get("status") != "pass" or not isinstance(entries, list) or not entries:
            return "canonical final-output receipt did not pass"
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("status") != "pass":
                return "canonical final-output receipt entry did not pass"
            path = Path(str(entry.get("artifact_path", "")))
            if not path.is_absolute() or not path.is_file() or entry.get("artifact_sha256") != sha256(path):
                return "canonical final-output receipt artifact is missing or stale"
            covered.add(str(path.resolve()))
    if covered != {str(artifact)}:
        return "canonical final-output receipt does not cover exactly the HTML artifact"

    manifest_spec = contract.get("finalization_manifest")
    if not isinstance(manifest_spec, dict) or set(manifest_spec) != {"path", "receiver_receipt"}:
        return "finalization manifest and receiver receipt are required"
    manifest_value, receiver_value = manifest_spec.get("path"), manifest_spec.get("receiver_receipt")
    manifest_path = Path(manifest_value) if isinstance(manifest_value, str) else Path()
    receiver_path = Path(receiver_value) if isinstance(receiver_value, str) else Path()
    if not manifest_path.is_absolute() or not receiver_path.is_absolute():
        return "finalization manifest and receiver receipt paths must be absolute"
    from quality_rules import finalization
    manifest, manifest_error = finalization.validate_manifest(manifest_path, expected, require_identity=True)
    if manifest_error or manifest is None:
        return manifest_error or "finalization manifest is invalid"
    if manifest.get("state") != "accepted":
        return "finalization manifest is not accepted"
    if (manifest.get("created_by_task_id") != browser["task_id"]
            or manifest.get("created_by_run_id") != browser["run_id"]):
        return "HTML browser and finalization producer identity mismatch"
    return finalization.validate_receiver_copy(manifest, receiver_path, require_identity=True)
