"""Hash-bound finalization-manifest checks for docs artifact delivery."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any

try:
    from . import execution_contract
except ImportError:  # standalone fixture/import path
    _spec = importlib.util.spec_from_file_location(
        "docs_finalization_execution_contract", Path(__file__).with_name("execution_contract.py")
    )
    if _spec is None or _spec.loader is None:
        raise
    execution_contract = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = execution_contract
    _spec.loader.exec_module(execution_contract)

SCHEMA_VERSION = "docs-finalization-manifest-1"
ALLOWED_STATES = frozenset({"finalizing", "checked", "reviewed", "accepted"})
EXACT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def media_type(path: Path) -> str:
    return EXACT_MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def artifact_set_id(items: list[dict[str, Any]]) -> str:
    canonical = [{k: item[k] for k in ("canonical_path", "sha256", "size_bytes", "media_type")} for item in items]
    payload = json.dumps(sorted(canonical, key=lambda x: x["canonical_path"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_handle(path: Path) -> dict[str, Any]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError(f"handle is not a file: {target}")
    return {"canonical_path": str(target), "sha256": sha256(target), "size_bytes": target.stat().st_size}


def build_manifest(
    artifacts: list[str], *, source: str | None = None, checker: str | None = None,
    created_by_task_id: str | None = None, created_by_run_id: int | None = None,
    execution_contract_path: str | None = None, producer_receipt: str | None = None,
    renderer_receipts: list[str] | None = None,
    state: str = "accepted",
) -> dict[str, Any]:
    """Build a deterministic body-free manifest.

    Identity/source/checker fields are emitted when supplied. They are mandatory
    when a contract opts into ``strict_identity``; legacy callers can keep using
    the historical two-argument form.
    """
    if state not in ALLOWED_STATES:
        raise ValueError(f"unsupported finalization state: {state}")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in artifacts:
        target = Path(value).resolve(strict=True)
        if not target.is_file():
            raise ValueError(f"artifact is not a file: {target}")
        canonical = str(target)
        if canonical in seen:
            raise ValueError(f"duplicate artifact: {canonical}")
        seen.add(canonical)
        entries.append({
            "canonical_path": canonical,
            "sha256": sha256(target),
            "size_bytes": target.stat().st_size,
            "media_type": media_type(target),
        })
    entries.sort(key=lambda item: item["canonical_path"])
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "artifacts": entries,
        "artifact_set_id": artifact_set_id(entries),
    }
    if source is not None:
        result["source"] = _file_handle(Path(source))
    if checker is not None:
        result["checker"] = _file_handle(Path(checker))
    if created_by_task_id is not None:
        result["created_by_task_id"] = created_by_task_id
    if created_by_run_id is not None:
        result["created_by_run_id"] = created_by_run_id
    integrated = any(value is not None for value in (execution_contract_path, producer_receipt, renderer_receipts))
    if integrated:
        if not execution_contract_path or not producer_receipt or not renderer_receipts:
            raise ValueError("integrated finalization requires execution contract, producer receipt, and renderer receipts")
        contract_path = Path(execution_contract_path).resolve(strict=True)
        contract, contract_errors = execution_contract.load_contract(contract_path, require_artifacts=True)
        if contract is None:
            raise ValueError("invalid execution contract: " + "; ".join(contract_errors))
        if result["artifact_set_id"] != contract["artifact_set_id"]:
            raise ValueError("finalization artifact set differs from execution contract")
        if created_by_run_id != contract["producer_run_id"]:
            raise ValueError("finalization producer run differs from execution contract")
        producer_path = Path(producer_receipt).resolve(strict=True)
        producer_data, producer_errors = execution_contract.validate_producer_receipt(producer_path, contract)
        if producer_data is None:
            raise ValueError("invalid producer receipt: " + "; ".join(producer_errors))
        renderer_handles = [_file_handle(Path(value)) for value in renderer_receipts]
        result["execution_identity"] = execution_contract.identity(contract)
        result["execution_contract"] = _file_handle(contract_path)
        result["producer_receipt"] = _file_handle(producer_path)
        result["renderer_receipts"] = renderer_handles
    return result


def write_manifest(path: Path, artifacts: list[str], **kwargs: Any) -> dict[str, Any]:
    if not path.is_absolute():
        raise ValueError("finalization manifest path must be absolute")
    manifest = build_manifest(artifacts, **kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return manifest


def validate_handle(value: Any, label: str, *, include_size: bool = False) -> str | None:
    if not isinstance(value, dict):
        return f"{label} handle is missing"
    raw, digest = value.get("canonical_path", value.get("path")), value.get("sha256")
    target = Path(raw) if isinstance(raw, str) else Path()
    if not target.is_absolute() or not target.is_file() or not _is_sha256(digest):
        return f"{label} path/hash handle is missing"
    if digest != sha256(target):
        return f"{label} hash drift"
    if include_size and value.get("size_bytes") != target.stat().st_size:
        return f"{label} size drift"
    return None


def validate_manifest(
    path: Path, artifacts: list[str], *, require_identity: bool = False,
    require_execution_contract: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "finalization manifest is unreadable or malformed"
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None, "finalization manifest schema is unsupported"
    if data.get("state") not in ALLOWED_STATES:
        return None, "finalization manifest state is missing or unsupported"
    entries = data.get("artifacts")
    if not isinstance(entries, list) or not entries:
        return None, "finalization manifest has no artifacts"
    expected = {str(Path(value).resolve()) for value in artifacts}
    observed: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            return None, "finalization manifest artifact is malformed"
        raw = item.get("canonical_path")
        target = Path(raw) if isinstance(raw, str) else Path()
        if not target.is_absolute() or not target.is_file():
            return None, "finalization manifest artifact is missing"
        canonical = str(target.resolve())
        if canonical in observed:
            return None, "finalization manifest contains a duplicate artifact"
        observed.add(canonical)
        if (not _is_sha256(item.get("sha256")) or item.get("sha256") != sha256(target)
                or item.get("size_bytes") != target.stat().st_size
                or item.get("media_type") != media_type(target)):
            return None, "finalization manifest artifact hash/size/media-type drift"
    try:
        computed_set_id = artifact_set_id(entries)
    except (KeyError, TypeError, ValueError):
        return None, "finalization manifest artifact is malformed"
    if observed != expected or not _is_sha256(data.get("artifact_set_id")) or data.get("artifact_set_id") != computed_set_id:
        return None, "finalization manifest artifact set mismatch"
    if require_identity:
        if validate_handle(data.get("source"), "finalization source", include_size=True):
            return None, validate_handle(data.get("source"), "finalization source", include_size=True)
        if validate_handle(data.get("checker"), "finalization checker", include_size=True):
            return None, validate_handle(data.get("checker"), "finalization checker", include_size=True)
        if not isinstance(data.get("created_by_task_id"), str) or not data["created_by_task_id"]:
            return None, "finalization manifest producer task identity is missing"
        if not isinstance(data.get("created_by_run_id"), int) or isinstance(data["created_by_run_id"], bool):
            return None, "finalization manifest producer run identity is missing"
    integrated_fields = ("execution_identity", "execution_contract", "producer_receipt", "renderer_receipts")
    integrated = any(field in data for field in integrated_fields)
    if require_execution_contract and not integrated:
        return None, "finalization execution contract is missing"
    if integrated:
        if not all(field in data for field in integrated_fields):
            return None, "finalization execution evidence is incomplete"
        contract_handle = data.get("execution_contract")
        error = validate_handle(contract_handle, "execution contract", include_size=True)
        if error:
            return None, error
        contract_path = Path(contract_handle["canonical_path"])
        contract, contract_errors = execution_contract.load_contract(contract_path, require_artifacts=True)
        if contract is None:
            return None, "invalid execution contract: " + "; ".join(contract_errors)
        if execution_contract.validate_identity(data.get("execution_identity"), contract, "finalization"):
            return None, "finalization execution identity mismatch"
        if data.get("artifact_set_id") != contract.get("artifact_set_id") or data.get("created_by_run_id") != contract.get("producer_run_id"):
            return None, "finalization run/artifact-set identity mismatch"
        producer_handle = data.get("producer_receipt")
        error = validate_handle(producer_handle, "producer receipt", include_size=True)
        if error:
            return None, error
        if execution_contract.validate_producer_receipt(Path(producer_handle["canonical_path"]), contract)[0] is None:
            return None, "finalization producer receipt mismatch"
        renderer_handles = data.get("renderer_receipts")
        if not isinstance(renderer_handles, list) or len(renderer_handles) != len(entries):
            return None, "finalization renderer receipt coverage mismatch"
        for handle in renderer_handles:
            error = validate_handle(handle, "renderer receipt", include_size=True)
            if error:
                return None, error
    return data, None


def validate_receiver_copy(manifest: dict[str, Any], receipt_path: Path, *, require_identity: bool = False) -> str | None:
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "receiver receipt is unreadable or malformed"
    if not isinstance(data, dict) or data.get("status") != "accepted" or data.get("artifact_set_id") != manifest.get("artifact_set_id"):
        return "receiver receipt is missing or does not accept the manifest"
    if require_identity:
        if not isinstance(data.get("receiver_task_id"), str) or not data["receiver_task_id"]:
            return "receiver receipt task identity is missing"
        if not isinstance(data.get("receiver_run_id"), int) or isinstance(data["receiver_run_id"], bool):
            return "receiver receipt run identity is missing"
    by_source = {item["canonical_path"]: item for item in manifest["artifacts"]}
    copies = data.get("copies")
    if not isinstance(copies, list) or len(copies) != len(by_source):
        return "receiver receipt copy coverage mismatch"
    seen: set[str] = set()
    for copy in copies:
        src = copy.get("canonical_path") if isinstance(copy, dict) else None
        target = Path(copy.get("copy_path", "")) if isinstance(copy, dict) else Path()
        if src in seen:
            return "receiver receipt copy coverage mismatch"
        seen.add(src)
        if require_identity and (not isinstance(copy, dict) or "size_bytes" not in copy or "media_type" not in copy):
            return "receiver receipt copy size/media-type is missing"
        if (src not in by_source or not target.is_absolute() or not target.is_file()
                or copy.get("size_bytes", target.stat().st_size) != target.stat().st_size
                or copy.get("media_type", media_type(target)) != by_source[src]["media_type"]
                or copy.get("sha256") != sha256(target)
                or copy.get("sha256") != by_source[src]["sha256"]):
            return "receiver-copy-hash-drift"
    return None
