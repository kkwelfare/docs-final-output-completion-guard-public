"""Source-aware content-block spacing for the existing AQ layout/relation paths.

The module is deliberately body-free: it consumes a producer manifest containing
stable block identifiers, roles, containment, and rendered geometry, and emits
only geometry/role/relation evidence. It never infers a semantic hard failure
from an ambiguous PDF-only mapping.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from . import relative_anchor_gap, visible_ink
except ImportError:  # standalone fixture/import path
    from quality_rules import relative_anchor_gap, visible_ink

SCHEMA_VERSION = "docs-content-block-evidence-1"
STRICT_SCHEMA_VERSION = "docs-content-block-evidence-2"
SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, STRICT_SCHEMA_VERSION})
SUPPORTED_KINDS = frozenset({"group", "block", "surface", "fill", "border", "text", "table", "line", "icon", "image", "shape"})
SVG_PRODUCER_CAPABILITIES = frozenset({
    "shape", "border", "text", "icon", "block", "parent_child", "nested_inset",
    "anchor_gap", "connector_route", "layer_relation", "content_inset",
})
REVIEW_RULES = frozenset({
    "content_block_spacing_candidate",
    "content_block_intra_spacing_candidate",
    "content_block_mapping_ambiguous",
    "content_block_vertical_association_unspecified",
    "content_block_geometry_relation_ambiguous",
})
HARD_RULES = frozenset({
    "content_block_clipping",
    "content_block_overlap",
    "content_block_overflow",
    "content_block_reflow_mismatch",
    "content_block_preservation_mismatch",
    "content_block_relation_mismatch",
    "content_block_vertical_alignment",
    "content_block_visible_coverage_missing",
})
CONTENT_BLOCK_SPACING_REVIEW_RULES = frozenset({
    "content_block_spacing_candidate",
    "content_block_intra_spacing_candidate",
})
CONTENT_BLOCK_SPACING_DEFECT_RULES = frozenset({"content_block_overlap"})
CONTENT_BLOCK_SPACING_RULES = CONTENT_BLOCK_SPACING_REVIEW_RULES | CONTENT_BLOCK_SPACING_DEFECT_RULES

# These are role-sensitive defaults, not an artifact-wide maximum. A contract
# may override any role or pair through spacing_baselines.
DEFAULT_INTER_BLOCK = {
    "heading_band": 2.0,
    "session_heading": 4.0,
    "heading": 4.0,
    "role_label": 4.0,
    "label": 4.0,
    "body": 6.0,
    "table": 6.0,
    "surface": 2.0,
    "block": 4.0,
    "text": 4.0,
    "*": 2.0,
}
DEFAULT_INTRA_BLOCK = {
    "heading_band": 0.5,
    "session_heading": 1.0,
    "heading": 1.0,
    "role_label": 1.0,
    "label": 1.0,
    "body": 1.0,
    "table": 0.5,
    "surface": 0.5,
    "block": 1.0,
    "text": 1.0,
    "*": 0.5,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _absolute_hash_handle(value: Any, label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}: absolute path and sha256 required"]
    path_value, digest = value.get("path"), value.get("sha256")
    path = Path(path_value) if isinstance(path_value, str) else Path()
    if not path.is_absolute() or not _hash(digest):
        return [f"{label}: absolute path and sha256 required"]
    if not path.is_file() or sha256(path) != digest:
        return [f"{label}: path/hash drift"]
    return []


def enabled(policy: Any) -> bool:
    return isinstance(policy, dict) and policy.get("enabled") is True


def _spacing_mode(policy: dict[str, Any]) -> str:
    """Return the opt-in spacing mode; absent/disabled keeps current behavior."""
    value = policy.get("spacing_policy") if isinstance(policy, dict) else None
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return "legacy"
    return str(value.get("mode", "legacy"))


def _spacing_target_scopes(policy: dict[str, Any]) -> set[str]:
    value = policy.get("spacing_policy") if isinstance(policy, dict) else None
    scopes = value.get("target_scopes") if isinstance(value, dict) else None
    if isinstance(scopes, list):
        return {str(item) for item in scopes}
    return {"inter_block", "intra_block", "block_to_following_text"}


def _any_spacing_issue(issue: dict[str, Any]) -> bool:
    return issue.get("rule") in CONTENT_BLOCK_SPACING_RULES


def _scoped_spacing_issue(issue: dict[str, Any], policy: dict[str, Any]) -> bool:
    return _any_spacing_issue(issue) and issue.get("scope") in _spacing_target_scopes(policy)


def _structured_suffix(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return Path(value).suffix.lower() in {".docx", ".pptx", ".svg", ".html", ".htm", ".xlsx", ".odt", ".ods"}


def _baseline_map(policy: dict[str, Any], scope: str) -> dict[str, Any]:
    raw = policy.get("spacing_baselines", policy.get("role_baselines", {}))
    if not isinstance(raw, dict):
        return {}
    value = raw.get(scope)
    if isinstance(value, dict):
        return value
    if all(isinstance(key, str) for key in raw) and any(_numeric(value) for value in raw.values()):
        return raw
    return {}


def validate_policy(policy: Any, *, source_path: Any = None) -> list[str]:
    """Validate the opt-in policy while leaving absent/disabled policy legacy-safe."""
    if policy is None:
        return []
    prefix = "AQ-LAYOUT-01: content_block_policy"
    if not isinstance(policy, dict):
        return [f"{prefix} must be an object"]
    if policy.get("enabled") is False:
        return []
    if policy.get("enabled") is not True:
        return [f"{prefix}.enabled must be boolean"]
    source_preference = policy.get("source_preference")
    if (not isinstance(source_preference, list) or not source_preference
            or any(not isinstance(item, str) or not item for item in source_preference)):
        return [f"{prefix}.source_preference must be a non-empty string array"]
    relation_sink = policy.get("relation_sink")
    if relation_sink != "AQ-RELATION-01":
        return [f"{prefix}.relation_sink must be AQ-RELATION-01"]
    roles = policy.get("target_roles")
    if (not isinstance(roles, list) or not roles or len(set(roles)) != len(roles)
            or any(not isinstance(item, str) or not item for item in roles)):
        return [f"{prefix}.target_roles must be unique non-empty roles"]
    surfaces = policy.get("include_surfaces")
    if (not isinstance(surfaces, list) or not surfaces
            or any(item not in {"text", "table_fill", "table_border", "line", "icon", "image", "shape"} for item in surfaces)):
        return [f"{prefix}.include_surfaces contains unsupported member"]
    excluded = policy.get("exclude_roles", [])
    if not isinstance(excluded, list) or any(not isinstance(item, str) or not item for item in excluded):
        return [f"{prefix}.exclude_roles must be a string array"]
    overlap = policy.get("min_x_overlap_ratio", 0.5)
    tolerance = policy.get("touch_tolerance_pt", 0.5)
    if not _numeric(overlap) or not 0 < float(overlap) <= 1:
        return [f"{prefix}.min_x_overlap_ratio must be in (0,1]"]
    if not _numeric(tolerance) or float(tolerance) < 0:
        return [f"{prefix}.touch_tolerance_pt must be non-negative"]
    if policy.get("ambiguous_mapping") != "manual_review":
        return [f"{prefix}.ambiguous_mapping must be manual_review"]
    max_groups = policy.get("max_review_groups", 20)
    if not isinstance(max_groups, int) or isinstance(max_groups, bool) or max_groups < 1:
        return [f"{prefix}.max_review_groups must be a positive integer"]
    if policy.get("model_call_if_zero_candidates") is not False:
        return [f"{prefix}.model_call_if_zero_candidates must be false"]
    relation_policy = policy.get("relation_policy")
    if relation_policy is not None:
        if not isinstance(relation_policy, dict) or relation_policy.get("status") not in {"not_applicable", "declared"}:
            return [f"{prefix}.relation_policy.status must be not_applicable or declared"]
        relation_ids = relation_policy.get("required_relation_ids")
        if relation_policy.get("status") == "declared":
            if (not isinstance(relation_ids, list) or not relation_ids
                    or len(set(relation_ids)) != len(relation_ids)
                    or any(not isinstance(item, str) or not item for item in relation_ids)):
                return [f"{prefix}.relation_policy.required_relation_ids must be unique non-empty IDs"]
        elif relation_ids not in (None, []):
            return [f"{prefix}.relation_policy.required_relation_ids must be absent when not_applicable"]
    coverage = policy.get("visible_element_coverage")
    if coverage is not None:
        coverage_prefix = f"{prefix}.visible_element_coverage"
        if not isinstance(coverage, dict) or coverage.get("required") is not True:
            return [f"{coverage_prefix}.required must be true"]
        kinds = coverage.get("kinds")
        if (not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds)
                or any(item not in {"fill", "border", "line", "text", "shape", "table", "image"} for item in kinds)):
            return [f"{coverage_prefix}.kinds must contain unique supported kinds"]
        tolerance = coverage.get("bbox_tolerance_pt", 0.75)
        if not _numeric(tolerance) or float(tolerance) < 0:
            return [f"{coverage_prefix}.bbox_tolerance_pt must be non-negative"]
        errors = _absolute_hash_handle(coverage.get("render_manifest"), f"{coverage_prefix}.render_manifest")
        if errors:
            return errors
    spacing_policy = policy.get("spacing_policy")
    if spacing_policy is not None:
        spacing_prefix = f"{prefix}.spacing_policy"
        if not isinstance(spacing_policy, dict):
            return [f"{spacing_prefix} must be an object"]
        if spacing_policy.get("enabled") is not False:
            if spacing_policy.get("enabled") is not True:
                return [f"{spacing_prefix}.enabled must be boolean"]
            if spacing_policy.get("mode") not in {"shadow", "enforced"}:
                return [f"{spacing_prefix}.mode must be shadow or enforced"]
            if spacing_policy.get("measurement_basis") != "pdf_points":
                return [f"{spacing_prefix}.measurement_basis must be pdf_points"]
            scopes = spacing_policy.get("target_scopes")
            if (not isinstance(scopes, list) or not scopes or len(set(scopes)) != len(scopes)
                    or any(item not in {"inter_block", "intra_block", "block_to_following_text"} for item in scopes)):
                return [f"{spacing_prefix}.target_scopes must contain unique supported scopes"]
            if spacing_policy.get("selection") != "manual_accepted_candidate":
                return [f"{spacing_prefix}.selection must be manual_accepted_candidate"]
    source_evidence = policy.get("source_evidence")
    if _structured_suffix(source_path) or policy.get("source_evidence_required") is True:
        errors = _absolute_hash_handle(source_evidence, f"{prefix}.source_evidence")
        if errors:
            return errors
    elif source_evidence is not None:
        errors = _absolute_hash_handle(source_evidence, f"{prefix}.source_evidence")
        if errors:
            return errors
    strict_identity = policy.get("strict_identity")
    if strict_identity is not None:
        if (not isinstance(strict_identity, dict)
                or not isinstance(strict_identity.get("producer_run_id"), int)
                or isinstance(strict_identity.get("producer_run_id"), bool)
                or strict_identity["producer_run_id"] < 0
                or not _hash(strict_identity.get("artifact_set_id"))):
            return [f"{prefix}.strict_identity requires producer_run_id and artifact_set_id"]
    for scope in ("inter_block", "intra_block", "block_to_following_text"):
        values = _baseline_map(policy, scope)
        for role, value in values.items():
            if not isinstance(role, str) or not role or not _numeric(value) or float(value) < 0:
                return [f"{prefix}.spacing_baselines.{scope} must contain non-negative numeric roles"]
    for key in ("inter_block_min_gap_pt", "intra_block_min_gap_pt", "minimum_gap_pt"):
        value = policy.get(key)
        if value is not None and (not _numeric(value) or float(value) < 0):
            return [f"{prefix}.{key} must be non-negative"]
    return []


def _forbidden_body_key(value: Any) -> bool:
    forbidden = {"text", "body", "original", "rewritten", "response_text", "value", "actual_value", "expected_value", "raw", "payload", "content", "visual_pass", "manual_visual_pass", "looks_good"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or _forbidden_body_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_forbidden_body_key(item) for item in value)
    return False


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        for key in ("bbox_pdf_points", "bbox", "candidate_bbox_pdf_points"):
            if key in value:
                value = value[key]
                break
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_numeric(item) for item in value):
        return None
    box = tuple(float(item) for item in value)
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _render_bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    render = block.get("render")
    if isinstance(render, dict):
        return _bbox(render.get("bbox_pdf_points", render.get("bbox")))
    return _bbox(block.get("bbox_pdf_points", block.get("bbox")))


def _actual_visible_elements(artifact: Path, kinds: set[str]) -> list[dict[str, Any]]:
    """Inventory PDF primitives without retaining extracted body text."""
    import fitz
    rows: list[dict[str, Any]] = []
    with fitz.open(artifact) as document:
        for page_no, page in enumerate(document, start=1):
            blocks = page.get_text("dict").get("blocks", [])
            if "text" in kinds:
                rows.extend({"kind": "text", "page": page_no, "bbox_pdf_points": list(_bbox(block["bbox"])), "edge_basis": "text_bbox"}
                            for block in blocks if block.get("type") == 0 and _bbox(block.get("bbox")) is not None)
            if "image" in kinds:
                rows.extend({"kind": "image", "page": page_no, "bbox_pdf_points": list(_bbox(block["bbox"])), "edge_basis": "image_bbox"}
                            for block in blocks if block.get("type") == 1 and _bbox(block.get("bbox")) is not None)
            for drawing in page.get_drawings():
                raw_rect = list(drawing.get("rect", ()))
                if len(raw_rect) != 4 or not all(_numeric(value) for value in raw_rect):
                    continue
                width = float(drawing.get("width", 0.0) or 0.0)
                # A PDF line may have zero logical height/width. Its visible
                # stroke bbox is still non-empty once half the stroke width is
                # applied; fills/shapes continue to use their geometric box.
                box = _bbox(raw_rect)
                stroke_box = [float(raw_rect[0]) - width / 2, float(raw_rect[1]) - width / 2,
                              float(raw_rect[2]) + width / 2, float(raw_rect[3]) + width / 2]
                if box is None and drawing.get("color") is None:
                    continue
                if drawing.get("fill") is not None and "fill" in kinds:
                    rows.append({"kind": "fill", "page": page_no, "bbox_pdf_points": list(box), "edge_basis": "fill_edge"})
                if drawing.get("color") is not None:
                    kind = "border" if drawing.get("type") in {"fs", "s"} and box is not None and box[2] > box[0] and box[3] > box[1] else "line"
                    if kind in kinds:
                        rows.append({"kind": kind, "page": page_no, "bbox_pdf_points": stroke_box,
                                     "edge_basis": "stroke_edge", "stroke_width_pt": width})
                if drawing.get("fill") is None and drawing.get("color") is None and "shape" in kinds:
                    rows.append({"kind": "shape", "page": page_no, "bbox_pdf_points": list(box), "edge_basis": "shape_bbox"})
    occurrences: dict[tuple[Any, ...], int] = defaultdict(int)
    for index, row in enumerate(rows):
        signature = (
            row["kind"], row["page"], row["edge_basis"],
            tuple(round(float(value), 6) for value in row["bbox_pdf_points"]),
        )
        occurrences[signature] += 1
        row["actual_id"] = f"pdf-visible-{index + 1}"
        row["occurrence"] = occurrences[signature]
    return rows


def _visible_coverage(manifest: dict[str, Any], policy: dict[str, Any], artifact: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, tuple[float, float, float, float]]]:
    spec = policy.get("visible_element_coverage")
    if not isinstance(spec, dict) or spec.get("required") is not True:
        return None, [], {}
    declared = manifest.get("visible_elements")
    if not isinstance(declared, list) or not declared:
        return None, [{"rule": "content_block_visible_coverage_missing", "page": 1, "reason_code": "visible_element_manifest_missing"}], {}
    actual = _actual_visible_elements(artifact, set(spec["kinds"]))
    tolerance = float(spec.get("bbox_tolerance_pt", 0.75))
    unmatched = set(range(len(actual)))
    matches: list[dict[str, Any]] = []
    measured: dict[str, tuple[float, float, float, float]] = {}
    issues: list[dict[str, Any]] = []
    expected_occurrences: dict[tuple[Any, ...], int] = defaultdict(int)
    for expected in declared:
        if not isinstance(expected, dict):
            issues.append({"rule": "content_block_visible_coverage_missing", "page": 1, "reason_code": "visible_element_entry_invalid"})
            continue
        expected_box = _bbox(expected.get("bbox_pdf_points"))
        candidates = [index for index in unmatched if actual[index]["kind"] == expected.get("kind") and actual[index]["page"] == expected.get("page")]
        if expected_box is not None:
            candidates = [index for index in candidates if max(abs(a - b) for a, b in zip(actual[index]["bbox_pdf_points"], expected_box)) <= tolerance]
        candidates = [index for index in candidates if actual[index]["edge_basis"] == expected.get("edge_basis")]
        signature = (
            expected.get("kind"), expected.get("page"), expected.get("edge_basis"),
            tuple(round(float(value), 6) for value in expected_box) if expected_box is not None else None,
        )
        expected_occurrences[signature] += 1
        occurrence = expected.get("occurrence", expected_occurrences[signature])
        candidates = [index for index in candidates if actual[index]["occurrence"] == occurrence]
        if len(candidates) != 1:
            issues.append({"rule": "content_block_visible_coverage_missing", "page": expected.get("page", 1),
                           "element_id": expected.get("id"), "reason_code": "actual_pdf_element_missing_or_ambiguous"})
            continue
        index = candidates[0]
        unmatched.remove(index)
        row = {"element_id": expected.get("id"), "block_id": expected.get("block_id"), **actual[index]}
        matches.append(row)
        if isinstance(expected.get("block_id"), str):
            block_id = expected["block_id"]
            actual_box = tuple(actual[index]["bbox_pdf_points"])
            existing_box = measured.get(block_id)
            measured[block_id] = actual_box if existing_box is None else (
                min(existing_box[0], actual_box[0]),
                min(existing_box[1], actual_box[1]),
                max(existing_box[2], actual_box[2]),
                max(existing_box[3], actual_box[3]),
            )
    for index in sorted(unmatched):
        row = actual[index]
        issues.append({"rule": "content_block_visible_coverage_missing", "page": row["page"], "actual_id": row["actual_id"],
                       "actual_kind": row["kind"], "reason_code": "actual_pdf_element_undeclared"})
    render_manifest_path = Path(spec["render_manifest"]["path"])
    try:
        render_data = json.loads(render_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        render_data = {}
    if (render_data.get("artifact_path") != str(artifact.resolve()) or render_data.get("artifact_sha256") != sha256(artifact)
            or not isinstance(render_data.get("renders"), list) or not render_data["renders"]):
        issues.append({"rule": "content_block_visible_coverage_missing", "page": 1, "reason_code": "render_manifest_artifact_binding_mismatch"})
    render_bindings: list[dict[str, Any]] = []
    if not issues or render_data.get("artifact_sha256") == sha256(artifact):
        try:
            from PIL import Image
            page_sizes = _page_sizes(artifact)
            renders = {item.get("page"): item for item in render_data.get("renders", []) if isinstance(item, dict)}
            for match in matches:
                render = renders.get(match["page"])
                page_size = page_sizes.get(match["page"])
                page_render_path = Path(render.get("path", "")) if isinstance(render, dict) else Path()
                if not page_size or not page_render_path.is_absolute() or not page_render_path.is_file() or render.get("sha256") != sha256(page_render_path):
                    raise ValueError("render missing or drifted")
                with Image.open(page_render_path) as image:
                    image.load()
                    box = match["bbox_pdf_points"]
                    scale_x, scale_y = image.width / page_size[0], image.height / page_size[1]
                    crop_box = (max(0, int(box[0] * scale_x)), max(0, int(box[1] * scale_y)),
                                min(image.width, max(1, int(math.ceil(box[2] * scale_x)))),
                                min(image.height, max(1, int(math.ceil(box[3] * scale_y)))))
                    crop = image.convert("RGBA").crop(crop_box)
                    crop_digest = hashlib.sha256(crop.tobytes()).hexdigest()
                render_bindings.append({"element_id": match["element_id"], "page": match["page"],
                                        "whole_render_sha256": render["sha256"], "crop_pixel_sha256": crop_digest,
                                        "crop_bbox_pixels": list(crop_box)})
        except Exception:
            issues.append({"rule": "content_block_visible_coverage_missing", "page": 1, "reason_code": "crop_full_render_binding_failed"})
    evidence = {"artifact_sha256": sha256(artifact), "render_manifest_sha256": sha256(render_manifest_path),
                "declared_count": len(declared), "actual_count": len(actual), "matched_count": len(matches),
                "matches": matches, "render_bindings": render_bindings, "status": "fail" if issues else "pass"}
    return evidence, issues, measured


def _block_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _nonnegative_fields(value: dict[str, Any], fields: set[str]) -> bool:
    return all(key not in value or (_numeric(value[key]) and float(value[key]) >= 0) for key in fields)


def _validate_geometry_relation(relation: dict[str, Any], relation_id: str) -> list[str]:
    """Strictly validate additive geometry relations without changing legacy types."""
    errors: list[str] = []
    relation_type = relation.get("type")
    constraints = relation.get("constraints")
    ordered = relation.get("ordered_member_ids")
    if not isinstance(constraints, dict):
        return [f"content_block_evidence: constraints required for {relation_id}"]
    declaration = constraints.get(relation_type) if isinstance(relation_type, str) else None
    if not isinstance(declaration, dict) or set(constraints) != {relation_type}:
        return [f"content_block_evidence: {relation_type} constraint required exclusively for {relation_id}"]
    if relation_type == "anchor_gap":
        if declaration.get("measurement_model") == relative_anchor_gap.MEASUREMENT_MODEL:
            try:
                normalized = relative_anchor_gap.canonicalize_contract(declaration)
                if isinstance(ordered, list) and ordered != [normalized["anchor"]["member_id"], normalized["reference"]["member_id"]]:
                    errors.append(f"content_block_evidence: relative anchor_gap member mapping invalid for {relation_id}")
            except ValueError as exc:
                errors.append(f"content_block_evidence: {exc} for {relation_id}")
        else:
            allowed = {"mode", "axis", "min_gap_pt", "max_gap_pt", "min_axis_overlap_ratio",
                       "max_width_delta_pt", "max_edge_delta_pt", "max_center_delta_pt",
                       "shared_container", "containment_tolerance_pt"}
            mode = declaration.get("mode")
            required = {"mode", "axis", "shared_container", "containment_tolerance_pt"}
            required |= ({"max_gap_pt", "min_axis_overlap_ratio", "max_width_delta_pt",
                          "max_edge_delta_pt", "max_center_delta_pt"} if mode == "attached"
                         else {"min_gap_pt"} if mode == "separate" else set())
            if set(declaration) - allowed or not required <= set(declaration):
                errors.append(f"content_block_evidence: anchor_gap fields invalid for {relation_id}")
            if mode not in {"attached", "separate", "unspecified"} or declaration.get("axis") not in {"vertical", "horizontal"}:
                errors.append(f"content_block_evidence: anchor_gap mode/axis invalid for {relation_id}")
            if declaration.get("shared_container") not in {"required", "allowed", "forbidden"}:
                errors.append(f"content_block_evidence: anchor_gap shared_container invalid for {relation_id}")
            numeric = allowed - {"mode", "axis", "shared_container"}
            if not _nonnegative_fields(declaration, numeric):
                errors.append(f"content_block_evidence: anchor_gap thresholds must be finite non-negative numbers for {relation_id}")
            ratio = declaration.get("min_axis_overlap_ratio")
            if ratio is not None and _numeric(ratio) and float(ratio) > 1:
                errors.append(f"content_block_evidence: anchor_gap overlap ratio invalid for {relation_id}")
            forbidden = {"min_gap_pt"} if mode == "attached" else (allowed - {"mode", "axis", "min_gap_pt", "shared_container", "containment_tolerance_pt"} if mode == "separate" else allowed - {"mode", "axis", "shared_container", "containment_tolerance_pt"})
            if forbidden & set(declaration):
                errors.append(f"content_block_evidence: anchor_gap mode contains forbidden fields for {relation_id}")
    elif relation_type == "connector_route":
        allowed = {"mode", "endpoint_tolerance_pt", "boundary_intersection_tolerance_pt",
                   "max_visible_length_inside_surface_pt", "max_visible_area_inside_surface_pt2",
                   "require_source_order_proof", "require_rendered_occlusion_evidence"}
        if set(declaration) != allowed or declaration.get("mode") not in {"behind_surface", "terminate_at_boundary"}:
            errors.append(f"content_block_evidence: connector_route fields invalid for {relation_id}")
        if not _nonnegative_fields(declaration, allowed - {"mode", "require_source_order_proof", "require_rendered_occlusion_evidence"}):
            errors.append(f"content_block_evidence: connector_route thresholds must be finite non-negative numbers for {relation_id}")
        if any(not isinstance(declaration.get(key), bool) for key in ("require_source_order_proof", "require_rendered_occlusion_evidence")):
            errors.append(f"content_block_evidence: connector_route proof flags must be boolean for {relation_id}")
    elif relation_type == "layer_relation":
        allowed = {"mode", "min_overlap_area_pt2", "max_overlap_area_pt2", "min_front_visible_ratio",
                   "max_occluded_ratio", "source_order_required", "flattened_pdf_ambiguity"}
        if set(declaration) != allowed or declaration.get("mode") not in {"front_of", "separate"} or declaration.get("flattened_pdf_ambiguity") != "review":
            errors.append(f"content_block_evidence: layer_relation fields invalid for {relation_id}")
        if not _nonnegative_fields(declaration, allowed - {"mode", "source_order_required", "flattened_pdf_ambiguity"}):
            errors.append(f"content_block_evidence: layer_relation thresholds must be finite non-negative numbers for {relation_id}")
        if any(_numeric(declaration.get(key)) and float(declaration[key]) > 1 for key in ("min_front_visible_ratio", "max_occluded_ratio")):
            errors.append(f"content_block_evidence: layer_relation ratios invalid for {relation_id}")
        if not isinstance(declaration.get("source_order_required"), bool):
            errors.append(f"content_block_evidence: layer_relation source_order_required must be boolean for {relation_id}")
        if (_numeric(declaration.get("min_overlap_area_pt2")) and _numeric(declaration.get("max_overlap_area_pt2"))
                and float(declaration["min_overlap_area_pt2"]) > float(declaration["max_overlap_area_pt2"])):
            errors.append(f"content_block_evidence: layer_relation overlap range invalid for {relation_id}")
    elif relation_type == "content_inset":
        allowed = {"minimum_pt", "balance_axes", "max_balance_delta_pt", "require_contained",
                   "containment_tolerance_pt", "inner_envelope"}
        minimum = declaration.get("minimum_pt")
        if set(declaration) != allowed or declaration.get("balance_axes") not in {"horizontal", "vertical", "both", "none"} or declaration.get("inner_envelope") != "union":
            errors.append(f"content_block_evidence: content_inset fields invalid for {relation_id}")
        if not isinstance(minimum, dict) or set(minimum) != {"left", "right", "top", "bottom"} or not _nonnegative_fields(minimum, set(minimum)):
            errors.append(f"content_block_evidence: content_inset minimum_pt invalid for {relation_id}")
        if not _nonnegative_fields(declaration, {"max_balance_delta_pt", "containment_tolerance_pt"}) or not isinstance(declaration.get("require_contained"), bool):
            errors.append(f"content_block_evidence: content_inset thresholds invalid for {relation_id}")
    if relation_type in {"anchor_gap", "connector_route", "layer_relation"} and isinstance(ordered, list) and len(ordered) != 2:
        errors.append(f"content_block_evidence: {relation_type} requires exactly two targets for {relation_id}")
    if relation_type == "content_inset" and isinstance(ordered, list) and len(ordered) < 2:
        errors.append(f"content_block_evidence: content_inset requires outer and inner targets for {relation_id}")
    applied, effective = relation.get("applied"), relation.get("effective")
    if (applied is None) != (effective is None):
        errors.append(f"content_block_evidence: applied/effective relation evidence must be paired for {relation_id}")
    if applied is not None:
        if (not isinstance(applied, dict) or set(applied) != {"format", "adapter", "ordered_primitive_ids", "source_order_pass", "source_evidence_sha256"}
                or applied.get("format") not in {"html", "pptx", "svg"}
                or _block_id(applied.get("adapter")) is None
                or not isinstance(applied.get("ordered_primitive_ids"), list) or not applied["ordered_primitive_ids"]
                or len(set(applied["ordered_primitive_ids"])) != len(applied["ordered_primitive_ids"])
                or any(_block_id(item) is None for item in applied["ordered_primitive_ids"])
                or not isinstance(applied.get("source_order_pass"), bool) or not _hash(applied.get("source_evidence_sha256"))):
            errors.append(f"content_block_evidence: applied relation evidence invalid for {relation_id}")
        relative_effective = relation_type == "anchor_gap" and declaration.get("measurement_model") == relative_anchor_gap.MEASUREMENT_MODEL
        allowed_effective = {"measurement_basis", "status", "evidence_sha256", "endpoint_distance_pt",
                             "boundary_intersection_distance_pt", "visible_length_inside_surface_pt",
                             "visible_area_inside_surface_pt2", "overlap_area_pt2", "front_visible_ratio",
                             "occluded_ratio", "primitive_mappings", "ambiguity_reason"}
        if relative_effective:
            allowed_effective |= {"annotation_id", "source_annotation_map_sha256", "tail_tip_pdf_points",
                                  "avatar_bbox_pdf_points", "avatar_width_pt", "direction_vector", "gap_pt",
                                  "gap_ratio", "target_gap_ratio", "tolerance", "facing", "no_contact",
                                  "contact_distance_pt", "failed_constraints"}
        if not isinstance(effective, dict) or set(effective) - allowed_effective:
            errors.append(f"content_block_evidence: effective relation evidence invalid for {relation_id}")
        else:
            if (effective.get("measurement_basis") not in {"pdf_vector", "pdf_vector_plus_render_mask", "pdf_render_mask", "png_alpha_mask", "unavailable"}
                    or effective.get("status") not in {"measured", "ambiguous"} or not _hash(effective.get("evidence_sha256"))):
                errors.append(f"content_block_evidence: effective relation basis/status/hash invalid for {relation_id}")
            numeric = {"endpoint_distance_pt", "boundary_intersection_distance_pt", "visible_length_inside_surface_pt",
                       "visible_area_inside_surface_pt2", "overlap_area_pt2", "front_visible_ratio", "occluded_ratio"}
            if not _nonnegative_fields(effective, numeric):
                errors.append(f"content_block_evidence: effective relation metrics invalid for {relation_id}")
            if any(_numeric(effective.get(key)) and float(effective[key]) > 1 for key in ("front_visible_ratio", "occluded_ratio")):
                errors.append(f"content_block_evidence: effective relation ratios invalid for {relation_id}")
            if relative_effective and effective.get("status") == "measured":
                required_relative = {"annotation_id", "source_annotation_map_sha256", "tail_tip_pdf_points",
                                     "avatar_bbox_pdf_points", "avatar_width_pt", "direction_vector", "gap_pt",
                                     "gap_ratio", "target_gap_ratio", "tolerance", "facing", "no_contact",
                                     "contact_distance_pt", "failed_constraints"}
                if not required_relative <= set(effective):
                    errors.append(f"content_block_evidence: relative anchor_gap effective evidence incomplete for {relation_id}")
                else:
                    try:
                        measured = relative_anchor_gap.measure_geometry(
                            tail_tip=effective["tail_tip_pdf_points"], visible_avatar_bbox=effective["avatar_bbox_pdf_points"],
                            direction_vector=effective["direction_vector"], target_gap_ratio=effective["target_gap_ratio"],
                            tolerance=effective["tolerance"], facing_expected=declaration["facing"],
                            facing_actual=effective["facing"], no_contact=declaration["no_contact"],
                            contact_tolerance_pt=declaration["contact_tolerance_pt"],
                            intersection=effective["no_contact"] is not True,
                        )
                        if (effective.get("annotation_id") != declaration["annotation_id"]
                                or effective.get("source_annotation_map_sha256") != declaration["source_annotation_map_sha256"]
                                or abs(float(effective["avatar_width_pt"]) - measured["avatar_width_pt"]) > 1e-4
                                or abs(float(effective["gap_pt"]) - measured["gap_pt"]) > 1e-4
                                or abs(float(effective["gap_ratio"]) - measured["gap_ratio"]) > 1e-8):
                            errors.append(f"content_block_evidence: relative anchor_gap effective evidence drift for {relation_id}")
                    except (KeyError, TypeError, ValueError):
                        errors.append(f"content_block_evidence: relative anchor_gap effective evidence invalid for {relation_id}")
            mappings = effective.get("primitive_mappings")
            if mappings is not None and (not isinstance(mappings, list) or not mappings
                    or any(not isinstance(item, dict) or set(item) != {"primitive_id", "member_id", "page", "bbox_pdf_points", "crop_sha256"}
                           or _block_id(item.get("primitive_id")) is None or _block_id(item.get("member_id")) is None
                           or not isinstance(item.get("page"), int) or isinstance(item.get("page"), bool) or item["page"] < 1
                           or _bbox(item.get("bbox_pdf_points")) is None or not _hash(item.get("crop_sha256")) for item in mappings)):
                errors.append(f"content_block_evidence: primitive mappings invalid for {relation_id}")
            if effective.get("status") == "ambiguous" and _block_id(effective.get("ambiguity_reason")) is None:
                errors.append(f"content_block_evidence: ambiguity_reason required for {relation_id}")
    return errors


def validate_manifest(data: Any, *, artifact: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict) or data.get("schema_version") not in SCHEMA_VERSIONS:
        return ["content_block_evidence: unsupported schema"]
    if _forbidden_body_key(data):
        return ["content_block_evidence: forbidden body field"]
    source = data.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not _hash(source.get("sha256")):
        errors.append("content_block_evidence: source path/hash required")
    elif (not Path(source["path"]).is_absolute() or not Path(source["path"]).is_file()
          or sha256(Path(source["path"])) != source["sha256"]):
        errors.append("content_block_evidence: source identity mismatch")
    producer_family = data.get("producer_family")
    capabilities = data.get("producer_capabilities")
    if producer_family is not None and (not isinstance(producer_family, str) or not producer_family.strip()):
        errors.append("content_block_evidence: producer_family must be non-empty")
    if capabilities is not None and (
            not isinstance(capabilities, list) or not capabilities
            or len(set(capabilities)) != len(capabilities)
            or any(not isinstance(item, str) or item not in SVG_PRODUCER_CAPABILITIES for item in capabilities)):
        errors.append("content_block_evidence: invalid producer_capabilities")
    if producer_family == "docs-svg-material-producer-1" and capabilities is None:
        errors.append("content_block_evidence: SVG producer capabilities required")
    artifact_handle = data.get("artifact")
    if artifact is not None:
        if not isinstance(artifact_handle, dict) or artifact_handle.get("path") != str(artifact.resolve()) or artifact_handle.get("sha256") != sha256(artifact):
            errors.append("content_block_evidence: artifact identity mismatch")
    if ("producer_run_id" in data or "artifact_set_id" in data) and (
            not isinstance(data.get("producer_run_id"), int)
            or isinstance(data.get("producer_run_id"), bool)
            or data["producer_run_id"] < 0 or not _hash(data.get("artifact_set_id"))):
        errors.append("content_block_evidence: run/artifact-set identity is malformed")
    execution_identity = data.get("execution_identity")
    if execution_identity is not None:
        if (not isinstance(execution_identity, dict)
                or set(execution_identity) != {"contract_id", "policy_hash", "producer_run_id", "artifact_set_id"}
                or not _hash(execution_identity.get("contract_id")) or not _hash(execution_identity.get("policy_hash"))
                or not isinstance(execution_identity.get("producer_run_id"), int)
                or isinstance(execution_identity.get("producer_run_id"), bool)
                or execution_identity.get("producer_run_id", -1) < 0
                or not _hash(execution_identity.get("artifact_set_id"))):
            errors.append("content_block_evidence: execution identity is malformed")
        elif (data.get("producer_run_id") != execution_identity["producer_run_id"]
              or data.get("artifact_set_id") != execution_identity["artifact_set_id"]):
            errors.append("content_block_evidence: execution identity drift")
    blocks = data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        errors.append("content_block_evidence: blocks are required")
        return errors
    by_id: dict[str, dict[str, Any]] = {}
    for item in blocks:
        if not isinstance(item, dict):
            errors.append("content_block_evidence: block must be object")
            continue
        identifier = _block_id(item.get("id"))
        if identifier is None or identifier in by_id:
            errors.append("content_block_evidence: block IDs must be unique non-empty strings")
            continue
        by_id[identifier] = item
        if item.get("kind") not in SUPPORTED_KINDS:
            errors.append(f"content_block_evidence: unsupported kind for {identifier}")
        if not isinstance(item.get("role"), str) or not item["role"]:
            errors.append(f"content_block_evidence: role missing for {identifier}")
        owner = item.get("owner_id")
        if owner is not None and not isinstance(owner, str):
            errors.append(f"content_block_evidence: invalid owner for {identifier}")
        parent = item.get("parent_id")
        if parent is not None and not isinstance(parent, str):
            errors.append(f"content_block_evidence: invalid parent for {identifier}")
        members = item.get("member_ids", [])
        if not isinstance(members, list) or len(set(members)) != len(members) or any(not isinstance(member, str) for member in members):
            errors.append(f"content_block_evidence: invalid members for {identifier}")
        order = item.get("order")
        if order is not None and (not isinstance(order, int) or isinstance(order, bool) or order < 0):
            errors.append(f"content_block_evidence: invalid order for {identifier}")
        locator = item.get("source_locator")
        if locator is not None and (not isinstance(locator, dict) or not isinstance(locator.get("part"), str) or not isinstance(locator.get("structural_path"), str)):
            errors.append(f"content_block_evidence: invalid source locator for {identifier}")
        render = item.get("render")
        if render is not None and not isinstance(render, dict):
            errors.append(f"content_block_evidence: invalid render for {identifier}")
        if render is not None and _bbox(render) is not None:
            page = render.get("page")
            if not isinstance(page, int) or page < 1:
                errors.append(f"content_block_evidence: render page missing for {identifier}")
        typography = item.get("typography")
        if typography is not None:
            required = ("effective_line_height_pt", "requested_paragraph_after_pt", "effective_paragraph_after_pt")
            if not isinstance(typography, dict) or any(not _numeric(typography.get(key)) or float(typography[key]) < 0 for key in required):
                errors.append(f"content_block_evidence: invalid typography for {identifier}")
            elif float(typography["effective_paragraph_after_pt"]) + 1e-9 < float(typography["requested_paragraph_after_pt"]):
                errors.append(f"content_block_evidence: effective paragraph-after below requested for {identifier}")
        alignment = item.get("vertical_alignment")
        if alignment is not None:
            boxes = ("container_bbox_pdf_points", "content_box_pdf_points", "occupied_bbox_pdf_points")
            numeric = ("center_tolerance_pt", "content_center_y_pt", "bbox_center_y_pt", "center_delta_pt")
            if (not isinstance(alignment, dict) or alignment.get("vertical_align") != "center"
                    or any(_bbox(alignment.get(key)) is None for key in boxes)
                    or not isinstance(alignment.get("padding_pt"), list) or len(alignment["padding_pt"]) != 4
                    or any(not _numeric(value) or float(value) < 0 for value in alignment["padding_pt"])
                    or any(not _numeric(alignment.get(key)) for key in numeric)
                    or float(alignment.get("center_tolerance_pt", -1)) < 0):
                errors.append(f"content_block_evidence: invalid vertical alignment for {identifier}")
    for identifier, item in by_id.items():
        parent = item.get("parent_id")
        if parent is not None and parent not in by_id:
            errors.append(f"content_block_evidence: parent missing for {identifier}")
        for member in item.get("member_ids", []):
            if member not in by_id:
                errors.append(f"content_block_evidence: member missing for {identifier}")
    relations = data.get("relations", [])
    if not isinstance(relations, list):
        errors.append("content_block_evidence: relations must be an array")
    else:
        relation_ids: set[str] = set()
        for relation in relations:
            if not isinstance(relation, dict):
                errors.append("content_block_evidence: relation must be object")
                continue
            relation_id = _block_id(relation.get("id"))
            if relation_id is None or relation_id in relation_ids:
                errors.append("content_block_evidence: relation IDs must be unique non-empty strings")
                continue
            relation_ids.add(relation_id)
            relation_type = relation.get("type", "ordered")
            geometry_types = {"anchor_gap", "connector_route", "layer_relation", "content_inset"}
            if not isinstance(relation_type, str) or relation_type not in {"ordered", "no-contact", "no-overlap", "gap", "overlap", "nested_inset", "vertical_association", *geometry_types}:
                errors.append(f"content_block_evidence: invalid relation type for {relation_id}")
            ordered = relation.get("ordered_member_ids")
            if (not isinstance(ordered, list) or len(ordered) < 2 or len(set(ordered)) != len(ordered)
                    or any(not isinstance(member, str) or member not in by_id for member in ordered)):
                errors.append(f"content_block_evidence: invalid relation targets for {relation_id}")
            if relation_type == "nested_inset" and isinstance(ordered, list) and len(ordered) != 2:
                errors.append(f"content_block_evidence: nested_inset requires outer and inner targets for {relation_id}")
            if relation_type == "vertical_association" and isinstance(ordered, list) and len(ordered) != 2:
                errors.append(f"content_block_evidence: vertical_association requires upper and lower targets for {relation_id}")
            if relation.get("scope") not in {"inter_block", "intra_block", "block_to_following_text", "parent_child", "sibling", "family"}:
                errors.append(f"content_block_evidence: invalid relation scope for {relation_id}")
            if relation_type in geometry_types:
                errors.extend(_validate_geometry_relation(relation, relation_id))
                continue
            constraints = relation.get("constraints")
            if not isinstance(constraints, dict) or not constraints:
                errors.append(f"content_block_evidence: constraints required for {relation_id}")
                continue
            if "same_page" in constraints and not isinstance(constraints["same_page"], bool):
                errors.append(f"content_block_evidence: same_page must be boolean for {relation_id}")
            if "no_overlap" in constraints and not isinstance(constraints["no_overlap"], bool):
                errors.append(f"content_block_evidence: no_overlap must be boolean for {relation_id}")
            if "min_gap_pt" in constraints and (not _numeric(constraints["min_gap_pt"]) or float(constraints["min_gap_pt"]) < 0):
                errors.append(f"content_block_evidence: min_gap_pt must be non-negative for {relation_id}")
            if relation_type == "nested_inset":
                balance = constraints.get("inset_balance")
                if not isinstance(balance, dict):
                    errors.append(f"content_block_evidence: inset_balance required for {relation_id}")
                else:
                    axes = balance.get("axes")
                    if not isinstance(axes, str) or axes not in {"horizontal", "vertical", "both"}:
                        errors.append(f"content_block_evidence: inset_balance.axes invalid for {relation_id}")
                    if not _numeric(balance.get("max_delta_pt")) or float(balance.get("max_delta_pt", -1)) < 0:
                        errors.append(f"content_block_evidence: inset_balance.max_delta_pt must be non-negative for {relation_id}")
                    if not isinstance(balance.get("require_contained"), bool):
                        errors.append(f"content_block_evidence: inset_balance.require_contained must be boolean for {relation_id}")
            if relation_type == "vertical_association":
                association = constraints.get("vertical_association")
                allowed_keys = {
                    "mode", "max_gap_pt", "min_gap_pt", "max_width_delta_pt",
                    "max_edge_delta_pt", "containment_tolerance_pt",
                }
                if not isinstance(association, dict):
                    errors.append(f"content_block_evidence: vertical_association constraint required for {relation_id}")
                elif set(association) - allowed_keys:
                    errors.append(f"content_block_evidence: vertical_association contains unsupported field for {relation_id}")
                else:
                    mode = association.get("mode")
                    if mode not in {"attached", "separate", "unspecified"}:
                        errors.append(f"content_block_evidence: vertical_association.mode invalid for {relation_id}")
                    if mode == "attached":
                        required = ("max_gap_pt", "max_width_delta_pt", "max_edge_delta_pt", "containment_tolerance_pt")
                        if any(not _numeric(association.get(key)) or float(association[key]) < 0 for key in required):
                            errors.append(f"content_block_evidence: attached vertical_association tolerances must be non-negative for {relation_id}")
                        if "min_gap_pt" in association:
                            errors.append(f"content_block_evidence: attached vertical_association must not declare min_gap_pt for {relation_id}")
                    elif mode == "separate":
                        if not _numeric(association.get("min_gap_pt")) or float(association.get("min_gap_pt", -1)) < 0:
                            errors.append(f"content_block_evidence: separate vertical_association.min_gap_pt must be non-negative for {relation_id}")
                        if any(key in association for key in ("max_gap_pt", "max_width_delta_pt", "max_edge_delta_pt", "containment_tolerance_pt")):
                            errors.append(f"content_block_evidence: separate vertical_association must not declare attached tolerances for {relation_id}")
                    elif mode == "unspecified" and set(association) != {"mode"}:
                        errors.append(f"content_block_evidence: unspecified vertical_association must not declare geometry thresholds for {relation_id}")
        if any(isinstance(item, dict) and item.get("type") in {"anchor_gap", "connector_route", "layer_relation", "content_inset"}
               and ("applied" in item or "effective" in item) for item in relations) and execution_identity is None:
            errors.append("content_block_evidence: execution identity required for applied/effective geometry evidence")
    if data.get("schema_version") == STRICT_SCHEMA_VERSION:
        coverage = data.get("relation_coverage")
        if not isinstance(coverage, dict) or coverage.get("status") not in {"declared", "not_applicable"}:
            errors.append("content_block_evidence: v2 relation_coverage required")
        else:
            surface_ids = coverage.get("surface_ids")
            required_ids = coverage.get("required_relation_ids")
            if (not isinstance(surface_ids, list) or len(set(surface_ids)) != len(surface_ids)
                    or any(item not in by_id for item in surface_ids)):
                errors.append("content_block_evidence: v2 surface inventory invalid")
            if coverage["status"] == "declared":
                declared_ids = {item.get("id") for item in relations if isinstance(item, dict)}
                if (not isinstance(required_ids, list) or not required_ids or len(set(required_ids)) != len(required_ids)
                        or any(item not in declared_ids for item in required_ids)):
                    errors.append("content_block_evidence: v2 required relation coverage missing")
            elif required_ids not in (None, []):
                errors.append("content_block_evidence: v2 not_applicable must not require relations")
            if coverage["status"] == "not_applicable" and (surface_ids or not isinstance(coverage.get("reason"), str)
                    or not coverage["reason"].strip()):
                errors.append("content_block_evidence: v2 not_applicable inventory/reason invalid")
        ink_records = data.get("visible_ink_evidence")
        if not isinstance(ink_records, list):
            errors.append("content_block_evidence: v2 visible_ink_evidence array required")
        else:
            seen_ink: set[str] = set()
            for record in ink_records:
                member_id = record.get("member_id") if isinstance(record, dict) else None
                if not isinstance(member_id, str) or member_id not in by_id or member_id in seen_ink:
                    errors.append("content_block_evidence: v2 visible ink member identity invalid")
                    continue
                seen_ink.add(member_id)
                if artifact is not None:
                    errors.extend(visible_ink.validate_evidence(record, artifact, member_id=member_id))
    visible = data.get("visible_elements")
    if visible is not None:
        if not isinstance(visible, list) or not visible:
            errors.append("content_block_evidence: visible_elements must be a non-empty array")
        else:
            visible_ids: set[str] = set()
            for item in visible:
                if (not isinstance(item, dict) or _block_id(item.get("id")) is None or item.get("id") in visible_ids
                        or item.get("block_id") not in by_id or item.get("kind") not in {"fill", "border", "line", "text", "shape", "table", "image"}
                        or not isinstance(item.get("page"), int) or item.get("page", 0) < 1 or _bbox(item.get("bbox_pdf_points")) is None
                        or item.get("edge_basis") not in {"fill_edge", "stroke_edge", "text_bbox", "shape_bbox", "table_bbox", "image_bbox"}):
                    errors.append("content_block_evidence: invalid visible element")
                elif item.get("kind") in {"fill"} and item.get("edge_basis") != "fill_edge":
                    errors.append("content_block_evidence: fill requires fill_edge")
                elif item.get("kind") in {"border", "line"} and item.get("edge_basis") != "stroke_edge":
                    errors.append("content_block_evidence: border/line requires stroke_edge")
                else:
                    visible_ids.add(item["id"])
    candidates = data.get("review_candidates", [])
    if not isinstance(candidates, list):
        errors.append("content_block_evidence: review_candidates must be an array")
    return list(dict.fromkeys(errors))


def load_manifest(policy: dict[str, Any], artifact: Path) -> tuple[dict[str, Any] | None, list[str]]:
    source_evidence = policy.get("source_evidence")
    errors = _absolute_hash_handle(source_evidence, "content_block_policy.source_evidence")
    if errors:
        return None, errors
    path = Path(source_evidence["path"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["content_block_evidence: manifest unreadable or malformed"]
    errors = validate_manifest(data, artifact=artifact)
    if data.get("schema_version") == STRICT_SCHEMA_VERSION:
        relation_policy = policy.get("relation_policy")
        coverage = data.get("relation_coverage")
        if not isinstance(relation_policy, dict):
            errors.append("content_block_evidence: v2 relation_policy declaration required")
        elif isinstance(coverage, dict) and relation_policy.get("status") != coverage.get("status"):
            errors.append("content_block_evidence: v2 relation coverage policy drift")
        elif isinstance(coverage, dict) and coverage.get("status") == "declared" and (
                relation_policy.get("required_relation_ids") != coverage.get("required_relation_ids")):
            errors.append("content_block_evidence: v2 required relation identity drift")
    strict_identity = policy.get("strict_identity")
    if isinstance(strict_identity, dict) and (
            data.get("producer_run_id") != strict_identity.get("producer_run_id")
            or data.get("artifact_set_id") != strict_identity.get("artifact_set_id")):
        errors.append("content_block_evidence: run/artifact-set identity mismatch")
    return (data, errors) if not errors else (None, errors)


def _minimum_gap(policy: dict[str, Any], scope: str, upper_role: str, lower_role: str) -> float:
    scope_map = _baseline_map(policy, scope)
    pair_keys = (f"{upper_role}->{lower_role}", f"{upper_role}:{lower_role}", lower_role, upper_role, "*")
    for key in pair_keys:
        if _numeric(scope_map.get(key)):
            return max(0.0, float(scope_map[key]))
    scalar_key = {
        "inter_block": "inter_block_min_gap_pt",
        "intra_block": "intra_block_min_gap_pt",
        "block_to_following_text": "block_to_following_text_min_gap_pt",
    }.get(scope, "minimum_gap_pt")
    if _numeric(policy.get(scalar_key)):
        return max(0.0, float(policy[scalar_key]))
    if _numeric(policy.get("minimum_gap_pt")):
        return max(0.0, float(policy["minimum_gap_pt"]))
    defaults = DEFAULT_INTER_BLOCK if scope == "inter_block" else DEFAULT_INTRA_BLOCK
    return float(defaults.get(lower_role, defaults.get(upper_role, defaults["*"])))


def _effective_line_height(block: dict[str, Any]) -> float:
    typography = block.get("typography")
    value = typography.get("effective_line_height_pt") if isinstance(typography, dict) else 0.0
    return max(0.0, float(value)) if _numeric(value) else 0.0


def _x_overlap_ratio(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    return overlap / min(a[2] - a[0], b[2] - b[0])


def _ancestor(by_id: dict[str, dict[str, Any]], left: str, right: str) -> bool:
    def reaches(start: str, target: str) -> bool:
        seen: set[str] = set()
        current = start
        while current in by_id and current not in seen:
            seen.add(current)
            parent = by_id[current].get("parent_id")
            if parent == target:
                return True
            if not isinstance(parent, str):
                break
            current = parent
        return False
    return reaches(left, right) or reaches(right, left)


def _surface_owner(by_id: dict[str, dict[str, Any]], identifier: str) -> str | None:
    for owner_id, owner in by_id.items():
        if identifier in owner.get("member_ids", []):
            return owner_id if owner.get("kind") == "surface" else None
    return None


def _pair_key(scope: str, upper: dict[str, Any], lower: dict[str, Any]) -> str:
    return f"{scope}:{upper.get('role')}->{lower.get('role')}"


def _pair_issue(policy: dict[str, Any], scope: str, upper: dict[str, Any], lower: dict[str, Any], artifact: Path) -> dict[str, Any] | None:
    upper_box, lower_box = _render_bbox(upper), _render_bbox(lower)
    if upper_box is None or lower_box is None:
        return None
    upper_page = upper.get("render", {}).get("page") if isinstance(upper.get("render"), dict) else None
    lower_page = lower.get("render", {}).get("page") if isinstance(lower.get("render"), dict) else None
    if isinstance(upper_page, int) and isinstance(lower_page, int) and upper_page != lower_page:
        return None
    if upper_box[1] > lower_box[1]:
        upper, lower = lower, upper
        upper_box, lower_box = lower_box, upper_box
    ratio = _x_overlap_ratio(upper_box, lower_box)
    if ratio < float(policy.get("min_x_overlap_ratio", 0.5)):
        return None
    gap = lower_box[1] - upper_box[3]
    requested_gap = _minimum_gap(policy, scope, str(upper.get("role")), str(lower.get("role")))
    min_gap = max(requested_gap, _effective_line_height(upper), _effective_line_height(lower))
    page = upper.get("render", {}).get("page", 1) if isinstance(upper.get("render"), dict) else 1
    pair_bbox = [
        round(min(upper_box[0], lower_box[0]), 3),
        round(min(upper_box[1], lower_box[1]), 3),
        round(max(upper_box[2], lower_box[2]), 3),
        round(max(upper_box[3], lower_box[3]), 3),
    ]
    base = {"scope": scope, "upper_id": upper["id"], "lower_id": lower["id"],
            "upper_role": upper.get("role"), "lower_role": lower.get("role"), "gap_pt": round(gap, 3),
            "requested_gap_pt": round(requested_gap, 3), "effective_gap_pt": round(min_gap, 3),
            "minimum_gap_pt": round(min_gap, 3), "x_overlap_ratio": round(ratio, 4),
            "page": page, "bbox_pdf_points": pair_bbox}
    if gap < 0:
        return {"rule": "content_block_overlap", **base}
    if gap < min_gap:
        return {"rule": "content_block_spacing_candidate" if scope == "inter_block" else "content_block_intra_spacing_candidate",
                "relation_key": _pair_key(scope, upper, lower), "artifact_path": str(artifact.resolve()), **base}
    return None


def _coalesce(issues: list[dict[str, Any]], artifact: Path, max_groups: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        if issue.get("rule") not in REVIEW_RULES:
            continue
        gap = float(issue.get("gap_pt", 0)) if _numeric(issue.get("gap_pt")) else 0.0
        if issue.get("rule") == "content_block_vertical_association_unspecified":
            key = (issue.get("rule"), issue.get("relation_id"), issue.get("association_mode"))
        elif issue.get("rule") == "content_block_geometry_relation_ambiguous":
            key = (issue.get("rule"), issue.get("relation_type"), issue.get("relation_mode"),
                   issue.get("reason_code"), issue.get("measurement_basis"))
        else:
            key = (issue.get("rule"), issue.get("scope"), issue.get("upper_role"), issue.get("lower_role"), round(gap / 0.5) * 0.5)
        grouped[key].append(issue)
    result: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))):
        members = sorted(grouped[key], key=lambda item: (int(item.get("page", 1)), str(item.get("upper_id")), str(item.get("lower_id"))))
        representative = dict(members[0])
        pages = sorted({int(item.get("page", 1)) for item in members})
        representative["id"] = "content-" + hashlib.sha256(json.dumps([sha256(artifact), key], sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
        representative["affected_pages"] = pages
        representative["affected_count"] = len(members)
        representative["example_member_ids"] = sorted({str(item.get("upper_id")) for item in members} | {str(item.get("lower_id")) for item in members})[:8]
        if representative.get("rule") in CONTENT_BLOCK_SPACING_REVIEW_RULES:
            representative["spacing_group_key"] = (
                f"{representative.get('scope')}:{representative.get('upper_role')}->"
                f"{representative.get('lower_role')}"
            )
        result.append(representative)
    if len(result) <= max_groups:
        return result
    retained = result[:max_groups - 1]
    overflow = result[max_groups - 1:]
    retained.append({
        "id": "content-" + hashlib.sha256(json.dumps([sha256(artifact), "overflow"], sort_keys=True).encode()).hexdigest()[:16],
        "rule": "content_block_mapping_ambiguous", "scope": "review", "page": overflow[0].get("page", 1),
        "affected_pages": sorted({page for item in overflow for page in item.get("affected_pages", [])}),
        "affected_count": sum(int(item.get("affected_count", 0)) for item in overflow),
        "overflow_group_count": len(result), "max_review_groups": max_groups,
        "reason_code": "max_review_groups_exceeded",
    })
    return retained


def _page_sizes(artifact: Path) -> dict[int, tuple[float, float]]:
    try:
        import fitz
        with fitz.open(artifact) as document:
            return {index: (float(page.rect.width), float(page.rect.height)) for index, page in enumerate(document, start=1)}
    except Exception:
        return {}


def _shared_container_id(upper: dict[str, Any], lower: dict[str, Any]) -> str | None:
    for key in ("owner_id", "parent_id"):
        left, right = upper.get(key), lower.get(key)
        if isinstance(left, str) and left and left == right:
            return left
    return None


def _vertical_association_metrics(
    upper: dict[str, Any], lower: dict[str, Any], by_id: dict[str, dict[str, Any]],
    measured_boxes: dict[str, tuple[float, float, float, float]],
) -> dict[str, Any]:
    upper_box = measured_boxes.get(upper["id"], _render_bbox(upper))
    lower_box = measured_boxes.get(lower["id"], _render_bbox(lower))
    upper_page = upper.get("render", {}).get("page") if isinstance(upper.get("render"), dict) else None
    lower_page = lower.get("render", {}).get("page") if isinstance(lower.get("render"), dict) else None
    result: dict[str, Any] = {
        "upper_id": upper["id"], "lower_id": lower["id"],
        "upper_page": upper_page, "lower_page": lower_page,
        "gap_pt": None, "upper_width_pt": None, "lower_width_pt": None,
        "width_delta_pt": None, "left_edge_delta_pt": None, "right_edge_delta_pt": None,
        "edge_delta_pt": None, "centerline_delta_pt": None,
        "shared_container_id": None, "shared_container_rendered": False,
        "containment_deltas_pt": {}, "containment_overflow_delta_pt": None,
    }
    if upper_box is None or lower_box is None:
        return result
    upper_width, lower_width = upper_box[2] - upper_box[0], lower_box[2] - lower_box[0]
    left_delta = abs(upper_box[0] - lower_box[0])
    right_delta = abs(upper_box[2] - lower_box[2])
    center_delta = abs((upper_box[0] + upper_box[2]) / 2.0 - (lower_box[0] + lower_box[2]) / 2.0)
    result.update({
        "gap_pt": round(lower_box[1] - upper_box[3], 3),
        "upper_width_pt": round(upper_width, 3), "lower_width_pt": round(lower_width, 3),
        "width_delta_pt": round(abs(upper_width - lower_width), 3),
        "left_edge_delta_pt": round(left_delta, 3), "right_edge_delta_pt": round(right_delta, 3),
        "edge_delta_pt": round(min(left_delta, right_delta), 3),
        "centerline_delta_pt": round(center_delta, 3),
    })
    container_id = _shared_container_id(upper, lower)
    result["shared_container_id"] = container_id
    if container_id is None or container_id not in by_id:
        return result
    container_box = measured_boxes.get(container_id, _render_bbox(by_id[container_id]))
    if container_box is None:
        return result
    result["shared_container_rendered"] = True
    deltas: dict[str, dict[str, float]] = {}
    overflow = 0.0
    for label, box in (("upper", upper_box), ("lower", lower_box)):
        values = {
            "left": round(box[0] - container_box[0], 3),
            "right": round(container_box[2] - box[2], 3),
            "top": round(box[1] - container_box[1], 3),
            "bottom": round(container_box[3] - box[3], 3),
        }
        deltas[label] = values
        overflow = max(overflow, max((-value for value in values.values()), default=0.0))
    result["containment_deltas_pt"] = deltas
    result["containment_overflow_delta_pt"] = round(overflow, 3)
    return result


def _vertical_association_result(
    relation: dict[str, Any], members: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
    measured_boxes: dict[str, tuple[float, float, float, float]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    relation_id = relation["id"]
    upper, lower = members
    association = relation["constraints"]["vertical_association"]
    mode = association["mode"]
    metrics = _vertical_association_metrics(upper, lower, by_id, measured_boxes)
    failures: list[str] = []
    pages = (metrics["upper_page"], metrics["lower_page"])
    if mode == "unspecified":
        result = {
            "id": relation_id, "scope": relation["scope"], "type": "vertical_association",
            "measurement_basis": "pdf_points", "ordered_member_ids": relation["ordered_member_ids"],
            "constraints": relation["constraints"], "association_mode": mode, "coverage_count": 2,
            **metrics, "status": "review", "reason_code": "vertical_association_unspecified",
            "failed_constraints": [], "auto_repair": False, "model_call": False,
        }
        issue = {
            "rule": "content_block_vertical_association_unspecified", "relation_id": relation_id,
            "relation_type": "vertical_association", "association_mode": mode,
            "reason_code": "vertical_association_unspecified", "scope": relation["scope"],
            "page": next((page for page in pages if isinstance(page, int)), 1),
            **metrics, "auto_repair": False, "model_call": False,
        }
        return result, issue
    if metrics["gap_pt"] is None or any(page is None for page in pages):
        failures.append("vertical_association_render_mapping_missing")
    elif len(set(pages)) != 1:
        failures.append("vertical_association_same_page_mismatch")
    elif mode == "attached":
        if abs(float(metrics["gap_pt"])) > float(association["max_gap_pt"]) + 1e-9:
            failures.append("vertical_association_attached_gap_exceeded")
        if float(metrics["width_delta_pt"]) > float(association["max_width_delta_pt"]) + 1e-9:
            failures.append("vertical_association_attached_width_mismatch")
        container_ok = (
            metrics["shared_container_rendered"] is True
            and float(metrics["containment_overflow_delta_pt"] or 0.0) <= float(association["containment_tolerance_pt"]) + 1e-9
        )
        edge_ok = float(metrics["edge_delta_pt"]) <= float(association["max_edge_delta_pt"]) + 1e-9
        if not (container_ok or edge_ok):
            failures.append("vertical_association_attached_container_or_edge_misaligned")
    elif float(metrics["gap_pt"]) + 1e-9 < float(association["min_gap_pt"]):
        failures.append("vertical_association_separate_gap_insufficient")
    reason_code = ",".join(failures) if failures else f"vertical_association_{mode}_satisfied"
    result = {
        "id": relation_id, "scope": relation["scope"], "type": "vertical_association",
        "measurement_basis": "pdf_points", "ordered_member_ids": relation["ordered_member_ids"],
        "constraints": relation["constraints"], "association_mode": mode, "coverage_count": 2,
        **metrics, "status": "fail" if failures else "pass", "reason_code": reason_code,
        "failed_constraints": failures, "auto_repair": False, "model_call": False,
    }
    issue = None
    if failures:
        issue = {
            "rule": "content_block_relation_mismatch", "relation_id": relation_id,
            "relation_type": "vertical_association", "association_mode": mode,
            "reason_code": reason_code, "page": next((page for page in pages if isinstance(page, int)), 1),
            **metrics, "auto_repair": False, "model_call": False,
        }
    return result, issue


def _geometry_issue(relation: dict[str, Any], status: str, reason: str, page: int,
                    metrics: dict[str, Any]) -> dict[str, Any] | None:
    if status == "pass":
        return None
    return {
        "rule": "content_block_geometry_relation_ambiguous" if status == "review" else "content_block_relation_mismatch",
        "relation_id": relation["id"], "relation_type": relation["type"],
        "relation_mode": relation["constraints"][relation["type"]].get("mode"),
        "reason_code": reason, "page": page, **metrics,
        "auto_repair": False, "model_call": False,
    }


def _anchor_gap_result(relation: dict[str, Any], members: list[dict[str, Any]],
                       by_id: dict[str, dict[str, Any]], measured_boxes: dict[str, tuple[float, float, float, float]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    declaration = relation["constraints"]["anchor_gap"]
    if declaration.get("measurement_model") == relative_anchor_gap.MEASUREMENT_MODEL:
        effective = relation.get("effective")
        failures: list[str] = []
        metrics: dict[str, Any] = {"measurement_model": relative_anchor_gap.MEASUREMENT_MODEL,
                                   "annotation_id": declaration.get("annotation_id"), "coverage_count": 2}
        if not isinstance(effective, dict) or effective.get("status") != "measured":
            failures.append("relative_anchor_gap_effective_evidence_unavailable")
        else:
            try:
                measured = relative_anchor_gap.measure_geometry(
                    tail_tip=effective.get("tail_tip_pdf_points"),
                    visible_avatar_bbox=effective.get("avatar_bbox_pdf_points"),
                    direction_vector=effective.get("direction_vector"),
                    target_gap_ratio=declaration.get("target_gap_ratio"), tolerance=declaration.get("tolerance"),
                    facing_expected=declaration.get("facing"), facing_actual=effective.get("facing"),
                    no_contact=declaration.get("no_contact"), contact_tolerance_pt=declaration.get("contact_tolerance_pt"),
                    intersection=effective.get("no_contact") is not True,
                )
                metrics.update({key: measured[key] for key in (
                    "tail_tip_pdf_points", "avatar_bbox_pdf_points", "avatar_width_pt", "direction_vector",
                    "gap_pt", "gap_ratio", "target_gap_ratio", "tolerance", "facing", "no_contact", "contact_distance_pt")})
                failures.extend(measured["failed_constraints"])
                if (effective.get("annotation_id") != declaration.get("annotation_id")
                        or effective.get("source_annotation_map_sha256") != declaration.get("source_annotation_map_sha256")):
                    failures.append("relative_anchor_gap_identity_drift")
                if effective.get("evidence_sha256") is None:
                    failures.append("relative_anchor_gap_evidence_hash_missing")
            except (TypeError, ValueError):
                failures.append("relative_anchor_gap_measurement_invalid")
        status = "fail" if failures else "pass"
        reason = ",".join(dict.fromkeys(failures)) if failures else "relative_anchor_gap_satisfied"
        result = {"id": relation["id"], "scope": relation["scope"], "type": "anchor_gap",
                  "measurement_basis": "pdf_points", "ordered_member_ids": relation["ordered_member_ids"],
                  "constraints": relation["constraints"], **metrics, "status": status, "reason_code": reason,
                  "failed_constraints": list(dict.fromkeys(failures)), "auto_repair": False, "model_call": False}
        return result, _geometry_issue(relation, status, reason, next((int(item.get("render", {}).get("page")) for item in members if isinstance(item.get("render", {}).get("page"), int)), 1), metrics)
    axis, mode = declaration["axis"], declaration["mode"]
    left, right = members
    boxes = [measured_boxes.get(item["id"], _render_bbox(item)) for item in members]
    pages = [item.get("render", {}).get("page") if isinstance(item.get("render"), dict) else None for item in members]
    failures: list[str] = []
    metrics: dict[str, Any] = {"axis": axis, "association_mode": mode, "coverage_count": 2}
    if any(box is None for box in boxes) or any(page is None for page in pages):
        failures.append("anchor_gap_render_mapping_missing")
    elif len(set(pages)) != 1:
        failures.append("anchor_gap_same_page_mismatch")
    else:
        first, second = boxes
        assert first is not None and second is not None
        if axis == "horizontal":
            first = (first[1], first[0], first[3], first[2]); second = (second[1], second[0], second[3], second[2])
        gap = second[1] - first[3]
        cross_overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
        widths = (first[2] - first[0], second[2] - second[0])
        overlap_ratio = cross_overlap / min(widths) if min(widths) > 0 else 0.0
        left_delta, right_delta = abs(first[0] - second[0]), abs(first[2] - second[2])
        center_delta = abs((first[0] + first[2]) / 2 - (second[0] + second[2]) / 2)
        shared_id = _shared_container_id(left, right)
        metrics.update({"gap_pt": round(gap, 3), "axis_overlap_ratio": round(overlap_ratio, 4),
                        "width_delta_pt": round(abs(widths[0] - widths[1]), 3),
                        "edge_delta_pt": round(max(left_delta, right_delta), 3),
                        "centerline_delta_pt": round(center_delta, 3), "shared_container_id": shared_id})
        if mode == "unspecified":
            result = {"id": relation["id"], "scope": relation["scope"], "type": "anchor_gap",
                      "measurement_basis": "pdf_points", "ordered_member_ids": relation["ordered_member_ids"],
                      "constraints": relation["constraints"], **metrics, "status": "review",
                      "reason_code": "anchor_gap_unspecified", "failed_constraints": [],
                      "auto_repair": False, "model_call": False}
            return result, _geometry_issue(relation, "review", "anchor_gap_unspecified", int(pages[0]), metrics)
        if declaration["shared_container"] == "required" and shared_id is None:
            failures.append("anchor_gap_shared_container_required")
        if declaration["shared_container"] == "forbidden" and shared_id is not None:
            failures.append("anchor_gap_shared_container_forbidden")
        if mode == "attached":
            if abs(gap) > float(declaration["max_gap_pt"]) + 1e-9: failures.append("anchor_gap_attached_gap_exceeded")
            if overlap_ratio + 1e-9 < float(declaration["min_axis_overlap_ratio"]): failures.append("anchor_gap_axis_overlap_insufficient")
            if metrics["width_delta_pt"] > float(declaration["max_width_delta_pt"]) + 1e-9: failures.append("anchor_gap_width_mismatch")
            if metrics["edge_delta_pt"] > float(declaration["max_edge_delta_pt"]) + 1e-9: failures.append("anchor_gap_edge_mismatch")
            if metrics["centerline_delta_pt"] > float(declaration["max_center_delta_pt"]) + 1e-9: failures.append("anchor_gap_center_mismatch")
        elif gap + 1e-9 < float(declaration["min_gap_pt"]):
            failures.append("anchor_gap_separate_gap_insufficient")
    status = "fail" if failures else "pass"
    reason = ",".join(failures) if failures else f"anchor_gap_{mode}_satisfied"
    result = {"id": relation["id"], "scope": relation["scope"], "type": "anchor_gap",
              "measurement_basis": "pdf_points", "ordered_member_ids": relation["ordered_member_ids"],
              "constraints": relation["constraints"], **metrics, "status": status,
              "reason_code": reason, "failed_constraints": failures,
              "auto_repair": False, "model_call": False}
    return result, _geometry_issue(relation, status, reason, next((int(page) for page in pages if isinstance(page, int)), 1), metrics)


def _content_inset_result(relation: dict[str, Any], members: list[dict[str, Any]],
                          measured_boxes: dict[str, tuple[float, float, float, float]],
                          *, require_visible_ink: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
    declaration = relation["constraints"]["content_inset"]
    boxes = [_render_bbox(members[0])] + [
        measured_boxes.get(item["id"]) if require_visible_ink else measured_boxes.get(item["id"], _render_bbox(item))
        for item in members[1:]
    ]
    pages = [item.get("render", {}).get("page") if isinstance(item.get("render"), dict) else None for item in members]
    failures: list[str] = []; insets: dict[str, float] = {}; deltas: dict[str, float] = {}
    if any(box is None for box in boxes) or any(page is None for page in pages):
        failures.append("content_inset_visible_evidence_missing" if require_visible_ink else "content_inset_render_mapping_missing")
    elif len(set(pages)) != 1:
        failures.append("content_inset_same_page_mismatch")
    else:
        outer = boxes[0]; inner_boxes = boxes[1:]
        assert outer is not None and all(box is not None for box in inner_boxes)
        inner = (min(box[0] for box in inner_boxes if box is not None), min(box[1] for box in inner_boxes if box is not None),
                 max(box[2] for box in inner_boxes if box is not None), max(box[3] for box in inner_boxes if box is not None))
        insets = {"left": round(inner[0] - outer[0], 3), "right": round(outer[2] - inner[2], 3),
                  "top": round(inner[1] - outer[1], 3), "bottom": round(outer[3] - inner[3], 3)}
        deltas = {"horizontal": round(abs(insets["left"] - insets["right"]), 3),
                  "vertical": round(abs(insets["top"] - insets["bottom"]), 3)}
        tolerance = float(declaration["containment_tolerance_pt"])
        if declaration["require_contained"] and any(value < -tolerance - 1e-9 for value in insets.values()):
            failures.append("content_inset_containment")
        for edge, minimum in declaration["minimum_pt"].items():
            if insets[edge] + 1e-9 < float(minimum): failures.append(f"content_inset_minimum_{edge}")
        axes = declaration["balance_axes"]
        if axes in {"horizontal", "both"} and deltas["horizontal"] > float(declaration["max_balance_delta_pt"]) + 1e-9:
            failures.append("content_inset_balance_horizontal")
        if axes in {"vertical", "both"} and deltas["vertical"] > float(declaration["max_balance_delta_pt"]) + 1e-9:
            failures.append("content_inset_balance_vertical")
    metrics = {"outer_id": relation["ordered_member_ids"][0], "inner_ids": relation["ordered_member_ids"][1:],
               "coverage_count": len(members), "insets_pt": insets, "deltas_pt": deltas,
               "primitive_mapping_count": len(relation.get("effective", {}).get("primitive_mappings", []))}
    status = "fail" if failures else "pass"; reason = ",".join(dict.fromkeys(failures)) if failures else "content_inset_satisfied"
    result = {"id": relation["id"], "scope": relation["scope"], "type": "content_inset",
              "measurement_basis": "pdf_points", "ordered_member_ids": relation["ordered_member_ids"],
              "constraints": relation["constraints"], **metrics, "status": status,
              "reason_code": reason, "failed_constraints": list(dict.fromkeys(failures)),
              "auto_repair": False, "model_call": False}
    return result, _geometry_issue(relation, status, reason, next((int(page) for page in pages if isinstance(page, int)), 1), metrics)


def _evidence_geometry_result(relation: dict[str, Any], members: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    relation_type = relation["type"]; declaration = relation["constraints"][relation_type]
    applied = relation.get("applied"); effective = relation.get("effective")
    pages = [item.get("render", {}).get("page") if isinstance(item.get("render"), dict) else None for item in members]
    metrics = {key: effective[key] for key in ("measurement_basis", "endpoint_distance_pt", "boundary_intersection_distance_pt",
               "visible_length_inside_surface_pt", "visible_area_inside_surface_pt2", "overlap_area_pt2",
               "front_visible_ratio", "occluded_ratio", "evidence_sha256") if isinstance(effective, dict) and key in effective}
    failures: list[str] = []
    if not isinstance(applied, dict) or not isinstance(effective, dict) or effective.get("status") == "ambiguous":
        reason = effective.get("ambiguity_reason", f"{relation_type}_effective_evidence_unavailable") if isinstance(effective, dict) else f"{relation_type}_effective_evidence_unavailable"
        result = {"id": relation["id"], "scope": relation["scope"], "type": relation_type,
                  "measurement_basis": metrics.get("measurement_basis", "unavailable"), "ordered_member_ids": relation["ordered_member_ids"],
                  "constraints": relation["constraints"], **metrics, "status": "review", "reason_code": reason,
                  "failed_constraints": [], "auto_repair": False, "model_call": False}
        return result, _geometry_issue(relation, "review", reason, next((int(page) for page in pages if isinstance(page, int)), 1), metrics)
    if relation_type == "connector_route":
        if declaration["require_source_order_proof"] and applied["source_order_pass"] is not True:
            failures.append("connector_route_source_order_contradiction")
        for metric, threshold, code in (("visible_length_inside_surface_pt", "max_visible_length_inside_surface_pt", "connector_route_visible_length_exceeded"),
                                        ("visible_area_inside_surface_pt2", "max_visible_area_inside_surface_pt2", "connector_route_visible_area_exceeded")):
            if metric not in effective: failures.append(f"connector_route_{metric}_missing")
            elif float(effective[metric]) > float(declaration[threshold]) + 1e-9: failures.append(code)
        if declaration["mode"] == "terminate_at_boundary":
            for metric, threshold, code in (("endpoint_distance_pt", "endpoint_tolerance_pt", "connector_route_endpoint_tolerance"),
                                             ("boundary_intersection_distance_pt", "boundary_intersection_tolerance_pt", "connector_route_boundary_intersection")):
                if metric not in effective: failures.append(f"connector_route_{metric}_missing")
                elif float(effective[metric]) > float(declaration[threshold]) + 1e-9: failures.append(code)
    else:
        overlap = effective.get("overlap_area_pt2")
        if overlap is None: failures.append("layer_relation_overlap_missing")
        else:
            if float(overlap) + 1e-9 < float(declaration["min_overlap_area_pt2"]): failures.append("layer_relation_overlap_below_minimum")
            if float(overlap) > float(declaration["max_overlap_area_pt2"]) + 1e-9: failures.append("layer_relation_overlap_exceeded")
        if declaration["mode"] == "front_of":
            if declaration["source_order_required"] and applied["source_order_pass"] is not True: failures.append("layer_relation_source_order_contradiction")
            if "front_visible_ratio" not in effective or "occluded_ratio" not in effective: failures.append("layer_relation_visibility_missing")
            else:
                if float(effective["front_visible_ratio"]) + 1e-9 < float(declaration["min_front_visible_ratio"]): failures.append("layer_relation_front_visibility_insufficient")
                if float(effective["occluded_ratio"]) > float(declaration["max_occluded_ratio"]) + 1e-9: failures.append("layer_relation_occlusion_exceeded")
    status = "fail" if failures else "pass"; reason = ",".join(dict.fromkeys(failures)) if failures else f"{relation_type}_satisfied"
    result = {"id": relation["id"], "scope": relation["scope"], "type": relation_type,
              "measurement_basis": effective["measurement_basis"], "ordered_member_ids": relation["ordered_member_ids"],
              "constraints": relation["constraints"], **metrics, "status": status, "reason_code": reason,
              "failed_constraints": list(dict.fromkeys(failures)), "auto_repair": False, "model_call": False}
    return result, _geometry_issue(relation, status, reason, next((int(page) for page in pages if isinstance(page, int)), 1), metrics)


def _declared_relation_results(
    manifest: dict[str, Any], policy: dict[str, Any], by_id: dict[str, dict[str, Any]],
    measured_boxes: dict[str, tuple[float, float, float, float]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    relation_policy = policy.get("relation_policy")
    if not isinstance(relation_policy, dict) or relation_policy.get("status") != "declared":
        return [], []
    declared = {item.get("id"): item for item in manifest.get("relations", []) if isinstance(item, dict)}
    issues: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    measured_boxes = measured_boxes or {}
    require_visible_ink = manifest.get("schema_version") == STRICT_SCHEMA_VERSION
    for relation_id in relation_policy.get("required_relation_ids", []):
        relation = declared.get(relation_id)
        if not isinstance(relation, dict):
            issues.append({"rule": "content_block_relation_mismatch", "relation_id": relation_id,
                           "reason_code": "declared_relation_coverage_missing", "page": 1})
            continue
        member_ids = relation["ordered_member_ids"]
        members = [by_id[identifier] for identifier in member_ids]
        constraints = relation["constraints"]
        relation_type = relation.get("type", "ordered")
        failures: list[str] = []
        boxes = [measured_boxes.get(member["id"], _render_bbox(member)) for member in members]
        pages = [member.get("render", {}).get("page") if isinstance(member.get("render"), dict) else None
                 for member in members]
        if relation_type == "vertical_association":
            result, issue = _vertical_association_result(relation, members, by_id, measured_boxes)
            results.append(result)
            if issue is not None:
                issues.append(issue)
            continue
        if relation_type == "anchor_gap":
            result, issue = _anchor_gap_result(relation, members, by_id, measured_boxes)
            results.append(result)
            if issue is not None:
                issues.append(issue)
            continue
        if relation_type == "content_inset":
            result, issue = _content_inset_result(
                relation, members, measured_boxes, require_visible_ink=require_visible_ink,
            )
            results.append(result)
            if issue is not None:
                issues.append(issue)
            continue
        if relation_type in {"connector_route", "layer_relation"}:
            result, issue = _evidence_geometry_result(relation, members)
            results.append(result)
            if issue is not None:
                issues.append(issue)
            continue
        if relation_type == "nested_inset":
            balance = constraints["inset_balance"]
            axes = balance["axes"]
            max_delta_pt = float(balance["max_delta_pt"])
            insets: dict[str, float] = {}
            deltas: dict[str, float] = {}
            if require_visible_ink:
                boxes = [_render_bbox(members[0]), measured_boxes.get(members[1]["id"])]
            if any(box is None for box in boxes) or any(page is None for page in pages):
                failures.append("visible_evidence_missing" if require_visible_ink else "render_mapping_missing")
            elif len(set(pages)) != 1:
                failures.append("same_page")
            else:
                outer_box, inner_box = boxes
                assert outer_box is not None and inner_box is not None
                insets = {
                    "left": round(inner_box[0] - outer_box[0], 3),
                    "right": round(outer_box[2] - inner_box[2], 3),
                    "top": round(inner_box[1] - outer_box[1], 3),
                    "bottom": round(outer_box[3] - inner_box[3], 3),
                }
                deltas = {
                    "horizontal": round(abs(insets["left"] - insets["right"]), 3),
                    "vertical": round(abs(insets["top"] - insets["bottom"]), 3),
                }
                if balance["require_contained"] and any(value < 0 for value in insets.values()):
                    failures.append("require_contained")
                if axes in {"horizontal", "both"} and deltas["horizontal"] > max_delta_pt + 1e-9:
                    failures.append("inset_balance_horizontal")
                if axes in {"vertical", "both"} and deltas["vertical"] > max_delta_pt + 1e-9:
                    failures.append("inset_balance_vertical")
            failures = list(dict.fromkeys(failures))
            selected_deltas = [deltas[name] for name in ("horizontal", "vertical") if name in deltas and axes in {name, "both"}]
            reason_code = ",".join(failures) if failures else "nested_inset_balanced"
            result = {
                "id": relation_id, "scope": relation["scope"], "type": relation_type,
                "measurement_basis": "pdf_points", "ordered_member_ids": member_ids,
                "outer_id": member_ids[0], "inner_id": member_ids[1], "constraints": constraints,
                "coverage_count": len(member_ids), "insets_pt": insets, "deltas_pt": deltas,
                "delta_pt": round(max(selected_deltas), 3) if selected_deltas else None,
                "reason_code": reason_code, "status": "fail" if failures else "pass",
                "failed_constraints": failures,
            }
            results.append(result)
            if failures:
                issues.append({
                    "rule": "content_block_relation_mismatch", "relation_id": relation_id,
                    "relation_type": relation_type, "reason_code": reason_code,
                    "page": next((page for page in pages if page), 1), "insets_pt": insets,
                    "deltas_pt": deltas, "delta_pt": result["delta_pt"],
                })
            continue
        if "min_gap_pt" not in constraints:
            issues.append({"rule": "content_block_relation_mismatch", "relation_id": relation_id,
                           "reason_code": "min_gap_pt_undeclared", "page": 1})
            continue
        requested_gap = float(constraints["min_gap_pt"])
        effective_gaps = [
            max(requested_gap, _effective_line_height(upper), _effective_line_height(lower))
            for upper, lower in zip(members, members[1:])
        ]
        measured_gaps: list[float] = []
        anchors: list[dict[str, Any]] = []
        if any(box is None for box in boxes) or any(page is None for page in pages):
            failures.append("render_mapping_missing")
        else:
            anchors = [
                {"block_id": member["id"], "page": page, "top_y_pt": round(box[1], 3), "bottom_y_pt": round(box[3], 3)}
                for member, page, box in zip(members, pages, boxes) if box is not None
            ]
            if constraints.get("same_page") is True and len(set(pages)) != 1:
                failures.append("same_page")
            # Spacing is a same-page measurement. A relation that crosses a
            # page boundary is owned by the page-break constraint, not by a
            # fabricated PDF-point gap between unrelated coordinate systems.
            if len(set(pages)) == 1:
                for pair_index, (upper_box, lower_box) in enumerate(zip(boxes, boxes[1:])):
                    assert upper_box is not None and lower_box is not None
                    gap = lower_box[1] - upper_box[3]
                    measured_gaps.append(round(gap, 3))
                    if constraints.get("no_overlap") is True and gap < 0:
                        failures.append("no_overlap")
                    if gap < effective_gaps[pair_index]:
                        failures.append("min_gap_pt")
        failures = list(dict.fromkeys(failures))
        result = {
            "id": relation_id, "scope": relation["scope"], "type": relation.get("type", "ordered"),
            "measurement_basis": "pdf_points", "ordered_member_ids": member_ids,
            "constraints": constraints, "coverage_count": len(member_ids),
            "requested_gap_pt": requested_gap,
            "effective_gaps_pt": [round(value, 3) for value in effective_gaps],
            "measured_gaps_pt": measured_gaps, "anchors": anchors,
            "status": "fail" if failures else "pass", "failed_constraints": failures,
        }
        results.append(result)
        if failures:
            issues.append({"rule": "content_block_relation_mismatch", "relation_id": relation_id,
                           "reason_code": ",".join(failures), "page": next((page for page in pages if page), 1)})
    return issues, results


def evaluate_manifest(manifest: dict[str, Any], policy: dict[str, Any], artifact: Path) -> dict[str, Any]:
    by_id = {item["id"]: item for item in manifest.get("blocks", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    issues: list[dict[str, Any]] = []
    page_sizes = _page_sizes(artifact)
    coverage_evidence, coverage_issues, measured_boxes = _visible_coverage(manifest, policy, artifact)
    issues.extend(coverage_issues)
    visible_ink_summary: list[dict[str, Any]] = []
    if manifest.get("schema_version") == STRICT_SCHEMA_VERSION:
        for record in manifest.get("visible_ink_evidence", []):
            if not isinstance(record, dict) or not isinstance(record.get("member_id"), str):
                continue
            member_id = record["member_id"]
            status = record.get("status")
            box = _bbox(record.get("visible_ink_bbox_pdf_points")) if status == "measured" else None
            visible_ink_summary.append({
                "member_id": member_id, "page": record.get("page"), "occurrence": record.get("occurrence"),
                "source_bbox_pdf_points": record.get("source_bbox_pdf_points"),
                "text_layer_bbox_pdf_points": record.get("text_layer_bbox_pdf_points"),
                "visible_ink_bbox_pdf_points": record.get("visible_ink_bbox_pdf_points"),
                "measurement_basis": "pdf_render_mask", "status": status,
                "confidence": record.get("confidence"), "crop_sha256": record.get("crop_sha256"),
                "render_sha256": record.get("render", {}).get("sha256") if isinstance(record.get("render"), dict) else None,
            })
            if box is not None:
                measured_boxes[member_id] = box
            else:
                issues.append({"rule": "content_block_visible_coverage_missing", "page": record.get("page", 1),
                               "block_id": member_id, "reason_code": record.get("ambiguity_reason", "visible_ink_not_measured")})
    ambiguous_ids: list[str] = []
    for identifier, block in by_id.items():
        box = _render_bbox(block)
        render = block.get("render") if isinstance(block.get("render"), dict) else {}
        mapping = render.get("mapping")
        # A group is a source-only container and may not have its own rendered
        # bbox. Its children carry the mapping; only geometry participants need
        # a deterministic render anchor or a manual-review fallback.
        if block.get("kind") != "group" and (box is None or mapping in {None, "unknown", "ambiguous", "pdf_inference", "inference"}):
            ambiguous_ids.append(identifier)
        if box is not None:
            page = render.get("page", 1)
            size = page_sizes.get(page)
            if size and (box[0] < -0.5 or box[1] < -0.5 or box[2] > size[0] + 0.5 or box[3] > size[1] + 0.5):
                issues.append({"rule": "content_block_clipping", "block_id": identifier, "role": block.get("role"), "page": page, "bbox_pdf_points": list(box)})
        if block.get("overflow") is True:
            issues.append({"rule": "content_block_overflow", "block_id": identifier, "role": block.get("role"), "page": render.get("page", 1)})
        preservation = block.get("preservation") or block.get("preservation_diff")
        if isinstance(preservation, dict) and any(value is not False for value in preservation.values()):
            issues.append({"rule": "content_block_preservation_mismatch", "block_id": identifier, "page": render.get("page", 1)})
        if block.get("reflow") in {"mismatch", "changed", "overflow"} or block.get("reflow_mismatch") is True:
            issues.append({"rule": "content_block_reflow_mismatch", "block_id": identifier, "page": render.get("page", 1)})
        alignment = block.get("vertical_alignment")
        if isinstance(alignment, dict):
            container = _bbox(alignment.get("container_bbox_pdf_points"))
            content_box = _bbox(alignment.get("content_box_pdf_points"))
            occupied = _bbox(alignment.get("occupied_bbox_pdf_points"))
            tolerance = float(alignment.get("center_tolerance_pt", 0.0)) if _numeric(alignment.get("center_tolerance_pt")) else 0.0
            if container and content_box and occupied:
                content_center = (content_box[1] + content_box[3]) / 2.0
                bbox_center = (occupied[1] + occupied[3]) / 2.0
                delta = bbox_center - content_center
                if (abs(delta) > tolerance + 1e-9 or occupied[1] < content_box[1] - tolerance
                        or occupied[3] > content_box[3] + tolerance):
                    issues.append({"rule": "content_block_vertical_alignment", "block_id": identifier,
                                   "page": render.get("page", 1), "vertical_align": "center",
                                   "content_center_y_pt": round(content_center, 3),
                                   "bbox_center_y_pt": round(bbox_center, 3),
                                   "center_delta_pt": round(delta, 3), "center_tolerance_pt": tolerance})
    if ambiguous_ids:
        issues.append({"rule": "content_block_mapping_ambiguous", "scope": "mapping", "block_ids": sorted(ambiguous_ids), "page": 1,
                       "reason_code": "source_anchor_or_render_bbox_missing", "mapping": "manual_review"})

    parent_members: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for block in by_id.values():
        parent_members[block.get("parent_id")].append(block)
    relations: dict[str, dict[str, Any]] = {}
    relation_policy = policy.get("relation_policy")
    declared_pair_ids: set[tuple[str, str]] = set()
    if isinstance(relation_policy, dict) and relation_policy.get("status") == "declared":
        required = set(relation_policy.get("required_relation_ids", []))
        for relation in manifest.get("relations", []):
            if isinstance(relation, dict) and relation.get("id") in required:
                members = relation.get("ordered_member_ids", [])
                declared_pair_ids.update(zip(members, members[1:]))
    for parent_id, siblings in parent_members.items():
        if parent_id is not None and parent_id not in by_id:
            continue
        peers = [item for item in siblings if item.get("role") not in set(policy.get("exclude_roles", []))]
        for index, left in enumerate(peers):
            for right in peers[index + 1:]:
                if (left["id"], right["id"]) in declared_pair_ids or (right["id"], left["id"]) in declared_pair_ids:
                    continue
                if _ancestor(by_id, left["id"], right["id"]):
                    continue
                left_owner, right_owner = _surface_owner(by_id, left["id"]), _surface_owner(by_id, right["id"])
                if left_owner is not None and left_owner == right_owner:
                    continue
                # A non-root content group owns the spacing between its sibling
                # members. Explicit owner_id remains authoritative for producers
                # that flatten members, while a shared non-null parent captures the
                # source-group form used by DOCX/PDF content-block manifests.
                scope = "intra_block" if (
                    (
                        isinstance(left.get("owner_id"), str)
                        and left.get("owner_id") == right.get("owner_id")
                    )
                    or (
                        left.get("parent_id") is not None
                        and left.get("parent_id") == right.get("parent_id")
                    )
                ) else "inter_block"
                if _spacing_mode(policy) != "legacy" and scope not in _spacing_target_scopes(policy):
                    continue
                issue = _pair_issue(policy, scope, left, right, artifact)
                if issue is not None:
                    issues.append(issue)
                    relation_key = issue.get("relation_key", _pair_key(scope, left, right))
                    aggregate = relations.setdefault(relation_key, {"id": relation_key, "scope": scope, "type": "gap",
                        "upper_role": issue.get("upper_role"), "lower_role": issue.get("lower_role"), "measurement_basis": "pdf_points",
                        "no_contact": True, "no_overlap": True, "coverage_count": 0, "candidate_count": 0,
                        "min_gap_pt": issue.get("minimum_gap_pt"), "status": "candidate"})
                    aggregate["coverage_count"] += 1
                    if issue.get("rule") in REVIEW_RULES:
                        aggregate["candidate_count"] += 1
                elif (_render_bbox(left) is not None and _render_bbox(right) is not None
                      and _x_overlap_ratio(_render_bbox(left), _render_bbox(right))
                      >= float(policy.get("min_x_overlap_ratio", 0.5))):
                    upper, lower = (left, right) if _render_bbox(left)[1] <= _render_bbox(right)[1] else (right, left)
                    relation_key = _pair_key(scope, upper, lower)
                    relations.setdefault(relation_key, {"id": relation_key, "scope": scope, "type": "gap",
                        "upper_role": upper.get("role"), "lower_role": lower.get("role"), "measurement_basis": "pdf_points",
                        "no_contact": True, "no_overlap": True, "coverage_count": 1, "candidate_count": 0,
                        "min_gap_pt": _minimum_gap(policy, scope, str(upper.get("role")), str(lower.get("role"))), "status": "pass"})
    declared_issues, declared_relations = _declared_relation_results(manifest, policy, by_id, measured_boxes)
    issues.extend(declared_issues)
    for relation in declared_relations:
        relations[relation["id"]] = relation
    review = _coalesce(issues, artifact, max(1, int(policy.get("max_review_groups", 20))))
    hard = [item for item in issues if item.get("rule") in HARD_RULES]
    spacing_mode = _spacing_mode(policy)
    target_scopes = _spacing_target_scopes(policy)
    spacing_candidates = [
        item for item in review
        if item.get("rule") in CONTENT_BLOCK_SPACING_REVIEW_RULES
        and item.get("scope") in target_scopes
    ]
    spacing_defects = [
        item for item in issues
        if item.get("rule") in CONTENT_BLOCK_SPACING_DEFECT_RULES
        and item.get("scope") in target_scopes
    ]
    non_spacing_review = [item for item in review if not _scoped_spacing_issue(item, policy)]
    spacing_evidence = {
        "enabled": spacing_mode != "legacy",
        "mode": None if spacing_mode == "legacy" else spacing_mode,
        "measurement_basis": "pdf_points",
        "target_scopes": sorted(target_scopes) if spacing_mode != "legacy" else [],
        "selection": "manual_accepted_candidate" if spacing_mode != "legacy" else None,
        "candidate_count": len(spacing_candidates),
        "defect_count": len(spacing_defects),
        "candidates": spacing_candidates,
        "defects": spacing_defects,
        "review_receipt": "required" if (
            non_spacing_review or (spacing_mode == "enforced" and (spacing_candidates or spacing_defects))
        ) else "not_created",
        "model_call": False,
    }
    return {
        "schema_version": manifest.get("schema_version", SCHEMA_VERSION),
        "artifact_sha256": sha256(artifact),
        **({"producer_run_id": manifest["producer_run_id"], "artifact_set_id": manifest["artifact_set_id"]}
           if "producer_run_id" in manifest and "artifact_set_id" in manifest else {}),
        "measurement_basis": "pdf_points",
        "issues": issues,
        "relations": list(relations.values()),
        "review_candidates": review,
        "hard_issue_count": len(hard),
        "review_candidate_count": len(review),
        "candidate_count": len(review),
        "model_call": False,
        "review_receipt": spacing_evidence["review_receipt"],
        "source_mapping": "manual_review" if ambiguous_ids else "deterministic",
        "spacing_evidence": spacing_evidence,
        **({"visible_element_coverage": coverage_evidence} if coverage_evidence is not None else {}),
        **({"visible_ink_evidence": visible_ink_summary} if visible_ink_summary else {}),
    }


def scan_content_blocks(artifact: Path, policy: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    if not enabled(policy):
        return None, [], []
    manifest, errors = load_manifest(policy, artifact)
    if errors or manifest is None:
        issue = {"rule": "content_block_mapping_ambiguous", "scope": "mapping", "page": 1,
                 "reason_code": ";".join(errors) if errors else "manifest_missing", "mapping": "manual_review"}
        return None, [issue], []
    result = evaluate_manifest(manifest, policy, artifact)
    mode = _spacing_mode(policy)
    if mode == "legacy":
        emitted_issues = [item for item in result.get("issues", []) if item.get("rule") in HARD_RULES]
        emitted_issues.extend(result.get("review_candidates", []))
    else:
        # Shadow retains measured spacing evidence but does not change the
        # existing AQ status. Enforced uses the same review/hard-fail buckets.
        emitted_issues = [
            item for item in result.get("issues", [])
            if item.get("rule") in HARD_RULES and not _scoped_spacing_issue(item, policy)
        ]
        emitted_issues.extend(
            item for item in result.get("review_candidates", [])
            if not _scoped_spacing_issue(item, policy)
        )
        if mode == "enforced":
            emitted_issues.extend(
                item for item in result.get("issues", [])
                if item.get("rule") in CONTENT_BLOCK_SPACING_DEFECT_RULES
                and item.get("scope") in _spacing_target_scopes(policy)
            )
            emitted_issues.extend(
                item for item in result.get("review_candidates", [])
                if item.get("rule") in CONTENT_BLOCK_SPACING_REVIEW_RULES
                and item.get("scope") in _spacing_target_scopes(policy)
            )
    return result, emitted_issues, result.get("relations", [])
