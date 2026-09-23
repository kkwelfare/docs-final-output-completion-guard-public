"""Body-free semantic block/surface IR and deterministic classifications."""
from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = "docs-semantic-surface-ir-1"
CLASSIFICATIONS = ("INFO", "REVIEW_CANDIDATE", "HARD_FAIL")
EXPLICIT_ALLOWED_ROLE_CLASSES = frozenset({"code", "warning", "question", "answer"})
DEFAULT_THRESHOLDS = {
    "max_surface_area_ratio": 0.60,
    "min_vertical_occupancy": 0.35,
    "max_bottom_blank_ratio": 0.45,
    "max_consecutive_prominent_pages": 2,
}


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ratio(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise ValueError(f"{label} must be a finite ratio from 0 to 1")
    return float(value)


def _thresholds(value: Any) -> dict[str, Any]:
    result = dict(DEFAULT_THRESHOLDS)
    if value is None:
        return result
    if not isinstance(value, dict) or not set(value).issubset(result):
        raise ValueError("semantic surface metric thresholds are malformed")
    for key in ("max_surface_area_ratio", "min_vertical_occupancy", "max_bottom_blank_ratio"):
        if key in value:
            result[key] = _ratio(value[key], key)
    if "max_consecutive_prominent_pages" in value:
        count = value["max_consecutive_prominent_pages"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("max_consecutive_prominent_pages must be positive")
        result["max_consecutive_prominent_pages"] = count
    return result


def _role_class(caller_role: str) -> str:
    normalized = caller_role.strip().lower().replace("_", "-")
    for role in EXPLICIT_ALLOWED_ROLE_CLASSES:
        if normalized == role or normalized.endswith("-" + role) or normalized.startswith(role + "-"):
            return role
    return "caller-defined"


def _page_runs(pages: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for page in sorted(set(pages)):
        if runs and page == runs[-1][-1] + 1:
            runs[-1].append(page)
        else:
            runs.append([page])
    return runs


def build_ir(*, regions: list[dict[str, Any]], page_sizes: dict[int, list[float]],
             line_boxes: dict[int, list[list[float]]], hard_failures: list[dict[str, Any]],
             vertical_occupancy, bottom_blank_ratio,
             thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a format-independent IR from validated producer states and geometry.

    Text is never accepted or retained. Region IDs, roles, tiers, geometry and measured
    ratios are sufficient for bounded detection and producer-side recomposition.
    """
    limits = _thresholds(thresholds)
    blocks: list[dict[str, Any]] = []
    surfaces: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    hard = [dict(item) for item in hard_failures if isinstance(item, dict)]
    seen: set[str] = set()
    prominent_by_scope: dict[str, list[int]] = {}
    for raw in sorted(regions, key=lambda item: str(item.get("region_id"))):
        region_id = raw.get("region_id")
        requested, effective = raw.get("requested"), raw.get("effective")
        if (not isinstance(region_id, str) or not region_id.strip() or region_id in seen
                or not isinstance(requested, dict) or not isinstance(effective, dict)):
            hard.append({"rule_id": "SURFACE-IR-SCHEMA-01", "region_id": region_id})
            continue
        seen.add(region_id)
        caller_role, intent = effective.get("caller_role"), effective.get("intent_class")
        page_index, tier, topology = effective.get("page_index"), effective.get("surface_tier"), effective.get("topology")
        if (not isinstance(caller_role, str) or not caller_role.strip() or not isinstance(intent, str)
                or isinstance(page_index, bool) or not isinstance(page_index, int)
                or not isinstance(tier, str) or not isinstance(topology, str)):
            hard.append({"rule_id": "SURFACE-IR-SCHEMA-01", "region_id": region_id})
            continue
        block = {
            "block_id": "semantic-block-" + _digest([region_id, caller_role, intent])[:16],
            "region_id": region_id, "caller_role": caller_role, "role_class": _role_class(caller_role),
            "intent_class": intent, "page_index": page_index,
            "semantic_group_id": effective.get("semantic_group_id"),
            "sequence_scope_id": effective.get("sequence_scope_id"),
            "sequence_index": effective.get("sequence_index"),
        }
        blocks.append(block)
        bbox = raw.get("bbox_pt")
        page_number = page_index + 1
        page_size = page_sizes.get(page_number)
        area_ratio = occupancy = blank = None
        present = bbox is not None
        if bbox is not None:
            if (not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(page_size, list)
                    or len(page_size) != 2 or page_size[0] <= 0 or page_size[1] <= 0):
                hard.append({"rule_id": "SURFACE-GEOMETRY-01", "region_id": region_id})
            else:
                area_ratio = round(max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]) /
                                   (page_size[0] * page_size[1]), 6)
                occupancy = vertical_occupancy(bbox, line_boxes.get(page_number, []))
                blank = bottom_blank_ratio(bbox, line_boxes.get(page_number, []))
        surface = {
            "surface_id": "semantic-surface-" + _digest([region_id, tier, topology])[:16],
            "region_id": region_id, "page_index": page_index, "surface_tier": tier,
            "topology": topology, "container_scope": effective.get("container_scope"),
            "visual_weight": effective.get("visual_weight"), "elevation_class": effective.get("elevation_class"),
            "present": present, "bbox_pt": bbox, "area_ratio": area_ratio,
            "vertical_occupancy": occupancy, "bottom_blank_ratio": blank,
        }
        surfaces.append(surface)
        effect = raw.get("effect_evidence")
        if effect is not None:
            effect_requested = effect.get("requested") if isinstance(effect, dict) else None
            effect_effective = effect.get("effective") if isinstance(effect, dict) else None
            if (not isinstance(effect_requested, dict) or not isinstance(effect_effective, dict)
                    or effect_requested.get("elevation_class") != effective.get("elevation_class")):
                hard.append({"rule_id": "SURFACE-EFFECT-DRIFT-01", "region_id": region_id})
            elif effective.get("elevation_class") == "none":
                if (effect_effective.get("shadow_present") is not False
                        or effect_effective.get("direct_outer_shadow_count") != 0
                        or effect_effective.get("theme_effect_ref_idx") not in {None, 0}):
                    hard.append({"rule_id": "SURFACE-ELEVATION-01", "region_id": region_id})
            elif (effect_effective.get("shadow_present") is not True
                  or effect_effective.get("direct_outer_shadow_count") != 1
                  or effect_effective.get("theme_effect_ref_idx") not in {None, 0}
                  or effect_effective.get("shadow_source") != "adapter-direct"):
                hard.append({"rule_id": "SURFACE-ELEVATION-01", "region_id": region_id})
        info.append({"id": "surface-info-" + _digest([region_id, tier])[:16],
                     "rule": "semantic_surface_observed", "classification": "INFO",
                     "region_id": region_id, "role_class": block["role_class"], "surface_tier": tier})
        if area_ratio is not None and area_ratio > limits["max_surface_area_ratio"]:
            review.append({"id": "surface-review-" + _digest([region_id, "area"])[:16],
                           "rule": "semantic_surface_area_ratio", "classification": "REVIEW_CANDIDATE",
                           "region_id": region_id, "page": page_number, "bbox_pdf_points": bbox,
                           "measured": area_ratio, "threshold": limits["max_surface_area_ratio"]})
        if (occupancy is not None and blank is not None
                and occupancy < limits["min_vertical_occupancy"] and blank > limits["max_bottom_blank_ratio"]):
            review.append({"id": "surface-review-" + _digest([region_id, "occupancy"])[:16],
                           "rule": "semantic_surface_sparse_container", "classification": "REVIEW_CANDIDATE",
                           "region_id": region_id, "page": page_number, "bbox_pdf_points": bbox,
                           "vertical_occupancy": occupancy, "bottom_blank_ratio": blank,
                           "min_vertical_occupancy": limits["min_vertical_occupancy"],
                           "max_bottom_blank_ratio": limits["max_bottom_blank_ratio"]})
        scope = effective.get("sequence_scope_id")
        if isinstance(scope, str) and tier in {"card", "emphasis"}:
            prominent_by_scope.setdefault(scope, []).append(page_index)
    for scope, pages in sorted(prominent_by_scope.items()):
        for run in _page_runs(pages):
            if len(run) > limits["max_consecutive_prominent_pages"]:
                review.append({"id": "surface-review-" + _digest([scope, run])[:16],
                               "rule": "semantic_surface_consecutive_prominent_pages",
                               "classification": "REVIEW_CANDIDATE", "sequence_scope_id": scope,
                               "affected_pages": [page + 1 for page in run], "affected_count": len(run),
                               "threshold": limits["max_consecutive_prominent_pages"]})
    for item in hard:
        item.setdefault("classification", "HARD_FAIL")
    return {
        "schema_version": SCHEMA_VERSION,
        "thresholds": limits,
        "semantic_blocks": blocks,
        "surfaces": surfaces,
        "metrics": {
            "block_count": len(blocks), "surface_count": len(surfaces),
            "surface_present_count": sum(item["present"] for item in surfaces),
            "max_surface_area_ratio": max((item["area_ratio"] for item in surfaces if item["area_ratio"] is not None), default=0.0),
            "min_vertical_occupancy": min((item["vertical_occupancy"] for item in surfaces if item["vertical_occupancy"] is not None), default=None),
            "max_bottom_blank_ratio": max((item["bottom_blank_ratio"] for item in surfaces if item["bottom_blank_ratio"] is not None), default=None),
        },
        "INFO": info, "REVIEW_CANDIDATE": review, "HARD_FAIL": hard,
        "status": "HARD_FAIL" if hard else "REVIEW_CANDIDATE" if review else "INFO",
    }
