"""Bounded copy-on-write repair controller for canonical artifact-quality runs.

The controller consumes only canonical AQ receipts and AQ-LAYOUT-01 scan evidence.
It never executes shell commands or command strings. Producers are callable adapters
registered explicitly in-process with immutable capabilities and a code fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PLAN_SCHEMA_VERSION = "docs-artifact-repair-plan-1"
CONTROLLER_SCHEMA_VERSION = "docs-artifact-repair-controller-receipt-1"
POLICY_VERSION = "docs-bounded-artifact-repair/1"
ISSUE_FAMILIES = frozenset({
    "boundary", "containment", "relation", "spacing", "alignment",
    "safe-wrap", "coverage", "identity", "semantic-surface",
})
OPERATIONS = frozenset({
    "geometry_adjustment", "spacing_adjustment", "alignment_adjustment",
    "wrap_adjustment", "coverage_adjustment", "identity_rebind",
    "surface_recompose",
})
MAPPINGS = {
    "rendered_orphan_ratio": ("safe-wrap", "wrap_adjustment"),
    "semantic_surface_area_ratio": ("semantic-surface", "surface_recompose"),
    "semantic_surface_sparse_container": ("semantic-surface", "surface_recompose"),
    "semantic_surface_consecutive_prominent_pages": ("semantic-surface", "surface_recompose"),
}
FORBIDDEN_KEYS = frozenset({
    "text", "body", "content", "raw", "payload", "original", "rewritten",
    "response_text", "actual_value", "expected_value", "command", "shell",
})
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^t_[a-z0-9]+$")


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def handle(path: Path) -> dict[str, Any]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError("handle target must be a regular file")
    return {"path": str(target), "sha256": sha256(target), "size_bytes": target.stat().st_size}


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in FORBIDDEN_KEYS or _contains_forbidden_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} is missing or not absolute")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def artifact_set_id(receipt: dict[str, Any]) -> str:
    finalization = receipt.get("finalization")
    if isinstance(finalization, dict) and _is_hash(finalization.get("artifact_set_id")):
        return finalization["artifact_set_id"]
    entries = receipt.get("entries")
    values = []
    if isinstance(entries, list):
        for entry in entries:
            artifact = entry.get("artifact") if isinstance(entry, dict) else None
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str) and _is_hash(artifact.get("sha256")):
                values.append({"path_id": digest({"path": str(Path(artifact["path"]).resolve())}), "sha256": artifact["sha256"]})
    if not values:
        raise ValueError("AQ receipt has no artifact set identity")
    return digest(sorted(values, key=lambda item: item["path_id"]))


@dataclass(frozen=True)
class Adapter:
    adapter_id: str
    capabilities: frozenset[tuple[str, str]]
    producer_kind: str
    invoke: Callable[[dict[str, Any]], dict[str, Any]]
    fingerprint: str


_ADAPTERS: dict[str, Adapter] = {}


def register_adapter(*, adapter_id: str, capabilities: set[tuple[str, str]] | frozenset[tuple[str, str]],
                     producer_kind: str, invoke: Callable[[dict[str, Any]], dict[str, Any]],
                     source_path: Path | None = None) -> Adapter:
    if not isinstance(adapter_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", adapter_id):
        raise ValueError("adapter_id is invalid")
    normalized = frozenset(capabilities)
    if not normalized or any(family not in ISSUE_FAMILIES or operation not in OPERATIONS for family, operation in normalized):
        raise ValueError("adapter capabilities are invalid")
    if not isinstance(producer_kind, str) or not producer_kind or not callable(invoke):
        raise ValueError("adapter declaration is incomplete")
    source = source_path or Path(getattr(invoke, "__code__", None).co_filename)
    source = source.resolve(strict=True)
    fingerprint = digest({
        "adapter_id": adapter_id,
        "producer_kind": producer_kind,
        "capabilities": sorted([list(item) for item in normalized]),
        "source": handle(source),
    })
    adapter = Adapter(adapter_id, normalized, producer_kind, invoke, fingerprint)
    existing = _ADAPTERS.get(adapter_id)
    if existing is not None and existing != adapter:
        raise ValueError("adapter_id is already registered with another fingerprint")
    _ADAPTERS[adapter_id] = adapter
    return adapter


def get_adapter(adapter_id: str) -> Adapter:
    adapter = _ADAPTERS.get(adapter_id)
    if adapter is None:
        raise ValueError("repair adapter is not explicitly registered")
    return adapter


def _layout_scan_handle(receipt: dict[str, Any]) -> dict[str, Any]:
    entries = receipt.get("entries")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError("repair requires exactly one canonical AQ artifact entry")
    rules = entries[0].get("rules")
    layout = next((item for item in rules or [] if isinstance(item, dict) and item.get("id") == "AQ-LAYOUT-01"), None)
    evidence = layout.get("evidence") if isinstance(layout, dict) else None
    if not isinstance(evidence, dict):
        raise ValueError("AQ-LAYOUT-01 scan evidence is missing")
    scan_path = Path(str(evidence.get("path", "")))
    if not scan_path.is_absolute() or not scan_path.is_file() or evidence.get("sha256") != sha256(scan_path):
        raise ValueError("AQ-LAYOUT-01 scan evidence hash is stale")
    return handle(scan_path)


def normalize_hard_failures(receipt_path: Path, *, task_id: str, contract_path: Path,
                            source_path: Path, adapter: Adapter, attempt: int,
                            max_attempts: int, producer_run_id: str,
                            new_revision_root: Path, content_identity_sha256: str) -> dict[str, Any]:
    if not TASK_RE.fullmatch(task_id or ""):
        raise ValueError("task identity is invalid")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt is invalid")
    if max_attempts not in {1, 2} or attempt > max_attempts:
        raise ValueError("repair attempt exceeds the bounded limit")
    if not _is_hash(content_identity_sha256):
        raise ValueError("source content identity is invalid")
    receipt = _read_object(receipt_path, "canonical AQ receipt")
    if receipt.get("schema_version") != "docs-artifact-quality-receipt-1" or receipt.get("status") != "block":
        raise ValueError("repair planning requires a canonical blocking AQ receipt")
    if _contains_forbidden_key(receipt):
        raise ValueError("canonical AQ receipt contains a body field")
    scan_handle = _layout_scan_handle(receipt)
    scan_path = Path(scan_handle["path"])
    scan = _read_object(scan_path, "AQ-LAYOUT-01 scan")
    if scan.get("artifact_sha256") is None or scan.get("status") != "block":
        raise ValueError("AQ scan is not a blocking artifact scan")
    reviews = scan.get("REVIEW_CANDIDATE")
    if not isinstance(reviews, list):
        raise ValueError("AQ scan review classification is missing")
    hard = scan.get("HARD_FAIL")
    if not isinstance(hard, list) or not hard:
        raise ValueError("AQ scan has no HARD_FAIL issues")
    issues = []
    for issue in hard:
        if not isinstance(issue, dict) or issue.get("bucket") != "HARD_FAIL" or issue.get("severity") != "hard":
            raise ValueError("AQ HARD_FAIL classification is malformed")
        rule = issue.get("rule")
        mapping = MAPPINGS.get(rule)
        if mapping is None:
            raise ValueError(f"unsupported HARD_FAIL issue: {rule}")
        family, operation = mapping
        if (family, operation) not in adapter.capabilities:
            raise ValueError("registered adapter lacks the admitted capability")
        target_seed = {key: issue.get(key) for key in ("id", "rule", "page", "frame_id_hash")}
        issues.append({
            "issue_id": digest(target_seed),
            "source_issue_id": issue.get("id"),
            "rule": rule,
            "family": family,
            "operation": operation,
        })
    entries = receipt.get("entries")
    artifact = entries[0].get("artifact") if isinstance(entries, list) else None
    artifact_path = Path(str(artifact.get("path", ""))) if isinstance(artifact, dict) else Path()
    if (not artifact_path.is_absolute() or not artifact_path.is_file()
            or artifact.get("sha256") != sha256(artifact_path)
            or scan.get("artifact_sha256") != artifact.get("sha256")):
        raise ValueError("artifact/scan hash identity is stale")
    revision_root = new_revision_root.resolve()
    old_root = artifact_path.resolve()
    if revision_root == old_root or old_root in revision_root.parents:
        raise ValueError("new revision root may not be nested under the old artifact file")
    core = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "status": "admitted",
        "task_id": task_id,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "contract_id": digest(_read_object(contract_path, "AQ contract")),
        "producer_run_id": producer_run_id,
        "artifact_set_id": artifact_set_id(receipt),
        "source": handle(source_path),
        "source_content_identity_sha256": content_identity_sha256,
        "artifact": handle(artifact_path),
        "scan": scan_handle,
        "scan_hash": digest(scan),
        "issues": sorted(issues, key=lambda item: item["issue_id"]),
        "adapter": {
            "adapter_id": adapter.adapter_id,
            "producer_kind": adapter.producer_kind,
            "fingerprint": adapter.fingerprint,
            "capabilities": sorted([{"family": family, "operation": operation} for family, operation in adapter.capabilities], key=lambda item: (item["family"], item["operation"])),
        },
        "old_artifact_path_id": digest({"path": str(old_root)}),
        "new_revision_root_id": digest({"path": str(revision_root)}),
    }
    if _contains_forbidden_key(core):
        raise ValueError("repair plan contains a forbidden body field")
    core["plan_hash"] = digest(core)
    return core


def validate_plan(plan: Any, *, expected_task_id: str | None = None, adapter: Adapter | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        return ["repair plan schema mismatch"]
    if _contains_forbidden_key(plan):
        errors.append("repair plan contains body or command fields")
    if plan.get("policy_version") != POLICY_VERSION or plan.get("status") != "admitted":
        errors.append("repair plan policy/status mismatch")
    if expected_task_id is not None and plan.get("task_id") != expected_task_id:
        errors.append("repair plan cross-task identity mismatch")
    if plan.get("max_attempts") not in {1, 2} or not isinstance(plan.get("attempt"), int) or plan.get("attempt") > plan.get("max_attempts", 0):
        errors.append("repair plan attempt bound is invalid")
    if not _is_hash(plan.get("source_content_identity_sha256")) or not _is_hash(plan.get("scan_hash")):
        errors.append("repair plan source/scan identity is invalid")
    for label in ("source", "artifact", "scan"):
        value = plan.get(label)
        path = Path(str(value.get("path", ""))) if isinstance(value, dict) else Path()
        try:
            if (not path.is_absolute() or not path.is_file() or value.get("sha256") != sha256(path)
                    or value.get("size_bytes") != path.stat().st_size):
                errors.append(f"repair plan {label} handle drift")
        except OSError:
            errors.append(f"repair plan {label} handle drift")
    declared = plan.get("adapter")
    if adapter is not None and (not isinstance(declared, dict) or declared.get("adapter_id") != adapter.adapter_id or declared.get("fingerprint") != adapter.fingerprint):
        errors.append("repair plan adapter fingerprint is not admitted")
    issues = plan.get("issues")
    if not isinstance(issues, list) or not issues:
        errors.append("repair plan has no admitted issues")
    else:
        for issue in issues:
            mapping = MAPPINGS.get(issue.get("rule")) if isinstance(issue, dict) else None
            if mapping != (issue.get("family"), issue.get("operation")):
                errors.append("repair plan issue mapping is unknown")
                break
    if plan.get("plan_hash") != digest({key: value for key, value in plan.items() if key != "plan_hash"}):
        errors.append("repair plan tamper detected")
    return list(dict.fromkeys(errors))


def write_plan(path: Path, plan: dict[str, Any]) -> Path:
    errors = validate_plan(plan)
    if errors:
        raise ValueError("invalid repair plan: " + "; ".join(errors))
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("repair plan receipt may not overwrite an existing file")
    target.write_bytes(canonical(plan) + b"\n")
    return target


def validate_surface_recompose_output(path: Path, *, plan: dict[str, Any], adapter: Adapter) -> list[str]:
    """Validate a producer-owned, body-free recompose boundary receipt.

    The generic controller never edits semantics or geometry itself.  A registered
    format adapter must prove requested/applied/effective state and equality of the
    content, text-order, and CJK-contract identities across its copy-on-write run.
    """
    try:
        value = _read_object(path, "surface recompose adapter output")
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    if (value.get("schema_version") != "docs-surface-recompose-adapter-output-1"
            or value.get("status") != "pass"):
        errors.append("surface recompose adapter output schema/status mismatch")
    if _contains_forbidden_key(value):
        errors.append("surface recompose adapter output contains body fields")
    if value.get("plan_hash") != plan.get("plan_hash"):
        errors.append("surface recompose adapter output plan identity drift")
    if value.get("adapter_id") != adapter.adapter_id or value.get("adapter_fingerprint") != adapter.fingerprint:
        errors.append("surface recompose adapter output adapter identity drift")
    for field in ("requested", "applied", "effective"):
        state = value.get(field)
        if not isinstance(state, dict) or state.get("schema_version") != f"docs-surface-recompose-{field}-1":
            errors.append(f"surface recompose {field} state missing")
    preserved = value.get("preserved")
    if not isinstance(preserved, dict) or set(preserved) != {
        "content_sha256_before", "content_sha256_after", "text_order_sha256_before",
        "text_order_sha256_after", "cjk_contract_sha256_before", "cjk_contract_sha256_after",
    }:
        errors.append("surface recompose preservation identities are incomplete")
    else:
        for prefix in ("content_sha256", "text_order_sha256", "cjk_contract_sha256"):
            before, after = preserved.get(prefix + "_before"), preserved.get(prefix + "_after")
            if not _is_hash(before) or before != after:
                errors.append(f"surface recompose {prefix} drift")
    return list(dict.fromkeys(errors))


def validate_controller_receipt(path: Path, *, expected_task_id: str | None = None,
                                final_aq_receipt: Path | None = None) -> list[str]:
    try:
        value = _read_object(path, "repair controller receipt")
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    if value.get("schema_version") != CONTROLLER_SCHEMA_VERSION or value.get("policy_version") != POLICY_VERSION:
        errors.append("repair controller receipt schema mismatch")
    if value.get("status") != "pass" or value.get("checker_exit_code") != 0:
        errors.append("repair controller did not converge to canonical pass")
    if expected_task_id is not None and value.get("task_id") != expected_task_id:
        errors.append("repair controller cross-task identity mismatch")
    if _contains_forbidden_key(value):
        errors.append("repair controller receipt contains body or command fields")
    attempts = value.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        errors.append("repair controller attempt history is invalid")
    else:
        for expected, item in enumerate(attempts, start=1):
            if not isinstance(item, dict) or item.get("attempt") != expected:
                errors.append("repair controller attempt sequence is invalid"); break
            for label in ("scan", "plan", "adapter_output", "aq_receipt"):
                if not isinstance(item.get(label), dict) or not _is_hash(item[label].get("sha256")):
                    errors.append(f"repair controller {label} handle is invalid"); break
    final_handle = value.get("final_aq_receipt")
    final_path = Path(str(final_handle.get("path", ""))) if isinstance(final_handle, dict) else Path()
    if final_aq_receipt is not None and final_path.resolve() != final_aq_receipt.resolve():
        errors.append("repair controller final AQ receipt path mismatch")
    try:
        final = _read_object(final_path, "final AQ receipt")
        if final.get("status") != "pass" or artifact_set_id(final) != value.get("artifact_set_id"):
            errors.append("repair controller final AQ pass/artifact-set mismatch")
        if final_handle.get("sha256") != sha256(final_path):
            errors.append("repair controller final AQ receipt hash drift")
    except (ValueError, OSError):
        errors.append("repair controller final AQ receipt is unavailable")
    if value.get("receipt_hash") != digest({key: item for key, item in value.items() if key != "receipt_hash"}):
        errors.append("repair controller receipt tamper detected")
    return list(dict.fromkeys(errors))


def run(*, task_id: str, contract_path: Path, source_path: Path, content_identity_sha256: str,
        initial_aq_receipt: Path, adapter_id: str, revision_root: Path, max_attempts: int,
        checker: Callable[[Path, Path, Path], tuple[dict[str, Any], int]],
        evidence_root: Path, output_receipt: Path, producer_run_id: str) -> tuple[dict[str, Any], int]:
    """Run scan→plan→registered adapter→regenerate→canonical recheck."""
    if max_attempts not in {1, 2}:
        raise ValueError("max_attempts must be 1 or 2")
    adapter = get_adapter(adapter_id)
    current_contract = contract_path.resolve(strict=True)
    current_receipt = initial_aq_receipt.resolve(strict=True)
    initial = _read_object(current_receipt, "initial AQ receipt")
    entries = initial.get("entries")
    first_artifact = Path(entries[0]["artifact"]["path"]).resolve(strict=True)
    old_hash = sha256(first_artifact)
    attempts: list[dict[str, Any]] = []
    previous_issue_signature: str | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_root = revision_root.resolve() / f"attempt-{attempt}"
        plan = normalize_hard_failures(
            current_receipt, task_id=task_id, contract_path=current_contract,
            source_path=source_path, adapter=adapter, attempt=attempt,
            max_attempts=max_attempts, producer_run_id=f"{producer_run_id}-a{attempt}",
            new_revision_root=attempt_root, content_identity_sha256=content_identity_sha256,
        )
        signature = digest([{"rule": item["rule"], "family": item["family"], "operation": item["operation"]} for item in plan["issues"]])
        if signature == previous_issue_signature:
            raise ValueError("non_convergent: repeated issue set")
        previous_issue_signature = signature
        plan_path = write_plan(attempt_root / "repair-plan.json", plan)
        try:
            result = adapter.invoke({
                "plan": plan,
                "plan_path": plan_path,
                "contract_path": current_contract,
                "source_path": source_path.resolve(strict=True),
                "revision_root": attempt_root,
                "evidence_root": evidence_root.resolve(),
            })
        except Exception as exc:
            raise ValueError(f"unrepairable: adapter-error: {exc}") from exc
        if not isinstance(result, dict) or set(result) != {"contract_path", "artifact_path", "adapter_output_path", "source_content_identity_sha256"}:
            raise ValueError("unrepairable: adapter output is malformed")
        if result.get("source_content_identity_sha256") != content_identity_sha256:
            raise ValueError("unrepairable: semantic source identity changed")
        next_contract = Path(result["contract_path"]).resolve(strict=True)
        next_artifact = Path(result["artifact_path"]).resolve(strict=True)
        adapter_output = Path(result["adapter_output_path"]).resolve(strict=True)
        if any(item.get("operation") == "surface_recompose" for item in plan["issues"]):
            boundary_errors = validate_surface_recompose_output(adapter_output, plan=plan, adapter=adapter)
            if boundary_errors:
                raise ValueError("unrepairable: " + "; ".join(boundary_errors))
        if next_artifact == first_artifact or next_artifact.parent == first_artifact:
            raise ValueError("unrepairable: adapter attempted old-root write")
        if sha256(first_artifact) != old_hash:
            raise ValueError("unrepairable: old artifact root changed")
        if sha256(next_artifact) == plan["artifact"]["sha256"]:
            raise ValueError("non_convergent: same artifact hash")
        next_receipt_path = attempt_root / "artifact-quality-receipt.json"
        next_evidence = attempt_root / "evidence"
        receipt, code = checker(next_contract, next_evidence, next_receipt_path)
        if not next_receipt_path.is_file():
            raise ValueError("checker-block: canonical checker did not write a receipt")
        scan = _layout_scan_handle(receipt)
        attempts.append({
            "attempt": attempt,
            "contract_id": digest(_read_object(next_contract, "revised AQ contract")),
            "producer_run_id": f"{producer_run_id}-a{attempt}",
            "artifact_set_id": artifact_set_id(receipt),
            "source": handle(source_path),
            "artifact": handle(next_artifact),
            "scan": scan,
            "plan": handle(plan_path),
            "adapter_output": handle(adapter_output),
            "aq_receipt": handle(next_receipt_path),
            "checker_exit_code": code,
        })
        if code == 0 and receipt.get("status") == "pass":
            value = {
                "schema_version": CONTROLLER_SCHEMA_VERSION,
                "policy_version": POLICY_VERSION,
                "status": "pass",
                "task_id": task_id,
                "max_attempts": max_attempts,
                "attempts": attempts,
                "artifact_set_id": artifact_set_id(receipt),
                "final_artifact": handle(next_artifact),
                "final_aq_receipt": handle(next_receipt_path),
                "checker_exit_code": 0,
                "old_artifact": {"path_id": digest({"path": str(first_artifact)}), "sha256": old_hash},
                "source_content_identity_sha256": content_identity_sha256,
            }
            value["receipt_hash"] = digest(value)
            target = output_receipt.resolve(); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(canonical(value) + b"\n")
            return value, 0
        current_contract, current_receipt = next_contract, next_receipt_path
    raise ValueError("non_convergent: max attempts exhausted")
