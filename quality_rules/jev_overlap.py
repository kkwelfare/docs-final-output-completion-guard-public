"""Structure-only Jev overlap classification for the docs quality candidate.

The adapter deliberately keeps the existing deterministic scan authoritative.  It
only classifies bounded review candidates that already have structural geometry;
unsupported candidates, provider failures, and malformed responses remain
unresolved and therefore cannot turn a review into a pass.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCHEMA_VERSION = "docs-jev-overlap-structure-1"
REQUEST_SCHEMA_VERSION = "docs-jev-overlap-request-1"
RESULT_SCHEMA_VERSION = "docs-jev-overlap-result-1"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_CANDIDATES = 20
MAX_REQUEST_BYTES = 16_000

PRIMARY_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
PRIMARY_API_KEY_ENV = "TYPESAFE_API_KEY"
PRIMARY_MODEL = "jev-latest"
FALLBACK_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
FALLBACK_API_KEY_ENV = "OPENROUTER_API_KEY"
FALLBACK_MODEL = "typesafe/jev-1.13"
FALLBACK_HTTP_STATUSES = frozenset({401, 403, 408, 429})

CLASSIFICATIONS = (
    "intended",
    "acceptable",
    "repair_required",
    "insufficient_information",
)
BRANCHES = ("maintain", "adjust", "request_information", "unresolved")
SUPPORTED_CANDIDATE_RULES = frozenset({
    "unclassified_source_overlap_or_gutter",
    "deterministic_coverage_gap_candidate",
    "line_spacing_candidate",
    "content_block_spacing_candidate",
    "content_block_intra_spacing_candidate",
})


class JevOverlapError(RuntimeError):
    """A bounded provider or response error with a stable, non-secret reason."""

    def __init__(
        self,
        reason: str,
        status: int | None = None,
        *,
        provider_attempts: list[str] | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        # Keep only route identifiers in the error.  The evaluator copies this
        # into per-request evidence so a failed fallback is auditable without
        # retaining exception text, request bodies, or credentials.
        self.provider_attempts = list(provider_attempts or [])


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_impact(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(4):
        raise JevOverlapError("malformed_response")
    return int(value)


def _box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    values = [_finite_number(item) for item in value]
    if any(item is None for item in values):
        return None
    result = tuple(float(item) for item in values if item is not None)
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result  # type: ignore[return-value]


def _overlap_area(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return round(width * height, 3)


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _shape_order(shape_id: Any) -> int | None:
    if not isinstance(shape_id, str) or "-s" not in shape_id:
        return None
    try:
        return int(shape_id.rsplit("-s", 1)[1])
    except (TypeError, ValueError):
        return None


def validate_config(config: Any) -> list[str]:
    """Validate the opt-in contract without contacting a provider."""
    if config is None:
        return []
    prefix = "AQ-LAYOUT-01: jev_overlap"
    if not isinstance(config, dict):
        return [f"{prefix} must be an object"]
    enabled = config.get("enabled")
    if not isinstance(enabled, bool):
        return [f"{prefix}.enabled must be boolean"]
    if "model" in config and (not isinstance(config["model"], str) or not config["model"].strip()):
        return [f"{prefix}.model must be a non-empty string"]
    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120:
        return [f"{prefix}.timeout_seconds must be in (0,120]"]
    maximum = config.get("max_candidates", DEFAULT_MAX_CANDIDATES)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 64:
        return [f"{prefix}.max_candidates must be an integer in [1,64]"]
    if config.get("provider_mode", "typesafe_then_openrouter") != "typesafe_then_openrouter":
        return [f"{prefix}.provider_mode must be typesafe_then_openrouter"]
    if "allow_live_provider" in config and not isinstance(config["allow_live_provider"], bool):
        return [f"{prefix}.allow_live_provider must be boolean"]
    return []


def _member_index(composition_data: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    pages = composition_data.get("pages") if isinstance(composition_data, Mapping) else None
    if not isinstance(pages, list):
        return index
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        regions = page.get("regions")
        if not isinstance(regions, list):
            continue
        for region in regions:
            if isinstance(region, Mapping) and isinstance(region.get("shape_id"), str):
                index[region["shape_id"]] = dict(region)
    return index


def _project_overlap_candidate(
    candidate: Mapping[str, Any], composition_data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project one source overlap candidate without retaining body text."""
    rule = candidate.get("rule")
    if rule not in SUPPORTED_CANDIDATE_RULES:
        return None, "unsupported_candidate_type"
    overlap_extent = _finite_number(candidate.get("overlap_pt"))
    if overlap_extent is None or overlap_extent <= 0:
        return None, "non_overlap_candidate"
    shape_ids = candidate.get("shape_ids")
    if not isinstance(shape_ids, list) or len(shape_ids) != 2 or any(not isinstance(item, str) or not item for item in shape_ids):
        return None, "member_geometry_missing"
    by_id = _member_index(composition_data)
    members: list[dict[str, Any]] = []
    missing: list[str] = []
    for shape_id in shape_ids:
        source = by_id.get(shape_id)
        box = _box(source.get("bbox_pt") if isinstance(source, Mapping) else None)
        if source is None or box is None:
            missing.append(f"member_geometry:{shape_id}")
            continue
        role = source.get("semantic_role") or source.get("role")
        members.append({
            "id": shape_id,
            "role": role if isinstance(role, str) and role else "unknown",
            "bbox_pdf_points": [round(item, 3) for item in box],
            "dimensions_pt": [round(box[2] - box[0], 3), round(box[3] - box[1], 3)],
            "source_order": _shape_order(shape_id),
            "known_occlusion": source.get("known_occlusion") if isinstance(source.get("known_occlusion"), bool) else None,
        })
    if len(members) != 2:
        return None, ";".join(missing) or "member_geometry_missing"
    boxes = [_box(member["bbox_pdf_points"]) for member in members]
    if any(box is None for box in boxes):
        return None, "member_geometry_missing"
    left, right = boxes  # type: ignore[misc]
    overlap_area = _overlap_area(left, right)
    smaller_area = min(_area(left), _area(right))
    overlap_ratio = round(overlap_area / smaller_area, 6) if smaller_area > 0 else None
    page = candidate.get("page")
    page_size = None
    pages = composition_data.get("pages") if isinstance(composition_data, Mapping) else None
    if isinstance(pages, list):
        for page_data in pages:
            if isinstance(page_data, Mapping) and page_data.get("page") == page:
                candidate_size = page_data.get("page_size_pt")
                if isinstance(candidate_size, list) and len(candidate_size) == 2 and all(_finite_number(item) is not None for item in candidate_size):
                    page_size = [round(float(candidate_size[0]), 3), round(float(candidate_size[1]), 3)]
                break
    source_orders = [member.get("source_order") for member in members]
    missing_information = []
    if any(order is None for order in source_orders):
        missing_information.append("source_stacking_order")
    missing_information.append("rendered_stacking_order")
    if all(member.get("known_occlusion") is None for member in members):
        missing_information.append("known_occlusion")
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate.get("id"),
        "candidate_type": "source_overlap",
        "page": page,
        "members": members,
        "overlap": {
            "extent_pt": round(overlap_extent, 3),
            "area_pt2": overlap_area,
            "ratio_of_smaller_member": overlap_ratio,
            "measurement_basis": "pdf_points",
        },
        "stacking_order": {
            "source_order": source_orders,
            "rendered_order": None,
            "basis": "source_shape_index",
        },
        "known_occlusion": None if all(member.get("known_occlusion") is None for member in members) else [member.get("known_occlusion") for member in members],
        "local_context": {"page_size_pt": page_size, "relation": "overlap", "source_group": candidate.get("semantic_group")},
        "candidate_options": list(CLASSIFICATIONS),
        "branch_options": ["maintain", "adjust", "request_information"],
        "missing_information": missing_information,
    }, None


def _projection_bbox(candidate: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    for key in ("bbox_pdf_points", "candidate_bbox_pdf_points", "bbox"):
        box = _box(candidate.get(key))
        if box is not None:
            return box
    return None


def _box_iou(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    intersection = _overlap_area(left, right)
    union = _area(left) + _area(right) - intersection
    return round(intersection / union, 6) if union > 0 else 0.0


def _source_regions(composition_data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(composition_data, Mapping):
        return []
    values: list[dict[str, Any]] = []
    direct = composition_data.get("source_regions")
    if isinstance(direct, list):
        values.extend(item for item in direct if isinstance(item, Mapping))
    geometry = composition_data.get("source_geometry")
    pages = geometry.get("pages") if isinstance(geometry, Mapping) else None
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, Mapping) and isinstance(page.get("regions"), list):
                values.extend(item for item in page["regions"] if isinstance(item, Mapping))
    if not values:
        pages = composition_data.get("pages")
        if isinstance(pages, list):
            for page in pages:
                if isinstance(page, Mapping) and isinstance(page.get("regions"), list):
                    values.extend(item for item in page["regions"] if isinstance(item, Mapping))
    unique: dict[str, dict[str, Any]] = {}
    for item in values:
        shape_id = item.get("shape_id")
        if isinstance(shape_id, str) and shape_id:
            unique.setdefault(shape_id, dict(item))
    return list(unique.values())


def _pdf_geometry_lines(composition_data: Mapping[str, Any] | None, page: Any) -> list[dict[str, Any]]:
    if not isinstance(composition_data, Mapping):
        return []
    geometry = composition_data.get("pdf_text_geometry")
    pages = geometry.get("pages") if isinstance(geometry, Mapping) else None
    if not isinstance(pages, list):
        return []
    for page_data in pages:
        if not isinstance(page_data, Mapping) or page_data.get("page") != page:
            continue
        lines = page_data.get("lines")
        return [dict(item) for item in lines if isinstance(item, Mapping)] if isinstance(lines, list) else []
    return []


def _safe_identity(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    path, digest = value.get("path"), value.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str) or not path or not digest:
        return None
    return {"path": path, "sha256": digest}


def _project_source_region_candidate(
    candidate: Mapping[str, Any], composition_data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pair a deterministic PDF coverage region with a source shape and lines."""
    box = _projection_bbox(candidate)
    page = candidate.get("page")
    if box is None or not isinstance(page, int):
        return None, "source_geometry_missing"
    if not isinstance(composition_data, Mapping):
        return None, "source_geometry_missing"
    source_identity = _safe_identity(composition_data.get("source"))
    artifact_identity = _safe_identity(composition_data.get("artifact"))
    source_regions = [
        item for item in _source_regions(composition_data)
        if item.get("page") == page or item.get("page_index") == page - 1
    ]
    if source_identity is None or artifact_identity is None or not source_regions:
        return None, "source_identity_or_geometry_missing"
    matches: list[tuple[float, dict[str, Any], float, float]] = []
    for region in source_regions:
        source_box = _box(region.get("bbox_pt", region.get("bbox_pdf_points")))
        if source_box is None:
            continue
        overlap = _overlap_area(box, source_box)
        if overlap <= 0:
            continue
        coverage_ratio = round(overlap / max(1.0, _area(source_box)), 6)
        iou = _box_iou(box, source_box)
        # Prefer the source element whose complete geometry explains the PDF
        # coverage block; text shapes nested inside a background are secondary.
        score = round(iou * 2.0 + coverage_ratio, 6)
        matches.append((score, region, coverage_ratio, iou))
    if not matches:
        return None, "source_match_missing"
    matches.sort(key=lambda item: (-item[0], _area(_box(item[1].get("bbox_pt")) or (0, 0, 0, 0)), str(item[1].get("shape_id", ""))))
    selected = matches[:3]
    lines = _pdf_geometry_lines(composition_data, page)
    matched_lines: list[dict[str, Any]] = []
    for line in lines:
        line_box = _box(line.get("bbox_pt"))
        if line_box is None or _overlap_area(box, line_box) <= 0:
            continue
        matched_lines.append({
            key: line.get(key) for key in (
                "line_id", "block_id", "line_index", "bbox_pt", "baseline_y_pt",
                "line_height_pt", "source_shape_id", "font_size_pt",
            ) if key in line
        })
    matched_lines = matched_lines[:8]
    source_matches = []
    for _score, region, coverage_ratio, iou in selected:
        source_matches.append({
            "shape_id": region.get("shape_id"),
            "role": region.get("semantic_role") or region.get("role") or "unknown",
            "shape_kind": region.get("shape_kind", "unknown"),
            "bbox_pt": [round(item, 3) for item in (_box(region.get("bbox_pt", region.get("bbox_pdf_points"))) or ())],
            "units": region.get("units", "pt"),
            "geometry_basis": region.get("geometry_basis", "pptx_points"),
            "match_overlap_ratio": coverage_ratio,
            "match_iou": iou,
        })
    missing_information = []
    if not matched_lines:
        missing_information.append("pdf_text_geometry")
    if any(item.get("source_shape_id") is None for item in matched_lines):
        missing_information.append("line_source_pairing")
    projection = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate.get("id"),
        "candidate_type": "source_backed_region",
        "coverage_kind": candidate.get("coverage_kind", "unknown"),
        "page": page,
        "units": "pt",
        "measurement_basis": "pdf_points",
        "candidate_bbox_pdf_points": [round(item, 3) for item in box],
        "source_identity": source_identity,
        "artifact_identity": artifact_identity,
        "source_matches": source_matches,
        "pdf_text_geometry": {
            "units": "pt",
            "line_count": len(matched_lines),
            "lines": matched_lines,
        },
        "relation_context": {
            "type": "pdf_coverage_to_pptx_source",
            "coverage_kind": candidate.get("coverage_kind", "unknown"),
            "source_roles": [item.get("role") for item in source_matches],
        },
        "candidate_options": list(CLASSIFICATIONS),
        "branch_options": ["maintain", "adjust", "request_information"],
        "missing_information": missing_information,
    }
    return projection, None


def _spacing_projection_index(composition_data: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(composition_data, Mapping):
        return {}
    container = composition_data.get("spacing_projections")
    values: list[Mapping[str, Any]] = []
    if isinstance(container, list):
        values.extend(item for item in container if isinstance(item, Mapping))
    elif isinstance(container, Mapping):
        for family in ("line_baseline_distances", "paragraph_gaps", "inner_margins", "outer_margins", "element_gaps", "projections"):
            group = container.get(family)
            if isinstance(group, list):
                values.extend(item for item in group if isinstance(item, Mapping))
    return {str(item.get("id")): dict(item) for item in values if isinstance(item.get("id"), str) and item.get("id")}


def _project_spacing_candidate(
    candidate: Mapping[str, Any], composition_data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    projection = candidate.get("spacing_projection")
    if not isinstance(projection, Mapping):
        index = _spacing_projection_index(composition_data)
        projection = index.get(str(candidate.get("spacing_projection_id", candidate.get("id"))))
    if not isinstance(projection, Mapping):
        return None, "spacing_geometry_missing"
    measurement = projection.get("measurement")
    if not isinstance(measurement, Mapping):
        measurement = {
            key: projection.get(key) for key in projection
            if isinstance(key, str) and key.endswith("_pt") and _finite_number(projection.get(key)) is not None
        }
    if not measurement or not any(_finite_number(item) is not None or isinstance(item, Mapping) for item in measurement.values()):
        return None, "spacing_measurement_missing"
    source_identity = _safe_identity((composition_data or {}).get("source") if isinstance(composition_data, Mapping) else None)
    artifact_identity = _safe_identity((composition_data or {}).get("artifact") if isinstance(composition_data, Mapping) else None)
    page = projection.get("page", candidate.get("page"))
    bbox = _box(projection.get("bbox_pdf_points", candidate.get("bbox_pdf_points")))
    missing_information = []
    if source_identity is None:
        missing_information.append("source_identity")
    if artifact_identity is None:
        missing_information.append("artifact_identity")
    if not isinstance(page, int) or page < 1:
        missing_information.append("page")
    if bbox is None:
        missing_information.append("spacing_bbox")
    source_shape_ids = projection.get("source_shape_ids", candidate.get("shape_ids"))
    if (not isinstance(source_shape_ids, list)
            or not source_shape_ids
            or any(not isinstance(item, str) or not item for item in source_shape_ids)):
        source_shape_ids = []
        missing_information.append("source_shape_ids")
    pdf_line_ids = projection.get("pdf_line_ids")
    if (not isinstance(pdf_line_ids, list)
            or any(not isinstance(item, str) or not item for item in pdf_line_ids)):
        pdf_line_ids = []
        missing_information.append("pdf_line_ids")
    projection_units = projection.get("units", candidate.get("units"))
    if projection_units != "pt":
        missing_information.append("explicit_units_pt")
    roles = projection.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        roles = {}
        missing_information.append("roles")
    relationship = projection.get("relationship")
    if not isinstance(relationship, Mapping) or not relationship:
        relationship = {}
        missing_information.append("relationship")
    spacing_kind = projection.get("kind", candidate.get("spacing_kind", "unknown"))
    if spacing_kind in {"line_baseline_distance", "paragraph_gap"} and not pdf_line_ids:
        missing_information.append("pdf_line_ids")
    result = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate.get("id"),
        "candidate_type": "measured_spacing",
        "spacing_kind": spacing_kind,
        "rule": candidate.get("rule"),
        "page": page,
        "units": "pt",
        "measurement_basis": projection.get("measurement_basis", "pdf_points"),
        "measurement": {"units": "pt", **dict(measurement)},
        "source_identity": source_identity,
        "artifact_identity": artifact_identity,
        "source_shape_ids": list(source_shape_ids),
        "pdf_line_ids": list(pdf_line_ids),
        "roles": dict(roles),
        "relation_context": dict(relationship),
        "candidate_bbox_pdf_points": [round(item, 3) for item in bbox] if bbox is not None else None,
        "candidate_options": list(CLASSIFICATIONS),
        "branch_options": ["maintain", "adjust", "request_information"],
        "missing_information": list(dict.fromkeys(missing_information)),
    }
    return result, None


def project_composition_candidate(
    candidate: Mapping[str, Any], composition_data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project overlap, source-region, or measured-spacing structure only."""
    rule = candidate.get("rule")
    if rule == "unclassified_source_overlap_or_gutter":
        return _project_overlap_candidate(candidate, composition_data)
    if rule == "deterministic_coverage_gap_candidate":
        return _project_source_region_candidate(candidate, composition_data)
    if rule in {"line_spacing_candidate", "content_block_spacing_candidate", "content_block_intra_spacing_candidate"}:
        return _project_spacing_candidate(candidate, composition_data)
    return None, "unsupported_candidate_type"


def _answer_key(projection: Mapping[str, Any]) -> str:
    if projection.get("candidate_type") == "measured_spacing":
        return "spacing_classification"
    if projection.get("candidate_type") == "source_backed_region":
        return "region_classification"
    return "overlap_classification"


def build_request(projection: Mapping[str, Any], *, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Build the current Jev choice-wire envelope used by the installed bridge."""
    if projection.get("schema_version") != SCHEMA_VERSION:
        raise JevOverlapError("projection_schema_mismatch")
    answer_key = _answer_key(projection)
    choices = list(CLASSIFICATIONS)
    response_shape = {
        "envelope": "object with answers object",
        "answer_paths": [f"answers.{answer_key}", "answers.route"],
        "required_fields": ["choice", "confidence"],
        "optional_fields": ["reading_impact"],
        "choice": {"type": "string", "enum": choices},
        "probabilities": {
            "field": "probabilities",
            "type": "object",
            "keys": choices,
            "value": "number in [0,1]",
            "required_for_answer_keys": ["route"],
            "optional_for_answer_keys": [answer_key] if answer_key != "route" else [],
        },
        "confidence": {"type": "number", "range": [0, 1]},
        "reading_impact": {"type": "integer", "range": [0, 3], "nullable": True},
    }
    state = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "projection": dict(projection),
        "advisory_only": True,
        "authority": "deterministic_docs_checker",
        "expected_output": {
            "answer_key": answer_key,
            "answer_keys": [answer_key, "route"],
            "classification": choices,
            "choices": choices,
            "probabilities": response_shape["probabilities"],
            "reading_impact": "integer 0..3 or null",
            "confidence": "number 0..1",
        },
    }
    encoded = _canonical(state)
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise JevOverlapError("request_too_large")
    subject = {
        "measured_spacing": "measured spacing",
        "source_backed_region": "source-backed region",
    }.get(str(projection.get("candidate_type")), "structural overlap")
    return {
        "model": str(model),
        "state": encoded,
        "questions": {
            answer_key: {
                "type": "choice",
                "instructions": f"Classify only the measured {subject}. Preserve supplied geometry and units; do not inspect images, request visual evidence, or grant completion authority.",
                "choices": choices,
                "criteria": {
                    "intended": "The source relationship or measured spacing is structurally declared or clearly intentional.",
                    "acceptable": "The source relationship or measured spacing is structurally harmless to reading impact.",
                    "repair_required": "The source relationship or measured spacing likely harms reading or requires layout adjustment.",
                    "insufficient_information": "The bounded structure cannot establish meaning or impact.",
                },
                "numeric_output": {"reading_impact": "0..3 or null"},
                "response_shape": response_shape,
            }
        },
    }


def _option_probabilities(value: Any, *, required: bool) -> dict[str, float] | None:
    """Validate the bridge-compatible probability object without inference."""
    if value is None:
        if required:
            raise JevOverlapError("malformed_response")
        return None
    if not isinstance(value, Mapping) or set(value) != set(CLASSIFICATIONS):
        raise JevOverlapError("malformed_response")
    clean: dict[str, float] = {}
    for option in CLASSIFICATIONS:
        probability = value.get(option)
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise JevOverlapError("malformed_response")
        number = float(probability)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise JevOverlapError("malformed_response")
        clean[option] = number
    return clean


def parse_response(value: Any) -> dict[str, Any]:
    """Validate the small categorical/numeric response; never infer missing data."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise JevOverlapError("malformed_response") from exc
    if not isinstance(value, Mapping):
        raise JevOverlapError("malformed_response")
    response_schema = value.get("schema_version")
    if response_schema is not None and response_schema != "docs-jev-overlap-response-1":
        raise JevOverlapError("malformed_response")
    answers = value.get("answers")
    if not isinstance(answers, Mapping):
        raise JevOverlapError("malformed_response")
    answer_key = None
    answer = None
    for key in (
        "overlap_classification", "region_classification", "spacing_classification", "structural_classification", "route",
    ):
        candidate = answers.get(key)
        if isinstance(candidate, Mapping):
            answer_key, answer = key, candidate
            break
    if not isinstance(answer, Mapping):
        raise JevOverlapError("malformed_response")
    classification = answer.get("choice")
    if classification not in CLASSIFICATIONS:
        raise JevOverlapError("malformed_response")
    if "reading_impact" in answer:
        impact_value = answer.get("reading_impact")
    elif "impact" in answer:
        if answer_key == "route":
            raise JevOverlapError("malformed_response")
        # Keep the old task-specific envelope readable; the canonical route
        # must use the explicit bridge field.
        impact_value = answer.get("impact")
    else:
        # Jev's current structured response may omit the optional numeric
        # annotation; absence is normalized to the explicitly supported null
        # value rather than inferred from prose or classification.
        impact_value = None
    impact = _finite_impact(impact_value)
    confidence_value = answer.get("confidence")
    if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)) or not math.isfinite(float(confidence_value)) or not 0 <= float(confidence_value) <= 1:
        raise JevOverlapError("malformed_response")
    probabilities = _option_probabilities(
        answer.get("probabilities"),
        required=answer_key == "route",
    ) if "probabilities" in answer or answer_key == "route" else None
    model = value.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise JevOverlapError("malformed_response")
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "classification": classification,
        "reading_impact": impact,
        "confidence": round(float(confidence_value), 6),
        "model": model.strip()[:128],
        "answer_key": answer_key,
    }
    if probabilities is not None:
        result["probabilities"] = probabilities
    return result


def _branch(classification: str) -> str:
    if classification in {"intended", "acceptable"}:
        return "maintain"
    if classification == "repair_required":
        return "adjust"
    return "request_information"


def _fallback_allowed(error: JevOverlapError) -> bool:
    return error.reason in {"credential_unavailable", "network_error", "timeout"} or error.status in FALLBACK_HTTP_STATUSES or (error.status is not None and 500 <= error.status <= 599)


def _runtime_key(env_name: str) -> str:
    key = os.environ.get(env_name)
    if not isinstance(key, str) or not key.strip():
        raise JevOverlapError("credential_unavailable")
    return key.strip()


def _post_provider(endpoint: str, api_key_env: str, model: str, request_body: Mapping[str, Any], timeout: float) -> Any:
    try:
        endpoint_value = str(endpoint)
        if not endpoint_value.startswith("https://"):
            raise JevOverlapError("configuration_invalid")
        key = _runtime_key(api_key_env)
        body = dict(request_body)
        body["model"] = model
        request = Request(endpoint_value, data=_canonical(body).encode("utf-8"), method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"})
    except JevOverlapError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise JevOverlapError("configuration_invalid") from exc
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
            status_value = getattr(response, "status", 200)
    except HTTPError as exc:
        raise JevOverlapError("http_error", exc.code) from exc
    except TimeoutError as exc:
        raise JevOverlapError("timeout") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        raise JevOverlapError("timeout" if isinstance(reason, TimeoutError) else "network_error") from exc
    except OSError as exc:
        raise JevOverlapError("network_error") from exc
    try:
        status = int(status_value)
    except (TypeError, ValueError) as exc:
        raise JevOverlapError("malformed_response") from exc
    if not 200 <= status < 300:
        raise JevOverlapError("http_error", status)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JevOverlapError("malformed_response") from exc


def _supported_wire_request(request_body: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[Any, str]:
    if config.get("allow_live_provider") is not True:
        raise JevOverlapError("provider_unavailable")
    timeout = float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    try:
        response = _post_provider(PRIMARY_ENDPOINT, PRIMARY_API_KEY_ENV, str(config.get("model", PRIMARY_MODEL)), request_body, timeout)
        return response, "typesafe-system-one"
    except JevOverlapError as error:
        if not _fallback_allowed(error):
            raise
    response = _post_provider(FALLBACK_ENDPOINT, FALLBACK_API_KEY_ENV, FALLBACK_MODEL, request_body, timeout)
    return response, "openrouter-decisions"


def _unresolved(candidate: Mapping[str, Any], reason: str, projection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "candidate_id": candidate.get("id"),
        "status": "unresolved",
        "classification": None,
        "reading_impact": None,
        "confidence": None,
        "branch": "unresolved",
        "reason_code": reason,
    }
    if projection is not None:
        result["projection"] = dict(projection)
    return result


def classify_candidates(
    candidates: list[Mapping[str, Any]], *,
    composition_data: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    request_fn: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Classify eligible candidates and aggregate a deterministic local branch."""
    results: list[dict[str, Any]] = []
    calls = 0
    eligible_count = 0
    maximum = int(config.get("max_candidates", DEFAULT_MAX_CANDIDATES))
    unique_candidates: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        candidate_id = candidate.get("id")
        identity = str(candidate_id) if isinstance(candidate_id, str) and candidate_id else _digest(candidate)
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        unique_candidates.append(candidate)
    for candidate in unique_candidates:
        projection, projection_error = project_composition_candidate(candidate, composition_data)
        if projection is None:
            results.append(_unresolved(candidate, projection_error or "projection_unavailable"))
            continue
        if (projection.get("candidate_type") in {"source_backed_region", "measured_spacing"}
                and projection.get("missing_information")):
            results.append(_unresolved(candidate, "projection_incomplete", projection))
            continue
        if eligible_count >= maximum:
            results.append(_unresolved(candidate, "candidate_limit_exceeded", projection))
            continue
        eligible_count += 1
        try:
            request = build_request(projection, model=str(config.get("model", DEFAULT_MODEL)))
            calls += 1
            if request_fn is not None:
                response = request_fn(request)
                provider = "injected"
            else:
                response, provider = _supported_wire_request(request, config)
            parsed = parse_response(response)
            results.append({
                "schema_version": RESULT_SCHEMA_VERSION,
                "candidate_id": candidate.get("id"),
                "status": "classified",
                "classification": parsed["classification"],
                "reading_impact": parsed["reading_impact"],
                "confidence": parsed["confidence"],
                "answer_key": parsed["answer_key"],
                "branch": _branch(parsed["classification"]),
                "provider": provider,
                "model": parsed["model"],
                "request_sha256": _digest(request),
                "projection": projection,
            })
            if "probabilities" in parsed:
                results[-1]["probabilities"] = parsed["probabilities"]
        except JevOverlapError as error:
            results.append(_unresolved(candidate, error.reason, projection))
        except TimeoutError:
            results.append(_unresolved(candidate, "timeout", projection))
        except OSError:
            results.append(_unresolved(candidate, "provider_error", projection))
        except Exception:
            # Provider implementations are untrusted integration points.  A
            # local checker failure is an unresolved review, never a pass.
            results.append(_unresolved(candidate, "provider_error", projection))
    branches = [item.get("branch") for item in results]
    if any(branch == "adjust" for branch in branches):
        decision = "adjust"
    elif any(branch in {"request_information", "unresolved"} for branch in branches):
        decision = "request_information"
    else:
        decision = "maintain"
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "route": "structure-only-jev-overlap",
        "candidate_count": len(unique_candidates),
        "classified_count": sum(item.get("status") == "classified" for item in results),
        "unresolved_count": sum(item.get("status") == "unresolved" for item in results),
        "provider_calls": calls,
        "decision": decision,
        "vision_model_invoked": False,
        "visual_fallback": False,
        "results": results,
        "limitations": [
            "Source-backed regions and measured spacing use bounded PPTX/PDF geometry; document body and pixels are never sent.",
            "Unsupported or incomplete source relationships remain unresolved.",
            "Jev classification cannot overwrite deterministic hard failures or completion authority.",
            "Live provider evaluation is unverified unless an approved provider is explicitly enabled.",
        ],
    }


# ---------------------------------------------------------------------------
# Opt-in structure-only design evaluation
# ---------------------------------------------------------------------------
#
# The overlap adapter above is kept as the compatibility route for contracts
# that already use ``jev_overlap``.  The design route is deliberately separate:
# it is enabled only by an additive contract and accepts a typed structural IR
# plus a declared design intent.  Keeping the two envelopes separate prevents a
# legacy overlap contract from silently acquiring semantic design evaluation.

DESIGN_SCHEMA_VERSION = "docs-jev-design-evaluation-1"
DESIGN_INTENT_SCHEMA_VERSION = "docs-jev-design-intent-1"
DESIGN_IR_SCHEMA_VERSION = "docs-jev-design-ir-1"
DESIGN_REQUEST_SCHEMA_VERSION = "docs-jev-design-request-1"
DESIGN_RESPONSE_SCHEMA_VERSION = "docs-jev-design-response-1"
DESIGN_RESULT_SCHEMA_VERSION = "docs-jev-design-result-1"
DESIGN_CONFIG_KEYS = (
    "jev_design_evaluation",
    "jev_design",
    "jev_structural_evaluation",
)
DESIGN_CATEGORIES = (
    "information_priority",
    "readability_proxies",
    "visual_hierarchy_structure",
    "claim_support",
    "comparison_semantics",
    "connector_meaning",
    "misinterpretation_risk",
)
DESIGN_CATEGORY_ALIASES = {
    "readability_proxy": "readability_proxies",
    "visual_hierarchy": "visual_hierarchy_structure",
    "claim_support_semantics": "claim_support",
    "comparison": "comparison_semantics",
    "connector": "connector_meaning",
    "misinterpretation": "misinterpretation_risk",
}
DESIGN_CLASSIFICATIONS = CLASSIFICATIONS
DESIGN_BRANCHES = BRANCHES
DESIGN_FORBIDDEN_KEYS = frozenset({
    "text", "body", "content", "original", "rewritten", "response_text",
    "raw", "payload", "provider_response", "response_payload", "credential",
    "credentials", "api_key", "token", "secret", "password", "prompt",
    "screenshot", "pixels", "pixel_data", "image_data", "rendered_image",
    "visual_evidence", "vision", "vision_model", "visual_fallback",
})
DESIGN_UNMEASURED_KEYS = frozenset({
    "no_clipping", "no_overlap", "visual_pass", "manual_visual_pass",
    "looks_good", "visually_clear", "readable", "hierarchy_pass",
})
# Structural IR is intentionally a small vocabulary.  Unknown keys are not
# copied into a provider request because they could carry body text, pixels, or
# an untyped provider payload under an innocuous-looking name.
DESIGN_IR_KEYS = frozenset({
    "schema_version", "category", "candidate_id", "id", "role", "kind",
    "type", "status", "severity", "page", "page_index", "bbox",
    "bbox_pdf_points", "dimensions_pt", "units", "measurement_basis",
    "order", "source_order", "rendered_order", "level", "priority",
    "priority_rank", "importance", "weight", "score", "ratio",
    "font_size_pt", "line_height_pt", "baseline_distance_pt",
    "paragraph_gap_pt", "margin_pt", "inner_margin_pt", "outer_margin_pt",
    "gap_pt", "overlap_pt", "contrast_ratio", "line_count", "count",
    "axis", "direction", "relation", "meaning", "claim_kind", "risk_kind",
    "source_id", "target_id", "target_ids", "from_id", "to_id", "to_ids",
    "left_id", "right_id", "support_id", "support_ids", "relation_ids",
    "element_ids", "member_ids", "source_shape_ids", "pdf_line_ids",
    "elements", "element_roles", "regions", "relations", "measurements", "metrics", "structural_ir",
    "priority_order", "hierarchy", "levels", "claims", "support",
    "comparisons", "connectors", "risks", "misinterpretation_risks",
    "provenance", "source_identity", "artifact_identity", "geometry_basis",
    "sha256", "declared", "known_occlusion", "is_primary", "is_optional",
})
DESIGN_INTENT_KEYS = DESIGN_IR_KEYS | frozenset({
    "risk_tolerance", "categories", "declared_categories", "intent_id",
    "design_intent", "relation_kind", "comparison_kind", "connector_kind",
})
DESIGN_PROJECTION_KEYS = DESIGN_INTENT_KEYS | frozenset({
    "candidate_type", "candidate_options", "branch_options",
    "missing_information", "vision_model_invoked",
})
DESIGN_RESPONSE_KEYS = DESIGN_INTENT_KEYS | frozenset({
    "schema_version", "model", "answers", "design_evaluation",
    "structural_evaluation", "route", "branch", "evaluation_category",
    "confidence", "reading_impact", "probabilities", "choice",
    "vision_model_invoked", "visual_fallback", "usage", "input_tokens",
    "output_tokens", "total_tokens",
}) | frozenset(DESIGN_CATEGORIES) | frozenset(DESIGN_CLASSIFICATIONS)
DESIGN_ID_KEYS = frozenset({
    "id", "candidate_id", "source_id", "target_id", "from_id", "to_id",
    "left_id", "right_id", "support_id", "intent_id", "element_ids",
    "member_ids", "source_shape_ids", "pdf_line_ids", "relation_ids",
})


def _design_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = DESIGN_CATEGORY_ALIASES.get(value.strip(), value.strip())
    return normalized if normalized in DESIGN_CATEGORIES else None


def _design_key_forbidden(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in DESIGN_FORBIDDEN_KEYS


def _design_visual_boolean(key: Any, value: Any) -> bool:
    return isinstance(key, str) and key.lower() in DESIGN_UNMEASURED_KEYS and isinstance(value, bool)


def _design_scalar(value: Any, *, key: str | None = None) -> bool:
    if isinstance(value, bool):
        return key not in DESIGN_UNMEASURED_KEYS
    if isinstance(value, (int, float)):
        return not isinstance(value, bool) and math.isfinite(float(value))
    if isinstance(value, str):
        return "\n" not in value and "\r" not in value and len(value) <= 256
    return False


def _design_validate_node(
    value: Any,
    *,
    allowed_keys: frozenset[str] = DESIGN_IR_KEYS,
    parent_key: str | None = None,
    depth: int = 0,
) -> str | None:
    """Return a stable reason when a node is not safe typed structural data."""
    if depth > 10:
        return "structural_ir_too_deep"
    if isinstance(value, Mapping):
        if len(value) > 128:
            return "structural_ir_too_large"
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 80:
                return "malformed_structural_ir"
            lowered = key.lower()
            if _design_key_forbidden(key) or _design_visual_boolean(key, item):
                return "forbidden_visual_inference" if lowered in DESIGN_UNMEASURED_KEYS or lowered in {
                    "screenshot", "pixels", "pixel_data", "image_data", "rendered_image", "visual_evidence", "vision", "vision_model", "visual_fallback",
                } else "body_or_raw_data"
            if key not in allowed_keys:
                return "untyped_structural_field"
            if key in {"path", "url", "uri"}:
                # Paths are not structural IR.  Source/artifact identity is
                # represented by a hash-bound handle in a named container.
                return "untyped_structural_field"
            if key == "sha256":
                if (
                    not isinstance(item, str)
                    or len(item) != 64
                    or any(char not in "0123456789abcdef" for char in item)
                ):
                    return "malformed_structural_ir"
            if key in DESIGN_ID_KEYS:
                if isinstance(item, str):
                    if not item or len(item) > 128 or any(char in item for char in "\r\n"):
                        return "malformed_structural_ir"
                elif isinstance(item, list):
                    if len(item) > 128 or any(not isinstance(entry, str) or not entry or len(entry) > 128 for entry in item):
                        return "malformed_structural_ir"
                else:
                    return "malformed_structural_ir"
            reason = _design_validate_node(
                item,
                allowed_keys=allowed_keys,
                parent_key=key,
                depth=depth + 1,
            )
            if reason:
                return reason
        return None
    if isinstance(value, list):
        if len(value) > 128:
            return "structural_ir_too_large"
        for item in value:
            reason = _design_validate_node(
                item,
                allowed_keys=allowed_keys,
                parent_key=parent_key,
                depth=depth + 1,
            )
            if reason:
                return reason
        return None
    if value is None:
        # Optional measured annotations (for example reading_impact) are
        # explicitly represented as null; absence must not be inferred.
        return None
    if _design_scalar(value, key=parent_key):
        return None
    return "malformed_structural_ir"


def _design_copy(value: Any) -> Any:
    """Copy only JSON-shaped values after structural validation."""
    if isinstance(value, Mapping):
        return {str(key): _design_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_design_copy(item) for item in value]
    return value


def _design_intent(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "design_intent_missing"
    reason = _design_validate_node(value, allowed_keys=DESIGN_INTENT_KEYS)
    if reason:
        return None, reason
    copied = _design_copy(value)
    raw_categories = copied.get("categories", copied.get("declared_categories"))
    if not isinstance(raw_categories, list) or not raw_categories:
        return None, "design_intent_categories_missing"
    categories: list[str] = []
    for raw in raw_categories:
        category = _design_category(raw)
        if category is None or category in categories:
            return None, "design_intent_categories_invalid"
        categories.append(category)
    copied["schema_version"] = copied.get("schema_version", DESIGN_INTENT_SCHEMA_VERSION)
    if copied["schema_version"] != DESIGN_INTENT_SCHEMA_VERSION:
        return None, "design_intent_schema_mismatch"
    copied["categories"] = categories
    copied.pop("declared_categories", None)
    return copied, None


def validate_design_config(
    config: Any,
    *,
    contract_schema_version: str | None = None,
) -> list[str]:
    """Validate the additive structure-only design contract without I/O."""
    if config is None:
        return []
    prefix = "AQ-LAYOUT-01: jev_design_evaluation"
    if not isinstance(config, dict):
        return [f"{prefix} must be an object"]
    if contract_schema_version is not None and contract_schema_version != "docs-artifact-quality-contract-2":
        return [f"{prefix} requires docs-artifact-quality-contract-2"]
    enabled = config.get("enabled")
    if not isinstance(enabled, bool):
        return [f"{prefix}.enabled must be boolean"]
    if "schema_version" in config and config.get("schema_version") != DESIGN_SCHEMA_VERSION:
        return [f"{prefix}.schema_version is unsupported"]
    model = config.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        return [f"{prefix}.model must be a non-empty string"]
    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120:
        return [f"{prefix}.timeout_seconds must be in (0,120]"]
    maximum = config.get("max_candidates", DEFAULT_MAX_CANDIDATES)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 64:
        return [f"{prefix}.max_candidates must be an integer in [1,64]"]
    if config.get("provider_mode", "typesafe_then_openrouter") != "typesafe_then_openrouter":
        return [f"{prefix}.provider_mode must be typesafe_then_openrouter"]
    if "allow_live_provider" in config and not isinstance(config["allow_live_provider"], bool):
        return [f"{prefix}.allow_live_provider must be boolean"]
    for forbidden in DESIGN_FORBIDDEN_KEYS:
        if forbidden in config:
            return [f"{prefix} contains forbidden field {forbidden}"]
    intent_value = config.get("design_intent", config.get("intent"))
    if not enabled:
        # Disabled is a compatibility declaration.  It may omit the intent,
        # but malformed optional values still fail closed rather than becoming
        # a partially active route later.
        if intent_value is not None:
            _, reason = _design_intent(intent_value)
            if reason:
                return [f"{prefix}.design_intent: {reason}"]
        return []
    intent, intent_error = _design_intent(intent_value)
    if intent_error or intent is None:
        return [f"{prefix}.design_intent: {intent_error or 'invalid'}"]
    raw_categories = config.get("categories", intent.get("categories"))
    if not isinstance(raw_categories, list) or not raw_categories:
        return [f"{prefix}.categories must be a non-empty array"]
    categories: list[str] = []
    for raw in raw_categories:
        category = _design_category(raw)
        if category is None or category in categories:
            return [f"{prefix}.categories contains an unsupported or duplicate category"]
        categories.append(category)
    if any(category not in intent["categories"] for category in categories):
        return [f"{prefix}.categories must be declared by design_intent.categories"]
    return []


def get_design_config(layout_config: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return the canonical/alias design config without activating it."""
    if not isinstance(layout_config, Mapping):
        return None, None
    for key in DESIGN_CONFIG_KEYS:
        candidate = layout_config.get(key)
        if isinstance(candidate, dict):
            return dict(candidate), key
    return None, None


def _design_candidates_from_ir(
    composition_data: Mapping[str, Any] | None,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Read declared design candidates, never infer them from pixels/body text."""
    if not isinstance(composition_data, Mapping):
        composition_data = {}
    for key in ("design_evaluation_candidates", "jev_design_candidates", "design_candidates"):
        values = composition_data.get(key)
        if isinstance(values, list):
            return [dict(item) for item in values if isinstance(item, Mapping)]
    shared_ir = composition_data.get("design_ir")
    categories = config.get("categories") or (config.get("design_intent") or {}).get("categories")
    normalized_categories = [
        category for category in (_design_category(item) for item in categories or []) if category is not None
    ]
    if isinstance(shared_ir, Mapping):
        return [
            {"id": f"design-{category}", "category": category, "structural_ir": shared_ir}
            for category in normalized_categories
        ]
    # An enabled route with no declared IR must be visibly unresolved.  Do not
    # treat an empty source as a successful no-candidate result.
    return [
        {"id": f"design-{category}", "category": category, "structural_ir": None}
        for category in normalized_categories
    ]


def candidates_from_request_set(
    request_set: Mapping[str, Any],
    *,
    expected_count: int | None = None,
    ir_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Project a hash-bound structured request set into typed candidates.

    The canonical checker may receive a producer-emitted request set whose
    ``wire.state`` is serialized JSON.  Only the typed projection and intent
    are admitted to the provider route; the original wire envelope is never
    copied into the evaluation result.
    """
    errors: list[str] = []
    if not isinstance(request_set, Mapping):
        return [], ["request_set_not_object"]
    binding = request_set.get("binding")
    if not isinstance(binding, Mapping):
        errors.append("request_set_binding_missing")
    elif ir_sha256 is not None and binding.get("ir_sha256") != ir_sha256:
        errors.append("request_set_ir_binding_mismatch")
    requests = request_set.get("requests")
    if not isinstance(requests, list):
        return [], errors + ["request_set_requests_missing"]
    if expected_count is not None and len(requests) != expected_count:
        errors.append("request_set_count_mismatch")

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(requests):
        prefix = f"request_{index}"
        if not isinstance(raw, Mapping):
            errors.append(f"{prefix}_not_object")
            continue
        request_id = raw.get("id")
        category = _design_category(raw.get("category"))
        wire = raw.get("wire")
        if not isinstance(request_id, str) or not request_id:
            errors.append(f"{prefix}_id_missing")
            continue
        if request_id in seen:
            errors.append(f"{prefix}_id_duplicate")
            continue
        seen.add(request_id)
        if category is None:
            errors.append(f"{prefix}_category_invalid")
            continue
        if not isinstance(wire, Mapping) or not isinstance(wire.get("state"), str):
            errors.append(f"{prefix}_wire_state_missing")
            continue
        try:
            state = json.loads(wire["state"])
        except (TypeError, json.JSONDecodeError):
            errors.append(f"{prefix}_wire_state_malformed")
            continue
        if not isinstance(state, Mapping) or state.get("schema_version") != DESIGN_REQUEST_SCHEMA_VERSION:
            errors.append(f"{prefix}_wire_schema_mismatch")
            continue
        if state.get("authority") != "deterministic_docs_checker" or state.get("advisory_only") is not True:
            errors.append(f"{prefix}_authority_mismatch")
            continue
        if state.get("vision_model_invoked") is not False:
            errors.append(f"{prefix}_visual_route_forbidden")
            continue
        projection = state.get("projection")
        if not isinstance(projection, Mapping):
            errors.append(f"{prefix}_projection_missing")
            continue
        if projection.get("candidate_id") != request_id:
            errors.append(f"{prefix}_candidate_identity_mismatch")
            continue
        if _design_category(projection.get("category")) != category:
            errors.append(f"{prefix}_category_mismatch")
            continue
        if projection.get("vision_model_invoked") is not False:
            errors.append(f"{prefix}_projection_visual_route_forbidden")
            continue
        structural_ir = projection.get("structural_ir")
        design_intent = projection.get("design_intent")
        if not isinstance(structural_ir, Mapping) or not isinstance(design_intent, Mapping):
            errors.append(f"{prefix}_typed_projection_missing")
            continue
        candidates.append({
            "id": request_id,
            "category": category,
            "structural_ir": _design_copy(structural_ir),
            "design_intent": _design_copy(design_intent),
            "_canonical_request": _design_copy(wire),
        })
    return candidates, errors


def project_design_candidate(
    candidate: Mapping[str, Any],
    *,
    composition_data: Mapping[str, Any] | None = None,
    design_intent: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project one design candidate into body-free typed structural IR."""
    if not isinstance(candidate, Mapping):
        return None, "malformed_structural_candidate"
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return None, "candidate_id_missing"
    category = _design_category(
        candidate.get("category", candidate.get("design_category", candidate.get("evaluation_category")))
    )
    if category is None:
        return None, "semantic_mismatch"
    intent_value = design_intent if design_intent is not None else candidate.get("design_intent")
    intent, intent_error = _design_intent(intent_value)
    if intent_error or intent is None:
        return None, intent_error or "design_intent_missing"
    if category not in intent["categories"]:
        return None, "semantic_mismatch"
    structural = candidate.get("structural_ir", candidate.get("ir"))
    if structural is None and isinstance(composition_data, Mapping):
        structural = composition_data.get("design_ir")
    if not isinstance(structural, Mapping):
        return None, "structural_ir_missing"
    reason = _design_validate_node(structural)
    if reason:
        return None, reason
    copied_ir = _design_copy(structural)
    if copied_ir.get("schema_version", DESIGN_IR_SCHEMA_VERSION) != DESIGN_IR_SCHEMA_VERSION:
        return None, "structural_ir_schema_mismatch"
    copied_ir["schema_version"] = DESIGN_IR_SCHEMA_VERSION
    declared_category = _design_category(copied_ir.get("category"))
    if declared_category is not None and declared_category != category:
        return None, "semantic_mismatch"
    copied_ir["category"] = category
    projection = {
        "schema_version": DESIGN_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "candidate_type": "design_evaluation",
        "category": category,
        "structural_ir": copied_ir,
        "design_intent": intent,
        "candidate_options": list(DESIGN_CLASSIFICATIONS),
        "branch_options": list(DESIGN_BRANCHES),
        "missing_information": [],
        "vision_model_invoked": False,
    }
    return projection, None


def build_design_request(
    projection: Mapping[str, Any], *, model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Build an explicit-choice request containing only typed structural data."""
    if projection.get("schema_version") != DESIGN_SCHEMA_VERSION:
        raise JevOverlapError("projection_schema_mismatch")
    category = _design_category(projection.get("category"))
    if category is None:
        raise JevOverlapError("semantic_mismatch")
    reason = _design_validate_node(projection, allowed_keys=DESIGN_PROJECTION_KEYS)
    if reason:
        raise JevOverlapError(reason)
    if projection.get("vision_model_invoked") is not False:
        raise JevOverlapError("forbidden_visual_inference")
    state = {
        "schema_version": DESIGN_REQUEST_SCHEMA_VERSION,
        "evaluation_category": category,
        "projection": _design_copy(projection),
        "design_intent": _design_copy(projection["design_intent"]),
        "advisory_only": True,
        "authority": "deterministic_docs_checker",
        "vision_model_invoked": False,
        "expected_output": {
            "choices": list(DESIGN_CLASSIFICATIONS),
            "branch_options": list(DESIGN_BRANCHES),
            "confidence": "number 0..1",
            "reading_impact": "integer 0..3 or null",
        },
    }
    encoded = _canonical(state)
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise JevOverlapError("request_too_large")
    response_shape = {
        "answer_paths": ["answers.design_evaluation", f"answers.{category}", "answers.route"],
        "required_fields": ["choice", "confidence"],
        "optional_fields": ["branch", "reading_impact", "probabilities"],
        "choice": {"type": "string", "enum": list(DESIGN_CLASSIFICATIONS)},
        "branch": {"type": "string", "enum": list(DESIGN_BRANCHES)},
        "probabilities": {"type": "object", "keys": list(DESIGN_CLASSIFICATIONS), "value": "number in [0,1]"},
    }
    return {
        "model": str(model),
        "state": encoded,
        "questions": {
            "design_evaluation": {
                "type": "choice",
                "category": category,
                "instructions": "Classify only the supplied typed structural IR and declared design intent. Do not inspect pixels, body text, credentials, or provider payloads.",
                "choices": list(DESIGN_CLASSIFICATIONS),
                "branch_options": list(DESIGN_BRANCHES),
                "criteria": {
                    "intended": "The declared structural design relationship is intentional and supports the stated category.",
                    "acceptable": "The measured structural design relationship is acceptable without semantic repair.",
                    "repair_required": "The structural relationship or declared design intent requires adjustment.",
                    "insufficient_information": "The typed structural IR and design intent cannot establish the requested meaning.",
                },
                "response_shape": response_shape,
            }
        },
    }


def _design_probabilities(value: Any, *, required: bool) -> dict[str, float] | None:
    if value is None:
        if required:
            raise JevOverlapError("malformed_response")
        return None
    if not isinstance(value, Mapping) or set(value) != set(DESIGN_CLASSIFICATIONS):
        raise JevOverlapError("malformed_response")
    clean: dict[str, float] = {}
    for label in DESIGN_CLASSIFICATIONS:
        probability = value.get(label)
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise JevOverlapError("malformed_response")
        number = float(probability)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise JevOverlapError("malformed_response")
        clean[label] = number
    return clean


def _design_branch(classification: str) -> str:
    if classification in {"intended", "acceptable"}:
        return "maintain"
    if classification == "repair_required":
        return "adjust"
    return "request_information"


def parse_design_response(
    value: Any, *, expected_category: str | None = None,
) -> dict[str, Any]:
    """Parse only the typed response; prose and visual assertions are rejected."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise JevOverlapError("malformed_response") from exc
    if not isinstance(value, Mapping):
        raise JevOverlapError("malformed_response")
    if value.get("schema_version") not in {None, DESIGN_RESPONSE_SCHEMA_VERSION}:
        raise JevOverlapError("malformed_response")
    if value.get("vision_model_invoked") is not None and value.get("vision_model_invoked") is not False:
        raise JevOverlapError("forbidden_visual_inference")
    if value.get("visual_fallback") is not None and value.get("visual_fallback") is not False:
        raise JevOverlapError("forbidden_visual_inference")
    forbidden_reason = _design_validate_node(value, allowed_keys=DESIGN_RESPONSE_KEYS)
    if forbidden_reason in {"forbidden_visual_inference", "body_or_raw_data"}:
        raise JevOverlapError(forbidden_reason)
    if forbidden_reason:
        raise JevOverlapError("malformed_response")
    usage = value.get("usage")
    if usage is not None:
        if not isinstance(usage, Mapping):
            raise JevOverlapError("malformed_response")
        allowed_usage_keys = {"input_tokens", "output_tokens", "total_tokens"}
        if set(usage) - allowed_usage_keys:
            raise JevOverlapError("malformed_response")
        for count in usage.values():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise JevOverlapError("malformed_response")
    expected = _design_category(expected_category) if expected_category is not None else None
    response_category = value.get("category", value.get("evaluation_category"))
    if response_category is not None:
        normalized = _design_category(response_category)
        if normalized is None or (expected is not None and normalized != expected):
            raise JevOverlapError("semantic_mismatch")
    answers = value.get("answers")
    if not isinstance(answers, Mapping):
        raise JevOverlapError("malformed_response")
    answer_keys = ["design_evaluation"]
    if expected is not None:
        answer_keys.append(expected)
    answer_keys.append("route")
    found: list[tuple[str, Mapping[str, Any]]] = []
    for key in answer_keys:
        answer = answers.get(key)
        if isinstance(answer, Mapping):
            found.append((key, answer))
    if len(found) != 1:
        raise JevOverlapError("semantic_mismatch" if len(found) > 1 else "malformed_response")
    answer_key, answer = found[0]
    answer_category = answer.get("category", answer.get("evaluation_category"))
    if answer_category is not None:
        normalized = _design_category(answer_category)
        if normalized is None or (expected is not None and normalized != expected):
            raise JevOverlapError("semantic_mismatch")
    classification = answer.get("choice")
    if classification not in DESIGN_CLASSIFICATIONS:
        raise JevOverlapError("malformed_response")
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise JevOverlapError("malformed_response")
    reading_impact = answer.get("reading_impact")
    if reading_impact is not None:
        if isinstance(reading_impact, bool) or not isinstance(reading_impact, int) or reading_impact not in range(4):
            raise JevOverlapError("malformed_response")
    branch = answer.get("branch", value.get("branch"))
    expected_branch = _design_branch(classification)
    if branch is not None and branch != expected_branch:
        raise JevOverlapError("semantic_mismatch")
    probabilities = _design_probabilities(answer.get("probabilities"), required=answer_key == "route")
    model = value.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise JevOverlapError("malformed_response")
    result: dict[str, Any] = {
        "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
        "classification": classification,
        "branch": expected_branch,
        "reading_impact": reading_impact,
        "confidence": round(float(confidence), 6),
        "model": model.strip(),
        "answer_key": answer_key,
        "category": expected,
    }
    if probabilities is not None:
        result["probabilities"] = probabilities
    return result


def _unresolved_design(
    candidate: Mapping[str, Any], reason: str, projection: Mapping[str, Any] | None = None,
    *, request_sha256: str | None = None, response_sha256: str | None = None,
    provider: str | None = None, model: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
        "candidate_id": candidate.get("id"),
        "status": "unresolved",
        "classification": None,
        "reading_impact": None,
        "confidence": None,
        "branch": "unresolved",
        "reason_code": reason,
        "vision_model_invoked": False,
    }
    if projection is not None:
        result["projection"] = dict(projection)
    if request_sha256 is not None:
        result["request_sha256"] = request_sha256
    if response_sha256 is not None:
        result["response_sha256"] = response_sha256
    if provider is not None:
        result["provider"] = provider
    if model is not None:
        result["model"] = model
    result["provenance"] = {
        "kind": "checker",
        "id": "quality_rules.jev_overlap.classify_design_candidates",
    }
    return result


def classify_design_candidates(
    candidates: list[Mapping[str, Any]], *,
    composition_data: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    request_fn: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Classify bounded design candidates using typed IR only."""
    config_errors = validate_design_config(config, contract_schema_version="docs-artifact-quality-contract-2")
    intent, intent_error = _design_intent(config.get("design_intent", config.get("intent"))) if isinstance(config, Mapping) else (None, "design_intent_missing")
    if intent_error or intent is None:
        config_errors = [*config_errors, f"design_intent: {intent_error or 'invalid'}"] if not config_errors else config_errors
    model = str(config.get("model", DEFAULT_MODEL)) if isinstance(config, Mapping) else DEFAULT_MODEL
    maximum = int(config.get("max_candidates", DEFAULT_MAX_CANDIDATES)) if isinstance(config, Mapping) and isinstance(config.get("max_candidates", DEFAULT_MAX_CANDIDATES), int) else DEFAULT_MAX_CANDIDATES
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for candidate in candidates:
        candidate_id = candidate.get("id") if isinstance(candidate, Mapping) else None
        identity = str(candidate_id) if isinstance(candidate_id, str) and candidate_id else _digest(candidate)
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        unique.append(candidate)
    for candidate in unique:
        projection: dict[str, Any] | None = None
        projection_error: str | None = None
        if config_errors:
            projection_error = "configuration_invalid"
        else:
            projection, projection_error = project_design_candidate(
                candidate,
                composition_data=composition_data,
                design_intent=intent,
            )
        if projection is None:
            results.append(_unresolved_design(candidate, projection_error or "projection_unavailable"))
            continue
        if projection.get("missing_information"):
            results.append(_unresolved_design(candidate, "projection_incomplete", projection))
            continue
        if len(results) >= maximum:
            results.append(_unresolved_design(candidate, "candidate_limit_exceeded", projection))
            continue
        request: dict[str, Any] | None = None
        request_hash: str | None = None
        response_hash: str | None = None
        provider: str | None = None
        try:
            request = build_design_request(projection, model=model)
            request_hash = _digest(request)
            if request_fn is not None:
                provider = "injected"
                response = request_fn(request)
            else:
                response, provider = _supported_wire_request(request, config)
            response_hash = _digest(response)
            parsed = parse_design_response(response, expected_category=projection["category"])
            row: dict[str, Any] = {
                "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
                "candidate_id": candidate.get("id"),
                "status": "classified",
                "classification": parsed["classification"],
                "reading_impact": parsed["reading_impact"],
                "confidence": parsed["confidence"],
                "answer_key": parsed["answer_key"],
                "category": projection["category"],
                "branch": parsed["branch"],
                "provider": provider,
                "model": parsed["model"],
                "request_sha256": request_hash,
                "response_sha256": response_hash,
                "projection": projection,
                "provenance": {
                    "kind": "checker",
                    "id": "quality_rules.jev_overlap.classify_design_candidates",
                },
                "vision_model_invoked": False,
            }
            if "probabilities" in parsed:
                row["probabilities"] = parsed["probabilities"]
            results.append(row)
        except JevOverlapError as error:
            results.append(_unresolved_design(
                candidate,
                error.reason,
                projection,
                request_sha256=request_hash,
                response_sha256=response_hash,
                provider=provider,
                model=model,
            ))
        except TimeoutError:
            results.append(_unresolved_design(
                candidate, "timeout", projection, request_sha256=request_hash,
                response_sha256=response_hash, provider=provider, model=model,
            ))
        except OSError:
            results.append(_unresolved_design(
                candidate, "provider_error", projection, request_sha256=request_hash,
                response_sha256=response_hash, provider=provider, model=model,
            ))
        except Exception:
            # Provider integrations are untrusted.  Never leak the exception or
            # its payload into AQ-LAYOUT evidence; the candidate stays unresolved.
            results.append(_unresolved_design(
                candidate, "provider_error", projection, request_sha256=request_hash,
                response_sha256=response_hash, provider=provider, model=model,
            ))
    branches = [item.get("branch") for item in results]
    if any(branch == "adjust" for branch in branches):
        decision = "adjust"
    elif any(branch in {"request_information", "unresolved"} for branch in branches):
        decision = "request_information"
    else:
        decision = "maintain"
    return {
        "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
        "route": "structure-only-jev-design-evaluation",
        "candidate_count": len(unique),
        "classified_count": sum(item.get("status") == "classified" for item in results),
        "unresolved_count": sum(item.get("status") == "unresolved" for item in results),
        "provider_calls": sum(1 for item in results if item.get("provider") in {"injected", "typesafe-system-one", "openrouter-decisions"}),
        "configuration_valid": not bool(config_errors),
        "configuration_errors": list(config_errors),
        "decision": decision,
        "categories": list(config.get("categories", intent.get("categories", []))) if isinstance(config, Mapping) and intent is not None else [],
        "design_intent_sha256": _digest(intent) if intent is not None else None,
        "provenance": {
            "kind": "checker",
            "id": "quality_rules.jev_overlap.classify_design_candidates",
        },
        "vision_model_invoked": False,
        "visual_fallback": False,
        "results": results,
        "limitations": [
            "Only typed structural IR and declared design intent are evaluated; body text and pixels are never sent.",
            "A Jev result is advisory and cannot override deterministic hard failures or completion authority.",
            "Malformed, incomplete, provider-failure, visual-inference, and semantic-mismatch results remain unresolved.",
        ],
    }


# Short aliases keep the route discoverable to callers that use the more
# generic structural-evaluation naming while retaining one implementation.
project_structural_candidate = project_design_candidate
build_structural_request = build_design_request
parse_structural_response = parse_design_response


STANDARD_REQUEST_COUNT = 17
WORKER_RETURN_SCHEMA_VERSION = "docs-jev-worker-return-1"


def _standard_worker_return(state: str, action: str, rows: list[Mapping[str, Any]], reason: str | None = None) -> dict[str, Any]:
    request_ids = [row.get("candidate_id") for row in rows if isinstance(row.get("candidate_id"), str)]
    result: dict[str, Any] = {
        "schema_version": WORKER_RETURN_SCHEMA_VERSION,
        "status": state,
        "action": action,
        "request_ids": request_ids,
        "owner_escalation_required": action == "adjust",
        "evidence": [
            {key: row.get(key) for key in ("candidate_id", "request_sha256", "response_sha256", "reason_code")}
            for row in rows
        ],
    }
    if reason:
        result["reason_code"] = reason
    if action == "request_information":
        result["missing_information"] = [
            {"request_id": row.get("candidate_id"), "reason_code": row.get("reason_code", "insufficient_information")}
            for row in rows
        ]
    if action == "adjust":
        result["proposal"] = {
            "kind": "worker_decision_proposal",
            "candidate_ids": request_ids,
            "requires_owner_decision": True,
        }
    return result


def blocked_design_input_result(
    *,
    request_set_id: str,
    declared_count: int | None,
    execution_mode: str,
    reason: str,
) -> dict[str, Any]:
    """Return a fail-closed result before any provider invocation."""
    return {
        "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
        "route": "live-jev-standard" if execution_mode == "live" else "offline-jev-dry-run",
        "execution_mode": execution_mode,
        "request_set": {
            "id": request_set_id,
            "declared_count": declared_count,
            "actual_count": 0,
            "complete": False,
        },
        "candidate_count": 0,
        "classified_count": 0,
        "unresolved_count": 0,
        "provider_calls": 0,
        "provider_invoked": False,
        "provider_routes": [],
        "decision": "unresolved",
        "workflow_state": "blocked",
        "worker_return": _standard_worker_return("blocked", "unresolved", [], reason),
        "configuration_valid": False,
        "configuration_errors": [reason],
        "vision_model_invoked": False,
        "visual_fallback": False,
        "results": [],
        "provenance": {"kind": "checker", "id": "quality_rules.jev_overlap.blocked_design_input_result"},
    }


def _standard_provider_call(request: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[Any, str, list[str]]:
    """Invoke the existing bounded primary/fallback route with provenance."""
    if config.get("allow_live_provider") is not True:
        raise JevOverlapError("provider_unavailable")
    timeout = float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    attempts: list[str] = []
    try:
        attempts.append("typesafe-system-one")
        response = _post_provider(PRIMARY_ENDPOINT, PRIMARY_API_KEY_ENV, str(config.get("model", PRIMARY_MODEL)), request, timeout)
        return response, attempts[-1], attempts
    except JevOverlapError as primary_error:
        if not _fallback_allowed(primary_error):
            primary_error.provider_attempts = list(attempts)
            raise
    try:
        attempts.append("openrouter-decisions")
        response = _post_provider(FALLBACK_ENDPOINT, FALLBACK_API_KEY_ENV, FALLBACK_MODEL, request, timeout)
        return response, attempts[-1], attempts
    except JevOverlapError as fallback_error:
        raise JevOverlapError(
            "both_providers_failed",
            fallback_error.status,
            provider_attempts=attempts,
        ) from fallback_error


def evaluate_design_request_set(
    candidates: list[Mapping[str, Any]], *,
    composition_data: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    execution_mode: str,
    request_fn: Callable[[Mapping[str, Any]], Any] | None = None,
    expected_request_count: int | None = None,
    request_set_id: str = "docs-jev-standard-17",
) -> dict[str, Any]:
    """Run one complete bounded live/dry-run Jev stage without visual fallback."""
    if execution_mode not in {"live", "dry_run"}:
        return {
            "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
            "route": "live-jev-standard",
            "execution_mode": execution_mode,
            "decision": "unresolved",
            "workflow_state": "blocked",
            "request_set": {"id": request_set_id, "declared_count": expected_request_count, "actual_count": 0, "complete": False},
            "results": [], "provider_calls": 0, "provider_invoked": False,
            "vision_model_invoked": False, "visual_fallback": False,
            "worker_return": _standard_worker_return("blocked", "unresolved", [], "execution_mode_invalid"),
        }
    maximum = config.get("max_candidates", DEFAULT_MAX_CANDIDATES)
    maximum = maximum if isinstance(maximum, int) and not isinstance(maximum, bool) else DEFAULT_MAX_CANDIDATES
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = str(candidate.get("id")) if isinstance(candidate, Mapping) and isinstance(candidate.get("id"), str) else _digest(candidate)
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    declared = expected_request_count
    request_set = config.get("request_set")
    if declared is None and isinstance(request_set, Mapping):
        declared = request_set.get("expected_count", request_set.get("count"))
    if declared is None:
        declared = STANDARD_REQUEST_COUNT if execution_mode == "live" else len(unique)
    complete = (
        isinstance(declared, int)
        and not isinstance(declared, bool)
        and 1 <= declared <= 64
        and len(unique) == declared
    )
    # ``max_candidates`` is the legacy per-route guard.  Once a request set is
    # explicitly declared, the set itself is the bounded unit and must not be
    # silently truncated by that older guard (the standard set is 17).  Keep
    # the hard schema ceiling so an invalid declaration cannot expand the
    # evaluation without bound.
    effective_maximum = maximum
    if isinstance(declared, int) and not isinstance(declared, bool):
        effective_maximum = min(64, max(effective_maximum, declared))
    results: list[dict[str, Any]] = []
    provider_calls = 0
    routes: list[str] = []
    for index, candidate in enumerate(unique):
        projection, projection_error = project_design_candidate(candidate, composition_data=composition_data, design_intent=config.get("design_intent", config.get("intent")))
        if projection is None:
            row = _unresolved_design(candidate, projection_error or "projection_unavailable")
            row.update({"invocation_count": 0, "provider_attempts": []})
            results.append(row)
            continue
        if index >= effective_maximum:
            row = _unresolved_design(candidate, "candidate_limit_exceeded", projection)
            row.update({"invocation_count": 0, "provider_attempts": []})
            results.append(row)
            continue
        request_hash: str | None = None
        response_hash: str | None = None
        provider: str | None = None
        attempts: list[str] = []
        try:
            canonical_request = candidate.get("_canonical_request") if isinstance(candidate, Mapping) else None
            request = (
                _design_copy(canonical_request)
                if isinstance(canonical_request, Mapping)
                else build_design_request(projection, model=str(config.get("model", DEFAULT_MODEL)))
            )
            request_hash = _digest(request)
            if execution_mode == "dry_run":
                row = _unresolved_design(candidate, "dry_run_not_evaluated", projection, request_sha256=request_hash)
                row.update({"status": "not_evaluated", "invocation_count": 0, "provider_attempts": []})
                results.append(row)
                continue
            provider_calls += 1
            if request_fn is not None:
                provider, attempts, response = "injected", ["injected"], request_fn(request)
            else:
                response, provider, attempts = _standard_provider_call(request, config)
            routes.extend(attempts)
            response_hash = _digest(response)
            parsed = parse_design_response(response, expected_category=projection["category"])
            row = {
                "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
                "candidate_id": candidate.get("id"), "status": "classified",
                "classification": parsed["classification"], "reading_impact": parsed["reading_impact"],
                "confidence": parsed["confidence"], "answer_key": parsed["answer_key"],
                "category": projection["category"], "branch": parsed["branch"],
                "provider": provider, "provider_attempts": attempts, "model": parsed["model"],
                "request_sha256": request_hash, "response_sha256": response_hash,
                "invocation_count": 1, "projection": projection,
                "provenance": {"kind": "checker", "id": "quality_rules.jev_overlap.evaluate_design_request_set"},
                "vision_model_invoked": False,
            }
            if "probabilities" in parsed:
                row["probabilities"] = parsed["probabilities"]
            results.append(row)
        except JevOverlapError as error:
            attempts = list(getattr(error, "provider_attempts", attempts))
            if provider is None and attempts:
                provider = attempts[-1]
            row = _unresolved_design(candidate, error.reason, projection, request_sha256=request_hash, response_sha256=response_hash, provider=provider, model=str(config.get("model", DEFAULT_MODEL)))
            row.update({"invocation_count": len(attempts) or 1, "provider_attempts": attempts})
            results.append(row)
        except (TimeoutError, OSError) as error:
            row = _unresolved_design(candidate, "timeout" if isinstance(error, TimeoutError) else "provider_error", projection, request_sha256=request_hash, response_sha256=response_hash, provider=provider, model=str(config.get("model", DEFAULT_MODEL)))
            row.update({"invocation_count": 1, "provider_attempts": attempts})
            results.append(row)
        except Exception:
            row = _unresolved_design(candidate, "provider_error", projection, request_sha256=request_hash, response_sha256=response_hash, provider=provider, model=str(config.get("model", DEFAULT_MODEL)))
            row.update({"invocation_count": 1, "provider_attempts": attempts})
            results.append(row)
    provider_failure_reasons = {"provider_unavailable", "both_providers_failed", "credential_unavailable", "network_error", "timeout", "http_error", "provider_error"}
    provider_rows = [row for row in results if row.get("reason_code") in provider_failure_reasons]
    info_rows = [row for row in results if row.get("classification") == "insufficient_information" and row.get("branch") == "request_information"]
    adjust_rows = [row for row in results if row.get("branch") == "adjust"]
    unresolved_rows = [row for row in results if row.get("status") == "unresolved"]
    input_unresolved_rows = [row for row in unresolved_rows if row.get("reason_code") not in provider_failure_reasons]
    if execution_mode == "dry_run":
        decision, state, action, selected, reason = "unresolved", "offline_not_evaluated", "unresolved", unresolved_rows, "dry_run_not_evaluated"
    elif not complete:
        decision, state, action, selected, reason = "unresolved", "blocked", "unresolved", unresolved_rows, "request_set_incomplete"
    elif provider_rows:
        decision, state, action, selected, reason = "unresolved", "blocked", "unresolved", provider_rows, "both_providers_failed"
    elif adjust_rows:
        decision, state, action, selected, reason = "adjust", "review_required", "adjust", adjust_rows, None
    elif info_rows:
        decision, state, action, selected, reason = "request_information", "review_required", "request_information", info_rows, None
    elif unresolved_rows:
        decision, state, action, selected, reason = "unresolved", "review_required", "unresolved", unresolved_rows, "unresolved_request"
    else:
        decision, state, action, selected, reason = "maintain", "ready", "maintain", [], None
    configuration_errors: list[str] = []
    configuration_valid = True
    if execution_mode == "live" and not complete:
        configuration_valid = False
        configuration_errors.append("request_set_incomplete")
    if execution_mode == "live" and input_unresolved_rows:
        configuration_valid = False
        configuration_errors.extend(sorted({str(row.get("reason_code")) for row in input_unresolved_rows if row.get("reason_code")}))
    return {
        "schema_version": DESIGN_RESULT_SCHEMA_VERSION,
        "route": "live-jev-standard" if execution_mode == "live" else "offline-jev-dry-run",
        "execution_mode": execution_mode,
        "request_set": {
            "id": request_set_id,
            "declared_count": declared,
            "actual_count": len(unique),
            "complete": complete,
            "effective_max_candidates": effective_maximum,
        },
        "candidate_count": len(unique), "classified_count": sum(row.get("status") == "classified" for row in results),
        "unresolved_count": sum(row.get("status") == "unresolved" for row in results),
        "provider_calls": provider_calls, "provider_invoked": provider_calls > 0,
        "provider_routes": sorted(set(routes)), "decision": decision, "workflow_state": state,
        "worker_return": _standard_worker_return(state, action, selected, reason),
        "configuration_valid": configuration_valid, "configuration_errors": configuration_errors,
        "vision_model_invoked": False, "visual_fallback": False, "results": results,
        "provenance": {"kind": "checker", "id": "quality_rules.jev_overlap.evaluate_design_request_set"},
    }


classify_structural_candidates = classify_design_candidates
