"""Canonical caller→producer→renderer→QA→finalization execution contract."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import mimetypes
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from . import color_contrast
    from . import readability
    from . import semantic_component
    from . import universal_visible_text_contract
except ImportError:  # standalone plugin import path
    _color_spec = importlib.util.spec_from_file_location(
        "docs_execution_contract_color_contrast", Path(__file__).with_name("color_contrast.py")
    )
    if _color_spec is None or _color_spec.loader is None:
        raise
    color_contrast = importlib.util.module_from_spec(_color_spec)
    sys.modules[_color_spec.name] = color_contrast
    _color_spec.loader.exec_module(color_contrast)
    _readability_spec = importlib.util.spec_from_file_location(
        "docs_execution_contract_readability", Path(__file__).with_name("readability.py")
    )
    if _readability_spec is None or _readability_spec.loader is None:
        raise
    readability = importlib.util.module_from_spec(_readability_spec)
    sys.modules[_readability_spec.name] = readability
    _readability_spec.loader.exec_module(readability)
    _component_spec = importlib.util.spec_from_file_location(
        "docs_execution_contract_semantic_component", Path(__file__).with_name("semantic_component.py")
    )
    if _component_spec is None or _component_spec.loader is None:
        raise
    semantic_component = importlib.util.module_from_spec(_component_spec)
    sys.modules[_component_spec.name] = semantic_component
    _component_spec.loader.exec_module(semantic_component)
    _universal_spec = importlib.util.spec_from_file_location(
        "docs_execution_contract_universal_visible_text",
        Path(__file__).with_name("universal_visible_text_contract.py"),
    )
    if _universal_spec is None or _universal_spec.loader is None:
        raise
    universal_visible_text_contract = importlib.util.module_from_spec(_universal_spec)
    sys.modules[_universal_spec.name] = universal_visible_text_contract
    _universal_spec.loader.exec_module(universal_visible_text_contract)

SCHEMA_VERSION = "docs-production-execution-contract-1"
PRODUCER_RECEIPT_SCHEMA_VERSION = "docs-producer-execution-receipt-1"
SEMANTIC_SURFACE_EFFECT_EVIDENCE_SCHEMA_VERSION = "docs-semantic-surface-effect-evidence-1"
SEMANTIC_SURFACE_EFFECT_ADAPTER = "pptx-semantic-surface-shadow-1"
SEMANTIC_POLICY_SCHEMA_VERSION = "docs-semantic-style-policy-1"
SEMANTIC_APPLIED_SCHEMA_VERSION = "docs-semantic-style-policy-applied-1"
VERTICAL_ALIGNMENT_POLICY_SCHEMA_VERSION = "docs-cell-vertical-alignment-policy-1"
VERTICAL_ALIGNMENT_APPLIED_SCHEMA_VERSION = "docs-cell-vertical-alignment-policy-applied-1"
VERTICAL_ALIGNMENT_EFFECTIVE_SCHEMA_VERSION = "docs-cell-vertical-alignment-policy-effective-1"
SEMANTIC_SURFACE_POLICY_SCHEMA_VERSION = "docs-semantic-surface-policy-1"
SEMANTIC_SURFACE_APPLIED_SCHEMA_VERSION = "docs-semantic-surface-policy-applied-1"
SEMANTIC_SURFACE_EFFECTIVE_SCHEMA_VERSION = "docs-semantic-surface-policy-effective-1"
SEMANTIC_SURFACE_FRAMES_SCHEMA_VERSION = "docs-semantic-surface-policy-frames-1"
SEMANTIC_COMPONENT_POLICY_SCHEMA_VERSION = semantic_component.POLICY_SCHEMA_VERSION
SEMANTIC_COMPONENT_APPLIED_SCHEMA_VERSION = semantic_component.APPLIED_SCHEMA_VERSION
SEMANTIC_COMPONENT_EFFECTIVE_SCHEMA_VERSION = semantic_component.EFFECTIVE_SCHEMA_VERSION
SURFACE_INTENT_CLASSES = frozenset({
    "ordinary", "compare", "define", "enumerate", "sequence", "contrast", "memorize",
})
SURFACE_TIERS = frozenset({"flat", "light-group", "card", "emphasis"})
SURFACE_TOPOLOGIES = frozenset({"none", "shared-group", "paired", "per-item", "band"})
SURFACE_CONTAINER_SCOPES = frozenset({"none", "group", "semantic-unit"})
SURFACE_VISUAL_WEIGHTS = frozenset({"none", "subtle", "standard", "strong"})
SURFACE_ELEVATION_CLASSES = frozenset({"none", "low", "standard"})
SURFACE_TIER_ORDER = {name: index for index, name in enumerate(("flat", "light-group", "card", "emphasis"))}
COLOR_POLICY_SCHEMA_VERSION = color_contrast.COLOR_POLICY_SCHEMA_VERSION
COLOR_APPLIED_SCHEMA_VERSION = color_contrast.COLOR_APPLIED_SCHEMA_VERSION
COLOR_EFFECTIVE_SCHEMA_VERSION = color_contrast.COLOR_EFFECTIVE_SCHEMA_VERSION
READABILITY_POLICY_SCHEMA_VERSION = readability.POLICY_SCHEMA_VERSION
UNIVERSAL_VISIBLE_TEXT_POLICY_SCHEMA_VERSION = universal_visible_text_contract.POLICY_SCHEMA_VERSION
SEMANTIC_ROLES = frozenset({"body", "bullet-list", "role-list", "heading", "label"})
SEMANTIC_TOKEN_ARGUMENTS = {
    ("paragraph-spacing", "before"): "space_before_pt",
    ("paragraph-spacing", "after"): "paragraph_after_pt",
    ("line-height", "within-lines"): "line_height_pt",
}
FORBIDDEN_BODY_KEYS = frozenset({
    "text", "body", "content", "original", "rewritten", "response_text",
    "raw", "payload", "actual_value", "expected_value",
})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _contains_body_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in FORBIDDEN_BODY_KEYS or _contains_body_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_body_key(item) for item in value)
    return False


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or _contains_body_key(value):
        raise ValueError(f"{label} must be a body-free JSON object")
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc


def _trimmed_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a trimmed non-empty string")
    return value


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _canonicalize_semantic_surface_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("semantic surface policy declaration is malformed")
    required = {"schema_version", "tokens", "regions"}
    allowed = required | {"sequence_policy", "metric_thresholds"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise ValueError("semantic surface policy declaration is malformed")
    if value.get("schema_version") != SEMANTIC_SURFACE_POLICY_SCHEMA_VERSION:
        raise ValueError("semantic surface policy schema mismatch")

    raw_tokens = value.get("tokens")
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise ValueError("semantic surface policy requires tokens")
    tokens: list[dict[str, Any]] = []
    token_ids: set[str] = set()
    token_by_id: dict[str, dict[str, Any]] = {}
    token_fields = {
        "id", "surface_tier", "topology", "container_scope", "visual_weight", "elevation_class",
    }
    for raw in raw_tokens:
        if not isinstance(raw, dict) or set(raw) != token_fields:
            raise ValueError("semantic surface token is malformed")
        token_id = _trimmed_string(raw.get("id"), "semantic surface token id")
        if token_id in token_ids:
            raise ValueError("semantic surface token IDs must be unique")
        tier = raw.get("surface_tier")
        topology = raw.get("topology")
        container_scope = raw.get("container_scope")
        visual_weight = raw.get("visual_weight")
        elevation_class = raw.get("elevation_class")
        if tier not in SURFACE_TIERS:
            raise ValueError("semantic surface token surface_tier is unknown")
        if topology not in SURFACE_TOPOLOGIES:
            raise ValueError("semantic surface token topology is unknown")
        if container_scope not in SURFACE_CONTAINER_SCOPES:
            raise ValueError("semantic surface token container_scope is unknown")
        if visual_weight not in SURFACE_VISUAL_WEIGHTS:
            raise ValueError("semantic surface token visual_weight is unknown")
        if elevation_class not in SURFACE_ELEVATION_CLASSES:
            raise ValueError("semantic surface token elevation_class is unknown")
        if tier == "flat" and topology == "per-item":
            raise ValueError("semantic surface flat tier cannot use per-item topology")
        token = {
            "id": token_id,
            "surface_tier": tier,
            "topology": topology,
            "container_scope": container_scope,
            "visual_weight": visual_weight,
            "elevation_class": elevation_class,
        }
        token_ids.add(token_id)
        token_by_id[token_id] = token
        tokens.append(token)

    raw_regions = value.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("semantic surface policy requires regions")
    regions: list[dict[str, Any]] = []
    region_ids: set[str] = set()
    sequence_indexes: set[tuple[str, int]] = set()
    region_required = {"region_id", "caller_role", "intent_class", "page_index", "selected_token_id"}
    region_optional = {"semantic_group_id", "sequence_scope_id", "sequence_index"}
    for raw in raw_regions:
        if (not isinstance(raw, dict) or not region_required.issubset(raw)
                or not set(raw).issubset(region_required | region_optional)):
            raise ValueError("semantic surface region is malformed")
        region_id = _trimmed_string(raw.get("region_id"), "semantic surface region id")
        if region_id in region_ids:
            raise ValueError("semantic surface region IDs must be unique")
        caller_role = _trimmed_string(raw.get("caller_role"), "semantic surface caller_role")
        intent_class = raw.get("intent_class")
        if intent_class not in SURFACE_INTENT_CLASSES:
            raise ValueError("semantic surface region intent_class is unknown")
        page_index = _non_negative_integer(raw.get("page_index"), "semantic surface page_index")
        selected_token_id = _trimmed_string(raw.get("selected_token_id"), "semantic surface selected_token_id")
        token = token_by_id.get(selected_token_id)
        if token is None:
            raise ValueError(f"semantic surface region references unknown token: {selected_token_id}")
        semantic_group_id = raw.get("semantic_group_id")
        if semantic_group_id is not None:
            semantic_group_id = _trimmed_string(semantic_group_id, "semantic surface semantic_group_id")
        if token["topology"] == "none" and semantic_group_id is not None:
            raise ValueError("semantic surface topology none cannot declare semantic_group_id")
        if token["topology"] != "none" and semantic_group_id is None:
            raise ValueError("semantic surface grouped topology requires semantic_group_id")
        has_scope = "sequence_scope_id" in raw
        has_index = "sequence_index" in raw
        if has_scope != has_index:
            raise ValueError("semantic surface sequence scope and index must be declared together")
        region = {
            "region_id": region_id,
            "caller_role": caller_role,
            "intent_class": intent_class,
            "page_index": page_index,
            "selected_token_id": selected_token_id,
        }
        if semantic_group_id is not None:
            region["semantic_group_id"] = semantic_group_id
        if has_scope:
            scope_id = _trimmed_string(raw.get("sequence_scope_id"), "semantic surface sequence_scope_id")
            sequence_index = _non_negative_integer(raw.get("sequence_index"), "semantic surface sequence_index")
            sequence_key = (scope_id, sequence_index)
            if sequence_key in sequence_indexes:
                raise ValueError("semantic surface sequence_index must be unique within sequence_scope_id")
            sequence_indexes.add(sequence_key)
            region["sequence_scope_id"] = scope_id
            region["sequence_index"] = sequence_index
        region_ids.add(region_id)
        regions.append(region)

    result: dict[str, Any] = {
        "schema_version": SEMANTIC_SURFACE_POLICY_SCHEMA_VERSION,
        "tokens": sorted(tokens, key=lambda item: item["id"]),
        "regions": sorted(regions, key=lambda item: item["region_id"]),
    }
    sequence_policy = value.get("sequence_policy")
    if sequence_policy is not None:
        fields = {"mode", "prominent_tiers", "max_consecutive_pages", "model_call_if_zero_candidates"}
        if not isinstance(sequence_policy, dict) or set(sequence_policy) != fields:
            raise ValueError("semantic surface sequence_policy is malformed")
        if sequence_policy.get("mode") != "review":
            raise ValueError("semantic surface sequence_policy mode is unknown")
        prominent_tiers = sequence_policy.get("prominent_tiers")
        if (not isinstance(prominent_tiers, list) or not prominent_tiers
                or any(item not in SURFACE_TIERS for item in prominent_tiers)
                or len(set(prominent_tiers)) != len(prominent_tiers)):
            raise ValueError("semantic surface sequence_policy prominent_tiers are malformed")
        max_pages = _non_negative_integer(
            sequence_policy.get("max_consecutive_pages"),
            "semantic surface sequence_policy max_consecutive_pages",
        )
        if max_pages < 1:
            raise ValueError("semantic surface sequence_policy max_consecutive_pages must be positive")
        if sequence_policy.get("model_call_if_zero_candidates") is not False:
            raise ValueError("semantic surface sequence_policy cannot call a model when candidate count is zero")
        result["sequence_policy"] = {
            "mode": "review",
            "prominent_tiers": sorted(prominent_tiers, key=SURFACE_TIER_ORDER.__getitem__),
            "max_consecutive_pages": max_pages,
            "model_call_if_zero_candidates": False,
        }
    metric_thresholds = value.get("metric_thresholds")
    if metric_thresholds is not None:
        fields = {
            "max_surface_area_ratio", "min_vertical_occupancy",
            "max_bottom_blank_ratio", "max_consecutive_prominent_pages",
        }
        if not isinstance(metric_thresholds, dict) or set(metric_thresholds) != fields:
            raise ValueError("semantic surface metric_thresholds are malformed")
        normalized_thresholds: dict[str, Any] = {}
        for field in (
            "max_surface_area_ratio", "min_vertical_occupancy", "max_bottom_blank_ratio",
        ):
            raw = metric_thresholds.get(field)
            if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                    or not math.isfinite(float(raw)) or not 0 <= float(raw) <= 1):
                raise ValueError(f"semantic surface {field} must be a finite ratio")
            normalized_thresholds[field] = float(raw)
        maximum = metric_thresholds.get("max_consecutive_prominent_pages")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("semantic surface max_consecutive_prominent_pages must be positive")
        normalized_thresholds["max_consecutive_prominent_pages"] = maximum
        result["metric_thresholds"] = normalized_thresholds
    return result


def canonicalize_policy(value: Any) -> dict[str, Any]:
    """Canonicalize caller policy tokens and fail closed on declaration defects."""
    policy = _json_object(value, "policy")
    result = deepcopy(policy)
    semantic_surface = policy.get("semantic_surface_policy")
    if semantic_surface is not None:
        result["semantic_surface_policy"] = _canonicalize_semantic_surface_policy(semantic_surface)
    semantic_components = policy.get("semantic_component_policy")
    if semantic_components is not None:
        result["semantic_component_policy"] = semantic_component.canonicalize_policy(semantic_components)
    declaration = policy.get("semantic_style_policy")
    if declaration is not None:
        if not isinstance(declaration, dict) or set(declaration) != {"schema_version", "tokens"}:
            raise ValueError("semantic style policy declaration is malformed")
        if declaration.get("schema_version") != SEMANTIC_POLICY_SCHEMA_VERSION:
            raise ValueError("semantic style policy schema mismatch")
        tokens = declaration.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise ValueError("semantic style policy requires tokens")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in tokens:
            if not isinstance(raw, dict) or set(raw) != {"id", "kind", "role", "relation", "value"}:
                raise ValueError("semantic style token is malformed")
            token_id = raw.get("id")
            kind, role, relation = raw.get("kind"), raw.get("role"), raw.get("relation")
            token_value = raw.get("value")
            if not isinstance(token_id, str) or not token_id.strip() or token_id != token_id.strip() or token_id in seen:
                raise ValueError("semantic style token IDs must be unique non-empty strings")
            if role not in SEMANTIC_ROLES or (kind, relation) not in SEMANTIC_TOKEN_ARGUMENTS:
                raise ValueError("semantic style token kind/role/relation is unknown")
            if not isinstance(token_value, dict) or set(token_value) != {"unit", "amount"} or token_value.get("unit") != "pt":
                raise ValueError("semantic style token value is malformed")
            amount = token_value.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)) or float(amount) < 0:
                raise ValueError("semantic style token amount must be finite and non-negative")
            if kind == "line-height" and float(amount) <= 0:
                raise ValueError("semantic line-height token amount must be positive")
            seen.add(token_id)
            normalized.append({
                "id": token_id, "kind": kind, "role": role, "relation": relation,
                "value": {"unit": "pt", "amount": float(amount)},
            })
        result["semantic_style_policy"] = {
            "schema_version": SEMANTIC_POLICY_SCHEMA_VERSION,
            "tokens": sorted(normalized, key=lambda item: item["id"]),
        }
    vertical = policy.get("vertical_alignment_policy")
    if vertical is not None:
        if not isinstance(vertical, dict) or set(vertical) != {"schema_version", "tokens"}:
            raise ValueError("vertical alignment policy declaration is malformed")
        if vertical.get("schema_version") != VERTICAL_ALIGNMENT_POLICY_SCHEMA_VERSION:
            raise ValueError("vertical alignment policy schema mismatch")
        tokens = vertical.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise ValueError("vertical alignment policy requires tokens")
        normalized_vertical: list[dict[str, Any]] = []
        seen_vertical: set[str] = set()
        for raw in tokens:
            if not isinstance(raw, dict) or set(raw) != {"id", "target_role", "vertical_align", "center_tolerance_pt"}:
                raise ValueError("vertical alignment token is malformed")
            token_id, target_role = raw.get("id"), raw.get("target_role")
            align, tolerance = raw.get("vertical_align"), raw.get("center_tolerance_pt")
            if not isinstance(token_id, str) or not token_id.strip() or token_id != token_id.strip() or token_id in seen_vertical:
                raise ValueError("vertical alignment token IDs must be unique non-empty strings")
            if not isinstance(target_role, str) or not target_role.strip() or target_role != target_role.strip():
                raise ValueError("vertical alignment target_role must be a non-empty caller role")
            if align != "center":
                raise ValueError("vertical alignment token vertical_align is invalid")
            if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(float(tolerance)) or float(tolerance) < 0:
                raise ValueError("vertical alignment center_tolerance_pt must be finite and non-negative")
            seen_vertical.add(token_id)
            normalized_vertical.append({
                "id": token_id, "target_role": target_role, "vertical_align": align,
                "center_tolerance_pt": float(tolerance),
            })
        result["vertical_alignment_policy"] = {
            "schema_version": VERTICAL_ALIGNMENT_POLICY_SCHEMA_VERSION,
            "tokens": sorted(normalized_vertical, key=lambda item: item["id"]),
        }
    color = policy.get("color_policy")
    if color is not None:
        result["color_policy"] = color_contrast.canonicalize_color_policy(color)
    readability_policy = policy.get("readability_policy")
    if readability_policy is not None:
        result["readability_policy"] = readability.canonicalize_policy(readability_policy)
    universal = policy.get("universal_visible_text_policy")
    if universal is not None:
        result["universal_visible_text_policy"] = universal_visible_text_contract.canonicalize_policy(universal)
    return json.loads(_canonical(result))


def apply_universal_visible_text_policy(policy: Any, items: Any) -> dict[str, Any]:
    """Apply the declared upstream text contract before any format adapter."""
    canonical = canonicalize_policy(policy)
    declaration = canonical.get("universal_visible_text_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare universal visible text policy")
    return universal_visible_text_contract.apply_contract(items, declaration)


def resolve_semantic_tokens(
    policy: Any, *, token_ids: Any, role: str,
    direct_values: dict[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Resolve generic semantic tokens to concrete producer call arguments."""
    canonical = canonicalize_policy(policy)
    declaration = canonical.get("semantic_style_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare semantic style policy")
    if (not isinstance(token_ids, (list, tuple)) or not token_ids
            or any(not isinstance(item, str) or not item for item in token_ids)
            or len(set(token_ids)) != len(token_ids)):
        raise ValueError("semantic_token_ids must contain unique non-empty token IDs")
    by_id = {item["id"]: item for item in declaration["tokens"]}
    concrete: dict[str, float] = {}
    applied_tokens: list[dict[str, Any]] = []
    supplied = direct_values or {}
    for token_id in token_ids:
        token = by_id.get(token_id)
        if token is None:
            raise ValueError(f"unknown semantic style token: {token_id}")
        if token["role"] != role:
            raise ValueError(f"semantic style token role drift: {token_id}")
        argument = SEMANTIC_TOKEN_ARGUMENTS[(token["kind"], token["relation"])]
        amount = float(token["value"]["amount"])
        if argument in concrete:
            raise ValueError(f"semantic style token resolution collision: {argument}")
        direct = supplied.get(argument)
        if direct is not None and (isinstance(direct, bool) or not isinstance(direct, (int, float)) or abs(float(direct) - amount) > 1e-9):
            raise ValueError(f"semantic style token resolution drift: {argument}")
        concrete[argument] = amount
        applied_tokens.append({
            "id": token_id, "kind": token["kind"], "role": role,
            "relation": token["relation"], "call_argument": argument, "value_pt": amount,
        })
    applied = {
        "schema_version": SEMANTIC_APPLIED_SCHEMA_VERSION,
        "tokens": sorted(applied_tokens, key=lambda item: item["id"]),
    }
    return concrete, applied


def resolve_vertical_alignment_tokens(
    policy: Any, *, token_ids: Any, target_role: str,
    direct_values: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a caller-role vertical-alignment token to producer arguments."""
    canonical = canonicalize_policy(policy)
    declaration = canonical.get("vertical_alignment_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare vertical alignment policy")
    if not isinstance(target_role, str) or not target_role.strip() or target_role != target_role.strip():
        raise ValueError("target_role must be a non-empty caller role")
    if (not isinstance(token_ids, (list, tuple)) or not token_ids
            or any(not isinstance(item, str) or not item for item in token_ids)
            or len(set(token_ids)) != len(token_ids)):
        raise ValueError("vertical_alignment_token_ids must contain unique non-empty token IDs")
    supplied = {} if direct_values is None else direct_values
    if not isinstance(supplied, dict) or any(key not in {"vertical_align", "center_tolerance_pt"} for key in supplied):
        raise ValueError("vertical alignment direct values are malformed")
    if len(token_ids) != 1:
        raise ValueError("vertical alignment resolution requires exactly one token")
    token_id = token_ids[0]
    token = {item["id"]: item for item in declaration["tokens"]}.get(token_id)
    if token is None:
        raise ValueError(f"unknown vertical alignment token: {token_id}")
    if token["target_role"] != target_role:
        raise ValueError(f"vertical alignment token role drift: {token_id}")
    concrete = {
        "vertical_align": token["vertical_align"],
        "center_tolerance_pt": float(token["center_tolerance_pt"]),
    }
    for argument, expected in concrete.items():
        direct = supplied.get(argument)
        if direct is None:
            continue
        if argument == "center_tolerance_pt":
            matches = (not isinstance(direct, bool) and isinstance(direct, (int, float))
                       and math.isfinite(float(direct)) and abs(float(direct) - expected) <= 1e-9)
        else:
            matches = direct == expected
        if not matches:
            raise ValueError(f"vertical alignment token resolution drift: {argument}")
    applied = {
        "schema_version": VERTICAL_ALIGNMENT_APPLIED_SCHEMA_VERSION,
        "target_role": target_role,
        "tokens": [{
            "id": token_id, "target_role": target_role,
            "vertical_align": concrete["vertical_align"],
            "center_tolerance_pt": concrete["center_tolerance_pt"],
        }],
    }
    return concrete, applied


def resolve_semantic_surface_region(
    policy: Any, *, region_id: str, caller_role: str,
    direct_values: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one declared region to a body-free format-independent state."""
    canonical = canonicalize_policy(policy)
    declaration = canonical.get("semantic_surface_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare semantic surface policy")
    region_id = _trimmed_string(region_id, "semantic surface region_id")
    caller_role = _trimmed_string(caller_role, "semantic surface caller_role")
    region = {item["region_id"]: item for item in declaration["regions"]}.get(region_id)
    if region is None:
        raise ValueError(f"unknown semantic surface region: {region_id}")
    if region["caller_role"] != caller_role:
        raise ValueError(f"semantic surface region role drift: {region_id}")
    token = {item["id"]: item for item in declaration["tokens"]}[region["selected_token_id"]]
    concrete = {
        "region_id": region_id,
        "caller_role": caller_role,
        "intent_class": region["intent_class"],
        "page_index": region["page_index"],
        "selected_token_id": region["selected_token_id"],
        "surface_tier": token["surface_tier"],
        "topology": token["topology"],
        "container_scope": token["container_scope"],
        "visual_weight": token["visual_weight"],
        "elevation_class": token["elevation_class"],
    }
    for optional in ("semantic_group_id", "sequence_scope_id", "sequence_index"):
        if optional in region:
            concrete[optional] = region[optional]
    supplied = {} if direct_values is None else _json_object(direct_values, "semantic surface direct values")
    if any(key not in concrete for key in supplied):
        raise ValueError("semantic surface direct values are malformed")
    for key, direct in supplied.items():
        if direct != concrete[key] or isinstance(direct, bool) != isinstance(concrete[key], bool):
            raise ValueError(f"semantic surface resolution drift: {key}")
    applied = {"schema_version": SEMANTIC_SURFACE_APPLIED_SCHEMA_VERSION, **deepcopy(concrete)}
    return concrete, applied


def resolve_semantic_component(
    policy: Any, *, component_id: str, caller_role: str,
    direct_values: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one opt-in component through the generic component contract."""
    canonical = canonicalize_policy(policy)
    declaration = canonical.get("semantic_component_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare semantic component policy")
    return semantic_component.resolve_component(
        declaration, component_id=component_id, caller_role=caller_role,
        direct_values=direct_values,
    )


def _validate_semantic_surface_state(value: Any, expected_schema: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != expected_schema or _contains_body_key(value):
        raise ValueError("producer semantic surface state schema is malformed")
    required = {
        "schema_version", "region_id", "caller_role", "intent_class", "page_index",
        "selected_token_id", "surface_tier", "topology", "container_scope",
        "visual_weight", "elevation_class",
    }
    optional = {"semantic_group_id", "sequence_scope_id", "sequence_index"}
    if not required.issubset(value) or not set(value).issubset(required | optional):
        raise ValueError("producer semantic surface state fields are malformed")
    for field in ("region_id", "caller_role", "selected_token_id"):
        _trimmed_string(value.get(field), f"producer semantic surface {field}")
    if value.get("intent_class") not in SURFACE_INTENT_CLASSES:
        raise ValueError("producer semantic surface intent_class is unknown")
    _non_negative_integer(value.get("page_index"), "producer semantic surface page_index")
    if value.get("surface_tier") not in SURFACE_TIERS:
        raise ValueError("producer semantic surface surface_tier is unknown")
    if value.get("topology") not in SURFACE_TOPOLOGIES:
        raise ValueError("producer semantic surface topology is unknown")
    if value.get("container_scope") not in SURFACE_CONTAINER_SCOPES:
        raise ValueError("producer semantic surface container_scope is unknown")
    if value.get("visual_weight") not in SURFACE_VISUAL_WEIGHTS:
        raise ValueError("producer semantic surface visual_weight is unknown")
    if value.get("elevation_class") not in SURFACE_ELEVATION_CLASSES:
        raise ValueError("producer semantic surface elevation_class is unknown")
    semantic_group_id = value.get("semantic_group_id")
    if semantic_group_id is not None:
        _trimmed_string(semantic_group_id, "producer semantic surface semantic_group_id")
    if value["topology"] == "none" and semantic_group_id is not None:
        raise ValueError("producer semantic surface topology none cannot declare semantic_group_id")
    if value["topology"] != "none" and semantic_group_id is None:
        raise ValueError("producer semantic surface grouped topology requires semantic_group_id")
    has_scope = "sequence_scope_id" in value
    has_index = "sequence_index" in value
    if has_scope != has_index:
        raise ValueError("producer semantic surface sequence scope and index must be declared together")
    if has_scope:
        _trimmed_string(value.get("sequence_scope_id"), "producer semantic surface sequence_scope_id")
        _non_negative_integer(value.get("sequence_index"), "producer semantic surface sequence_index")
    return json.loads(_canonical(value))


def resolve_color_role(
    policy: Any, *, role: str, direct_values: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one arbitrary caller color role from the canonical policy."""
    canonical = canonicalize_policy(policy)
    declaration = canonical.get("color_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare color policy")
    return color_contrast.resolve_color_role(declaration, role=role, direct_values=direct_values)


def _ids(values: Any, label: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"{label} must contain non-empty IDs")
    normalized = sorted({item.strip() for item in values})
    if len(normalized) != len(values):
        raise ValueError(f"{label} IDs must be unique")
    return normalized


def file_handle(path: Path, *, artifact: bool = False) -> dict[str, Any]:
    target = path.resolve(strict=True)
    if not target.is_file():
        raise ValueError(f"not a regular file: {target}")
    handle: dict[str, Any] = {
        "path": str(target), "sha256": sha256(target), "size_bytes": target.stat().st_size,
    }
    if artifact:
        exact = {
            ".pdf": "application/pdf", ".png": "image/png", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        handle["media_type"] = exact.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return handle


def artifact_set_id(handles: list[dict[str, Any]]) -> str:
    canonical = [
        {"canonical_path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"], "media_type": item["media_type"]}
        for item in sorted(handles, key=lambda item: item["path"])
    ]
    return _digest(canonical)


def issue_contract(*, source: Path, policy: dict[str, Any], scope_ids: list[str], acceptance_ids: list[str], producer_run_id: int) -> dict[str, Any]:
    if isinstance(producer_run_id, bool) or not isinstance(producer_run_id, int) or producer_run_id < 0:
        raise ValueError("producer_run_id must be a non-negative integer")
    declaration = {
        "policy": canonicalize_policy(policy),
        "source": file_handle(source),
        "scope_ids": _ids(scope_ids, "scope_ids"),
        "acceptance_ids": _ids(acceptance_ids, "acceptance_ids"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": _digest(declaration),
        "policy_hash": _digest(declaration["policy"]),
        "producer_run_id": producer_run_id,
        "declaration": declaration,
    }


def bind_artifacts(contract: dict[str, Any], artifacts: list[Path]) -> dict[str, Any]:
    errors = validate_contract(contract, require_artifacts=False)
    if errors:
        raise ValueError("invalid execution contract: " + "; ".join(errors))
    if not artifacts:
        raise ValueError("integrated execution contract requires artifacts")
    handles = [file_handle(path, artifact=True) for path in artifacts]
    if len({item["path"] for item in handles}) != len(handles):
        raise ValueError("integrated execution contract contains duplicate artifacts")
    result = deepcopy(contract)
    result["artifacts"] = sorted(handles, key=lambda item: item["path"])
    result["artifact_set_id"] = artifact_set_id(result["artifacts"])
    return result


def write_contract(path: Path, contract: dict[str, Any]) -> Path:
    path = path.resolve()
    if not path.is_absolute():
        raise ValueError("execution contract path must be absolute")
    errors = validate_contract(contract, require_artifacts=True)
    if errors:
        raise ValueError("invalid execution contract: " + "; ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(contract) + b"\n")
    return path


def load_contract(value: Path | dict[str, Any], *, require_artifacts: bool = True) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(value, Path):
        try:
            data = json.loads(value.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, ["execution contract is unreadable or malformed"]
    else:
        data = deepcopy(value)
    if not isinstance(data, dict):
        return None, ["execution contract must be an object"]
    errors = validate_contract(data, require_artifacts=require_artifacts)
    return (data if not errors else None), errors


def _validate_live_handle(value: Any, label: str, *, artifact: bool = False) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} handle is missing"]
    required = {"path", "sha256", "size_bytes"} | ({"media_type"} if artifact else set())
    if set(value) != required:
        return [f"{label} handle fields mismatch"]
    raw = value.get("path")
    path = Path(raw) if isinstance(raw, str) else Path()
    if not path.is_absolute() or not path.is_file() or value.get("sha256") != sha256(path) or value.get("size_bytes") != path.stat().st_size:
        return [f"{label} path/hash/size drift"]
    if artifact and value.get("media_type") != file_handle(path, artifact=True)["media_type"]:
        return [f"{label} media-type drift"]
    return []


def validate_contract(contract: Any, *, require_artifacts: bool = True) -> list[str]:
    if not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION:
        return ["execution contract schema mismatch"]
    if _contains_body_key(contract):
        return ["execution contract contains body fields"]
    declaration = contract.get("declaration")
    if not isinstance(declaration, dict) or set(declaration) != {"policy", "source", "scope_ids", "acceptance_ids"}:
        return ["execution contract declaration is incomplete"]
    errors: list[str] = []
    try:
        policy = canonicalize_policy(declaration.get("policy"))
        if declaration.get("policy") != policy:
            errors.append("execution contract policy is not canonical")
        _ids(declaration.get("scope_ids"), "scope_ids")
        _ids(declaration.get("acceptance_ids"), "acceptance_ids")
    except ValueError as exc:
        errors.append(str(exc)); policy = {}
    errors.extend(_validate_live_handle(declaration.get("source"), "execution source"))
    if contract.get("contract_id") != _digest(declaration):
        errors.append("execution contract canonical hash mismatch")
    if contract.get("policy_hash") != _digest(policy):
        errors.append("execution contract policy hash mismatch")
    run_id = contract.get("producer_run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 0:
        errors.append("execution contract producer_run_id is invalid")
    artifacts = contract.get("artifacts")
    set_id = contract.get("artifact_set_id")
    if require_artifacts:
        if not isinstance(artifacts, list) or not artifacts:
            errors.append("execution contract artifact set is missing")
        else:
            for index, handle in enumerate(artifacts):
                errors.extend(_validate_live_handle(handle, f"execution artifact[{index}]", artifact=True))
            try:
                if set_id != artifact_set_id(artifacts):
                    errors.append("execution contract artifact_set_id mismatch")
            except (KeyError, TypeError, ValueError):
                errors.append("execution contract artifact set is malformed")
    elif artifacts is not None or set_id is not None:
        errors.append("unbound execution contract may not partially declare artifacts")
    return list(dict.fromkeys(errors))


def identity(contract: dict[str, Any]) -> dict[str, Any]:
    errors = validate_contract(contract, require_artifacts=True)
    if errors:
        raise ValueError("invalid execution contract: " + "; ".join(errors))
    return {key: contract[key] for key in ("contract_id", "policy_hash", "producer_run_id", "artifact_set_id")}


def validate_identity(value: Any, contract: dict[str, Any], label: str) -> list[str]:
    try:
        expected = identity(contract)
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(value, dict) or any(value.get(key) != expected[key] for key in expected):
        return [f"{label} execution identity mismatch"]
    return []


def validate_semantic_surface_effect_evidence(value: Path | dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """Validate body-free requested/applied/effective shadow evidence."""
    try:
        data = json.loads(value.read_text(encoding="utf-8")) if isinstance(value, Path) else deepcopy(value)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["semantic surface effect evidence is unreadable or malformed"]
    if (not isinstance(data, dict)
            or set(data) != {"schema_version", "execution_identity", "artifact", "regions"}
            or data.get("schema_version") != SEMANTIC_SURFACE_EFFECT_EVIDENCE_SCHEMA_VERSION
            or _contains_body_key(data)):
        return ["semantic surface effect evidence schema mismatch"]
    errors = validate_identity(data.get("execution_identity"), contract, "semantic surface effect evidence")
    artifact = data.get("artifact")
    artifacts = {item.get("path"): item for item in contract.get("artifacts", []) if isinstance(item, dict)}
    if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}
            or artifact.get("path") not in artifacts
            or artifacts.get(artifact.get("path"), {}).get("sha256") != artifact.get("sha256")):
        errors.append("semantic surface effect artifact identity/hash drift")
    regions = data.get("regions")
    if not isinstance(regions, list):
        return list(dict.fromkeys(errors + ["semantic surface effect regions are malformed"]))
    seen_regions: set[str] = set(); seen_primitives: set[tuple[str, str, str]] = set()
    for row in regions:
        if (not isinstance(row, dict)
                or set(row) != {"region_id", "primitive_identity", "requested", "applied", "effective"}):
            errors.append("semantic surface effect region schema mismatch"); continue
        region_id = row.get("region_id")
        primitive = row.get("primitive_identity")
        requested, applied, effective = row.get("requested"), row.get("applied"), row.get("effective")
        if not isinstance(region_id, str) or not region_id.strip() or region_id in seen_regions:
            errors.append("semantic surface effect region identity drift"); continue
        seen_regions.add(region_id)
        if (not isinstance(primitive, dict) or set(primitive) != {"format", "part", "shape_id"}
                or primitive.get("format") not in {"pptx", "html"}
                or any(not isinstance(primitive.get(key), str) or not primitive[key] for key in ("part", "shape_id"))):
            errors.append("semantic surface effect primitive identity is malformed"); continue
        primitive_key = (primitive["format"], primitive["part"], primitive["shape_id"])
        if primitive_key in seen_primitives:
            errors.append("semantic surface effect duplicate primitive identity")
        seen_primitives.add(primitive_key)
        if (not isinstance(requested, dict) or set(requested) != {"elevation_class", "ownership_scope"}
                or requested.get("elevation_class") not in SURFACE_ELEVATION_CLASSES
                or requested.get("ownership_scope") != "shadow-only"):
            errors.append("semantic surface effect requested state is malformed"); continue
        if (not isinstance(applied, dict) or set(applied) != {"adapter", "ownership_scope", "theme_effect_ref_action", "direct_outer_shadow_action", "preset"}
                or applied.get("ownership_scope") != "shadow-only"
                or applied.get("adapter") not in {SEMANTIC_SURFACE_EFFECT_ADAPTER, "html-semantic-surface-shadow-1"}):
            errors.append("semantic surface effect applied state is malformed"); continue
        if (not isinstance(effective, dict)
                or set(effective) != {"shadow_present", "shadow_source", "theme_effect_ref_idx", "direct_outer_shadow_count", "blur_pt", "distance_pt", "alpha_fraction"}):
            errors.append("semantic surface effect effective state is malformed"); continue
        elevation = requested["elevation_class"]
        if elevation == "none":
            if effective.get("shadow_present") is not False or effective.get("direct_outer_shadow_count") != 0 or effective.get("theme_effect_ref_idx") not in {None, 0} or effective.get("shadow_source") != "explicit-none" or applied.get("preset") is not None:
                errors.append("semantic surface effect none mapping drift")
        else:
            if effective.get("shadow_present") is not True or effective.get("direct_outer_shadow_count") != 1 or effective.get("theme_effect_ref_idx") not in {None, 0} or effective.get("shadow_source") != "adapter-direct" or not isinstance(applied.get("preset"), dict):
                errors.append("semantic surface effect elevated mapping drift")
            for key in ("blur_pt", "distance_pt", "alpha_fraction"):
                number = effective.get(key)
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)):
                    errors.append("semantic surface effect numeric state is malformed"); break
    return list(dict.fromkeys(errors))


def aggregate_producer_policy(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """Aggregate semantic per-frame states while preserving legacy receipt shape."""
    states: list[tuple[str, list[str] | None, dict[str, Any]]] = []
    for record in records:
        identity_value = record.get("execution_contract") if isinstance(record, dict) else None
        if not isinstance(identity_value, dict) or not isinstance(identity_value.get(field), dict):
            raise ValueError(f"producer frame {field} policy missing")
        frame_id = record.get("frame_id")
        if not isinstance(frame_id, str) or not frame_id:
            raise ValueError("producer frame_id is missing")
        selected = identity_value.get("selected_token_ids")
        if selected is not None and (
                not isinstance(selected, list) or not selected
                or any(not isinstance(item, str) or not item for item in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError("producer selected semantic token IDs are malformed")
        states.append((frame_id, selected, identity_value[field]))
    universal_schema = (
        "docs-universal-visible-text-policy-applied-1"
        if field == "applied"
        else "docs-universal-visible-text-policy-effective-1"
    )
    state_schemas = {value.get("schema_version") for _, _, value in states}
    expected_surface_schema = (
        SEMANTIC_SURFACE_APPLIED_SCHEMA_VERSION
        if field == "applied"
        else SEMANTIC_SURFACE_EFFECTIVE_SCHEMA_VERSION
    )
    if state_schemas == {expected_surface_schema}:
        if len({frame_id for frame_id, _, _ in states}) != len(states):
            raise ValueError("producer semantic surface frame IDs must be unique")
        frames: list[dict[str, Any]] = []
        for frame_id, selected, raw_state in sorted(states, key=lambda item: item[0]):
            state = _validate_semantic_surface_state(raw_state, expected_surface_schema)
            if selected != [state["selected_token_id"]]:
                raise ValueError("producer semantic surface selected token drift")
            frames.append({
                "frame_id": frame_id,
                "selected_token_ids": selected,
                "region_id": state["region_id"],
                "policy": state,
            })
        return {
            "schema_version": SEMANTIC_SURFACE_FRAMES_SCHEMA_VERSION,
            "state": field,
            "frames": frames,
        }
    strict_surface_schemas = {
        SEMANTIC_SURFACE_APPLIED_SCHEMA_VERSION,
        SEMANTIC_SURFACE_EFFECTIVE_SCHEMA_VERSION,
    }
    if state_schemas & strict_surface_schemas:
        raise ValueError("producer semantic surface state schema mix is malformed")
    if state_schemas == {universal_schema}:
        if len({frame_id for frame_id, _, _ in states}) != len(states):
            raise ValueError("producer universal frame IDs must be unique")
        return {
            "schema_version": "docs-universal-visible-text-policy-frames-1",
            "state": field,
            "frames": [
                {"frame_id": frame_id, "policy": value}
                for frame_id, _, value in sorted(states, key=lambda item: item[0])
            ],
        }
    semantic = [selected is not None for _, selected, _ in states]
    if any(semantic) and not all(semantic):
        raise ValueError("producer plan mixes semantic and legacy policy states")
    if not any(semantic):
        if any(state[2] != states[0][2] for state in states[1:]):
            raise ValueError(f"producer {field} policy differs across frames")
        return states[0][2]
    if len({frame_id for frame_id, _, _ in states}) != len(states):
        raise ValueError("producer semantic frame IDs must be unique")
    expected_color_schema = COLOR_APPLIED_SCHEMA_VERSION if field == "applied" else COLOR_EFFECTIVE_SCHEMA_VERSION
    if state_schemas == {expected_color_schema}:
        return {
            "schema_version": "docs-color-contrast-policy-frames-1",
            "state": field,
            "frames": [
                {"frame_id": frame_id, "selected_token_ids": selected,
                 "role": value["role"], "policy": value}
                for frame_id, selected, value in sorted(states, key=lambda item: item[0])
            ],
        }
    return {
        "schema_version": "docs-semantic-style-policy-frames-1",
        "frames": [
            {"frame_id": frame_id, "selected_token_ids": selected, "policy": value}
            for frame_id, selected, value in sorted(states, key=lambda item: item[0])
        ],
    }


def validate_producer_receipt(path: Path, contract: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["producer receipt is unreadable or malformed"]
    errors: list[str] = []
    if not isinstance(data, dict) or data.get("schema_version") != PRODUCER_RECEIPT_SCHEMA_VERSION or data.get("status") != "pass":
        return None, ["producer receipt schema/status mismatch"]
    if _contains_body_key(data):
        errors.append("producer receipt contains body fields")
    errors.extend(validate_identity(data.get("execution_identity"), contract, "producer receipt"))
    if data.get("requested") != contract.get("declaration", {}).get("policy"):
        errors.append("producer receipt requested policy mismatch")
    for field in ("applied", "effective"):
        if not isinstance(data.get(field), dict):
            errors.append(f"producer receipt {field} policy missing")
    contract_handle = data.get("contract")
    errors.extend(_validate_live_handle(contract_handle, "producer receipt contract"))
    if isinstance(contract_handle, dict) and contract_handle.get("sha256") != sha256(Path(contract_handle["path"])):
        errors.append("producer receipt contract hash drift")
    expected_artifacts = contract.get("artifacts")
    if data.get("artifacts") != expected_artifacts:
        errors.append("producer receipt artifact identity mismatch")
    plan_log = data.get("plan_log")
    errors.extend(_validate_live_handle(plan_log, "producer plan log"))
    effect_handle = data.get("semantic_surface_effect_evidence")
    if effect_handle is not None:
        errors.extend(_validate_live_handle(effect_handle, "semantic surface effect evidence"))
        if isinstance(effect_handle, dict) and not _validate_live_handle(effect_handle, "semantic surface effect evidence"):
            errors.extend(validate_semantic_surface_effect_evidence(Path(effect_handle["path"]), contract))
    if isinstance(plan_log, dict) and not _validate_live_handle(plan_log, "producer plan log"):
        try:
            frame_records = [json.loads(line) for line in Path(plan_log["path"]).read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeError, json.JSONDecodeError):
            frame_records = []
        if not frame_records or any(not isinstance(record, dict) for record in frame_records):
            errors.append("producer plan log frame records are missing")
        else:
            for record in frame_records:
                frame = record.get("execution_contract")
                if (not isinstance(frame, dict)
                        or frame.get("contract_id") != contract.get("contract_id")
                        or frame.get("policy_hash") != contract.get("policy_hash")
                        or frame.get("producer_run_id") != contract.get("producer_run_id")
                        or frame.get("requested") != data.get("requested")):
                    errors.append("producer receipt differs from producer plan log")
                    break
                for field, expected_schema in (
                    ("applied", SEMANTIC_SURFACE_APPLIED_SCHEMA_VERSION),
                    ("effective", SEMANTIC_SURFACE_EFFECTIVE_SCHEMA_VERSION),
                ):
                    state = frame.get(field)
                    if not isinstance(state, dict) or state.get("schema_version") != expected_schema:
                        continue
                    try:
                        _, expected_state = resolve_semantic_surface_region(
                            data.get("requested"),
                            region_id=state.get("region_id"),
                            caller_role=state.get("caller_role"),
                        )
                        expected_state["schema_version"] = expected_schema
                    except ValueError:
                        errors.append(f"producer semantic surface {field} drift from requested policy")
                        continue
                    if state != expected_state:
                        errors.append(f"producer semantic surface {field} drift from requested policy")
            try:
                if (aggregate_producer_policy(frame_records, "applied") != data.get("applied")
                        or aggregate_producer_policy(frame_records, "effective") != data.get("effective")):
                    errors.append("producer receipt differs from producer plan log")
            except ValueError as exc:
                errors.append(str(exc))
            if data.get("frame_count") != len(frame_records):
                errors.append("producer receipt frame count mismatch")
    return (data if not errors else None), list(dict.fromkeys(errors))


def validate_bundle(
    *, contract_path: Path, producer_receipt_path: Path, finalization_path: Path,
) -> list[str]:
    """Independently re-read every integrated receipt and fresh artifact handle."""
    contract, errors = load_contract(contract_path, require_artifacts=True)
    if contract is None:
        return errors
    producer, producer_errors = validate_producer_receipt(producer_receipt_path, contract)
    if producer is None:
        return producer_errors
    try:
        finalization = json.loads(finalization_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["integrated finalization manifest is unreadable or malformed"]
    if not isinstance(finalization, dict):
        return ["integrated finalization manifest must be an object"]
    errors = validate_identity(finalization.get("execution_identity"), contract, "finalization")
    if finalization.get("artifact_set_id") != contract["artifact_set_id"]:
        errors.append("finalization artifact_set_id mismatch")
    if finalization.get("created_by_run_id") != contract["producer_run_id"]:
        errors.append("finalization producer_run_id mismatch")
    expected_contract = file_handle(contract_path)
    expected_producer = file_handle(producer_receipt_path)
    for field, expected in (("execution_contract", expected_contract), ("producer_receipt", expected_producer)):
        value = finalization.get(field)
        normalized = ({"path": value.get("canonical_path"), "sha256": value.get("sha256"), "size_bytes": value.get("size_bytes")}
                      if isinstance(value, dict) else None)
        if normalized != expected:
            errors.append(f"finalization {field} handle mismatch")
    renderer_handles = finalization.get("renderer_receipts")
    if not isinstance(renderer_handles, list) or len(renderer_handles) != len(contract["artifacts"]):
        errors.append("renderer receipt coverage mismatch")
        return list(dict.fromkeys(errors))
    artifacts = {item["path"]: item for item in contract["artifacts"]}
    covered: set[str] = set()
    for index, handle in enumerate(renderer_handles):
        raw = handle.get("canonical_path") if isinstance(handle, dict) else None
        renderer_path = Path(raw) if isinstance(raw, str) else Path()
        if (not renderer_path.is_absolute() or not renderer_path.is_file()
                or handle.get("sha256") != sha256(renderer_path)
                or handle.get("size_bytes") != renderer_path.stat().st_size):
            errors.append(f"renderer receipt[{index}] handle drift")
            continue
        try:
            renderer = json.loads(renderer_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append(f"renderer receipt[{index}] malformed")
            continue
        artifact_path = renderer.get("artifact_path") if isinstance(renderer, dict) else None
        artifact = Path(artifact_path) if isinstance(artifact_path, str) else Path()
        if artifact_path in covered or artifact_path not in artifacts:
            errors.append("renderer artifact coverage mismatch")
            continue
        covered.add(artifact_path)
        if renderer.get("artifact_sha256") != artifacts[artifact_path]["sha256"] or sha256(artifact) != artifacts[artifact_path]["sha256"]:
            errors.append("renderer artifact hash mismatch")
        errors.extend(validate_identity(renderer.get("execution_identity"), contract, "renderer receipt"))
        color_errors = color_contrast.validate_rendered_color_receipt(
            renderer.get("color_receipt"), contract, identity(contract),
        )
        errors.extend(color_errors)
        readability_errors = readability.validate_receipt(
            renderer.get("readability_receipt"), contract, identity(contract), artifact=artifact,
        )
        errors.extend(readability_errors)
        if renderer.get("execution_contract") != expected_contract or renderer.get("producer_receipt") != expected_producer:
            errors.append("renderer contract/producer receipt binding mismatch")
        renders = renderer.get("renders")
        if not isinstance(renders, list) or not renders:
            errors.append("renderer image evidence missing")
            continue
        for rendered in renders:
            render_path = Path(rendered.get("path", "")) if isinstance(rendered, dict) else Path()
            try:
                if not render_path.is_absolute() or rendered.get("sha256") != sha256(render_path):
                    raise ValueError
                with Image.open(render_path) as image:
                    image.load()
                    if list(image.size) != [rendered.get("width"), rendered.get("height")]:
                        raise ValueError
            except Exception:
                errors.append("renderer image readback mismatch")
    if covered != set(artifacts):
        errors.append("renderer receipt artifact set mismatch")
    return list(dict.fromkeys(errors))
