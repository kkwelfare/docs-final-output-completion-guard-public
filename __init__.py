"""Fail-closed docs Kanban completion guard backed by canonical checker receipts.

The hook reads only completion metadata, receipt JSON, file paths, and hashes.
It never reads or logs artifact text.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import production_retry_guard
except ImportError:  # plugin loaders may import __init__.py as a standalone module
    _production_guard_spec = importlib.util.spec_from_file_location(
        "docs_final_output_production_retry_guard",
        Path(__file__).with_name("production_retry_guard.py"),
    )
    if _production_guard_spec is None or _production_guard_spec.loader is None:
        raise ImportError("production retry guard is unavailable")
    production_retry_guard = importlib.util.module_from_spec(_production_guard_spec)
    _production_guard_spec.loader.exec_module(production_retry_guard)

_execution_contract_spec = importlib.util.spec_from_file_location(
    "docs_final_output_execution_contract",
    Path(__file__).parent / "quality_rules" / "execution_contract.py",
)
if _execution_contract_spec is None or _execution_contract_spec.loader is None:
    raise ImportError("execution contract validator is unavailable")
execution_contract_rules = importlib.util.module_from_spec(_execution_contract_spec)
_execution_contract_spec.loader.exec_module(execution_contract_rules)

_delivery_workflow_spec = importlib.util.spec_from_file_location(
    "docs_final_output_delivery_workflow",
    Path(__file__).parent / "quality_rules" / "delivery_workflow.py",
)
if _delivery_workflow_spec is None or _delivery_workflow_spec.loader is None:
    raise ImportError("delivery workflow validator is unavailable")
delivery_workflow_rules = importlib.util.module_from_spec(_delivery_workflow_spec)
_delivery_workflow_spec.loader.exec_module(delivery_workflow_rules)

_quality_rules_namespace = "docs_final_output_quality_rules"
_quality_rules_package = types.ModuleType(_quality_rules_namespace)
_quality_rules_package.__path__ = [str(Path(__file__).parent / "quality_rules")]
_quality_rules_package.__package__ = _quality_rules_namespace
sys.modules[_quality_rules_namespace] = _quality_rules_package
_visual_spec = importlib.util.spec_from_file_location(
    f"{_quality_rules_namespace}.jev_post_render", Path(__file__).parent / "quality_rules" / "jev_post_render.py",
)
if _visual_spec is None or _visual_spec.loader is None:
    raise ImportError("Jev visual route is unavailable")
jev_post_render_rules = importlib.util.module_from_spec(_visual_spec)
sys.modules[_visual_spec.name] = jev_post_render_rules
_visual_spec.loader.exec_module(jev_post_render_rules)

SCHEMA_VERSION = "docs-final-output-receipt-1"
ARTIFACT_QUALITY_SCHEMA_VERSION = "docs-artifact-quality-receipt-1"
ARTIFACT_QUALITY_ROOT = Path(__file__).resolve().parent
_FILTER_ROOT_OVERRIDE = os.environ.get("FINAL_OUTPUT_LOCAL_FILTER_ROOT")
CANONICAL_ROOT = (
    Path(_FILTER_ROOT_OVERRIDE).expanduser().resolve()
    if _FILTER_ROOT_OVERRIDE
    else Path(__file__).resolve().parent / "canonical_checker"
)
CANONICAL_CHECKER = CANONICAL_ROOT / "check_document.py"
CANONICAL_CORE = CANONICAL_ROOT / "filter_core.py"
FORBIDDEN_RECEIPT_KEYS = frozenset({"text", "original", "rewritten", "response_text", "body", "value", "actual_value", "expected_value"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contains_body_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in FORBIDDEN_RECEIPT_KEYS or _contains_body_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_body_key(item) for item in value)
    return False


def _receipt_paths(args: dict[str, Any]) -> list[Path]:
    metadata = args.get("metadata")
    verification = metadata.get("verification") if isinstance(metadata, dict) else None
    section = verification.get("final_output_filter") if isinstance(verification, dict) else None
    values = section.get("receipts") if isinstance(section, dict) else None
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
        return []
    return [Path(item) for item in values]


def _final_output_filter_section(args: dict[str, Any]) -> dict[str, Any] | None:
    metadata = args.get("metadata")
    verification = metadata.get("verification") if isinstance(metadata, dict) else None
    section = verification.get("final_output_filter") if isinstance(verification, dict) else None
    return section if isinstance(section, dict) else None


RECEIPT_REUSE_SCHEMA_VERSION = "docs-receipt-reuse-1"
RECEIPT_REUSE_INVALIDATIONS = frozenset({
    "artifact_hash_drift", "receipt_hash_drift", "checker_version_drift",
    "scope_drift", "authority_missing",
})


def _receipt_reuse_section(args: dict[str, Any]) -> dict[str, Any] | None:
    section = _final_output_filter_section(args)
    value = section.get("receipt_reuse") if section else None
    return value if isinstance(value, dict) else None


def build_receipt_reuse_record(
    args: dict[str, Any], receipt_paths: list[Path], *, scope: str, authority: Path,
) -> dict[str, Any]:
    """Build a body-free PASS record after the normal receipt checker succeeds.

    The record is an optional handoff optimization.  It binds every path to its
    current SHA-256 and records the exact invalidation conditions; it never stores
    artifact or receipt bodies.  A later call may use it to skip the semantic
    receipt scan, but any identity drift falls back to the ordinary validator.
    """
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("receipt reuse scope is required")
    authority = authority.resolve(strict=True)
    artifacts = args.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("receipt reuse requires declared artifacts")
    artifact_handles = []
    for value in artifacts:
        path = Path(value).resolve(strict=True) if isinstance(value, str) else Path()
        artifact_handles.append({"path": str(path), "sha256": _sha256(path)})
    receipt_handles = []
    covered: set[str] = set()
    for path in receipt_paths:
        path = path.resolve(strict=True)
        receipt_covered, error = _validate_receipt(path)
        if error:
            raise ValueError(f"receipt reuse requires a current PASS receipt: {error}")
        covered.update(receipt_covered)
        receipt_handles.append({"path": str(path), "sha256": _sha256(path)})
    declared_paths = {str(Path(value).resolve()) for value in artifacts if isinstance(value, str)}
    if declared_paths != covered:
        raise ValueError("receipt reuse artifact set differs from PASS receipt coverage")
    checker = CANONICAL_CHECKER.resolve(strict=True)
    return {
        "schema_version": RECEIPT_REUSE_SCHEMA_VERSION,
        "status": "pass",
        "scope": scope,
        "authority": {"path": str(authority), "sha256": _sha256(authority)},
        "artifacts": artifact_handles,
        "receipts": receipt_handles,
        "receipt_schema_version": SCHEMA_VERSION,
        "receipt_status": "pass",
        "checker": {"path": str(checker), "sha256": _sha256(checker)},
        "invalidated_by": sorted(RECEIPT_REUSE_INVALIDATIONS),
    }


def _reuse_handle_error(value: Any, label: str) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, f"{label}_missing"
    raw_path, digest = value.get("path"), value.get("sha256")
    path = Path(raw_path) if isinstance(raw_path, str) else Path()
    if not path.is_absolute() or not path.is_file() or not isinstance(digest, str):
        return None, f"{label}_missing"
    try:
        actual = _sha256(path)
    except OSError:
        return None, f"{label}_missing"
    if actual != digest:
        return None, f"{label}_hash_drift"
    return str(path.resolve()), None


def _receipt_reuse_decision(args: dict[str, Any], receipt_paths: list[Path]) -> dict[str, Any]:
    """Return ``reuse`` only when all body-free identity bindings still match."""
    reuse = _receipt_reuse_section(args)
    if reuse is None:
        return {"decision": "not_requested", "covered": set()}
    revalidate = lambda reason: {"decision": "revalidate", "reason": reason, "covered": set()}
    if _contains_body_key(reuse):
        return revalidate("reuse_record_contains_body_field")
    if reuse.get("schema_version") != RECEIPT_REUSE_SCHEMA_VERSION or reuse.get("status") != "pass":
        return revalidate("reuse_record_schema_or_status_drift")
    section = _final_output_filter_section(args)
    current_scope = section.get("scope") if section else None
    if not isinstance(current_scope, str) or not current_scope:
        return revalidate("scope_missing")
    if reuse.get("scope") != current_scope:
        return revalidate("scope_drift")
    invalidated_by = reuse.get("invalidated_by")
    if not isinstance(invalidated_by, list) or set(invalidated_by) != RECEIPT_REUSE_INVALIDATIONS:
        return revalidate("invalidation_contract_drift")
    current_authority = section.get("authority")
    if not isinstance(current_authority, str) or not current_authority.startswith("/"):
        return revalidate("authority_missing")
    current_authority_path = Path(current_authority)
    if not current_authority_path.is_file():
        return revalidate("authority_missing")
    recorded_authority_path, error = _reuse_handle_error(reuse.get("authority"), "authority")
    if error:
        return revalidate("authority_missing" if error.endswith("_missing") else error)
    if recorded_authority_path != str(current_authority_path.resolve()):
        return revalidate("authority_scope_drift")
    if _sha256(current_authority_path) != reuse["authority"].get("sha256"):
        return revalidate("authority_hash_drift")
    checker = reuse.get("checker")
    expected_checker = {"path": str(CANONICAL_CHECKER.resolve()), "sha256": _sha256(CANONICAL_CHECKER)}
    if checker != expected_checker:
        return revalidate("checker_version_drift")
    artifacts = args.get("artifacts")
    recorded_artifacts = reuse.get("artifacts")
    if not isinstance(artifacts, list) or not isinstance(recorded_artifacts, list):
        return revalidate("artifact_scope_drift")
    declared_paths = {str(Path(value).resolve()) for value in artifacts if isinstance(value, str)}
    recorded_paths: set[str] = set()
    for handle in recorded_artifacts:
        path, error = _reuse_handle_error(handle, "artifact")
        if error:
            return revalidate(error)
        if path is not None:
            recorded_paths.add(path)
    if declared_paths != recorded_paths:
        return revalidate("artifact_scope_drift")
    recorded_receipts = reuse.get("receipts")
    if not isinstance(recorded_receipts, list):
        return revalidate("receipt_scope_drift")
    current_receipt_paths = {str(path.resolve()) for path in receipt_paths}
    receipt_paths_from_record: set[str] = set()
    for handle in recorded_receipts:
        path, error = _reuse_handle_error(handle, "receipt")
        if error:
            return revalidate(error)
        if path is not None:
            receipt_paths_from_record.add(path)
    if current_receipt_paths != receipt_paths_from_record:
        return revalidate("receipt_scope_drift")
    if reuse.get("receipt_schema_version") != SCHEMA_VERSION or reuse.get("receipt_status") != "pass":
        return revalidate("receipt_checker_version_drift")
    return {
        "decision": "reuse",
        "reason": "same path/hash/scope/authority/checker PASS receipt is reusable",
        "covered": declared_paths,
    }


def _read_json_object(path: Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_absolute() or not path.is_file():
        return None, f"{label} is missing or not an absolute file: {path}"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"{label} is unreadable or malformed: {path}"
    if not isinstance(value, dict):
        return None, f"{label} must be a JSON object: {path}"
    return value, None


def _verified_handle_map(handles: Any, label: str) -> tuple[dict[str, str], str | None]:
    if not isinstance(handles, list) or not handles:
        return {}, f"{label} has no artifact handles"
    result: dict[str, str] = {}
    for handle in handles:
        if not isinstance(handle, dict):
            return {}, f"{label} contains a non-object handle"
        raw_path, digest = handle.get("path"), handle.get("sha256")
        path = Path(raw_path) if isinstance(raw_path, str) else Path()
        if not path.is_absolute() or not path.is_file() or not isinstance(digest, str):
            return {}, f"{label} contains a missing path/hash handle: {raw_path}"
        canonical = str(path.resolve())
        if canonical in result:
            return {}, f"{label} contains a duplicate artifact path: {canonical}"
        if digest != _sha256(path):
            return {}, f"{label} hash drift detected: {canonical}"
        result[canonical] = digest
    return result, None


def _validate_receipt_generation(args: dict[str, Any], receipt_paths: list[Path]) -> str | None:
    """Verify one declared receipt generation without inventing quality evidence.

    The check activates only when a declared handoff manifest contains at least
    one ``final_output_receipt`` handle. Ordinary single-artifact deliveries that
    do not declare a generated handoff remain on the existing receipt path.
    """
    section = _final_output_filter_section(args)
    manifest_value = section.get("manifest") if section else None
    if not isinstance(manifest_value, str) or not manifest_value:
        return None
    manifest_path = Path(manifest_value)
    manifest, error = _read_json_object(manifest_path, "final-output handoff manifest")
    if error:
        return error
    produced = manifest.get("produced_artifacts")
    if not isinstance(produced, list) or not produced:
        return None
    declared_receipts = [
        item.get("final_output_receipt") if isinstance(item, dict) else None
        for item in produced
    ]
    if not any(isinstance(item, dict) for item in declared_receipts):
        return None
    if not all(isinstance(item, dict) for item in declared_receipts):
        return "declared receipt generation is missing a final-output receipt"

    handoff_map, error = _verified_handle_map(
        [item.get("artifact") if isinstance(item, dict) else None for item in produced],
        "handoff manifest",
    )
    if error:
        return error
    receipt_handle_map, error = _verified_handle_map(declared_receipts, "handoff receipt set")
    if error:
        return error
    declared_paths = [str(path.resolve()) for path in receipt_paths]
    if len(declared_paths) != len(set(declared_paths)):
        return "completion metadata contains a duplicate final-output receipt path"
    if set(declared_paths) != set(receipt_handle_map):
        return "completion receipt paths differ from the canonical handoff receipt set"

    final_handle = manifest.get("final_verification")
    final_map_handle, error = _verified_handle_map([final_handle], "final-verification handle")
    if error:
        return error
    final_path = Path(next(iter(final_map_handle)))
    final_verification, error = _read_json_object(final_path, "final-verification receipt")
    if error:
        return error
    if str(final_verification.get("status", "")).lower() != "pass":
        return "final-verification receipt status did not pass"
    final_map, error = _verified_handle_map(final_verification.get("artifacts"), "final-verification artifact set")
    if error:
        return error

    receipt_artifacts: dict[str, str] = {}
    for receipt_path in receipt_paths:
        receipt, error = _read_json_object(receipt_path, "final-output receipt")
        if error:
            return error
        entries = receipt.get("entries")
        if not isinstance(entries, list) or not entries:
            return f"final-output receipt has no entries: {receipt_path}"
        for entry in entries:
            if not isinstance(entry, dict):
                return f"final-output receipt contains a non-object entry: {receipt_path}"
            raw_path, digest = entry.get("artifact_path"), entry.get("artifact_sha256")
            artifact = Path(raw_path) if isinstance(raw_path, str) else Path()
            if not artifact.is_absolute() or not artifact.is_file() or not isinstance(digest, str):
                return f"final-output receipt contains a missing artifact path/hash: {receipt_path}"
            canonical = str(artifact.resolve())
            if canonical in receipt_artifacts:
                return f"final-output receipts contain a duplicate artifact path: {canonical}"
            if digest != _sha256(artifact):
                return f"final-output receipt artifact hash drift detected: {canonical}"
            receipt_artifacts[canonical] = digest

    if handoff_map != final_map or handoff_map != receipt_artifacts:
        return "canonical artifact path/sha256 maps differ across handoff, final verification, and final-output receipts"
    return None


def _validate_receipt(path: Path) -> tuple[set[str], str | None]:
    if not path.is_absolute() or not path.is_file():
        return set(), f"receipt path is missing or not an absolute file: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set(), f"receipt is unreadable or malformed: {path}"
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or data.get("status") != "pass":
        return set(), f"receipt schema/status did not pass: {path}"
    if _contains_body_key(data):
        return set(), f"receipt contains a forbidden body field: {path}"
    expected = {
        "canonical_checker_path": str(CANONICAL_CHECKER),
        "canonical_core_path": str(CANONICAL_CORE),
    }
    for key, value in expected.items():
        if data.get(key) != value:
            return set(), f"canonical import/path drift detected in {path}"
    try:
        if data.get("canonical_checker_sha256") != _sha256(CANONICAL_CHECKER):
            return set(), f"canonical checker hash drift detected in {path}"
        if data.get("canonical_core_sha256") != _sha256(CANONICAL_CORE):
            return set(), f"canonical core hash drift detected in {path}"
    except OSError:
        return set(), "canonical checker/core is unavailable"

    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return set(), f"receipt has no entries: {path}"
    covered: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "pass":
            return set(), f"receipt entry did not pass: {path}"
        artifact_value = entry.get("artifact_path")
        if not isinstance(artifact_value, str):
            return set(), f"receipt entry has no artifact path: {path}"
        artifact = Path(artifact_value)
        if not artifact.is_absolute() or not artifact.is_file():
            return set(), f"receipt artifact is missing: {artifact}"
        if entry.get("artifact_sha256") != _sha256(artifact):
            return set(), f"artifact changed after receipt: {artifact}"
        kind = entry.get("kind")
        if kind == "text":
            source_value = entry.get("source_text_path")
            source = Path(source_value) if isinstance(source_value, str) else None
            final = entry.get("final_readback")
            if source is None or not source.is_absolute() or not source.is_file():
                return set(), f"text source is missing for: {artifact}"
            if entry.get("source_text_sha256") != _sha256(source):
                return set(), f"text source changed after receipt: {source}"
            if not isinstance(final, dict) or final.get("changed") is not False:
                return set(), f"final canonical dry-run did not pass: {source}"
        elif kind == "non-text":
            source_value = entry.get("source_text_path")
            reason = entry.get("no_applicable_reason")
            if source_value:
                source = Path(source_value)
                final = entry.get("final_readback")
                if not source.is_absolute() or not source.is_file() or entry.get("source_text_sha256") != _sha256(source):
                    return set(), f"non-text source receipt is stale: {artifact}"
                if not isinstance(final, dict) or final.get("changed") is not False:
                    return set(), f"non-text source canonical dry-run did not pass: {source}"
            elif not isinstance(reason, str) or len(reason.strip()) < 12:
                return set(), f"non-text artifact lacks source text or a specific no-applicable reason: {artifact}"
        else:
            return set(), f"unknown receipt entry kind: {path}"
        covered.add(str(artifact.resolve()))
    return covered, None


def _artifact_quality_section(args: dict[str, Any]) -> dict[str, Any] | None:
    metadata = args.get("metadata")
    verification = metadata.get("verification") if isinstance(metadata, dict) else None
    section = verification.get("artifact_quality") if isinstance(verification, dict) else None
    return section if isinstance(section, dict) else None


def _delivery_workflow_section(args: dict[str, Any]) -> dict[str, Any] | None:
    metadata = args.get("metadata")
    verification = metadata.get("verification") if isinstance(metadata, dict) else None
    section = verification.get("delivery_workflow") if isinstance(verification, dict) else None
    return section if isinstance(section, dict) else None


def _artifact_repair_section(args: dict[str, Any]) -> dict[str, Any] | None:
    metadata = args.get("metadata")
    verification = metadata.get("verification") if isinstance(metadata, dict) else None
    section = verification.get("artifact_repair") if isinstance(verification, dict) else None
    return section if isinstance(section, dict) else None


def _validate_artifact_repair_completion(args: dict[str, Any], task_id: str, aq_receipts: list[Path]) -> str | None:
    section = _artifact_repair_section(args)
    if section is None:
        return None
    if set(section) != {"controller_receipt"}:
        return "artifact repair declaration must contain only controller_receipt"
    value = section.get("controller_receipt")
    path = Path(value) if isinstance(value, str) else Path()
    if not path.is_absolute() or not path.is_file():
        return "artifact repair controller receipt is missing or not absolute"
    try:
        controller = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "artifact repair controller receipt is unreadable or malformed"
    final_handle = controller.get("final_aq_receipt") if isinstance(controller, dict) else None
    final_path = Path(str(final_handle.get("path", ""))) if isinstance(final_handle, dict) else Path()
    declared = {str(item.resolve()) for item in aq_receipts}
    if str(final_path.resolve()) not in declared:
        return "artifact repair final AQ receipt is not in completion receipt set"
    errors = production_retry_guard.repair_controller.validate_controller_receipt(
        path, expected_task_id=task_id, final_aq_receipt=final_path,
    )
    return errors[0] if errors else None


def _validate_delivery_workflow_completion(args: dict[str, Any], task_id: str) -> str | None:
    """Validate the opt-in workflow without mutating producers or artifacts."""
    section = _delivery_workflow_section(args)
    if section is None:
        return None
    if set(section) != {"contract"}:
        return "delivery workflow declaration must contain only an absolute contract path"
    value = section.get("contract")
    contract_path = Path(value) if isinstance(value, str) else Path()
    if not contract_path.is_absolute() or not contract_path.is_file():
        return "delivery workflow contract is missing or not an absolute file"
    _, errors = delivery_workflow_rules.load_workflow(
        contract_path, expected_task_id=task_id, require_completion=True,
    )
    return errors[0] if errors else None


def _declared_contract_from_task(task_id: str | None = None) -> Path | None:
    """Resolve an explicit route/task declaration without guessing from suffixes."""
    if task_id is None:
        env_value = os.environ.get("HERMES_ARTIFACT_QUALITY_CONTRACT")
        if env_value and Path(env_value).is_absolute():
            return Path(env_value)
        task_id = os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_TASK_ID")
    if not task_id:
        return None
    try:
        from hermes_cli import kanban_db
        connection = kanban_db.connect()
        try:
            task = kanban_db.get_task(connection, task_id)
        finally:
            connection.close()
        body = task.body or ""
    except Exception:
        return None
    match = re.search(r"(?m)^artifact_quality_contract\s*:\s*(/\S+)\s*$", body)
    return Path(match.group(1)) if match else None


def _validate_scan_readback(
    scan: dict[str, Any], *, review_path: Path | None = None,
    artifact_set_id: str | None = None,
    visual_config: dict[str, Any] | None = None, artifact: Path | None = None,
    producer: dict[str, Any] | None = None,
) -> str | None:
    """Recount scanner output and re-read the review instead of trusting status."""
    limited_visual = scan.get("limited_visual_review")
    if isinstance(limited_visual, dict) and limited_visual.get("review_mode") == jev_post_render_rules.ROUTE:
        if visual_config is None or artifact is None:
            return "AQ-LAYOUT-01: visual route requires explicit opt-in"
        try:
            raw_path = jev_post_render_rules.check_handle(limited_visual["raw_scan"])
            raw = jev_post_render_rules.read_json(raw_path)
            candidates = jev_post_render_rules.scan_candidates(raw)
            # Do not trust producer-written status/counts or dropped hard issues.
            if (scan.get("issues") != raw.get("issues") or scan.get("hard_issue_count", 0) != 0
                    or scan.get("HARD_FAIL") or scan.get("status") != "pass"
                    or limited_visual.get("status") != "pass"
                    or limited_visual.get("vision_model_invoked") is not False
                    or limited_visual.get("evidence") != visual_config.get("receipt")
                    or limited_visual.get("candidate_count") != len(candidates)
                    or limited_visual.get("candidate_pages") != sorted({c["page"] for c in candidates})):
                return "AQ-LAYOUT-01: visual scan readback mismatch"
            evidence, error = jev_post_render_rules.consume(
                visual_config, artifact=artifact, artifact_set_id=artifact_set_id,
                producer=producer, raw_scan=raw_path,
            )
            if error:
                return error
            if scan.get("jev_post_render_review") != evidence:
                return "AQ-LAYOUT-01: visual receipt metadata mismatch"
            return None
        except Exception:
            return "AQ-LAYOUT-01: visual scan evidence invalid"
    issues = scan.get("issues")
    if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
        return "AQ-LAYOUT-01: scan issue set is malformed"
    hard = [item for item in issues if item.get("bucket") == "HARD_FAIL" and item.get("severity") == "hard"]
    review = [item for item in issues if item.get("bucket") == "REVIEW_CANDIDATE" and item.get("severity") == "review"]
    if len(hard) + len(review) != len(issues):
        return "AQ-LAYOUT-01: scan issue classification is incomplete"
    if (scan.get("hard_issue_count") != len(hard)
            or scan.get("review_candidate_count") != len(review)
            or scan.get("HARD_FAIL") != hard
            or scan.get("REVIEW_CANDIDATE") != review):
        return "AQ-LAYOUT-01: producer count/status differs from checker recount"
    limited = scan.get("limited_visual_review")
    if not isinstance(limited, dict):
        return "AQ-LAYOUT-01: limited visual review status is missing"
    pages = sorted({item.get("page") for item in review if isinstance(item.get("page"), int)})
    if limited.get("candidate_count") != len(review) or limited.get("candidate_pages") != pages:
        return "AQ-LAYOUT-01: review candidate coverage differs from checker recount"
    expected_status = "block" if hard else "pass" if (not review or limited.get("status") == "pass") else "review"
    if scan.get("status") != expected_status:
        return "AQ-LAYOUT-01: producer status differs from checker recount"
    if scan.get("measurement_basis") != "pdf_points":
        return "AQ-LAYOUT-01: scan measurement basis is not pdf_points"
    if review:
        if review_path is None:
            return "AQ-LAYOUT-01: review attachment is missing"
        review_data, error = _read_json_object(review_path, "AQ-LAYOUT-01 review attachment")
        if error or review_data is None:
            return error or "AQ-LAYOUT-01: review attachment is malformed"
        if artifact_set_id is not None:
            producer = (review_data.get("producer_task_id"), review_data.get("producer_run_id"))
            reviewer = (review_data.get("reviewer_task_id"), review_data.get("reviewer_run_id"))
            if (review_data.get("schema_version") != "docs-layout-visual-review-2"
                    or review_data.get("artifact_set_id") != artifact_set_id
                    or not producer[0] or not reviewer[0] or producer[0] == reviewer[0]
                    or review_data.get("llm_invoked") is not False
                    or review_data.get("vision_invoked") is not False):
                return "AQ-LAYOUT-01: review identity/artifact-set/tool-use contract failed"
            reviews = review_data.get("reviews")
            if not isinstance(reviews, list) or not reviews:
                return "AQ-LAYOUT-01: review candidate dispositions are missing"
            first = reviews[0]
            if (not isinstance(first, dict) or first.get("llm_invoked") is not False
                    or first.get("vision_invoked") is not False):
                return "AQ-LAYOUT-01: candidate[0] may not use LLM or vision"
    return None


def _validate_artifact_quality_receipt(contract_path: Path, receipt_path: Path) -> tuple[set[str], str | None]:
    if not contract_path.is_absolute() or not contract_path.is_file():
        return set(), "artifact-quality contract is missing or not an absolute file"
    if not receipt_path.is_absolute() or not receipt_path.is_file():
        return set(), "artifact-quality receipt is missing or not an absolute file"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set(), "artifact-quality contract/receipt is unreadable or malformed"
    if not isinstance(contract, dict) or not isinstance(receipt, dict):
        return set(), "artifact-quality contract/receipt must be objects"
    if receipt.get("schema_version") != ARTIFACT_QUALITY_SCHEMA_VERSION or receipt.get("status") != "pass":
        return set(), "artifact-quality receipt schema/status did not pass"
    if _contains_body_key(receipt) or _contains_body_key(contract):
        return set(), "artifact-quality contract/receipt contains forbidden body field"
    allowed_receipt_fields = {"schema_version", "created_at", "status", "contract", "checker", "rulesets", "entries", "quality_layers", "finalization", "execution_contract"}
    if not set(receipt).issubset(allowed_receipt_fields):
        return set(), "artifact-quality receipt has unsupported top-level fields"
    quality_layers = receipt.get("quality_layers")
    if not isinstance(quality_layers, list) or any(
        not isinstance(layer, dict) or layer.get("status") != "pass" for layer in quality_layers
    ):
        return set(), "artifact-quality composition layers did not pass"
    contract_handle = receipt.get("contract")
    if not isinstance(contract_handle, dict) or contract_handle.get("path") != str(contract_path.resolve()) or contract_handle.get("sha256") != _sha256(contract_path):
        return set(), "artifact-quality contract hash drift detected"
    execution_spec = contract.get("execution_contract")
    if execution_spec is not None:
        if not isinstance(execution_spec, dict):
            return set(), "execution contract declaration is malformed"
        execution_path = Path(execution_spec.get("path", ""))
        producer_path = Path(execution_spec.get("producer_receipt", ""))
        execution_data, execution_errors = execution_contract_rules.load_contract(execution_path, require_artifacts=True)
        if execution_data is None:
            return set(), "execution contract validation failed: " + "; ".join(execution_errors)
        producer_data, producer_errors = execution_contract_rules.validate_producer_receipt(producer_path, execution_data)
        if producer_data is None:
            return set(), "producer execution receipt validation failed: " + "; ".join(producer_errors)
        integrated = receipt.get("execution_contract")
        expected_integrated = {
            "contract": {"path": str(execution_path.resolve()), "sha256": _sha256(execution_path)},
            "producer_receipt": {"path": str(producer_path.resolve()), "sha256": _sha256(producer_path)},
            "execution_identity": execution_contract_rules.identity(execution_data),
        }
        if integrated != expected_integrated:
            return set(), "artifact-quality execution identity/receipt binding mismatch"
        final_handle = receipt.get("finalization")
        final_path = Path(final_handle.get("path", "")) if isinstance(final_handle, dict) else Path()
        final_data, final_error = _read_json_object(final_path, "integrated finalization manifest")
        if final_error or final_data is None:
            return set(), final_error or "integrated finalization manifest is missing"
        if final_data.get("execution_identity") != expected_integrated["execution_identity"]:
            return set(), "finalization execution identity mismatch"
        final_contract = final_data.get("execution_contract")
        final_producer = final_data.get("producer_receipt")
        if (not isinstance(final_contract, dict) or final_contract.get("canonical_path") != str(execution_path.resolve())
                or final_contract.get("sha256") != _sha256(execution_path)
                or not isinstance(final_producer, dict) or final_producer.get("canonical_path") != str(producer_path.resolve())
                or final_producer.get("sha256") != _sha256(producer_path)):
            return set(), "finalization contract/producer receipt binding mismatch"
    try:
        created_at = datetime.fromisoformat(str(receipt.get("created_at")).replace("Z", "+00:00"))
        if created_at.tzinfo is None or created_at.timestamp() + 1 < contract_path.stat().st_mtime or created_at > datetime.now(timezone.utc):
            return set(), "artifact-quality receipt is stale or future-dated"
    except (TypeError, ValueError, OSError):
        return set(), "artifact-quality receipt timestamp is invalid"
    checker = ARTIFACT_QUALITY_ROOT / "check_docs_artifact_quality.py"
    checker_handle = receipt.get("checker")
    if not isinstance(checker_handle, dict) or checker_handle.get("path") != str(checker) or checker_handle.get("sha256") != _sha256(checker):
        return set(), "artifact-quality checker hash drift detected"
    try:
        import sys
        if str(ARTIFACT_QUALITY_ROOT) not in sys.path:
            sys.path.insert(0, str(ARTIFACT_QUALITY_ROOT))
        from quality_rules import common, fax, finalization
    except Exception:
        return set(), "artifact-quality ruleset is unavailable"
    declared = contract.get("artifacts")
    if not isinstance(declared, list) or not declared:
        return set(), "artifact-quality contract has no declared artifacts"
    finalization_spec = contract.get("finalization_manifest")
    quality_spec = contract.get("artifact_quality")
    manifest: dict[str, Any] | None = None
    finalization_required = isinstance(quality_spec, dict) and quality_spec.get("required") is True
    if finalization_required and not isinstance(finalization_spec, dict):
        return set(), "finalization manifest and receiver receipt are required for an artifact-quality target"
    if finalization_spec is not None:
        if not isinstance(finalization_spec, dict):
            return set(), "finalization manifest declaration is malformed"
        manifest_value = finalization_spec.get("path")
        manifest_path = Path(manifest_value) if isinstance(manifest_value, str) else Path()
        manifest, manifest_error = finalization.validate_manifest(manifest_path, declared, require_identity=True)
        if manifest_error or manifest is None:
            return set(), f"docs artifact-quality guard: {manifest_error or 'finalization manifest invalid'}"
        if manifest.get("state") != "accepted":
            return set(), "docs artifact-quality guard: finalization manifest is not accepted"
        receipt_finalization = receipt.get("finalization")
        if (not isinstance(receipt_finalization, dict)
                or receipt_finalization.get("path") != str(manifest_path.resolve())
                or receipt_finalization.get("sha256") != _sha256(manifest_path)
                or receipt_finalization.get("artifact_set_id") != manifest.get("artifact_set_id")):
            return set(), "docs artifact-quality guard: checker receipt is not bound to the finalization manifest"
        receiver_value = finalization_spec.get("receiver_receipt")
        receiver_path = Path(receiver_value) if isinstance(receiver_value, str) else Path()
        receiver_error = finalization.validate_receiver_copy(manifest, receiver_path, require_identity=True)
        if receiver_error:
            return set(), f"docs artifact-quality guard: {receiver_error}"
    expected_rulesets = {
        "common-1": ARTIFACT_QUALITY_ROOT / "quality_rules/common.py",
        "rendered-readback-1": ARTIFACT_QUALITY_ROOT / "quality_rules/rendered_readback.py",
        "layout-typography-1": ARTIFACT_QUALITY_ROOT / "quality_rules/layout_typography.py",
        "source-typography-1": ARTIFACT_QUALITY_ROOT / "quality_rules/source_typography.py",
    }
    if fax.applies(contract):
        expected_rulesets["fax-1"] = ARTIFACT_QUALITY_ROOT / "quality_rules/fax.py"
    recorded = {x.get("id"): x for x in receipt.get("rulesets", []) if isinstance(x, dict)}
    for rule_id, path in expected_rulesets.items():
        item = recorded.get(rule_id)
        if not item or item.get("path") != str(path) or item.get("sha256") != _sha256(path):
            return set(), "artifact-quality ruleset hash drift detected"
    entries = receipt.get("entries")
    if not isinstance(entries, list) or not entries:
        return set(), "artifact-quality receipt has no entries"
    covered: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "pass":
            return set(), "artifact-quality entry did not pass"
        if not set(entry).issubset({"artifact", "actual_delivery_readback", "rules", "status", "local_readback", "global_readback"}):
            return set(), "artifact-quality entry has unsupported fields"
        artifact_handle = entry.get("artifact")
        artifact = Path(str(artifact_handle.get("path", ""))) if isinstance(artifact_handle, dict) else Path()
        if not artifact.is_absolute() or not artifact.is_file() or artifact_handle.get("sha256") != _sha256(artifact):
            return set(), "artifact-quality artifact hash drift detected"
        validation_entry = {"artifact_path": str(artifact), "artifact_sha256": artifact_handle["sha256"], **{key: value for key, value in entry.items() if key != "artifact"}}
        errors = common.validate_entry(contract, validation_entry) + fax.validate_entry(contract, validation_entry)
        if errors:
            return set(), f"artifact-quality rule validation failed: {errors[0]}"
        if artifact.suffix.lower() == ".pdf":
            layout_rule = next((rule for rule in entry.get("rules", []) if isinstance(rule, dict) and rule.get("id") == "AQ-LAYOUT-01"), None)
            evidence_handle = layout_rule.get("evidence") if isinstance(layout_rule, dict) else None
            if not isinstance(layout_rule, dict) or layout_rule.get("status") != "pass" or not isinstance(evidence_handle, dict):
                return set(), "AQ-LAYOUT-01: passed rule and evidence are required"
            scan_path = Path(str(evidence_handle.get("path", "")))
            if (not scan_path.is_absolute() or not scan_path.is_file()
                    or evidence_handle.get("sha256") != _sha256(scan_path)):
                return set(), "AQ-LAYOUT-01: scan evidence path/hash drift"
            try:
                scan = json.loads(scan_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return set(), "AQ-LAYOUT-01: scan evidence is malformed"
            review = scan.get("limited_visual_review") if isinstance(scan, dict) else None
            if (not isinstance(scan, dict) or scan.get("schema_version") != "docs-layout-typography-scan-3"
                    or scan.get("artifact_sha256") != _sha256(artifact)
                    or not isinstance(review, dict) or review.get("status") != "pass"):
                return set(), "AQ-LAYOUT-01: hard issues or unresolved review candidates remain"
            review_path: Path | None = None
            if review.get("required") is True:
                review_handle = review.get("evidence")
                if not isinstance(review_handle, dict):
                    return set(), "AQ-LAYOUT-01: limited visual review evidence is missing"
                review_path = Path(str(review_handle.get("path", "")))
                if not review_path.is_absolute() or not review_path.is_file() or review_handle.get("sha256") != _sha256(review_path):
                    return set(), "AQ-LAYOUT-01: limited visual review evidence drift"
            scan_error = _validate_scan_readback(
                scan, review_path=review_path,
                artifact_set_id=manifest.get("artifact_set_id") if manifest else None,
                visual_config=(contract.get("artifact_quality", {}).get("layout_typography", {}).get("jev_post_render_review")),
                artifact=artifact,
                producer=({"task_id": manifest.get("created_by_task_id"), "run_id": manifest.get("created_by_run_id")} if manifest else None),
            )
            if scan_error:
                return set(), scan_error
        covered.add(str(artifact.resolve()))
    target_paths = {str(path.resolve()) for path in common.target_artifacts(contract)}
    if target_paths != covered:
        return set(), "artifact-quality receipt coverage mismatch"
    return covered, None


def _parallel_production_section(args: dict[str, Any]) -> dict[str, Any] | None:
    metadata = args.get("metadata")
    verification = metadata.get("verification") if isinstance(metadata, dict) else None
    section = verification.get("parallel_production") if isinstance(verification, dict) else None
    return section if isinstance(section, dict) else None


_PARALLEL_IMPORT_LOCK = threading.RLock()
_PARALLEL_CONSUMER: Any = None


def _load_parallel_consumer() -> Any:
    global _PARALLEL_CONSUMER
    with _PARALLEL_IMPORT_LOCK:
        if _PARALLEL_CONSUMER is not None:
            return _PARALLEL_CONSUMER
        path = Path(__file__).with_name("parallel_receipt_consumer.py")
        if not path.is_file():
            raise RuntimeError("parallel receipt consumer is unavailable")
        import_path = str(path.parent)
        added = import_path not in sys.path
        if added:
            sys.path.insert(0, import_path)
        try:
            spec = importlib.util.spec_from_file_location(
                "docs_parallel_receipt_consumer", path,
            )
            if spec is None or spec.loader is None:
                raise RuntimeError("parallel receipt consumer cannot be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            _PARALLEL_CONSUMER = module
            return module
        finally:
            if added and sys.path and sys.path[0] == import_path:
                sys.path.pop(0)


def _validate_parallel_completion(args: dict[str, Any], task_id: str) -> str | None:
    section = _parallel_production_section(args)
    if section is None:
        return None
    receipt = section.get("integration_receipt")
    if not isinstance(receipt, str) or not receipt.strip():
        return "parallel production completion requires integration_receipt"
    artifacts = args.get("artifacts") or []
    if not isinstance(artifacts, list):
        return "parallel production completion artifacts must be a list"
    try:
        consumer = _load_parallel_consumer()
        errors = consumer.verify_integration_receipt(
            receipt, task_id=task_id, artifacts=artifacts,
        )
    except Exception as exc:
        return f"parallel receipt consumer failed: {exc}"
    return errors[0] if errors else None


def _pre_tool_call(*, tool_name: str = "", args: Any = None, task_id: str | None = None, **kwargs: Any):
    production_block = production_retry_guard.pre_tool_call(
        tool_name=tool_name, args=args, task_id=task_id, **kwargs,
    )
    if production_block is not None:
        return production_block
    if tool_name != "kanban_complete":
        return None
    task = task_id if task_id is not None else (
        os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_TASK_ID")
    )
    if not task:
        return None
    tool_args = args if isinstance(args, dict) else {}
    workflow_error = _validate_delivery_workflow_completion(tool_args, task)
    if workflow_error:
        return {"action": "block", "message": f"docs delivery-workflow guard: {workflow_error}"}
    receipts = _receipt_paths(tool_args)
    if not receipts:
        return {"action": "block", "message": "docs final-output guard: completion metadata must include verification.final_output_filter.receipts"}
    generation_error = _validate_receipt_generation(tool_args, receipts)
    if generation_error:
        return {"action": "block", "message": f"docs final-output guard: {generation_error}"}
    artifacts = tool_args.get("artifacts") or []
    if not isinstance(artifacts, list):
        return {"action": "block", "message": "docs final-output guard: artifacts must be a list"}
    reuse_decision = _receipt_reuse_decision(tool_args, receipts)
    if reuse_decision["decision"] == "reuse":
        covered = set(reuse_decision["covered"])
    else:
        covered = set()
        for receipt in receipts:
            receipt_covered, error = _validate_receipt(receipt)
            if error:
                return {"action": "block", "message": f"docs final-output guard: {error}"}
            covered.update(receipt_covered)
    missing = [str(Path(item).resolve()) for item in artifacts if isinstance(item, str) and str(Path(item).resolve()) not in covered]
    if missing:
        return {"action": "block", "message": f"docs final-output guard: artifact has no valid checker receipt: {missing[0]}"}
    parallel_error = _validate_parallel_completion(tool_args, task)
    if parallel_error:
        return {"action": "block", "message": f"docs parallel-production guard: {parallel_error}"}
    quality = _artifact_quality_section(tool_args)
    target_suffixes = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".html", ".htm", ".pptx", ".docx", ".xlsx", ".odt", ".ods"}
    delivered_target = any(isinstance(value, str) and Path(value).suffix.lower() in target_suffixes for value in artifacts)
    if quality is None:
        if delivered_target or _declared_contract_from_task(task_id) is not None:
            return {"action": "block", "message": "docs artifact-quality guard: target completion requires artifact_quality contract and receipts"}
        return None
    contract_value = quality.get("contract")
    receipt_values = quality.get("receipts")
    if not isinstance(contract_value, str) or not contract_value or not isinstance(receipt_values, list) or not receipt_values:
        return {"action": "block", "message": "docs artifact-quality guard: target completion requires contract and receipts"}
    contract_path = Path(contract_value)
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"action": "block", "message": "docs artifact-quality guard: contract is unreadable"}
    target_required = isinstance(contract, dict) and isinstance(contract.get("artifact_quality"), dict) and contract["artifact_quality"].get("required") is True and bool(contract.get("artifact_kind")) and bool(contract.get("artifacts"))
    if not target_required:
        return {"action": "block", "message": "docs artifact-quality guard: contract does not declare a required target"}
    finalization_spec = contract.get("finalization_manifest")
    if not isinstance(finalization_spec, dict):
        return {"action": "block", "message": "docs artifact-quality guard: required target must declare finalization_manifest with manifest and receiver receipt"}
    if (not isinstance(finalization_spec.get("path"), str) or not finalization_spec.get("path")
            or not isinstance(finalization_spec.get("receiver_receipt"), str) or not finalization_spec.get("receiver_receipt")):
        return {"action": "block", "message": "docs artifact-quality guard: strict finalization_manifest requires path and receiver_receipt"}
    try:
        import sys
        if str(ARTIFACT_QUALITY_ROOT) not in sys.path:
            sys.path.insert(0, str(ARTIFACT_QUALITY_ROOT))
        from quality_rules import common
        layout_contract_errors = common.validate_pdf_layout_contract(contract)
        if layout_contract_errors:
            return {"action": "block", "message": f"docs artifact-quality guard: {layout_contract_errors[0]}"}
        quality_targets = {str(path.resolve()) for path in common.target_artifacts(contract)}
    except Exception:
        return {"action": "block", "message": "docs artifact-quality guard: target ruleset is unavailable"}
    declared = {str(Path(value).resolve()) for value in contract.get("artifacts", []) if isinstance(value, str)}
    delivered = {str(Path(value).resolve()) for value in artifacts if isinstance(value, str)}
    if declared != delivered:
        return {"action": "block", "message": "docs artifact-quality guard: contract artifacts must exactly match delivered artifacts"}
    if not quality_targets:
        return None
    quality_covered: set[str] = set()
    for value in receipt_values:
        if not isinstance(value, str):
            return {"action": "block", "message": "docs artifact-quality guard: receipt path must be a string"}
        covered_paths, error = _validate_artifact_quality_receipt(contract_path, Path(value))
        if error:
            return {"action": "block", "message": f"docs artifact-quality guard: {error}"}
        quality_covered.update(covered_paths)
    if quality_targets != quality_covered:
        return {"action": "block", "message": "docs artifact-quality guard: delivered artifact coverage mismatch"}
    repair_error = _validate_artifact_repair_completion(tool_args, task, [Path(value) for value in receipt_values if isinstance(value, str)])
    if repair_error:
        return {"action": "block", "message": f"docs artifact-repair guard: {repair_error}"}
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    jev_post_render_rules.register(ctx)
