"""Build a body-free artifact-quality receipt from deterministic and reviewed evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import fitz
from PIL import Image
from quality_rules import jev_post_render

from quality_rules import cjk_font_selector, common, composition, execution_contract, fax, finalization, generic_pdf_repair_adapter, html_pdf_harness, jev_overlap, layout_typography, readability, rendered_readback, repair_controller, safe_wrap, source_typography, visible_ink

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "docs-artifact-quality-receipt-1"
RULE_CLASSES = {
    "AQ-VISIBLE-01": "visual", "AQ-CHANGE-01": "semantic", "AQ-IDENTITY-01": "semantic",
    "AQ-ASSET-01": "semantic", "AQ-RELATION-01": "semantic", "AQ-SCOPE-01": "semantic",
    "AQ-AUTHORITY-01": "semantic", "AQ-BOUNDARY-01": "visual",
    "AQ-LOCAL-GLOBAL-01": "visual", "AQ-FIXTURE-01": "deterministic",
    "AQ-CONTRAST-01": "deterministic", "AQ-LEGIBILITY-01": "deterministic",
    "AQ-PRINT-01": "deterministic", "AQ-HIERARCHY-01": "visual", "AQ-VISUAL-01": "visual",
    "FAX-LEGIBILITY-01": "visual",
}


def sha256(path: Path) -> str:
    return common.sha256(path)


def _handle(path: Path) -> dict[str, str]:
    return common.handle(path)


def _read_hash_bound_json(value: Any, label: str) -> tuple[dict[str, Any] | None, dict[str, str] | None, str | None]:
    """Read a named JSON container while retaining only its hash-bound handle."""
    if not isinstance(value, dict):
        return None, None, f"{label}_handle_missing"
    path_value = value.get("path")
    declared_sha256 = value.get("sha256")
    if (not isinstance(path_value, str) or not Path(path_value).is_absolute()
            or not isinstance(declared_sha256, str) or len(declared_sha256) != 64
            or any(character not in "0123456789abcdef" for character in declared_sha256.lower())):
        return None, None, f"{label}_handle_invalid"
    path = Path(path_value)
    if not path.is_file():
        return None, None, f"{label}_container_missing"
    actual_sha256 = sha256(path)
    if actual_sha256 != declared_sha256:
        return None, None, f"{label}_hash_mismatch"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None, f"{label}_json_invalid"
    if not isinstance(payload, dict):
        return None, None, f"{label}_payload_invalid"
    return payload, {"path": str(path.resolve()), "sha256": actual_sha256}, None


def _rule(
    rule_id: str,
    *,
    status: str = "pass",
    evidence: dict[str, str] | None = None,
    binary: dict[str, Any] | None = None,
    disposition: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"id": rule_id, "class": RULE_CLASSES.get(rule_id, "deterministic"), "status": status}
    if evidence is not None:
        value["evidence"] = evidence
    if binary is not None:
        value["binary"] = binary
    if disposition is not None:
        value["disposition"] = disposition
    return value


def _key(artifact: Path) -> str:
    return artifact.name.replace("/", "_")


def _visible_inset_required(contract: dict[str, Any]) -> bool:
    """Detect caller-declared visible table/cell inset requirements."""
    criteria = contract.get("visible_criteria")
    if not isinstance(criteria, list):
        return False
    for item in criteria:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip().upper() in {"VIS-02", "VIS-03"}:
            return True
        criterion_id = str(item.get("criterion_id", "")).strip().lower()
        tokens = {token for token in criterion_id.replace("_", "-").split("-") if token}
        if tokens & {"table", "cell", "inset", "insets"} or "vertical-inset" in criterion_id:
            return True
    return False


def _safe_wrap_rule(
    contract: dict[str, Any], source: Path | None, artifact: Path
) -> tuple[str, dict[str, str] | None, list[str]]:
    """Validate the producer receipt for PDF entries in this completion flow."""
    if artifact.suffix.lower() != ".pdf":
        return "not_applicable", None, []
    config = contract.get("artifact_quality", {}).get("safe_wrap", {}) if isinstance(contract.get("artifact_quality"), dict) else {}
    if not isinstance(config, dict) or config.get("required") is not True:
        return "not_applicable", None, []
    receipt_value = config.get("receipt_path")
    receipt_path = Path(receipt_value) if isinstance(receipt_value, str) and receipt_value.startswith("/") else None
    if source is None or not source.is_file() or receipt_path is None or not receipt_path.is_file():
        return "fail", _handle(receipt_path) if receipt_path and receipt_path.is_file() else None, ["AQ-SAFE-WRAP-01"]
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "fail", _handle(receipt_path), ["AQ-SAFE-WRAP-RECEIPT-01"]

    reasons: list[str] = []
    font = data.get("font") if isinstance(data, dict) else None
    font_path = Path(str(font.get("path", ""))) if isinstance(font, dict) else None
    ratio = data.get("safe_ratio") if isinstance(data, dict) else None
    expected_ratio = config.get("safe_ratio", safe_wrap.SAFE_RATIO_DEFAULT)
    if data.get("schema_version") != "docs-safe-wrap-receipt-1" or data.get("status") != "pass":
        reasons.append("AQ-SAFE-WRAP-RECEIPT-01")
    if data.get("source_sha256") != sha256(source):
        reasons.append("AQ-SAFE-WRAP-SOURCE-01")
    if (not isinstance(ratio, (int, float)) or isinstance(ratio, bool)
            or not safe_wrap.SAFE_RATIO_MIN <= float(ratio) <= safe_wrap.SAFE_RATIO_MAX
            or abs(float(ratio) - float(expected_ratio)) > 1e-9):
        reasons.append("SAFE-WRAP-RATIO-01")
    selected = cjk_font_selector.select_cjk_font()
    if (not isinstance(font, dict) or selected.status != "ud-installed"
            or font.get("family") != selected.family
            or font.get("status") != selected.status
            or font.get("path") != selected.path
            or font.get("sha256") != selected.sha256
            or font.get("priority") != selected.priority
            or font.get("fallback_decision") != selected.fallback_decision
            or font_path is None or not font_path.is_file()
            or font.get("sha256") != sha256(font_path)):
        reasons.append("CJK-FONT-UNAVAILABLE-01")
    if common.contains_forbidden_key(data):
        reasons.append("AQ-SAFE-WRAP-BODY-FREE-01")

    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list) or not frames:
        reasons.append("AQ-SAFE-WRAP-FRAMES-01")
    else:
        for frame in frames:
            if not isinstance(frame, dict) or frame.get("status") != "pass" or frame.get("rule_ids") != []:
                reasons.append("AQ-SAFE-WRAP-FRAMES-01")
                continue
            numbers = [frame.get(key) for key in ("frame_width", "font_size", "safe_width", "line_height", "total_height")]
            margins = frame.get("margins")
            widths = frame.get("measured_widths")
            if (any(not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 for value in numbers)
                    or not isinstance(margins, list) or len(margins) != 2
                    or any(not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 for value in margins)
                    or not isinstance(widths, list)
                    or any(not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 for value in widths)):
                reasons.append("SAFE-WRAP-GEOMETRY-01")
                continue
            computed_safe = (float(frame["frame_width"]) - float(margins[0]) - float(margins[1])) * float(ratio)
            if abs(computed_safe - float(frame["safe_width"])) > 0.01:
                reasons.append("SAFE-WRAP-GEOMETRY-01")
            if any(float(value) > float(frame["safe_width"]) + 0.01 for value in widths) or frame.get("implicit_wrap_candidates") != []:
                reasons.append("SAFE-WRAP-IMPLICIT-OVERFLOW-01")
            line_count, max_lines = frame.get("line_count"), frame.get("max_lines")
            if (not isinstance(line_count, int) or isinstance(line_count, bool) or line_count != len(widths)
                    or not isinstance(max_lines, int) or isinstance(max_lines, bool) or line_count > max_lines):
                reasons.append("SAFE-WRAP-MAX-LINES-01")
            max_height = frame.get("max_height")
            if max_height is not None and (not isinstance(max_height, (int, float)) or isinstance(max_height, bool)
                    or float(frame["total_height"]) > float(max_height) + 0.01):
                reasons.append("SAFE-WRAP-MAX-HEIGHT-01")
            if any(key in frame for key in ("font_size_adjusted", "shrink_font", "auto_fit")):
                reasons.append("SAFE-WRAP-NO-FONT-SHRINK-01")
    reasons = list(dict.fromkeys(reasons))
    return ("pass" if not reasons else "fail"), _handle(receipt_path), reasons


def _html_pdf_rule(contract: dict[str, Any], artifact: Path) -> tuple[str, dict[str, str] | None, list[str]]:
    """Bind an editable HTML delivery artifact to its controlled final PDF.

    Content authority remains independent: an original PDF may stay in
    ``authoritative_source`` while the declared HTML is the deterministic edit
    source for the HTML→PDF receipt.
    """
    if artifact.suffix.lower() != ".pdf":
        return "not_applicable", None, []
    quality = contract.get("artifact_quality") if isinstance(contract.get("artifact_quality"), dict) else {}
    config = quality.get("html_pdf") if isinstance(quality, dict) else None
    declared = contract.get("artifacts")
    html_paths = [Path(item) for item in declared if isinstance(item, str) and Path(item).suffix.lower() in {".html", ".htm"}] if isinstance(declared, list) else []
    if not html_paths:
        return "not_applicable", None, []
    if len(html_paths) != 1:
        return "fail", None, ["HTML-PDF-SOURCE-01"]
    if not isinstance(config, dict) or config.get("required") is not True:
        return "fail", None, ["HTML-PDF-CONTRACT-01"]
    source_value = config.get("source_path")
    source = Path(source_value) if isinstance(source_value, str) and source_value.startswith("/") else None
    if source is None or source != html_paths[0] or not source.is_file():
        return "fail", None, ["HTML-PDF-SOURCE-01"]
    receipt_value = config.get("receipt_path")
    receipt_path = Path(receipt_value) if isinstance(receipt_value, str) and receipt_value.startswith("/") else None
    if receipt_path is None or not receipt_path.is_file():
        return "fail", None, ["HTML-PDF-RECEIPT-01"]
    reasons = html_pdf_harness.validate_html_receipt(receipt_path, source, artifact)
    return ("pass" if not reasons else "fail"), _handle(receipt_path), reasons


def _render_fax(pdf: Path, evidence: Path, threshold: int) -> dict[str, Path]:
    evidence.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf) as document:
        if len(document) != 1:
            raise ValueError("FAX PDF must have exactly one page")
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, colorspace=fitz.csRGB)
        original = evidence / f"{_key(pdf)}.FAX-RENDER-01.png"
        pixmap.save(str(original))
    with Image.open(original) as image:
        reduced = image.convert("L").resize((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)
        reduced_path = evidence / f"{_key(pdf)}.FAX-REDUCED-01.png"
        reduced.save(reduced_path)
        binary_path = evidence / f"{_key(pdf)}.FAX-BINARY-01.png"
        reduced.point(lambda value: 255 if value >= threshold else 0, mode="1").save(binary_path)
    return {"FAX-RENDER-01": original, "FAX-REDUCED-01": reduced_path, "FAX-BINARY-01": binary_path}


def _manual_evidence(evidence: Path, artifact: Path, rule_id: str) -> dict[str, str] | None:
    path = evidence / f"{_key(artifact)}.{rule_id}.json"
    return _handle(path) if path.is_file() else None


def _readability_rules(manifest: Path | None) -> list[dict[str, Any]]:
    statuses = {rule_id: "NOT_APPLICABLE" for rule_id in readability.RULE_IDS}
    evidence = None
    if manifest is not None and manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = None
        receipt = data.get("readability_receipt") if isinstance(data, dict) else None
        if isinstance(receipt, dict) and isinstance(receipt.get("outcomes"), list):
            evidence = _handle(manifest)
            for item in receipt["outcomes"]:
                if (isinstance(item, dict) and item.get("rule_id") in statuses
                        and item.get("status") in readability.STATUSES):
                    statuses[item["rule_id"]] = item["status"]
    return [_rule(rule_id, status=statuses[rule_id], evidence=evidence) for rule_id in readability.RULE_IDS]


def _candidate_set_digest(candidates: list[dict[str, Any]]) -> str:
    identity = [
        {"issue_id": item.get("id"), "rule": item.get("rule"), "scan_hash": item.get("scan_hash")}
        for item in candidates
    ]
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _layout_review_passes(
    review_path: Path, artifact: Path, scan_path: Path,
    candidates: list[dict[str, Any]], *, artifact_set_id: str | None = None,
) -> bool:
    if not candidates:
        return True
    if not review_path.is_file():
        return False
    try:
        data = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (not isinstance(data, dict) or data.get("schema_version") not in {"docs-layout-visual-review-1", "docs-layout-visual-review-2"}
            or data.get("status") != "pass" or common.contains_forbidden_key(data)
            or data.get("artifact_path") != str(artifact.resolve())
            or data.get("artifact_sha256") != sha256(artifact)
            or data.get("scan_path") != str(scan_path.resolve())
            or data.get("scan_sha256") != sha256(scan_path)):
        return False
    if artifact_set_id is not None and data.get("schema_version") != "docs-layout-visual-review-2":
        return False
    if data.get("schema_version") == "docs-layout-visual-review-2":
        producer = (data.get("producer_task_id"), data.get("producer_run_id"))
        reviewer = (data.get("reviewer_task_id"), data.get("reviewer_run_id"))
        if (not all(isinstance(value, (str, int)) and str(value) for value in (*producer, *reviewer))
                or producer[0] == reviewer[0]
                or not isinstance(data.get("reviewer_role"), str) or not data["reviewer_role"]
                or data.get("candidate_set_digest") != _candidate_set_digest(candidates)
                or data.get("llm_invoked") is not False or data.get("vision_invoked") is not False):
            return False
        if artifact_set_id is not None and data.get("artifact_set_id") != artifact_set_id:
            return False
    reviews = data.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != len(candidates):
        return False
    by_id = {item.get("issue_id"): item for item in reviews if isinstance(item, dict)}
    if len(by_id) != len(reviews) or set(by_id) != {item["id"] for item in candidates}:
        return False
    for candidate_index, candidate in enumerate(candidates):
        review = by_id[candidate["id"]]
        candidate_rule = candidate.get("rule")
        reviewable_spacing = candidate_rule in {
            "line_spacing_candidate",
            "content_block_spacing_candidate",
            "content_block_intra_spacing_candidate",
        }
        allowed_status = {"accepted", "rejected"} if reviewable_spacing else {"accepted"}
        if review.get("status") not in allowed_status or review.get("rule") != candidate.get("rule"):
            return False
        if data.get("schema_version") == "docs-layout-visual-review-2":
            if (review.get("disposition") != review.get("status")
                    or not isinstance(review.get("rationale_code"), str) or not review["rationale_code"]):
                return False
            if candidate_index == 0 and (review.get("llm_invoked") is not False or review.get("vision_invoked") is not False):
                return False
        render = candidate.get("render_evidence")
        if isinstance(candidate.get("page"), int) and render is None:
            return False
        if render is not None:
            if (review.get("page") != candidate.get("page")
                    or review.get("bbox_pdf_points") != candidate.get("bbox_pdf_points")
                    or review.get("render_path") != render.get("path")
                    or review.get("render_sha256") != render.get("sha256")):
                return False
            if review.get("affected_pages") != candidate.get("affected_pages"):
                return False
            render_path = Path(str(render.get("path", "")))
            if not render_path.is_absolute() or not render_path.is_file() or sha256(render_path) != render.get("sha256"):
                return False
        elif review.get("source_member") != candidate.get("source_member") or review.get("paragraph") != candidate.get("paragraph"):
            return False
    line_groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if candidate.get("rule") == "line_spacing_candidate":
            line_groups.setdefault(str(candidate.get("region_id_hash")), []).append(candidate)
    for values in line_groups.values():
        if sum(by_id[item["id"]].get("status") == "accepted" for item in values) != 1:
            return False
    content_groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if candidate.get("rule") not in {
            "content_block_spacing_candidate",
            "content_block_intra_spacing_candidate",
        }:
            continue
        group_key = candidate.get("spacing_group_key")
        if not isinstance(group_key, str) or not group_key:
            group_key = ":".join(str(candidate.get(key, "")) for key in ("scope", "upper_role", "lower_role"))
        content_groups.setdefault(group_key, []).append(candidate)
    for values in content_groups.values():
        if sum(by_id[item["id"]].get("status") == "accepted" for item in values) != 1:
            return False
    return True


def _common_evidence_manifest_path(evidence: Path, artifact: Path) -> Path:
    return evidence / f"{_key(artifact)}.common-evidence.json"


def _common_evidence_rows(
    contract: dict[str, Any], artifact: Path, rules: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = {item.get("id"): item for item in rules if isinstance(item, dict)}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    declarations = common.common_rule_declarations(contract)
    for rule_id in common.CONTRACT_RULES:
        rule = by_id.get(rule_id, {"id": rule_id, "status": "fail"})
        evaluation_rule = dict(rule)
        if rule.get("disposition") in {"missing_evidence", "invalid_evidence"}:
            # Preserve the evidence failure class when the entry was already
            # marked fail; otherwise the second pass would become rule_failed.
            evaluation_rule["status"] = "pass"
        evaluation = common.evaluate_common_rule(rule_id, evaluation_rule, contract, artifact)
        declaration = declarations.get(rule_id) or {}
        row = {
            "rule_id": rule_id,
            "applicability": declaration.get("applicability"),
            "status": evaluation["status"],
            "disposition": evaluation["disposition"],
            "reason_code": declaration.get("reason_code"),
            "reason": declaration.get("reason"),
            "basis": declaration.get("basis"),
            "decision_provenance": declaration.get("decision_provenance"),
            "evidence": evaluation.get("evidence"),
        }
        rows.append(row)
        errors.extend(evaluation["errors"])
    return rows, errors


def _common_evidence_execution_identity(
    contract_path: Path,
    artifact: Path,
    checker_path: Path,
    *,
    execution_contract_data: dict[str, Any] | None = None,
    finalization_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_digest = common.sha256(artifact) if artifact.is_file() else ""
    identity: dict[str, Any] = {
        "checker": _handle(checker_path),
        "contract_sha256": common.sha256(contract_path),
        "artifact_sha256": artifact_digest,
        "producer_run_id": None,
        "artifact_set_id": None,
    }
    if execution_contract_data is not None:
        identity["producer_run_id"] = execution_contract_data.get("producer_run_id")
        identity["artifact_set_id"] = execution_contract_data.get("artifact_set_id")
    elif finalization_manifest is not None:
        identity["producer_run_id"] = finalization_manifest.get("created_by_run_id")
        identity["artifact_set_id"] = finalization_manifest.get("artifact_set_id")
    return identity


def _write_common_evidence_manifest(
    contract_path: Path,
    contract: dict[str, Any],
    artifact: Path,
    evidence: Path,
    rules: list[dict[str, Any]],
    *,
    execution_contract_data: dict[str, Any] | None = None,
    finalization_manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path, list[str]]:
    evidence.mkdir(parents=True, exist_ok=True)
    output_path = _common_evidence_manifest_path(evidence, artifact)
    rows, row_errors = _common_evidence_rows(contract, artifact, rules)
    checker_path = Path(__file__).resolve()
    manifest = common.build_common_evidence_manifest(
        contract_path,
        artifact,
        output_path,
        rows,
        execution_identity=_common_evidence_execution_identity(
            contract_path,
            artifact,
            checker_path,
            execution_contract_data=execution_contract_data,
            finalization_manifest=finalization_manifest,
        ),
        provenance={"kind": "checker", "id": "check_docs_artifact_quality.build_receipt"},
    )
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    try:
        readback = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        readback = None
    contract_for_validation = dict(contract)
    contract_for_validation["__contract_path"] = str(contract_path.resolve())
    errors = row_errors + common.validate_common_evidence_manifest(
        readback, contract_for_validation, artifact, output_path,
    )
    return manifest, output_path, errors


def build_receipt(
    contract_path: Path, *, evidence_dir: Path | None = None,
    jev_request_fn: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    contract_path = contract_path.resolve(strict=True)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    additive_common = common.uses_common_rule_applicability(contract)
    contract_for_validation = dict(contract)
    if additive_common:
        contract_for_validation["__contract_path"] = str(contract_path)
    contract_errors = common.validate_contract(contract_for_validation)
    execution_contract_data: dict[str, Any] | None = None
    producer_receipt_data: dict[str, Any] | None = None
    execution_contract_path: Path | None = None
    producer_receipt_path: Path | None = None
    execution_spec = contract.get("execution_contract")
    if execution_spec is not None:
        if not isinstance(execution_spec, dict):
            contract_errors.append("AQ-HASH-01: execution_contract must be an object")
        else:
            raw_contract = execution_spec.get("path")
            raw_producer = execution_spec.get("producer_receipt")
            execution_contract_path = Path(raw_contract) if isinstance(raw_contract, str) else None
            producer_receipt_path = Path(raw_producer) if isinstance(raw_producer, str) else None
            if execution_contract_path is None or producer_receipt_path is None:
                contract_errors.append("AQ-HASH-01: execution contract and producer receipt paths are required")
            else:
                execution_contract_data, execution_errors = execution_contract.load_contract(
                    execution_contract_path, require_artifacts=True
                )
                if execution_contract_data is None:
                    contract_errors.extend(f"AQ-HASH-01: {error}" for error in execution_errors)
                else:
                    producer_receipt_data, producer_errors = execution_contract.validate_producer_receipt(
                        producer_receipt_path, execution_contract_data
                    )
                    if producer_receipt_data is None:
                        contract_errors.append("AQ-HASH-01: producer receipt does not match execution contract")
    finalization_manifest: dict[str, Any] | None = None
    finalization_path: Path | None = None
    finalization_spec = contract.get("finalization_manifest")
    if isinstance(finalization_spec, dict) and isinstance(finalization_spec.get("path"), str):
        finalization_path = Path(finalization_spec["path"])
        finalization_manifest, finalization_error = finalization.validate_manifest(
            finalization_path, contract.get("artifacts", []), require_identity=True,
            require_execution_contract=execution_spec is not None,
        )
        if finalization_error:
            contract_errors.append(f"AQ-HASH-01: {finalization_error}")
        elif (execution_contract_data is not None and execution_contract_path is not None
              and producer_receipt_path is not None and finalization_path is not None):
            bundle_errors = execution_contract.validate_bundle(
                contract_path=execution_contract_path,
                producer_receipt_path=producer_receipt_path,
                finalization_path=finalization_path,
            )
            contract_errors.extend(f"AQ-HASH-01: {error}" for error in bundle_errors)
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "block",
        "contract": _handle(contract_path),
        "checker": _handle(Path(__file__).resolve()),
        "rulesets": [
            {"id": "common-1", **_handle(ROOT / "quality_rules/common.py")},
            {"id": "rendered-readback-1", **_handle(ROOT / "quality_rules/rendered_readback.py")},
            {"id": "readability-1", **_handle(ROOT / "quality_rules/readability.py")},
            {"id": "layout-typography-1", **_handle(ROOT / "quality_rules/layout_typography.py")},
            {"id": "visible-ink-1", **_handle(ROOT / "quality_rules/visible_ink.py")},
            {"id": "composition-layers-1", **_handle(ROOT / "quality_rules/composition.py")},
            {"id": "jev-overlap-structure-1", **_handle(ROOT / "quality_rules/jev_overlap.py")},
            {"id": "source-typography-1", **_handle(ROOT / "quality_rules/source_typography.py")},
            {"id": "cjk-font-selector-1", **_handle(ROOT / "quality_rules/cjk_font_selector.py")},
            {"id": "safe-wrap-1", **_handle(ROOT / "quality_rules/safe_wrap.py")},
        ],
        "entries": [],
        "quality_layers": [],
    }
    if finalization_manifest is not None and finalization_path is not None:
        receipt["finalization"] = {
            **_handle(finalization_path),
            "artifact_set_id": finalization_manifest["artifact_set_id"],
        }
    if execution_contract_data is not None and execution_contract_path is not None and producer_receipt_path is not None:
        receipt["execution_contract"] = {
            "contract": _handle(execution_contract_path),
            "producer_receipt": _handle(producer_receipt_path),
            "execution_identity": execution_contract.identity(execution_contract_data),
        }
    quality_config = contract.get("artifact_quality")
    artifact_kind = str(contract.get("artifact_kind", ""))
    slide_required = artifact_kind in {"slide_deck", "slides", "training_slide_deck", "pptx_pdf"}
    layer_enabled = isinstance(quality_config, dict) and (slide_required or "quality_foundation" in quality_config)
    # Static contract validation happens above. L0 is deliberately evaluated only
    # after real entry rules exist; an empty preliminary baseline is never a result.
    composition_context: tuple[dict[str, Any], Path | None, Path | None] | None = None
    if fax.applies(contract):
        receipt["rulesets"].append({"id": "fax-1", **_handle(ROOT / "quality_rules/fax.py")})

    artifacts = common.target_artifacts(contract)
    # An editable HTML member of a controlled HTML→PDF pair is the edit source,
    # not a second independently delivered render. Its receipt is validated by
    # AQ-HTML-PDF-01 on the final PDF entry.
    declared_artifacts = contract.get("artifacts")
    has_html_pdf_pair = isinstance(declared_artifacts, list) and any(isinstance(item, str) and Path(item).suffix.lower() in {".html", ".htm"} for item in declared_artifacts) and any(isinstance(item, str) and Path(item).suffix.lower() == ".pdf" for item in declared_artifacts)
    if has_html_pdf_pair:
        artifacts = [artifact for artifact in artifacts if artifact.suffix.lower() not in {".html", ".htm"}]
    for artifact in artifacts:
        evidence = (evidence_dir or artifact.parent).resolve()
        readback_manifest: Path | None = None
        if artifact.is_file() and execution_contract_data is not None and finalization_manifest is not None:
            for handle in finalization_manifest.get("renderer_receipts", []):
                raw = handle.get("canonical_path") if isinstance(handle, dict) else None
                candidate = Path(raw) if isinstance(raw, str) else None
                if candidate is None or not candidate.is_file():
                    continue
                try:
                    renderer_data = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(renderer_data, dict) and renderer_data.get("artifact_path") == str(artifact.resolve()):
                    readback_manifest = candidate
                    break
        elif artifact.is_file():
            try:
                integrated_readback = (
                    execution_contract_data is not None
                    and execution_contract_path is not None
                    and producer_receipt_path is not None
                )
                readback_manifest = rendered_readback.build_manifest(
                    artifact, evidence,
                    producer_run_id=(execution_contract_data.get("producer_run_id") if integrated_readback
                                     else finalization_manifest.get("created_by_run_id") if finalization_manifest else None),
                    artifact_set_id=(execution_contract_data.get("artifact_set_id") if integrated_readback
                                     else finalization_manifest.get("artifact_set_id") if finalization_manifest else None),
                    execution_contract_path=execution_contract_path if integrated_readback else None,
                    producer_receipt_path=producer_receipt_path if integrated_readback else None,
                )
            except Exception:
                readback_manifest = None
        entry: dict[str, Any] = {
            "artifact": _handle(artifact) if artifact.is_file() else {"path": str(artifact), "sha256": ""},
            "actual_delivery_readback": _handle(readback_manifest) if readback_manifest and readback_manifest.is_file() else {},
            "rules": [],
            "status": "block",
        }
        effective_quality: dict[str, Any] = {}
        source_path: Path | None = None
        if layer_enabled and artifact.is_file():
            # Evidence is generated from the authoritative PPTX during the existing
            # receipt path and bound to both source and delivered artifact hashes.
            source_spec = contract.get("authoritative_source")
            source_value = source_spec.get("path") if isinstance(source_spec, dict) else None
            source_path = Path(source_value) if isinstance(source_value, str) and Path(source_value).is_absolute() else None
            effective_quality = dict(quality_config)
            declared = effective_quality.get("composition_evidence")
            if source_path is not None and source_path.is_file() and source_path.suffix.lower() == ".pptx":
                try:
                    foundation = effective_quality.get("quality_foundation")
                    semantic = foundation.get("semantic_regions") if isinstance(foundation, dict) else {}
                    minimum_gutter = semantic.get("minimum_gutter_pt") if isinstance(semantic, dict) else None
                    max_groups = semantic.get("max_review_groups", composition.DEFAULT_MAX_REVIEW_GROUPS) if isinstance(semantic, dict) else composition.DEFAULT_MAX_REVIEW_GROUPS
                    layout_spec = effective_quality.get("layout_typography")
                    jev_spec = layout_spec.get("jev_overlap") if isinstance(layout_spec, dict) else None
                    spacing_candidates_enabled = isinstance(jev_spec, dict) and jev_spec.get("enabled") is True
                    spacing_candidate_cap = (
                        jev_spec.get("max_candidates", composition.DEFAULT_MAX_SPACING_CANDIDATES)
                        if isinstance(jev_spec, dict) else composition.DEFAULT_MAX_SPACING_CANDIDATES
                    )
                    evidence_path = composition.write_evidence(source_path, artifact, evidence,
                                                                minimum_gutter_pt=minimum_gutter,
                                                                max_review_groups=max_groups,
                                                                producer_receipt=producer_receipt_data,
                                                                include_spacing_candidates=spacing_candidates_enabled,
                                                                max_spacing_candidates=spacing_candidate_cap)
                    effective_quality["composition_evidence"] = _handle(evidence_path)
                    if readback_manifest is not None and readback_manifest.is_file():
                        readback_manifest = rendered_readback.attach_surface_readback(
                            readback_manifest, evidence_path,
                            output_path=evidence / f"{artifact.name}.delivery.surface.json",
                        )
                        entry["actual_delivery_readback"] = _handle(readback_manifest)
                except Exception:
                    pass
            elif source_path is not None and source_path.is_file() and isinstance(declared, dict):
                # Non-Office producers may declare an already generated, hash-bound
                # composition record. Reuse the existing rendered-readback and
                # AQ-LAYOUT-01 seam instead of creating a parallel quality gate.
                composition_data, composition_error = composition._load_evidence(
                    declared, source_path, artifact,
                )
                if composition_data is not None and composition_error is None:
                    effective_quality["composition_evidence"] = declared
                    if readback_manifest is not None and readback_manifest.is_file():
                        try:
                            readback_manifest = rendered_readback.attach_surface_readback(
                                readback_manifest,
                                Path(str(declared["path"])),
                                output_path=evidence / f"{artifact.name}.delivery.surface.json",
                            )
                            entry["actual_delivery_readback"] = _handle(readback_manifest)
                        except Exception:
                            pass
            if composition_context is None:
                composition_context = (effective_quality, source_path, artifact)
        source_spec = contract.get("authoritative_source")
        source_value = source_spec.get("path") if isinstance(source_spec, dict) else None
        source_path = Path(source_value) if isinstance(source_value, str) and Path(source_value).is_absolute() else None
        safe_wrap_status, safe_wrap_evidence, safe_wrap_reason_ids = _safe_wrap_rule(contract, source_path, artifact)
        if safe_wrap_status != "not_applicable":
            entry["rules"].append(_rule("AQ-SAFE-WRAP-01", status="pass" if safe_wrap_status == "pass" else "fail", evidence=safe_wrap_evidence))
        html_pdf_status, html_pdf_evidence, html_pdf_reason_ids = _html_pdf_rule(contract, artifact)
        if html_pdf_status != "not_applicable":
            entry["rules"].append(_rule("AQ-HTML-PDF-01", status="pass" if html_pdf_status == "pass" else "fail", evidence=html_pdf_evidence))
        for rule_id in common.RULES:
            evidence_handle = _manual_evidence(evidence, artifact, rule_id) if rule_id in common.EVIDENCE_RULES else None
            if additive_common and rule_id in common.CONTRACT_RULES:
                candidate_rule = {"id": rule_id, "status": "pass", "evidence": evidence_handle}
                evaluation = common.evaluate_common_rule(rule_id, candidate_rule, contract_for_validation, artifact)
                entry["rules"].append(
                    _rule(
                        rule_id,
                        status=evaluation["status"],
                        evidence=evaluation.get("evidence"),
                        disposition=evaluation["disposition"],
                    )
                )
            else:
                entry["rules"].append(_rule(rule_id, evidence=evidence_handle))
        common_evidence_errors: list[str] = []
        if additive_common:
            _, common_evidence_path, common_evidence_errors = _write_common_evidence_manifest(
                contract_path,
                contract_for_validation,
                artifact,
                evidence,
                entry["rules"],
                execution_contract_data=execution_contract_data,
                finalization_manifest=finalization_manifest,
            )
            entry["common_evidence"] = _handle(common_evidence_path)
        entry["rules"].extend(_readability_rules(readback_manifest))

        quality_config = contract.get("artifact_quality")
        layout_config = quality_config.get("layout_typography") if isinstance(quality_config, dict) else None
        pdf_target = artifact.suffix.lower() == ".pdf"
        layout_enabled = isinstance(layout_config, dict) and layout_config.get("required") is True
        # AQ-LAYOUT-01 is a PDF-only rule.  A shared required configuration
        # remains fail-closed for PDF entries, but must not add a failing rule to
        # another declared artifact type in a mixed-delivery contract.
        if pdf_target:
            scan_path = evidence / f"{_key(artifact)}.AQ-LAYOUT-01.json"
            if artifact.is_file() and layout_enabled:
                scan_config = dict(layout_config)
                scan_config["visible_inset_required"] = _visible_inset_required(contract)
                if finalization_manifest is not None:
                    identity = {
                        "producer_run_id": finalization_manifest["created_by_run_id"],
                        "artifact_set_id": finalization_manifest["artifact_set_id"],
                    }
                    content_policy = scan_config.get("content_block_policy")
                    if isinstance(content_policy, dict) and content_policy.get("enabled") is True:
                        content_policy = dict(content_policy)
                        content_policy["strict_identity"] = identity
                        scan_config["content_block_policy"] = content_policy
                scan = layout_typography.scan_pdf(artifact, scan_config, render_manifest=readback_manifest)
                if finalization_manifest is not None:
                    scan["producer_run_id"] = finalization_manifest["created_by_run_id"]
                    scan["artifact_set_id"] = finalization_manifest["artifact_set_id"]
                source_spec = contract.get("authoritative_source")
                source_value = source_spec.get("path") if isinstance(source_spec, dict) else None
                source_path = Path(source_value) if isinstance(source_value, str) and Path(source_value).is_absolute() else None
                source_scan = source_typography.scan_ooxml(source_path) if source_path is not None else {
                    "schema_version": "docs-source-typography-scan-1", "status": "not_applicable", "issues": []
                }
                scan["source_scan"] = source_scan
                # Composition candidates intentionally use the same AQ-LAYOUT-01
                # limited-review receipt: no parallel completion seam is allowed.
                composition_candidates: list[dict[str, Any]] = []
                composition_data, _ = composition._load_evidence(
                    effective_quality.get("composition_evidence"), source_path, artifact
                )
                if composition_data is not None:
                    renders = layout_typography._render_records(readback_manifest)
                    for raw in composition_data.get("review_candidates", []):
                        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                            continue
                        bbox = raw.get("candidate_bbox_pdf_points")
                        candidate = {"id": raw["id"], "rule": raw.get("rule"), "severity": "review",
                                     "page": raw.get("page"), "bbox_pdf_points": bbox,
                                     "shape_ids": raw.get("shape_ids"), "affected_pages": raw.get("affected_pages"),
                                     "affected_count": raw.get("affected_count"), "example_shape_ids": raw.get("example_shape_ids"),
                                     "overlap_pt": raw.get("overlap_pt"), "gutter_pt": raw.get("gutter_pt"),
                                     "semantic_group": raw.get("semantic_group"),
                                     "candidate_bbox_pdf_points": raw.get("candidate_bbox_pdf_points", bbox),
                                     "spacing_kind": raw.get("spacing_kind"),
                                     "spacing_projection_id": raw.get("spacing_projection_id"),
                                     "spacing_projection": raw.get("spacing_projection"),
                                     "source_member": raw.get("source_member"),
                                     "paragraph": raw.get("paragraph"),
                                     "units": raw.get("units", "pt"),
                                     "measurement_basis": raw.get("measurement_basis"),
                                     "candidate_type": "composition"}
                        if isinstance(candidate["page"], int) and isinstance(bbox, list):
                            layout_typography._attach_render_evidence(candidate, renders)
                        composition_candidates.append(candidate)
                    scan["composition_evidence"] = effective_quality.get("composition_evidence")
                surface_hard_failures: list[dict[str, Any]] = []
                if readback_manifest is not None and readback_manifest.is_file():
                    try:
                        rendered_data = json.loads(readback_manifest.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        rendered_data = {}
                    surface_readback = rendered_data.get("surface_readback") if isinstance(rendered_data, dict) else None
                    if isinstance(surface_readback, dict):
                        for raw in surface_readback.get("REVIEW_CANDIDATE", []):
                            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                                continue
                            candidate = {**raw, "severity": "review", "candidate_type": "composition"}
                            bbox = candidate.get("bbox_pdf_points")
                            if isinstance(candidate.get("page"), int) and isinstance(bbox, list):
                                layout_typography._attach_render_evidence(candidate, layout_typography._render_records(readback_manifest))
                            composition_candidates.append(candidate)
                        surface_hard_failures = [item for item in surface_readback.get("HARD_FAIL", []) if isinstance(item, dict)]
                        scan["surface_readback"] = _handle(readback_manifest)
                scan["issues"].extend(composition_candidates)
                candidates = [item for item in scan["issues"] if item.get("severity") == "review"] + [
                    item for item in source_scan["issues"] if item.get("severity") == "review"
                ]
                hard_failed = scan.get("hard_issue_count", 0) > 0 or source_scan.get("status") == "block" or bool(surface_hard_failures)
                visual_config = scan_config.get("jev_post_render_review")
                visual_enabled = isinstance(visual_config, dict) and visual_config.get("enabled") is True
                if visual_enabled:
                    scan = jev_post_render.prepare_raw_scan(scan, source_scan, surface_hard_failures)
                scan_path.write_text(json.dumps(scan, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                candidate_scan_sha256 = sha256(scan_path)
                raw_visual_scan = scan_path.with_suffix(".raw.json")
                if visual_enabled:
                    # Preserve the exact pre-review bytes; later scan annotations
                    # must not change the object the visual reviewer evaluated.
                    raw_visual_scan.write_bytes(scan_path.read_bytes())
                review_path = evidence / f"{_key(artifact)}.AQ-LAYOUT-01-REVIEW.json"
                jev_config = scan_config.get("jev_overlap")
                jev_enabled = isinstance(jev_config, dict) and jev_config.get("enabled") is True
                design_config, design_config_key = jev_overlap.get_design_config(scan_config)
                # The additive design route is deliberately gated by the
                # additive contract schema.  A legacy v1 contract can carry
                # unknown fields without acquiring this route.
                design_enabled = (
                    contract.get("schema_version") == "docs-artifact-quality-contract-2"
                    and isinstance(design_config, dict)
                    and design_config.get("enabled") is True
                )
                outer_design = contract.get("design_evaluation")
                explicit_design_mode = (
                    outer_design.get("execution_mode")
                    if isinstance(outer_design, dict) else design_config.get("execution_mode")
                    if isinstance(design_config, dict) and "execution_mode" in design_config else None
                )
                design_execution_mode = explicit_design_mode or (
                    "live" if isinstance(design_config, dict) and design_config.get("allow_live_provider") is True else "dry_run"
                )
                standard_design_route = design_enabled and explicit_design_mode in {"live", "dry_run"}
                overlap_passed = True
                design_passed = True
                overlap_result: dict[str, Any] | None = None
                design_result: dict[str, Any] | None = None
                design_input_handles: dict[str, dict[str, str]] = {}
                design_input_error: str | None = None
                if jev_enabled:
                    # The opt-in Jev route is structure-only.  It receives the
                    # bounded candidate set and composition IR, never rendered
                    # pixels or body text, and it cannot replace hard failures.
                    overlap_result = jev_overlap.classify_candidates(
                        candidates,
                        composition_data=composition_data,
                        config=jev_config,
                        request_fn=jev_request_fn,
                    )
                    scan["jev_overlap"] = overlap_result
                    jev_rows = overlap_result.get("results", [])
                    overlap_passed = (
                        not candidates
                        or (
                            overlap_result.get("decision") == "maintain"
                            and isinstance(jev_rows, list)
                            and len(jev_rows) == len(candidates)
                            and all(
                                isinstance(item, dict)
                                and item.get("status") == "classified"
                                and item.get("branch") == "maintain"
                                for item in jev_rows
                            )
                        )
                    )
                if design_enabled and design_config is not None:
                    # Design candidates are declared structural IR.  They are
                    # never synthesized from rendered pixels or body text.
                    design_candidates = jev_overlap._design_candidates_from_ir(
                        composition_data, design_config,
                    )
                    design_composition_data = composition_data
                    request_set = design_config.get("request_set") if isinstance(design_config, dict) else None
                    expected_count = request_set.get("expected_count", request_set.get("count")) if isinstance(request_set, dict) else None
                    request_set_id = request_set.get("id", "docs-jev-standard-17") if isinstance(request_set, dict) else "docs-jev-standard-17"
                    if standard_design_route:
                        outer_design = contract.get("design_evaluation")
                        outer_design = outer_design if isinstance(outer_design, dict) else {}
                        ir_payload, ir_handle, ir_error = _read_hash_bound_json(outer_design.get("ir"), "design_ir")
                        request_payload, request_handle, request_error = _read_hash_bound_json(outer_design.get("request_set"), "request_set")
                        if ir_handle is not None:
                            design_input_handles["ir"] = ir_handle
                        if request_handle is not None:
                            design_input_handles["request_set"] = request_handle
                        design_input_error = ir_error or request_error
                        if design_input_error is None and ir_payload is not None and request_payload is not None:
                            if not isinstance(ir_payload.get("schema_version"), str):
                                design_input_error = "design_ir_schema_missing"
                            else:
                                request_candidates, request_errors = jev_overlap.candidates_from_request_set(
                                    request_payload,
                                    expected_count=expected_count,
                                    ir_sha256=ir_handle["sha256"] if ir_handle else None,
                                )
                                if request_errors:
                                    design_input_error = request_errors[0]
                                else:
                                    design_candidates = request_candidates
                                    design_composition_data = {
                                        "design_evaluation_candidates": request_candidates,
                                        "design_ir": ir_payload,
                                    }
                        if design_input_error is not None:
                            design_result = jev_overlap.blocked_design_input_result(
                                request_set_id=request_set_id,
                                declared_count=expected_count if isinstance(expected_count, int) else None,
                                execution_mode=design_execution_mode,
                                reason=design_input_error,
                            )
                        else:
                            design_result = jev_overlap.evaluate_design_request_set(
                                design_candidates,
                                composition_data=design_composition_data,
                                config=design_config,
                                execution_mode=design_execution_mode,
                                request_fn=jev_request_fn,
                                expected_request_count=expected_count,
                                request_set_id=request_set_id,
                            )
                    else:
                        design_result = jev_overlap.classify_design_candidates(
                            design_candidates,
                            composition_data=design_composition_data,
                            config=design_config,
                            request_fn=jev_request_fn,
                        )
                    scan["jev_design_evaluation"] = design_result
                    design_rows = design_result.get("results", [])
                    design_passed = (
                        design_result.get("configuration_valid") is not False
                        and design_result.get("decision") == "maintain"
                        and isinstance(design_rows, list)
                        and bool(design_rows)
                        and all(
                            isinstance(item, dict)
                            and item.get("status") == "classified"
                            and item.get("branch") == "maintain"
                            for item in design_rows
                        )
                    )
                if jev_enabled or design_enabled:
                    if standard_design_route:
                        # Live Jev is advisory workflow evidence.  The
                        # deterministic scan remains the independent AQ result.
                        review_passed = (
                            not hard_failed
                            and (not jev_enabled or overlap_passed)
                            and not (design_result is not None and design_result.get("configuration_valid") is False)
                        )
                        review_mode = "live-jev-standard" if design_execution_mode == "live" else "jev-dry-run"
                        scan["workflow_state"] = design_result.get("workflow_state", "review_required") if design_result else "review_required"
                        scan["worker_return"] = design_result.get("worker_return") if design_result else None
                    else:
                        review_passed = overlap_passed and design_passed
                        review_mode = (
                            "structure-only-jev-overlap+design-evaluation"
                            if jev_enabled and design_enabled
                            else "structure-only-jev-overlap"
                            if jev_enabled
                            else "structure-only-jev-design-evaluation"
                        )
                    review_candidate_count = len(candidates)
                    review_candidate_pages = {
                        item["page"] for item in candidates if isinstance(item.get("page"), int)
                    }
                    if design_result is not None:
                        for item in design_result.get("results", []):
                            projection = item.get("projection") if isinstance(item, dict) else None
                            if isinstance(projection, dict):
                                structural = projection.get("structural_ir")
                                page = structural.get("page") if isinstance(structural, dict) else None
                                if isinstance(page, int):
                                    review_candidate_pages.add(page)
                        review_candidate_count += int(design_result.get("candidate_count", 0))
                    limited_review = {
                        "required": False,
                        "candidate_count": review_candidate_count,
                        "candidate_pages": sorted(review_candidate_pages),
                        "review_mode": review_mode,
                        "vision_model_invoked": False,
                        "evidence": None,
                        "status": "pass" if review_passed else "review-required",
                    }
                else:
                    review_passed = True if not candidates else _layout_review_passes(
                        review_path, artifact, scan_path, candidates,
                        artifact_set_id=finalization_manifest.get("artifact_set_id") if finalization_manifest else None,
                    )
                    limited_review = {
                        "required": bool(candidates),
                        "candidate_count": len(candidates),
                        "candidate_pages": sorted({item["page"] for item in candidates if isinstance(item.get("page"), int)}),
                        "review_mode": "manual-receipt-only",
                        "vision_model_invoked": False,
                        "evidence": _handle(review_path) if review_passed and review_path.is_file() else None,
                        "status": "pass" if review_passed else "review-required",
                    }
                if visual_enabled:
                    producer = ({"task_id": finalization_manifest.get("created_by_task_id"),
                                 "run_id": finalization_manifest.get("created_by_run_id")}
                                if finalization_manifest else None)
                    visual_evidence, visual_error = jev_post_render.consume(
                        visual_config, artifact=artifact, raw_scan=raw_visual_scan,
                        artifact_set_id=finalization_manifest.get("artifact_set_id") if finalization_manifest else None,
                        producer=producer,
                    )
                    # Visual review resolves only the deterministic review set.
                    # It cannot replace a separately enabled structure evaluation.
                    visual_passed = visual_error is None and not hard_failed
                    review_passed = visual_passed and (review_passed if jev_enabled or design_enabled else True)
                    limited_review = {
                        "required": True, "candidate_count": len(candidates),
                        "candidate_pages": sorted({c["page"] for c in candidates if isinstance(c.get("page"), int)}),
                        "review_mode": jev_post_render.ROUTE,
                        "vision_model_invoked": False,
                        "evidence": visual_config.get("receipt"),
                        "raw_scan": _handle(raw_visual_scan),
                        "status": "pass" if review_passed else "review-required",
                    }
                    scan["jev_post_render_review"] = visual_evidence or {"status": "unresolved"}
                scan["reviewed_scan_sha256"] = candidate_scan_sha256
                scan["limited_visual_review"] = limited_review
                scan["status"] = "block" if hard_failed else "pass" if review_passed else "review"
                scan_path.write_text(json.dumps(scan, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                if standard_design_route and design_result is not None:
                    receipt["workflow_state"] = design_result.get("workflow_state", "review_required")
                    receipt["worker_return"] = design_result.get("worker_return")
                    receipt["jev_evaluation"] = {
                        "path": str(scan_path.resolve()),
                        "sha256": sha256(scan_path),
                        "execution_mode": design_execution_mode,
                        "request_set_evaluation": design_result.get("request_set"),
                        "provider_invoked": design_result.get("provider_invoked", False),
                        "provider_calls": design_result.get("provider_calls", 0),
                        "vision_model_invoked": design_result.get("vision_model_invoked", False),
                        "visual_fallback": design_result.get("visual_fallback", False),
                        **design_input_handles,
                    }
                entry["rules"].append(_rule("AQ-LAYOUT-01", status="pass" if scan["status"] == "pass" else "fail", evidence=_handle(scan_path)))
            else:
                entry["rules"].append(_rule("AQ-LAYOUT-01", status="fail"))

        if fax.applies_to_artifact(contract, artifact) and artifact.is_file():
            threshold = contract.get("render_contract", {}).get("fax", {}).get("binary", {}).get("threshold")
            renders: dict[str, Path] = {}
            if isinstance(threshold, int):
                try:
                    renders = _render_fax(artifact, evidence, threshold)
                except Exception:
                    renders = {}
            for rule_id in fax.RULES:
                binary_meta = None
                if rule_id == "FAX-BINARY-META-01":
                    path = renders.get("FAX-BINARY-01")
                    binary_meta = {"mode": "1-bit", "threshold": threshold}
                elif rule_id == "FAX-LEGIBILITY-01":
                    path = evidence / f"{_key(artifact)}.FAX-LEGIBILITY-01.json"
                else:
                    path = renders.get(rule_id)
                entry["rules"].append(_rule(rule_id, evidence=_handle(path) if path and path.is_file() else None, binary=binary_meta))

        validation_view = {
            "artifact_path": entry["artifact"]["path"],
            "artifact_sha256": entry["artifact"]["sha256"],
            "actual_delivery_readback": entry["actual_delivery_readback"],
            "rules": entry["rules"],
        }
        if additive_common:
            validation_view["common_evidence"] = entry.get("common_evidence")
        errors = contract_errors + common.validate_entry(contract_for_validation, validation_view) + fax.validate_entry(contract, validation_view)
        errors.extend(common_evidence_errors)
        if finalization_manifest is not None:
            errors.extend(rendered_readback.validate_handle(
                entry["actual_delivery_readback"], artifact,
                producer_run_id=finalization_manifest["created_by_run_id"],
                artifact_set_id=finalization_manifest["artifact_set_id"],
                execution_contract_path=execution_contract_path,
                producer_receipt_path=producer_receipt_path,
            ))
        if safe_wrap_status == "fail":
            tagged = ",".join(safe_wrap_reason_ids or ["AQ-SAFE-WRAP-01"])
            errors.append(f"AQ-SAFE-WRAP-01: [{tagged}] source-side safe-wrap receipt is missing, stale, unsafe, or uses an unbound font")
        if html_pdf_status == "fail":
            tagged = ",".join(html_pdf_reason_ids or ["AQ-HTML-PDF-01"])
            errors.append(f"AQ-HTML-PDF-01: [{tagged}] controlled HTML-to-PDF source requires a canonical body-free wrap/spacing receipt with rendered PDF parity")
        layout_rule = next((rule for rule in entry["rules"] if rule.get("id") == "AQ-LAYOUT-01"), None)
        if layout_rule is not None and layout_rule.get("status") != "pass":
            errors.append("AQ-LAYOUT-01: deterministic layout/typography scan found blocking candidates")
        for rule in entry["rules"]:
            if any(error.startswith(rule["id"] + ":") or error.startswith(rule["id"] + "-") for error in errors):
                rule["status"] = "fail"
        entry["status"] = "pass" if not errors else "block"
        receipt["entries"].append(entry)

    if layer_enabled:
        baseline = [
            rule for entry in receipt["entries"] for rule in entry["rules"]
            if rule.get("status") != "NOT_APPLICABLE"
        ]
        if composition_context is None:
            receipt["quality_layers"] = composition.evaluate(quality_config, artifact_kind=artifact_kind, baseline_statuses=baseline)
        else:
            effective_quality, source_path, target_artifact = composition_context
            receipt["quality_layers"] = composition.evaluate(
                effective_quality, artifact_kind=artifact_kind, baseline_statuses=baseline,
                source=source_path, artifact=target_artifact,
            )
        if composition.blocking(receipt["quality_layers"]):
            receipt["status"] = "block"
    if receipt["entries"] and all(entry.get("status") == "pass" for entry in receipt["entries"]) and not common.contains_forbidden_key(receipt) and not composition.blocking(receipt["quality_layers"]):
        receipt["status"] = "pass"
    receipt["artifact_quality"] = "pass" if receipt["status"] == "pass" else "block"
    receipt.setdefault("workflow_state", "ready" if receipt["status"] == "pass" else "blocked")
    return receipt, 0 if receipt["status"] == "pass" else 2


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--auto-repair", action="store_true", help="opt in to bounded registered-adapter repair")
    parser.add_argument("--repair-adapter", choices=[generic_pdf_repair_adapter.ADAPTER_ID])
    parser.add_argument("--repair-source", type=Path)
    parser.add_argument("--repair-content-identity-sha256")
    parser.add_argument("--repair-root", type=Path)
    parser.add_argument("--repair-controller-receipt", type=Path)
    parser.add_argument("--repair-task-id")
    parser.add_argument("--repair-producer-run-id", default="canonical-aq")
    parser.add_argument("--repair-max-attempts", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    receipt, code = build_receipt(args.contract, evidence_dir=args.evidence_dir)
    _write_receipt(args.receipt, receipt)
    if not args.auto_repair or code == 0:
        print(json.dumps({"status": receipt["status"], "receipt": str(args.receipt.resolve())}))
        return code
    required = {
        "repair_adapter": args.repair_adapter,
        "repair_source": args.repair_source,
        "repair_content_identity_sha256": args.repair_content_identity_sha256,
        "repair_root": args.repair_root,
        "repair_controller_receipt": args.repair_controller_receipt,
        "repair_task_id": args.repair_task_id,
    }
    missing = [key for key, value in required.items() if value in {None, ""}]
    if missing:
        print(json.dumps({"status": "block", "reason": "auto-repair arguments missing", "fields": missing}))
        return 2
    generic_pdf_repair_adapter.register()

    def canonical_recheck(contract_path: Path, evidence_dir: Path, receipt_path: Path) -> tuple[dict[str, Any], int]:
        revised, exit_code = build_receipt(contract_path, evidence_dir=evidence_dir)
        _write_receipt(receipt_path, revised)
        return revised, exit_code

    try:
        controller, exit_code = repair_controller.run(
            task_id=args.repair_task_id,
            contract_path=args.contract,
            source_path=args.repair_source,
            content_identity_sha256=args.repair_content_identity_sha256,
            initial_aq_receipt=args.receipt,
            adapter_id=args.repair_adapter,
            revision_root=args.repair_root,
            max_attempts=args.repair_max_attempts,
            checker=canonical_recheck,
            evidence_root=args.evidence_dir or args.receipt.parent,
            output_receipt=args.repair_controller_receipt,
            producer_run_id=args.repair_producer_run_id,
        )
    except Exception as exc:
        print(json.dumps({"status": "block", "reason": str(exc), "receipt": str(args.receipt.resolve())}))
        return 2
    print(json.dumps({
        "status": controller["status"],
        "receipt": controller["final_aq_receipt"]["path"],
        "controller_receipt": str(args.repair_controller_receipt.resolve()),
        "artifact": controller["final_artifact"]["path"],
    }))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
