"""Fail-closed artifact-ready production retry guard for docs Kanban workers.

The guard reads a compact body-free state file. It does not read artifact bodies
or persist tool arguments. Once a baseline-passing primary artifact is ready,
re-running the same production fingerprint is refused; the caller must verify or
complete from the primary readback. Transient retry and hard-fail correction
states retain explicit bounded behavior.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPAIR_SPEC = importlib.util.spec_from_file_location(
    "docs_production_retry_repair_controller",
    Path(__file__).parent / "quality_rules" / "repair_controller.py",
)
if _REPAIR_SPEC is None or _REPAIR_SPEC.loader is None:
    raise ImportError("bounded repair controller is unavailable")
repair_controller = importlib.util.module_from_spec(_REPAIR_SPEC)
sys.modules[_REPAIR_SPEC.name] = repair_controller
_REPAIR_SPEC.loader.exec_module(repair_controller)

SCHEMA_VERSION = "docs-artifact-ready-state-1"
POLICY_VERSION = "docs-production-retry-control/1"
VERIFY_MODES = frozenset({"verify-only", "complete-from-primary-readback", "readback-only"})
DEFAULT_PRODUCTION_TOOLS = frozenset({
    "image_generate", "write_file", "patch", "terminal", "execute_code",
    "browser_navigate", "browser_click", "browser_type", "browser_press",
    "browser_scroll",
})
SHA256_LENGTH = 64


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == SHA256_LENGTH and all(char in "0123456789abcdef" for char in value)


def _task_id(task_id: str | None = None) -> str:
    value = task_id
    if value is None:
        value = os.environ.get("HERMES_KANBAN_TASK_ID", "") or os.environ.get("HERMES_KANBAN_TASK", "")
    if isinstance(value, str) and value.startswith("{"):
        try:
            value = json.loads(value).get("id", "")
        except (TypeError, ValueError):
            value = ""
    return value if isinstance(value, str) and value.startswith("t_") else ""


def _workspace() -> Path | None:
    value = os.environ.get("HERMES_KANBAN_WORKSPACE", "")
    if not value:
        return None
    path = Path(value)
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.is_dir() else None


def _state_path(args: dict[str, Any] | None = None) -> Path | None:
    args = args if isinstance(args, dict) else {}
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    candidates: list[Any] = [
        args.get("artifact_ready_state_path"),
        metadata.get("artifact_ready_state_path"),
    ]
    verification = metadata.get("verification") if isinstance(metadata.get("verification"), dict) else {}
    artifact_ready = verification.get("artifact_ready") if isinstance(verification.get("artifact_ready"), dict) else {}
    candidates.append(artifact_ready.get("state_path"))
    candidates.append(os.environ.get("HERMES_ARTIFACT_READY_STATE", ""))
    for value in candidates:
        if isinstance(value, str) and value.startswith("/"):
            return Path(value)
    workspace = _workspace()
    return workspace / "artifact-ready-state.json" if workspace else None


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_absolute() or not path.is_file():
        return None, f"artifact-ready state is missing: {path}"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"artifact-ready state is unreadable or malformed: {path}"
    if not isinstance(value, dict):
        return None, f"artifact-ready state must be a JSON object: {path}"
    return value, None


def read_effective_max_retries(state: dict[str, Any]) -> tuple[int | None, str | None]:
    """Read the effective retry limit, requiring a machine-readable source."""
    value = state.get("effective_max_retries")
    source = state.get("retry_limit_source")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None, "effective_max_retries is missing or invalid"
    if not isinstance(source, str) or not source:
        return None, "retry_limit_source is missing"
    return value, source


def production_fingerprint(tool_name: str, args: Any) -> str | None:
    """Use an explicit fingerprint or derive a stable body-free action identity."""
    if isinstance(args, dict):
        for key in ("production_fingerprint", "artifact_fingerprint", "same_condition_fingerprint"):
            value = args.get(key)
            if _is_sha256(value):
                return value
        selected = {
            key: args.get(key)
            for key in (
                "tool_name", "artifact_kind", "artifact_set_id", "source_path", "target_path",
                "contract_path", "production_mode", "operation", "prompt", "command",
            )
            if key in args
        }
    else:
        selected = {}
    selected["tool_name"] = tool_name
    if not selected:
        return None
    try:
        packed = json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(packed.encode("utf-8", "replace")).hexdigest()


def _validate_primary_artifacts(state: dict[str, Any]) -> str | None:
    artifacts = state.get("primary_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return "primary_artifacts are missing"
    for item in artifacts:
        if not isinstance(item, dict):
            return "primary_artifacts contains a malformed entry"
        path = Path(item.get("path", "")) if isinstance(item.get("path"), str) else Path()
        digest = item.get("sha256")
        if not path.is_absolute() or not path.is_file() or not _is_sha256(digest):
            return "primary artifact path/hash handle is missing"
        try:
            if digest != _sha256(path):
                return f"primary artifact hash drift: {path}"
        except OSError:
            return f"primary artifact is unreadable: {path}"
    return None


def validate_state(state: dict[str, Any], *, task_id: str = "") -> str | None:
    if state.get("schema_version") != SCHEMA_VERSION:
        return "artifact-ready state schema is unsupported"
    if state.get("policy_version") != POLICY_VERSION:
        return "artifact-ready policy version drift"
    if task_id and state.get("task_id") != task_id:
        return "artifact-ready state belongs to another task"
    if state.get("status") not in {"artifact_ready", "transient_retry", "hard_fail", "review_only"}:
        return "artifact-ready state status is unsupported"
    limit, _ = read_effective_max_retries(state)
    if limit is None:
        return _
    retry_count = state.get("retry_count")
    if not isinstance(retry_count, int) or isinstance(retry_count, bool) or retry_count < 0:
        return "retry_count is missing or invalid"
    fingerprint = state.get("production_fingerprint")
    if not _is_sha256(fingerprint):
        return "production_fingerprint is missing or invalid"
    baseline = state.get("baseline_status")
    if baseline not in {"pass", "fail", "unavailable"}:
        return "baseline_status is missing or invalid"
    delivery = state.get("primary_delivery")
    if delivery not in {"attachment", "durable_deliverable", "none"}:
        return "primary_delivery is missing or invalid"
    if state.get("status") == "artifact_ready":
        if baseline != "pass" or delivery not in {"attachment", "durable_deliverable"}:
            return "artifact_ready requires baseline pass and primary delivery"
        return _validate_primary_artifacts(state)
    if state.get("status") == "hard_fail":
        plan_value = state.get("repair_plan_path")
        fingerprint = state.get("admitted_adapter_fingerprint")
        plan_path = Path(plan_value) if isinstance(plan_value, str) else Path()
        if not plan_path.is_absolute() or not plan_path.is_file() or not _is_sha256(fingerprint):
            return "hard_fail requires a hash-bound repair plan and admitted adapter fingerprint"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "hard_fail repair plan is unreadable or malformed"
        errors = repair_controller.validate_plan(plan, expected_task_id=task_id or state.get("task_id"))
        if errors:
            return "hard_fail repair plan is invalid: " + errors[0]
        declared = plan.get("adapter") if isinstance(plan, dict) else None
        if not isinstance(declared, dict) or declared.get("fingerprint") != fingerprint:
            return "hard_fail adapter fingerprint differs from the repair plan"
    if state.get("status") in {"transient_retry", "hard_fail"} and baseline == "pass" and delivery != "none":
        return _validate_primary_artifacts(state)
    return None


def _verification_mode(args: dict[str, Any]) -> str:
    values = [
        args.get("production_mode"), args.get("operation_mode"),
        args.get("mode"),
    ]
    metadata = args.get("metadata")
    if isinstance(metadata, dict):
        ready = metadata.get("artifact_ready")
        if isinstance(ready, dict):
            values.append(ready.get("mode"))
        verification = metadata.get("verification")
        if isinstance(verification, dict):
            ready = verification.get("artifact_ready")
            if isinstance(ready, dict):
                values.append(ready.get("mode"))
    for value in values:
        if isinstance(value, str) and value in VERIFY_MODES:
            return value
    return ""


def _production_tools(state: dict[str, Any]) -> frozenset[str]:
    values = state.get("production_tools")
    if isinstance(values, list) and values and all(isinstance(item, str) and item for item in values):
        return frozenset(values)
    return DEFAULT_PRODUCTION_TOOLS


def evaluate_production_call(
    tool_name: str, args: dict[str, Any], state: dict[str, Any], *, task_id: str = "",
) -> dict[str, Any]:
    """Return ``allow`` or ``block`` plus a machine-readable reason."""
    error = validate_state(state, task_id=task_id)
    if error:
        return {"decision": "block", "reason": error, "policy_version": POLICY_VERSION}
    if tool_name not in _production_tools(state):
        return {"decision": "allow", "reason": "non-production tool", "policy_version": POLICY_VERSION}
    mode = _verification_mode(args)
    if mode in VERIFY_MODES:
        return {"decision": "allow", "reason": mode, "policy_version": POLICY_VERSION}
    current = production_fingerprint(tool_name, args)
    expected = state.get("production_fingerprint")
    limit, source = read_effective_max_retries(state)
    retry_count = int(state.get("retry_count", 0))
    status = state.get("status")
    if status == "review_only":
        return {"decision": "block", "reason": "review-only state forbids production recreation", "policy_version": POLICY_VERSION}
    if status == "artifact_ready":
        if current == expected:
            return {
                "decision": "block",
                "reason": "same production fingerprint after artifact-ready; use verify-only or complete-from-primary-readback",
                "policy_version": POLICY_VERSION,
                "effective_max_retries": limit,
                "retry_limit_source": source,
                "same_condition": True,
            }
        if not state.get("allow_new_fingerprint", False):
            return {
                "decision": "block",
                "reason": "artifact-ready state forbids broad rebuild without explicit new-fingerprint admission",
                "policy_version": POLICY_VERSION,
                "effective_max_retries": limit,
                "retry_limit_source": source,
                "same_condition": False,
            }
        if retry_count >= limit:
            return {"decision": "block", "reason": "effective retry limit exhausted", "policy_version": POLICY_VERSION, "effective_max_retries": limit, "retry_limit_source": source}
        return {"decision": "allow", "reason": "explicitly admitted new production fingerprint", "policy_version": POLICY_VERSION, "effective_max_retries": limit, "retry_limit_source": source, "same_condition": False}
    if status == "transient_retry":
        if retry_count >= limit:
            return {"decision": "block", "reason": "effective retry limit exhausted", "policy_version": POLICY_VERSION, "effective_max_retries": limit, "retry_limit_source": source}
        return {"decision": "allow", "reason": "transient retry semantics preserved; primary artifact is not ready", "policy_version": POLICY_VERSION, "effective_max_retries": limit, "retry_limit_source": source}
    if status == "hard_fail":
        plan_value = args.get("repair_plan_path")
        fingerprint = args.get("repair_adapter_fingerprint")
        if plan_value != state.get("repair_plan_path") or fingerprint != state.get("admitted_adapter_fingerprint"):
            return {"decision": "block", "reason": "hard-fail correction is not bound to the admitted plan and adapter fingerprint", "policy_version": POLICY_VERSION}
        return {"decision": "allow", "reason": "hash-bound hard-fail correction admitted", "policy_version": POLICY_VERSION, "effective_max_retries": limit, "retry_limit_source": source}
    return {"decision": "block", "reason": "unsupported production state", "policy_version": POLICY_VERSION}


def pre_tool_call(*, tool_name: str = "", args: Any = None, task_id: str | None = None, **_: Any) -> dict[str, Any] | None:
    if not isinstance(args, dict) or tool_name == "kanban_complete":
        return None
    tid = _task_id(task_id)
    if not tid:
        return None
    path = _state_path(args)
    if path is None or not path.is_file():
        return None
    state, error = _read_json(path)
    if error or state is None:
        return {"action": "block", "message": f"docs production retry guard: {error or 'state unavailable'}"}
    result = evaluate_production_call(tool_name, args, state, task_id=tid)
    if result.get("decision") == "block":
        return {"action": "block", "message": json.dumps({"policy": POLICY_VERSION, "state_path": str(path.resolve()), **result}, ensure_ascii=False, sort_keys=True)}
    return None
