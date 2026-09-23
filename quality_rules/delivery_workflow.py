"""Standalone two-track delivery workflow and body-free candidate ledger.

This module is deliberately separate from the production execution contract.  It
coordinates delivery state and exposes a bounded adapter boundary; it never
mutates a producer, artifact, or source.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

WORKFLOW_SCHEMA_VERSION = "docs-delivery-workflow-1"
LEDGER_SCHEMA_VERSION = "docs-delivery-candidate-ledger-1"
CANDIDATE_SCHEMA_VERSION = "docs-delivery-candidate-1"
HARDENING_SCHEMA_VERSION = "docs-delivery-hardening-1"
MODES = frozenset({"harness_development", "deadline_delivery"})
STRATEGIES = {
    "harness_development": frozenset({"source_first_rebuild"}),
    "deadline_delivery": frozenset({"existing_artifact_repair", "source_repair"}),
}
STATES = ("repair_in_progress", "submitted", "hardening_ready", "hardened")
ISSUE_FAMILIES = frozenset({
    "boundary", "containment", "relation", "spacing", "alignment",
    "safe-wrap", "coverage", "identity",
})
REPAIR_OPERATIONS = frozenset({
    "geometry_adjustment", "spacing_adjustment", "alignment_adjustment",
    "wrap_adjustment", "coverage_adjustment", "identity_rebind",
})
FORBIDDEN_BODY_KEYS = frozenset({
    "text", "body", "content", "raw", "payload", "original", "rewritten",
    "response_text", "actual_value", "expected_value", "user", "name",
    "email", "phone", "address", "campaign",
})
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^t_[a-z0-9]+$")
EXTENSION_RE = re.compile(r"^extension:[a-z0-9][a-z0-9-]{0,62}$")
PII_RE = re.compile(r"(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+?\d[\d ()-]{7,}\d)", re.I)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def _contains_body_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in FORBIDDEN_BODY_KEYS or _contains_body_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_body_key(item) for item in value)
    return False


def _contains_pii(value: Any, *, parent_key: str = "") -> bool:
    if isinstance(value, dict):
        return any(_contains_pii(item, parent_key=str(key)) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_pii(item, parent_key=parent_key) for item in value)
    if not isinstance(value, str) or parent_key == "path" or _is_hash(value):
        return False
    return PII_RE.search(value) is not None


def _path_id(path: Path) -> str:
    return _digest({"canonical_path": str(path.resolve())})


def snapshot_identity(path: Path) -> dict[str, Any]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError("workflow identity target must be a regular file")
    return {
        "path_id": _path_id(target),
        "sha256": sha256(target),
        "size_bytes": target.stat().st_size,
    }


def live_handle(path: Path) -> dict[str, Any]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError("workflow handle target must be a regular file")
    return {"path": str(target), "sha256": sha256(target), "size_bytes": target.stat().st_size}


def _validate_live_handle(value: Any, label: str) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size_bytes"}:
        return [f"{label} handle fields mismatch"]
    path = Path(value.get("path", ""))
    try:
        if (not path.is_absolute() or not path.is_file() or value.get("sha256") != sha256(path)
                or value.get("size_bytes") != path.stat().st_size):
            return [f"{label} path/hash/size drift"]
    except OSError:
        return [f"{label} path/hash/size drift"]
    return []


def _immutable_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": record["task_id"],
        "mode": record["mode"],
        "strategy": record["strategy"],
        "source_snapshot": record["source_snapshot"],
        "baseline_artifact": record["baseline_artifact"],
        "output_path_id": record["output_path_id"],
        "adapter": record["adapter"],
    }


def _seal(record: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(record)
    result["workflow_id"] = _digest(_immutable_identity(result))
    result["record_hash"] = _digest({key: value for key, value in result.items() if key != "record_hash"})
    return result


def issue_workflow(
    *, task_id: str, mode: str, strategy: str, source: Path, output_path: Path,
    baseline_artifact: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(task_id, str) or TASK_RE.fullmatch(task_id) is None:
        raise ValueError("workflow task_id is invalid")
    if mode not in MODES or strategy not in STRATEGIES.get(mode, frozenset()):
        raise ValueError("workflow mode/strategy conflict")
    source_snapshot = snapshot_identity(source)
    baseline = snapshot_identity(baseline_artifact) if baseline_artifact is not None else None
    output_id = _path_id(output_path)
    if mode == "harness_development":
        if baseline is not None or output_id == source_snapshot["path_id"]:
            raise ValueError("harness development requires a new artifact identity")
    elif strategy == "existing_artifact_repair":
        if baseline is None or output_id != baseline["path_id"]:
            raise ValueError("existing-artifact repair must preserve the artifact path identity")
    elif baseline is not None or output_id != source_snapshot["path_id"]:
        raise ValueError("source repair must preserve the source path identity")
    adapter = {
        "id": "docs-delivery-workflow-adapter-1",
        "capabilities": ["record_candidate", "validate_completion"],
        "producer_mutation": False,
    }
    return _seal({
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "workflow_id": "",
        "record_hash": "",
        "task_id": task_id,
        "mode": mode,
        "strategy": strategy,
        "state": "repair_in_progress",
        "source_snapshot": source_snapshot,
        "baseline_artifact": baseline,
        "output_path_id": output_id,
        "adapter": adapter,
        "result_artifact": None,
        "candidate_ledger": None,
        "hardening_receipt": None,
    })


def write_workflow(path: Path, record: dict[str, Any]) -> Path:
    errors = validate_workflow(record)
    if errors:
        raise ValueError("invalid delivery workflow: " + "; ".join(errors))
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical(record) + b"\n")
    return target


def load_workflow(path: Path, *, expected_task_id: str | None = None, require_completion: bool = False) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["delivery workflow is unreadable or malformed"]
    errors = validate_workflow(value, expected_task_id=expected_task_id, require_completion=require_completion)
    return (value if not errors else None), errors


def create_candidate_ledger(path: Path, workflow: dict[str, Any]) -> Path:
    errors = validate_workflow(workflow)
    if errors:
        raise ValueError("invalid delivery workflow: " + "; ".join(errors))
    if workflow["state"] != "repair_in_progress":
        raise ValueError("candidate ledger must be created during repair")
    header = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "workflow_id": workflow["workflow_id"],
        "task_id": workflow["task_id"],
        "mode": workflow["mode"],
        "strategy": workflow["strategy"],
        "source_sha256": workflow["source_snapshot"]["sha256"],
    }
    header["header_hash"] = _digest(header)
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("candidate ledger is append-only and may not overwrite an existing file")
    target.write_bytes(_canonical(header) + b"\n")
    return target


def _read_ledger(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], ["candidate ledger is unreadable or malformed"]
    if not records or any(not isinstance(item, dict) for item in records):
        return [], ["candidate ledger records are missing"]
    return records, []


def ledger_snapshot(path: Path, workflow: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    records, errors = _read_ledger(path)
    if errors:
        return None, errors
    errors.extend(validate_ledger_records(records, workflow))
    if errors:
        return None, list(dict.fromkeys(errors))
    return {"schema_version": LEDGER_SCHEMA_VERSION, "header": records[0], "entries": records[1:]}, []


def _valid_geometry(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"bbox_pt"}:
        return False
    bbox = value["bbox_pt"]
    return (isinstance(bbox, list) and len(bbox) == 4
            and all(not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(float(item)) for item in bbox)
            and float(bbox[2]) >= float(bbox[0]) and float(bbox[3]) >= float(bbox[1]))


def append_candidate(
    path: Path, workflow: dict[str, Any], *, issue_family: str,
    target_ids: list[str], artifact_before_sha256: str, artifact_after_sha256: str,
    before_geometry: dict[str, Any], after_geometry: dict[str, Any],
    repair_operation: str, renderer_readback: Path, genericization_candidate: bool,
) -> dict[str, Any]:
    if workflow.get("mode") != "deadline_delivery" or workflow.get("state") != "repair_in_progress":
        raise ValueError("candidates may be appended only during deadline-delivery repair")
    snapshot, errors = ledger_snapshot(path, workflow)
    if errors or snapshot is None:
        raise ValueError("invalid candidate ledger: " + "; ".join(errors))
    if issue_family not in ISSUE_FAMILIES:
        raise ValueError("candidate issue family is unknown")
    if (not isinstance(target_ids, list) or not target_ids or len(set(target_ids)) != len(target_ids)
            or any(not _is_hash(item) for item in target_ids)):
        raise ValueError("candidate target IDs must be unique anonymized hashes")
    if not _is_hash(artifact_before_sha256) or not _is_hash(artifact_after_sha256):
        raise ValueError("candidate artifact hashes are invalid")
    if not _valid_geometry(before_geometry) or not _valid_geometry(after_geometry):
        raise ValueError("candidate geometry is invalid")
    if repair_operation not in REPAIR_OPERATIONS:
        raise ValueError("candidate repair operation is not adapter-bounded")
    previous = snapshot["entries"][-1]["entry_hash"] if snapshot["entries"] else snapshot["header"]["header_hash"]
    core = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "workflow_id": workflow["workflow_id"],
        "task_id": workflow["task_id"],
        "issue_family": issue_family,
        "target_ids": sorted(target_ids),
        "source_sha256": workflow["source_snapshot"]["sha256"],
        "artifact_before_sha256": artifact_before_sha256,
        "artifact_after_sha256": artifact_after_sha256,
        "before_geometry": before_geometry,
        "after_geometry": after_geometry,
        "repair": {
            "adapter_id": "docs-local-repair-adapter-1",
            "operation": repair_operation,
            "producer_mutation": False,
        },
        "renderer_readback": live_handle(renderer_readback),
        "genericization_candidate": genericization_candidate,
        "previous_entry_hash": previous,
    }
    core["candidate_id"] = _digest(core)
    core["entry_hash"] = _digest(core)
    if _contains_body_key(core) or _contains_pii(core):
        raise ValueError("candidate ledger entry contains body or PII")
    with path.open("ab") as handle:
        handle.write(_canonical(core) + b"\n")
    return core


def validate_ledger_records(records: Any, workflow: dict[str, Any]) -> list[str]:
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return ["candidate ledger records are missing"]
    errors: list[str] = []
    header = records[0]
    expected_header_keys = {"schema_version", "workflow_id", "task_id", "mode", "strategy", "source_sha256", "header_hash"}
    if set(header) != expected_header_keys or header.get("schema_version") != LEDGER_SCHEMA_VERSION:
        errors.append("candidate ledger header schema mismatch")
    if (header.get("workflow_id") != workflow.get("workflow_id") or header.get("task_id") != workflow.get("task_id")
            or header.get("mode") != workflow.get("mode") or header.get("strategy") != workflow.get("strategy")):
        errors.append("candidate ledger cross-task or mode identity mismatch")
    if header.get("source_sha256") != workflow.get("source_snapshot", {}).get("sha256"):
        errors.append("candidate ledger source hash drift")
    if header.get("header_hash") != _digest({key: value for key, value in header.items() if key != "header_hash"}):
        errors.append("candidate ledger header tamper detected")
    previous = header.get("header_hash")
    seen: set[str] = set()
    expected_entry_keys = {
        "schema_version", "candidate_id", "entry_hash", "workflow_id", "task_id", "issue_family",
        "target_ids", "source_sha256", "artifact_before_sha256", "artifact_after_sha256",
        "before_geometry", "after_geometry", "repair", "renderer_readback",
        "genericization_candidate", "previous_entry_hash",
    }
    for index, entry in enumerate(records[1:]):
        label = f"candidate[{index}]"
        if not isinstance(entry, dict) or set(entry) != expected_entry_keys or entry.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
            errors.append(f"{label} schema mismatch"); continue
        if _contains_body_key(entry) or _contains_pii(entry):
            errors.append(f"{label} contains body or PII")
        if entry.get("workflow_id") != workflow.get("workflow_id") or entry.get("task_id") != workflow.get("task_id"):
            errors.append(f"{label} cross-task identity mismatch")
        if entry.get("source_sha256") != workflow.get("source_snapshot", {}).get("sha256"):
            errors.append(f"{label} source hash drift")
        if entry.get("issue_family") not in ISSUE_FAMILIES:
            errors.append(f"{label} issue family is unknown")
        target_ids = entry.get("target_ids")
        if (not isinstance(target_ids, list) or not target_ids or target_ids != sorted(set(target_ids))
                or any(not _is_hash(item) for item in target_ids)):
            errors.append(f"{label} target IDs are not anonymized")
        if not _is_hash(entry.get("artifact_before_sha256")) or not _is_hash(entry.get("artifact_after_sha256")):
            errors.append(f"{label} artifact hash is invalid")
        if not _valid_geometry(entry.get("before_geometry")) or not _valid_geometry(entry.get("after_geometry")):
            errors.append(f"{label} geometry is invalid")
        repair = entry.get("repair")
        if repair != {"adapter_id": "docs-local-repair-adapter-1", "operation": repair.get("operation") if isinstance(repair, dict) else None, "producer_mutation": False} or repair.get("operation") not in REPAIR_OPERATIONS:
            errors.append(f"{label} repair adapter is invalid")
        errors.extend(f"{label} {error}" for error in _validate_live_handle(entry.get("renderer_readback"), "renderer readback"))
        if not isinstance(entry.get("genericization_candidate"), bool):
            errors.append(f"{label} genericization flag is invalid")
        if entry.get("previous_entry_hash") != previous:
            errors.append(f"{label} append-only chain mismatch")
        expected_entry_hash = _digest({key: value for key, value in entry.items() if key != "entry_hash"})
        if entry.get("entry_hash") != expected_entry_hash:
            errors.append(f"{label} tamper detected")
        core_without_ids = {key: value for key, value in entry.items() if key not in {"candidate_id", "entry_hash"}}
        if entry.get("candidate_id") != _digest(core_without_ids):
            errors.append(f"{label} candidate identity mismatch")
        if entry.get("candidate_id") in seen:
            errors.append(f"{label} duplicate candidate identity")
        seen.add(entry.get("candidate_id")); previous = entry.get("entry_hash")
    return list(dict.fromkeys(errors))


def attach_ledger(workflow: dict[str, Any], ledger_path: Path) -> dict[str, Any]:
    snapshot, errors = ledger_snapshot(ledger_path, workflow)
    if errors or snapshot is None:
        raise ValueError("invalid candidate ledger: " + "; ".join(errors))
    result = deepcopy(workflow)
    result["candidate_ledger"] = live_handle(ledger_path)
    return _seal(result)


def transition(
    workflow: dict[str, Any], *, to_state: str, result_artifact: Path | None = None,
    hardening_receipt: Path | None = None,
) -> dict[str, Any]:
    errors = validate_workflow(workflow)
    if errors:
        raise ValueError("invalid delivery workflow: " + "; ".join(errors))
    current = workflow["state"]
    if current not in STATES or to_state not in STATES or STATES.index(to_state) != STATES.index(current) + 1:
        raise ValueError("workflow state transition is invalid")
    result = deepcopy(workflow)
    if to_state == "submitted":
        if result_artifact is None or result.get("candidate_ledger") is None:
            raise ValueError("submission requires result artifact and candidate ledger")
        handle = live_handle(result_artifact)
        if _path_id(result_artifact) != result["output_path_id"]:
            raise ValueError("submission output artifact identity mismatch")
        result["result_artifact"] = handle
    else:
        if result_artifact is not None:
            raise ValueError("post-submission transitions may not replace the result artifact")
        if result.get("result_artifact") is None:
            raise ValueError("post-submission transition lacks submitted artifact")
    if to_state == "hardened":
        if hardening_receipt is None:
            raise ValueError("hardened state requires a hardening receipt")
        result["hardening_receipt"] = live_handle(hardening_receipt)
    elif hardening_receipt is not None:
        raise ValueError("hardening receipt is premature")
    result["state"] = to_state
    return _seal(result)


def write_hardening_receipt(
    workflow: dict[str, Any], ledger_path: Path, mappings: list[dict[str, str]], output_path: Path,
) -> Path:
    errors = validate_workflow(workflow)
    if errors:
        raise ValueError("invalid delivery workflow: " + "; ".join(errors))
    if workflow["state"] != "hardening_ready":
        raise ValueError("hardening may consume candidates only after submitted and hardening_ready")
    snapshot, errors = ledger_snapshot(ledger_path, workflow)
    if errors or snapshot is None:
        raise ValueError("invalid candidate ledger: " + "; ".join(errors))
    eligible = {item["candidate_id"]: item for item in snapshot["entries"] if item["genericization_candidate"] is True}
    if not isinstance(mappings, list) or any(not isinstance(item, dict) or set(item) != {"candidate_id", "mapped_family"} for item in mappings):
        raise ValueError("hardening mappings are malformed")
    mapped_ids = [item["candidate_id"] for item in mappings]
    if len(mapped_ids) != len(set(mapped_ids)) or set(mapped_ids) != set(eligible):
        raise ValueError("hardening must consume exactly the submitted genericization candidates")
    for item in mappings:
        family = item["mapped_family"]
        if family not in ISSUE_FAMILIES and (not isinstance(family, str) or EXTENSION_RE.fullmatch(family) is None):
            raise ValueError("hardening family must map to an existing family or bounded extension")
    value = {
        "schema_version": HARDENING_SCHEMA_VERSION,
        "workflow_id": workflow["workflow_id"],
        "task_id": workflow["task_id"],
        "submitted_artifact_sha256": workflow["result_artifact"]["sha256"],
        "candidate_ledger": live_handle(ledger_path),
        "candidate_count": len(snapshot["entries"]),
        "eligible_count": len(eligible),
        "mappings": sorted(mappings, key=lambda item: item["candidate_id"]),
        "adapter": {"id": "docs-bounded-hardening-adapter-1", "producer_mutation": False},
    }
    value["receipt_hash"] = _digest(value)
    target = output_path.resolve(); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical(value) + b"\n")
    return target


def _validate_hardening_receipt(path: Path, workflow: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["hardening receipt is unreadable or malformed"]
    expected_keys = {"schema_version", "workflow_id", "task_id", "submitted_artifact_sha256", "candidate_ledger", "candidate_count", "eligible_count", "mappings", "adapter", "receipt_hash"}
    if not isinstance(value, dict) or set(value) != expected_keys or value.get("schema_version") != HARDENING_SCHEMA_VERSION:
        return ["hardening receipt schema mismatch"]
    if _contains_body_key(value) or _contains_pii(value):
        return ["hardening receipt contains body or PII"]
    errors: list[str] = []
    if value.get("workflow_id") != workflow.get("workflow_id") or value.get("task_id") != workflow.get("task_id"):
        errors.append("hardening receipt cross-task identity mismatch")
    if value.get("submitted_artifact_sha256") != workflow.get("result_artifact", {}).get("sha256"):
        errors.append("hardening receipt artifact hash drift")
    if value.get("receipt_hash") != _digest({key: item for key, item in value.items() if key != "receipt_hash"}):
        errors.append("hardening receipt tamper detected")
    ledger_handle = value.get("candidate_ledger")
    errors.extend(_validate_live_handle(ledger_handle, "hardening candidate ledger"))
    mappings = value.get("mappings")
    if not isinstance(mappings, list):
        errors.append("hardening mappings are malformed")
    else:
        for item in mappings:
            family = item.get("mapped_family") if isinstance(item, dict) else None
            if not isinstance(item, dict) or set(item) != {"candidate_id", "mapped_family"} or not _is_hash(item.get("candidate_id")) or (family not in ISSUE_FAMILIES and (not isinstance(family, str) or EXTENSION_RE.fullmatch(family) is None)):
                errors.append("hardening mapping is invalid"); break
    if isinstance(ledger_handle, dict) and not _validate_live_handle(ledger_handle, "hardening candidate ledger"):
        snapshot, snapshot_errors = ledger_snapshot(Path(ledger_handle["path"]), workflow)
        errors.extend(snapshot_errors)
        if snapshot is not None:
            eligible = {item["candidate_id"] for item in snapshot["entries"] if item["genericization_candidate"] is True}
            mapped = {item.get("candidate_id") for item in mappings} if isinstance(mappings, list) and all(isinstance(item, dict) for item in mappings) else set()
            if mapped != eligible or value.get("candidate_count") != len(snapshot["entries"]) or value.get("eligible_count") != len(eligible):
                errors.append("hardening receipt candidate coverage mismatch")
    return list(dict.fromkeys(errors))


def validate_workflow(record: Any, *, expected_task_id: str | None = None, require_completion: bool = False) -> list[str]:
    expected_keys = {
        "schema_version", "workflow_id", "record_hash", "task_id", "mode", "strategy", "state",
        "source_snapshot", "baseline_artifact", "output_path_id", "adapter", "result_artifact",
        "candidate_ledger", "hardening_receipt",
    }
    if not isinstance(record, dict) or set(record) != expected_keys or record.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        return ["delivery workflow schema mismatch"]
    errors: list[str] = []
    if _contains_body_key(record) or _contains_pii(record):
        errors.append("delivery workflow contains body or PII")
    task_id, mode, strategy, state = record.get("task_id"), record.get("mode"), record.get("strategy"), record.get("state")
    if not isinstance(task_id, str) or TASK_RE.fullmatch(task_id) is None:
        errors.append("delivery workflow task_id is invalid")
    if expected_task_id is not None and task_id != expected_task_id:
        errors.append("delivery workflow cross-task identity mismatch")
    if mode not in MODES or strategy not in STRATEGIES.get(mode, frozenset()):
        errors.append("delivery workflow mode/strategy conflict")
    if state not in STATES:
        errors.append("delivery workflow state is invalid")
    snapshot = record.get("source_snapshot")
    baseline = record.get("baseline_artifact")
    for label, value, nullable in (("source snapshot", snapshot, False), ("baseline artifact", baseline, True)):
        if value is None and nullable:
            continue
        if not isinstance(value, dict) or set(value) != {"path_id", "sha256", "size_bytes"} or not _is_hash(value.get("path_id")) or not _is_hash(value.get("sha256")) or isinstance(value.get("size_bytes"), bool) or not isinstance(value.get("size_bytes"), int) or value.get("size_bytes") < 0:
            errors.append(f"delivery workflow {label} is invalid")
    if not _is_hash(record.get("output_path_id")):
        errors.append("delivery workflow output path identity is invalid")
    if mode == "harness_development" and (baseline is not None or isinstance(snapshot, dict) and record.get("output_path_id") == snapshot.get("path_id")):
        errors.append("harness development requires new artifact identity")
    if mode == "deadline_delivery" and strategy == "existing_artifact_repair" and (not isinstance(baseline, dict) or record.get("output_path_id") != baseline.get("path_id")):
        errors.append("deadline existing-artifact strategy identity mismatch")
    if mode == "deadline_delivery" and strategy == "source_repair" and (baseline is not None or not isinstance(snapshot, dict) or record.get("output_path_id") != snapshot.get("path_id")):
        errors.append("deadline source-repair strategy identity mismatch")
    if record.get("adapter") != {"id": "docs-delivery-workflow-adapter-1", "capabilities": ["record_candidate", "validate_completion"], "producer_mutation": False}:
        errors.append("delivery workflow adapter boundary mismatch")
    try:
        if record.get("workflow_id") != _digest(_immutable_identity(record)):
            errors.append("delivery workflow identity tamper detected")
    except (KeyError, TypeError):
        errors.append("delivery workflow identity is incomplete")
    if record.get("record_hash") != _digest({key: value for key, value in record.items() if key != "record_hash"}):
        errors.append("delivery workflow record tamper detected")
    result = record.get("result_artifact")
    ledger = record.get("candidate_ledger")
    hardening = record.get("hardening_receipt")
    if state == "repair_in_progress":
        if result is not None or hardening is not None:
            errors.append("repair-in-progress workflow contains premature submission or hardening")
    else:
        errors.extend(_validate_live_handle(result, "delivery result artifact"))
        errors.extend(_validate_live_handle(ledger, "delivery candidate ledger"))
        if isinstance(result, dict) and isinstance(result.get("path"), str) and Path(result["path"]).is_absolute() and _path_id(Path(result["path"])) != record.get("output_path_id"):
            errors.append("delivery result artifact identity mismatch")
    if ledger is not None:
        errors.extend(_validate_live_handle(ledger, "delivery candidate ledger"))
        if isinstance(ledger, dict) and not _validate_live_handle(ledger, "delivery candidate ledger"):
            _, ledger_errors = ledger_snapshot(Path(ledger["path"]), record)
            errors.extend(ledger_errors)
    if state == "hardened":
        errors.extend(_validate_live_handle(hardening, "delivery hardening receipt"))
        if isinstance(hardening, dict) and not _validate_live_handle(hardening, "delivery hardening receipt"):
            errors.extend(_validate_hardening_receipt(Path(hardening["path"]), record))
    elif hardening is not None:
        errors.append("delivery hardening receipt is premature")
    if require_completion and state not in {"submitted", "hardening_ready", "hardened"}:
        errors.append("delivery workflow has not reached submitted state")
    if state != "repair_in_progress" and isinstance(ledger, dict) and not _validate_live_handle(ledger, "delivery candidate ledger") and isinstance(result, dict):
        snapshot_value, snapshot_errors = ledger_snapshot(Path(ledger["path"]), record)
        if not snapshot_errors and snapshot_value and snapshot_value["entries"]:
            if snapshot_value["entries"][-1]["artifact_after_sha256"] != result.get("sha256"):
                errors.append("candidate ledger final artifact hash differs from submitted artifact")
    return list(dict.fromkeys(errors))
