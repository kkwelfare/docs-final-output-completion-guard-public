"""Automatic advisory Jev route for generated PDF quality scans.

The route is selected by PDF/layout applicability, not a producer-supplied
``enabled`` switch. Fixture execution is diagnostic only and never substitutes
for a formal provider receipt or the owner's review disposition.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from . import jev_post_render


def _handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _dump(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def run(*, artifact: Path, scan: dict[str, Any], evidence_dir: Path,
        artifact_set_id: str | None = None, producer: dict[str, Any] | None = None,
        reviewer: dict[str, Any] | None = None,
        fixture_adapter: Callable | None = None) -> dict[str, Any]:
    """Select, evaluate, persist, and read back the generated-artifact route."""
    if artifact.suffix.lower() != ".pdf":
        return {"status": "not_applicable", "candidate_count": 0,
                "formal_provider_evidence": False}
    issues = scan.get("issues", [])
    review = [item for item in issues if isinstance(item, dict)
              and item.get("severity") == "review"]
    supported = [item for item in review if item.get("rule") in jev_post_render.RULE_CRITERIA]
    if scan.get("hard_issue_count", 0) or scan.get("status") == "block":
        return {"status": "not_applicable", "candidate_count": len(supported),
                "formal_provider_evidence": False, "reason_code": "deterministic_hard_fail"}
    if not review:
        return {"status": "scanned_no_candidates", "candidate_count": 0,
                "formal_provider_evidence": False}
    if not supported:
        return {"status": "connection_failed", "candidate_count": len(review),
                "formal_provider_evidence": False, "reason_code": "no_supported_candidate_type"}
    if not artifact_set_id or not isinstance(producer, dict) or not reviewer:
        return {"status": "connection_failed", "candidate_count": len(supported),
                "formal_provider_evidence": False, "reason_code": "evaluation_identity_unavailable"}

    # Bind only unchanged supported candidate descriptors; existing spacing
    # candidates retain their original rule and measurements.
    route_scan = {
        "schema_version": scan.get("schema_version"),
        "artifact_sha256": scan.get("artifact_sha256"),
        "measurement_basis": scan.get("measurement_basis"),
        "status": "review", "hard_issue_count": 0,
        "review_candidate_count": len(supported),
        "issues": copy.deepcopy(supported),
        "REVIEW_CANDIDATE": copy.deepcopy(supported), "HARD_FAIL": [],
    }
    raw_path = (evidence_dir / f"{artifact.stem}.AQ-LAYOUT-01.JEV.raw.json").resolve()
    request_path = (evidence_dir / f"{artifact.stem}.AQ-LAYOUT-01.JEV.request.json").resolve()
    receipt_path = (evidence_dir / f"{artifact.stem}.AQ-LAYOUT-01.JEV.receipt.json").resolve()
    try:
        _dump(raw_path, route_scan)
        request = jev_post_render.build_request(
            artifact=artifact, raw_scan=raw_path,
            artifact_set_id=artifact_set_id, producer=producer,
            reviewer=reviewer, allow_live_provider=True,
        )
        # Reuse unchanged, bound evidence during the owner readback pass.
        # Re-evaluating would change receipt timestamps and invalidate the
        # owner's scan hash even when the artifact and candidate set are fixed.
        receipt = None
        if request_path.is_file() and receipt_path.is_file():
            try:
                previous_request = jev_post_render.read_json(request_path)
                previous_receipt = jev_post_render.read_json(receipt_path)
                jev_post_render.validate_request(previous_request)
                jev_post_render.validate_schema(previous_receipt, "receipt")
                expected_mode = "fixture" if fixture_adapter is not None else "live"
                if (jev_post_render.digest(previous_request) == jev_post_render.digest(request)
                        and previous_receipt.get("request_sha256") == jev_post_render.digest(request)
                        and previous_receipt.get("execution_mode") == expected_mode):
                    receipt = previous_receipt
            except Exception:
                pass
        _dump(request_path, request)
        if receipt is None:
            receipt = jev_post_render.evaluate(request, fixture_adapter=fixture_adapter)
        _dump(receipt_path, receipt)
        # Re-open and validate exact persisted bytes. Fixture-only evidence is
        # explicitly non-formal, even when its diagnostic response is accepted.
        stored_request = jev_post_render.read_json(request_path)
        stored_receipt = jev_post_render.read_json(receipt_path)
        jev_post_render.validate_request(stored_request)
        jev_post_render.validate_schema(stored_receipt, "receipt")
        if stored_receipt.get("request_sha256") != jev_post_render.digest(stored_request):
            raise jev_post_render.PostRenderError("identity_drift")
        execution_mode = stored_receipt.get("execution_mode")
        evaluated = stored_receipt.get("status") != "unresolved"
        provider_invoked = stored_receipt.get("provider_invoked") is True
        formal_provider_evidence = (
            execution_mode == "live" and provider_invoked
            and stored_receipt.get("reason_code") == "ok"
            and stored_receipt.get("status") in {"accepted", "uncertain", "repair_required"}
        )
        return {
            "status": "evaluated" if evaluated else "connection_failed",
            "candidate_count": len(supported),
            "execution_mode": execution_mode,
            "provider_invoked": provider_invoked,
            "provider_status": stored_receipt.get("status"),
            "reason_code": stored_receipt.get("reason_code"),
            "formal_provider_evidence": formal_provider_evidence,
            "request": _handle(request_path), "receipt": _handle(receipt_path),
            "raw_scan": _handle(raw_path),
        }
    except Exception as exc:
        reason = str(exc) if isinstance(exc, jev_post_render.PostRenderError) else "provider_or_evidence_error"
        return {"status": "connection_failed", "candidate_count": len(supported),
                "formal_provider_evidence": False, "reason_code": reason,
                "raw_scan": _handle(raw_path) if raw_path.is_file() else None}


def validate_readback(scan: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Validate route selection and any persisted request/receipt handles."""
    if not isinstance(state, dict) or state.get("status") not in {
            "not_applicable", "scanned_no_candidates", "evaluated", "connection_failed"}:
        return "AQ-LAYOUT-01: generated-artifact Jev route status is missing or invalid"
    issues = scan.get("issues", []) if isinstance(scan.get("issues"), list) else []
    supported = [item for item in issues if isinstance(item, dict)
                 and item.get("severity") == "review"
                 and item.get("rule") in jev_post_render.RULE_CRITERIA]
    if state.get("candidate_count") != len(supported) and state.get("reason_code") != "no_supported_candidate_type":
        return "AQ-LAYOUT-01: generated-artifact Jev candidate count mismatch"
    if not supported and state.get("status") not in {"not_applicable", "scanned_no_candidates"}:
        return "AQ-LAYOUT-01: generated-artifact Jev incorrectly skipped candidate-free scan"
    if supported and state.get("status") == "scanned_no_candidates":
        return "AQ-LAYOUT-01: generated-artifact Jev candidate scan was omitted"
    if state.get("status") in {"evaluated", "connection_failed"}:
        if isinstance(state.get("request"), dict) or isinstance(state.get("receipt"), dict):
            try:
                request_path = jev_post_render.check_handle(state["request"])
                receipt_path = jev_post_render.check_handle(state["receipt"])
                request = jev_post_render.read_json(request_path)
                receipt = jev_post_render.read_json(receipt_path)
                jev_post_render.validate_request(request)
                jev_post_render.validate_schema(receipt, "receipt")
                expected_ids = {item["id"] for item in supported}
                receipt_mode = receipt.get("execution_mode")
                provider_invoked = receipt.get("provider_invoked") is True
                formal_provider_evidence = (
                    receipt_mode == "live" and provider_invoked
                    and receipt.get("reason_code") == "ok"
                    and receipt.get("status") in {"accepted", "uncertain", "repair_required"}
                )
                if ({candidate["id"] for candidate in request.get("candidates", [])} != expected_ids
                        or state.get("raw_scan") != request.get("binding", {}).get("scan")
                        or receipt_mode not in {"fixture", "live"}
                        or receipt.get("request_sha256") != jev_post_render.digest(request)
                        or state.get("provider_status") != receipt.get("status")
                        or state.get("provider_invoked") is not provider_invoked
                        or state.get("formal_provider_evidence") is not formal_provider_evidence
                        or (state.get("status") == "evaluated") != (receipt.get("status") != "unresolved")):
                    return "AQ-LAYOUT-01: generated-artifact Jev request/receipt readback mismatch"
            except Exception:
                return "AQ-LAYOUT-01: generated-artifact Jev request/receipt readback failed"
        elif state.get("status") == "evaluated" or not state.get("reason_code"):
            return "AQ-LAYOUT-01: generated-artifact Jev failed state lacks an explicit reason"
    return None
