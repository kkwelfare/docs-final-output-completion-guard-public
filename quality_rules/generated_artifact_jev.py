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

# Status for routes that stopped before any Jev/provider call. It is deliberately
# distinct from "connection_failed", which is reserved for a real provider/transport failure.
NOT_INVOKED = "not_invoked"


def _handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _dump(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def run(*, artifact: Path, scan: dict[str, Any], evidence_dir: Path,
        artifact_set_id: str | None = None, producer: dict[str, Any] | None = None,
        reviewer: dict[str, Any] | None = None,
        allow_live_provider: bool = False,
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
                "formal_provider_evidence": False, "evaluation_state": "error",
                "reason_code": "deterministic_hard_fail"}
    if review and not supported:
        return {"status": NOT_INVOKED, "candidate_count": len(review),
                "formal_provider_evidence": False, "evaluation_state": "error",
                "reason_code": "no_supported_candidate_type"}
    valid_id = (isinstance(artifact_set_id, str) and len(artifact_set_id) == 64
                and all(ch in "0123456789abcdef" for ch in artifact_set_id))
    valid_identity = lambda value: (
        isinstance(value, dict) and isinstance(value.get("task_id"), str)
        and bool(value.get("task_id")) and isinstance(value.get("run_id"), int)
        and not isinstance(value.get("run_id"), bool) and value["run_id"] > 0
    )
    if reviewer is None and valid_identity(producer):
        # Master decision: the producing task may call Jev itself; a separate reviewer
        # task is not required. Without an explicit reviewer the producer identity is used.
        reviewer = dict(producer)
    if not valid_id or not valid_identity(producer) or not valid_identity(reviewer):
        return {"status": NOT_INVOKED, "candidate_count": len(supported),
                "formal_provider_evidence": False, "evaluation_state": "error",
                "reason_code": "evaluation_identity_unavailable"}

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
    receipt = None  # set once evidence is reused or jev_post_render.evaluate() has returned
    evaluation_started = False  # True from the moment jev_post_render.evaluate() is entered
    try:
        _dump(raw_path, route_scan)
        request = jev_post_render.build_request(
            artifact=artifact, raw_scan=raw_path,
            artifact_set_id=artifact_set_id, producer=producer,
            reviewer=reviewer, allow_live_provider=allow_live_provider,
        )
        # Reuse unchanged, bound evidence during the owner readback pass.
        # Re-evaluating would change receipt timestamps and invalidate the
        # owner's scan hash even when the artifact and candidate set are fixed.
        receipt = None
        evidence_reused = False
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
                    # Split requests reuse only a receipt whose chunk records match this request.
                    jev_post_render.validate_receipt_chunks(request, previous_receipt)
                    receipt = previous_receipt
                    evidence_reused = True
            except Exception:
                pass
        _dump(request_path, request)
        if receipt is None:
            evaluation_started = True
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
        jev_post_render.validate_receipt_chunks(stored_request, stored_receipt)
        execution_mode = stored_receipt.get("execution_mode")
        evaluated = stored_receipt.get("status") != "unresolved"
        provider_invoked = stored_receipt.get("provider_invoked") is True
        formal_provider_evidence = (
            execution_mode == "live" and provider_invoked
            and stored_receipt.get("reason_code") == "ok"
            and stored_receipt.get("status") in {"accepted", "uncertain", "repair_required"}
        )
        state = {
            "status": ("evaluated" if evaluated
                       else "connection_failed" if provider_invoked else NOT_INVOKED),
            "evaluation_state": "evidence_reused" if evidence_reused else "executed",
            "candidate_count": len(supported),
            "review_scope": request["binding"]["review_scope"],
            "execution_mode": execution_mode,
            "provider_invoked": provider_invoked,
            "provider_status": stored_receipt.get("status"),
            "reason_code": stored_receipt.get("reason_code"),
            "formal_provider_evidence": formal_provider_evidence,
            "request": _handle(request_path), "receipt": _handle(receipt_path),
            "raw_scan": _handle(raw_path),
        }
        if isinstance(stored_receipt.get("wire_chunks"), list):
            state["wire_chunk_count"] = len(stored_receipt["wire_chunks"])
        return state
    except Exception as exc:
        reason = str(exc) if isinstance(exc, jev_post_render.PostRenderError) else "provider_or_evidence_error"
        failure = {"candidate_count": len(supported),
                   "formal_provider_evidence": False, "evaluation_state": "error", "reason_code": reason,
                   "raw_scan": _handle(raw_path) if raw_path.is_file() else None}
        # Only a failure after the Jev/provider call is "connection_failed". An exception raised
        # before the call (e.g. jev_glyph_locale.measure -> insufficient_evidence while the request is
        # built) never reached the provider: keep its own reason and record it as not_invoked.
        if receipt is not None:
            invoked = receipt.get("provider_invoked") is True
        else:
            # evaluate() itself raised, so the provider call cannot be ruled out: keep connection_failed.
            invoked = None if evaluation_started else False
        if invoked is False:
            return {"status": NOT_INVOKED, "provider_invoked": False, **failure}
        if invoked:
            failure["provider_invoked"] = True
        return {"status": "connection_failed", **failure}


def validate_readback(scan: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Validate route selection and any persisted request/receipt handles."""
    if not isinstance(state, dict) or state.get("status") not in {
            "not_applicable", "scanned_no_candidates", "evaluated", "connection_failed", NOT_INVOKED}:
        return "AQ-LAYOUT-01: generated-artifact Jev route status is missing or invalid"
    if state.get("status") == NOT_INVOKED and state.get("provider_invoked") is True:
        return "AQ-LAYOUT-01: generated-artifact Jev not_invoked state conflicts with provider invocation"
    issues = scan.get("issues", []) if isinstance(scan.get("issues"), list) else []
    supported = [item for item in issues if isinstance(item, dict)
                 and item.get("severity") == "review"
                 and item.get("rule") in jev_post_render.RULE_CRITERIA]
    if state.get("candidate_count") != len(supported) and state.get("reason_code") != "no_supported_candidate_type":
        return "AQ-LAYOUT-01: generated-artifact Jev candidate count mismatch"
    if state.get("status") not in {"scanned_no_candidates", "not_applicable"} and state.get("evaluation_state") not in {
            "executed", "evidence_reused", "error"}:
        return "AQ-LAYOUT-01: generated-artifact Jev execution state is missing or invalid"
    if state.get("status") == "evaluated" and state.get("evaluation_state") == "error":
        return "AQ-LAYOUT-01: generated-artifact Jev evaluated state conflicts with execution error"
    if supported and state.get("status") == "scanned_no_candidates":
        return "AQ-LAYOUT-01: generated-artifact Jev candidate scan was omitted"
    if state.get("status") in {"evaluated", "connection_failed", NOT_INVOKED}:
        if isinstance(state.get("request"), dict) or isinstance(state.get("receipt"), dict):
            try:
                request_path = jev_post_render.check_handle(state["request"])
                receipt_path = jev_post_render.check_handle(state["receipt"])
                request = jev_post_render.read_json(request_path)
                receipt = jev_post_render.read_json(receipt_path)
                jev_post_render.validate_request(request)
                jev_post_render.validate_schema(receipt, "receipt")
                jev_post_render.validate_receipt_chunks(request, receipt)
                expected_ids = {item["id"] for item in supported}
                expected_scope = "candidates" if supported else "whole_artifact"
                receipt_mode = receipt.get("execution_mode")
                provider_invoked = receipt.get("provider_invoked") is True
                formal_provider_evidence = (
                    receipt_mode == "live" and provider_invoked
                    and receipt.get("reason_code") == "ok"
                    and receipt.get("status") in {"accepted", "uncertain", "repair_required"}
                )
                whole_artifact_valid = (
                    request.get("binding", {}).get("review_scope", "candidates") == expected_scope
                    and state.get("review_scope", expected_scope) == expected_scope
                    and ((bool(supported) and "whole_artifact" not in request)
                         or (not supported and isinstance(request.get("whole_artifact"), dict)
                             and request["whole_artifact"].get("scope") == "all_pages_structural_review"
                             and request["whole_artifact"].get("metrics_sha256")
                             == jev_post_render.digest(request["whole_artifact"].get("metrics"))))
                    and ((bool(supported) and "whole_artifact_review" not in receipt)
                         or (not supported and receipt.get("status") == "unresolved"
                             or (not supported and isinstance(receipt.get("whole_artifact_review"), dict))))
                )
                if ({candidate["id"] for candidate in request.get("candidates", [])} != expected_ids
                        or state.get("raw_scan") != request.get("binding", {}).get("scan")
                        or not whole_artifact_valid
                        or receipt_mode not in {"fixture", "live"}
                        or receipt.get("request_sha256") != jev_post_render.digest(request)
                        or state.get("provider_status") != receipt.get("status")
                        or state.get("provider_invoked") is not provider_invoked
                        or state.get("formal_provider_evidence") is not formal_provider_evidence
                        or (state.get("status") == "evaluated") != (receipt.get("status") != "unresolved")
                        or state.get("wire_chunk_count") != (len(receipt["wire_chunks"])
                                                             if isinstance(receipt.get("wire_chunks"), list) else None)):
                    return "AQ-LAYOUT-01: generated-artifact Jev request/receipt readback mismatch"
            except Exception:
                return "AQ-LAYOUT-01: generated-artifact Jev request/receipt readback failed"
        elif state.get("status") == "evaluated" or not state.get("reason_code"):
            return "AQ-LAYOUT-01: generated-artifact Jev failed state lacks an explicit reason"
    return None


# Plan C (master decision): a layout-required generated PDF that Jev could evaluate does not
# complete until this artifact has live provider evidence. Only "Jev was not run" stops;
# the Jev result itself (accepted / uncertain / repair_required) keeps its existing handling.
NOT_ELIGIBLE_REASONS = {"deterministic_hard_fail", "no_supported_candidate_type"}

REASON_GUIDANCE = {
    "live_provider_not_allowed": "契約の artifact_quality.layout_typography.allow_live_provider が true ではありません。true にしてチェッカーを再実行してください",
    "evaluation_identity_unavailable": "finalization manifest の created_by_task_id / created_by_run_id（1以上）か artifact_set_id が使えません。producer を --task-id 付きで実行し、実際の run ID を入れてください",
    "provider_unavailable": "Jev の API キーがこの環境にありません。キーのある環境でチェッカーを再実行してください",
    "invalid_request": "送信データを作れませんでした（候補1件だけで送信上限 32,000 bytes を超える場合を含む）。範囲を分けるか候補を減らして作り直してください",
    "insufficient_evidence": "CJK字形の証拠が足りません（範囲の端に字形がかかっている、字形数が上限を超える等）。cjk_appearance_regions を見直して作り直してください",
    "timeout": "Jev の応答が時間内に返りませんでした。自動では再試行しません。default に報告してください",
    "provider_error": "Jev の呼び出しが失敗しました。自動では再試行しません。default に報告してください",
    "malformed_response": "Jev の応答を検証できませんでした。default に報告してください",
    "identity_drift": "Jev の request/receipt と成果物・スキャンの結び付きが合いません。チェッカーを再実行してください",
    "fixture_only": "fixture の結果しかありません。live の呼び出しが必要です",
    "jev_state_missing": "Jev 経路の記録がスキャンにありません。現行チェッカーで再実行してください",
}


def effective_reason_code(state: Any) -> str | None:
    """Human-facing reason for this route state (derives live_provider_not_allowed)."""
    if not isinstance(state, dict):
        return "jev_state_missing"
    reason = state.get("reason_code")
    request_handle = state.get("request")
    if (state.get("provider_invoked") is not True and isinstance(request_handle, dict)
            and reason in {None, "provider_unavailable"}):
        try:
            request = jev_post_render.read_json(Path(str(request_handle.get("path"))))
            if request.get("allow_live_provider") is not True:
                return "live_provider_not_allowed"
        except Exception:
            pass
    if state.get("status") == "evaluated" and state.get("execution_mode") == "fixture":
        return "fixture_only"
    if state.get("status") == "scanned_no_candidates":
        return "jev_state_missing"
    return reason if isinstance(reason, str) and reason else None


def jev_required_block_reason(scan: dict[str, Any], state: Any) -> str | None:
    """Return a readable block reason when an eligible generated PDF lacks live Jev evidence."""
    if isinstance(state, dict) and (state.get("status") == "not_applicable"
                                    or state.get("reason_code") in NOT_ELIGIBLE_REASONS):
        return None
    if (isinstance(state, dict) and state.get("status") == "evaluated"
            and state.get("execution_mode") == "live" and state.get("provider_invoked") is True
            and state.get("reason_code") == "ok" and state.get("formal_provider_evidence") is True
            and state.get("provider_status") in {"accepted", "uncertain", "repair_required"}):
        try:
            receipt = jev_post_render.read_json(jev_post_render.check_handle(state["receipt"]))
            chunks = receipt.get("wire_chunks")
            if (receipt.get("execution_mode") == "live" and receipt.get("provider_invoked") is True
                    and receipt.get("reason_code") == "ok"
                    and (chunks is None or all(c.get("provider_invoked") is True for c in chunks))):
                return None
        except Exception:
            pass
        code = "identity_drift"
    else:
        code = effective_reason_code(state) or "provider_unavailable"
    guidance = REASON_GUIDANCE.get(code, "Jev が呼ばれていません。default に報告してください")
    return f"AQ-LAYOUT-01: Jevの評価がないため完了できません（reason_code={code}）: {guidance}"
