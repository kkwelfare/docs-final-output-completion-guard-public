from __future__ import annotations

"""Body-free PPTX/SVG adapters for generic geometry relations."""

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from . import relative_anchor_gap
except ImportError:  # standalone fixture/import path
    from quality_rules import relative_anchor_gap

FORMAT_RELATION_SCHEMA_VERSION = "docs-format-geometry-relation-frame-1"
SOURCE_EVIDENCE_SCHEMA_VERSION = "docs-format-geometry-source-evidence-1"
SURFACE_RECOMPOSE_SCHEMA_VERSION = "docs-format-surface-recompose-frame-1"
GEOMETRY_TYPES = frozenset({"anchor_gap", "connector_route", "layer_relation", "content_inset"})
IDENTITY_FIELDS = ("contract_id", "policy_hash", "producer_run_id", "artifact_set_id")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise ValueError("geometry relation execution identity must contain exactly four fields")
    if not _is_hash(value.get("contract_id")) or not _is_hash(value.get("policy_hash")):
        raise ValueError("geometry relation contract/policy identity is invalid")
    run = value.get("producer_run_id")
    if isinstance(run, bool) or not isinstance(run, int) or run < 0 or not _is_hash(value.get("artifact_set_id")):
        raise ValueError("geometry relation run/artifact-set identity is invalid")
    return {key: value[key] for key in IDENTITY_FIELDS}


def _bbox(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("primitive bbox must contain four values")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError("primitive bbox must be finite numeric geometry")
        result.append(round(float(item), 4))
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("primitive bbox must have positive area")
    return result


def _body_free(value: Any) -> bool:
    forbidden = {"text", "body", "content", "payload", "raw", "value", "actual_value", "expected_value"}
    if isinstance(value, dict):
        return all(str(key).lower() not in forbidden and _body_free(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_body_free(item) for item in value)
    return True


def write_source_evidence(*, format_name: str, adapter: str, primitives: list[dict[str, Any]], output_path: Path) -> Path:
    if format_name not in {"pptx", "svg"} or not isinstance(adapter, str) or not adapter.strip():
        raise ValueError("unsupported geometry source adapter")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(primitives):
        if not isinstance(raw, dict):
            raise ValueError("geometry source primitives must be objects")
        primitive_id = raw.get("primitive_id")
        member_id = raw.get("member_id")
        order = raw.get("source_order", index)
        page = raw.get("page", 1)
        if (not isinstance(primitive_id, str) or not primitive_id.strip() or primitive_id in seen
                or not isinstance(member_id, str) or not member_id.strip()
                or isinstance(order, bool) or not isinstance(order, int) or order < 0
                or isinstance(page, bool) or not isinstance(page, int) or page < 1):
            raise ValueError("geometry primitive identity/order is malformed")
        seen.add(primitive_id)
        item = {
            "primitive_id": primitive_id,
            "member_id": member_id,
            "source_order": order,
            "page": page,
            "bbox_pdf_points": _bbox(raw.get("bbox_pdf_points")),
            "primitive_kind": str(raw.get("primitive_kind", "shape")),
        }
        for key in ("shape_id", "shape_tree_order", "group_order"):
            if key in raw:
                number = raw[key]
                if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                    raise ValueError(f"{key} must be a non-negative integer")
                item[key] = number
        for key in ("connector_start_shape_id", "connector_end_shape_id"):
            if key in raw and raw[key] is not None:
                number = raw[key]
                if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                    raise ValueError(f"{key} must be a non-negative integer")
                item[key] = number
        for key in ("path_data_sha256", "clip_sha256", "mask_sha256", "source_annotation_map_sha256"):
            if key in raw and raw[key] is not None:
                if not _is_hash(raw[key]):
                    raise ValueError(f"{key} must be sha256")
                item[key] = raw[key]
        for key in ("annotation_id", "semantic_role"):
            if key in raw:
                if not isinstance(raw[key], str) or not raw[key].strip():
                    raise ValueError(f"{key} must be a non-empty string")
                item[key] = raw[key].strip()
        if "tail_tip_pdf_points" in raw:
            point = raw["tail_tip_pdf_points"]
            if not isinstance(point, (list, tuple)) or len(point) != 2 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in point):
                raise ValueError("tail_tip_pdf_points must be finite point geometry")
            item["tail_tip_pdf_points"] = [round(float(value), 4) for value in point]
        if "visible_bbox_pdf_points" in raw:
            item["visible_bbox_pdf_points"] = _bbox(raw["visible_bbox_pdf_points"])
        normalized.append(item)
    normalized.sort(key=lambda item: (item["source_order"], item["primitive_id"]))
    record = {
        "schema_version": SOURCE_EVIDENCE_SCHEMA_VERSION,
        "format": format_name,
        "adapter": adapter.strip(),
        "primitive_count": len(normalized),
        "primitives": normalized,
    }
    if not _body_free(record):
        raise ValueError("geometry source evidence must remain body-free")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_canonical(record) + b"\n")
    return output_path


def build_relation_frame(*, relation: dict[str, Any], format_name: str, adapter: str,
                         execution_identity: dict[str, Any], source_evidence_path: Path,
                         primitives: list[dict[str, Any]], render_metrics: dict[str, Any],
                         crop_sha256_by_primitive: dict[str, str]) -> dict[str, Any]:
    identity = _identity(execution_identity)
    relation_type = relation.get("type") if isinstance(relation, dict) else None
    ordered_members = relation.get("ordered_member_ids") if isinstance(relation, dict) else None
    if relation_type not in GEOMETRY_TYPES or not isinstance(ordered_members, list) or len(ordered_members) < 2:
        raise ValueError("generic geometry relation declaration is malformed")
    source = json.loads(source_evidence_path.read_text(encoding="utf-8"))
    if source.get("format") != format_name or source.get("adapter") != adapter:
        raise ValueError("geometry source evidence adapter mismatch")
    by_member: dict[str, list[dict[str, Any]]] = {}
    for item in source["primitives"]:
        by_member.setdefault(item["member_id"], []).append(item)
    missing = [member for member in ordered_members if member not in by_member]
    if missing:
        raise ValueError("geometry relation source mapping is incomplete")
    declaration = relation.get("constraints", {}).get("anchor_gap") if relation_type == "anchor_gap" else None
    if isinstance(declaration, dict) and declaration.get("measurement_model") == relative_anchor_gap.MEASUREMENT_MODEL:
        policy = relative_anchor_gap.canonicalize_contract(declaration)
        if ordered_members != [policy["anchor"]["member_id"], policy["reference"]["member_id"]]:
            raise ValueError("relative anchor-gap ordered member mapping drift")
        if any(len(by_member[member]) != 1 for member in ordered_members):
            raise ValueError("relative anchor-gap primitive mapping is ambiguous")
        anchor_primitive = by_member[ordered_members[0]][0]
        reference_primitive = by_member[ordered_members[1]][0]
        if (anchor_primitive.get("semantic_role") != "tail_tip" or "tail_tip_pdf_points" not in anchor_primitive
                or reference_primitive.get("semantic_role") != "visible_envelope" or "visible_bbox_pdf_points" not in reference_primitive):
            raise ValueError("relative anchor-gap source geometry mapping is incomplete")
        for primitive in (anchor_primitive, reference_primitive):
            if (primitive.get("annotation_id") != policy["annotation_id"]
                    or primitive.get("source_annotation_map_sha256") != policy["source_annotation_map_sha256"]):
                raise ValueError("relative anchor-gap source identity drift")
    ordered_primitives = [item["primitive_id"] for member in ordered_members for item in by_member[member]]
    source_orders = [item["source_order"] for member in ordered_members for item in by_member[member]]
    source_order_pass = source_orders == sorted(source_orders)
    mappings: list[dict[str, Any]] = []
    for member in ordered_members:
        for item in by_member[member]:
            crop_hash = crop_sha256_by_primitive.get(item["primitive_id"])
            if not _is_hash(crop_hash):
                raise ValueError("every mapped primitive requires a real rendered crop hash")
            mappings.append({
                "primitive_id": item["primitive_id"], "member_id": member,
                "page": item["page"], "bbox_pdf_points": item["bbox_pdf_points"],
                "crop_sha256": crop_hash,
            })
    effective = deepcopy(render_metrics)
    if not isinstance(effective, dict):
        raise ValueError("render metrics must be an object")
    effective.setdefault("measurement_basis", "pdf_vector_plus_render_mask")
    effective.setdefault("status", "measured")
    if isinstance(declaration, dict) and declaration.get("measurement_model") == relative_anchor_gap.MEASUREMENT_MODEL:
        effective.setdefault("annotation_id", declaration["annotation_id"])
        effective.setdefault("source_annotation_map_sha256", declaration["source_annotation_map_sha256"])
        if effective.get("status") != "measured":
            raise ValueError("relative anchor-gap effective evidence must be measured")
        measured = relative_anchor_gap.measure_geometry(
            tail_tip=effective.get("tail_tip_pdf_points"), visible_avatar_bbox=effective.get("avatar_bbox_pdf_points"),
            direction_vector=effective.get("direction_vector"), target_gap_ratio=declaration["target_gap_ratio"],
            tolerance=declaration["tolerance"], facing_expected=declaration["facing"],
            facing_actual=effective.get("facing"), no_contact=declaration["no_contact"],
            contact_tolerance_pt=declaration["contact_tolerance_pt"], intersection=effective.get("no_contact") is not True,
        )
        for key in ("avatar_width_pt", "gap_pt", "gap_ratio"):
            if key not in effective or abs(float(effective[key]) - float(measured[key])) > (1e-8 if key == "gap_ratio" else 1e-4):
                raise ValueError("relative anchor-gap render metric drift")
        effective.setdefault("failed_constraints", measured["failed_constraints"])
    effective["evidence_sha256"] = _hash_bytes(_canonical({
        "identity": identity, "relation_id": relation.get("id"), "mappings": mappings,
        "metrics": effective,
    }))
    effective["primitive_mappings"] = mappings
    applied = {
        "format": format_name,
        "adapter": adapter,
        "ordered_primitive_ids": ordered_primitives,
        "source_order_pass": source_order_pass,
        "source_evidence_sha256": sha256(source_evidence_path),
    }
    bound_relation = deepcopy(relation)
    bound_relation["applied"] = applied
    bound_relation["effective"] = effective
    frame = {
        "schema_version": FORMAT_RELATION_SCHEMA_VERSION,
        "frame_id": f"geometry-relation:{relation['id']}",
        "execution_identity": identity,
        "requested": deepcopy(relation),
        "applied": deepcopy(applied),
        "effective": deepcopy(effective),
        "relation": bound_relation,
    }
    if not _body_free(frame):
        raise ValueError("geometry relation frame must remain body-free")
    return frame


def append_relation_frame(path: Path, frame: dict[str, Any]) -> None:
    if frame.get("schema_version") != FORMAT_RELATION_SCHEMA_VERSION or not _body_free(frame):
        raise ValueError("invalid geometry relation frame")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(_canonical(frame).decode("utf-8") + "\n")


def aggregate_relation_frames(path: Path, execution_identity: dict[str, Any]) -> dict[str, Any]:
    identity = _identity(execution_identity)
    frames = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not frames:
        raise ValueError("geometry relation plan log has no frames")
    ids: set[str] = set()
    relations: list[dict[str, Any]] = []
    for frame in frames:
        if (not isinstance(frame, dict) or frame.get("schema_version") != FORMAT_RELATION_SCHEMA_VERSION
                or frame.get("execution_identity") != identity):
            raise ValueError("geometry relation frame identity drift")
        relation_id = frame.get("relation", {}).get("id")
        if not isinstance(relation_id, str) or not relation_id or relation_id in ids:
            raise ValueError("geometry relation frame IDs must be unique")
        ids.add(relation_id)
        relations.append(frame["relation"])
    return {
        "schema_version": "docs-format-geometry-relation-frames-1",
        "execution_identity": identity,
        "frame_count": len(frames),
        "plan_log_sha256": sha256(path),
        "relations": sorted(relations, key=lambda item: item["id"]),
    }


def build_surface_recompose_frame(*, execution_identity: dict[str, Any], format_name: str,
                                  adapter: str, semantic_surface_ir: dict[str, Any],
                                  selected_region_ids: list[str], source_evidence_path: Path,
                                  replacement_primitives: list[dict[str, Any]],
                                  preservation: dict[str, str]) -> dict[str, Any]:
    """Bind a producer-owned surface recomposition to generic semantic IR.

    This adapter boundary carries no body text.  It requires every selected semantic
    block to map to a replacement primitive and requires before/after identity hashes
    for content, text order, and the CJK contract to remain equal.
    """
    identity = _identity(execution_identity)
    if format_name not in {"pptx", "svg"} or not isinstance(adapter, str) or not adapter.strip():
        raise ValueError("surface recompose format adapter is unsupported")
    if (not isinstance(semantic_surface_ir, dict)
            or semantic_surface_ir.get("schema_version") != "docs-semantic-surface-ir-1"):
        raise ValueError("surface recompose semantic IR schema mismatch")
    if (not isinstance(selected_region_ids, list) or not selected_region_ids
            or any(not isinstance(item, str) or not item.strip() for item in selected_region_ids)
            or len(set(selected_region_ids)) != len(selected_region_ids)):
        raise ValueError("surface recompose region selection is malformed")
    blocks = semantic_surface_ir.get("semantic_blocks")
    known = {item.get("region_id") for item in blocks if isinstance(item, dict)} if isinstance(blocks, list) else set()
    if not set(selected_region_ids).issubset(known):
        raise ValueError("surface recompose selection contains an ambiguous semantic block")
    source = json.loads(source_evidence_path.read_text(encoding="utf-8"))
    if source.get("format") != format_name or source.get("adapter") != adapter:
        raise ValueError("surface recompose source evidence adapter mismatch")
    mappings: list[dict[str, Any]] = []
    covered: set[str] = set()
    for raw in replacement_primitives:
        if not isinstance(raw, dict) or set(raw) != {"region_id", "primitive_id", "bbox_pdf_points", "source_order"}:
            raise ValueError("surface recompose primitive mapping is malformed")
        region_id, primitive_id, order = raw["region_id"], raw["primitive_id"], raw["source_order"]
        if (region_id not in selected_region_ids or region_id in covered or not isinstance(primitive_id, str)
                or not primitive_id.strip() or isinstance(order, bool) or not isinstance(order, int) or order < 0):
            raise ValueError("surface recompose primitive identity/order is malformed")
        covered.add(region_id)
        mappings.append({"region_id": region_id, "primitive_id": primitive_id,
                         "bbox_pdf_points": _bbox(raw["bbox_pdf_points"]), "source_order": order})
    if covered != set(selected_region_ids):
        raise ValueError("surface recompose primitive mapping is incomplete")
    required_preservation = {
        "content_sha256_before", "content_sha256_after", "text_order_sha256_before",
        "text_order_sha256_after", "cjk_contract_sha256_before", "cjk_contract_sha256_after",
    }
    if not isinstance(preservation, dict) or set(preservation) != required_preservation:
        raise ValueError("surface recompose preservation identity is incomplete")
    for prefix in ("content_sha256", "text_order_sha256", "cjk_contract_sha256"):
        if not _is_hash(preservation[prefix + "_before"]) or preservation[prefix + "_before"] != preservation[prefix + "_after"]:
            raise ValueError(f"surface recompose {prefix} drift")
    requested = {"schema_version": "docs-surface-recompose-requested-1",
                 "selected_region_ids": sorted(selected_region_ids),
                 "semantic_surface_ir_sha256": _hash_bytes(_canonical(semantic_surface_ir))}
    applied = {"schema_version": "docs-surface-recompose-applied-1", "format": format_name,
               "adapter": adapter, "source_evidence_sha256": sha256(source_evidence_path),
               "primitive_mappings": sorted(mappings, key=lambda item: item["source_order"])}
    effective = {"schema_version": "docs-surface-recompose-effective-1",
                 "mapped_region_count": len(mappings), "status": "measured"}
    frame = {"schema_version": SURFACE_RECOMPOSE_SCHEMA_VERSION,
             "execution_identity": identity, "requested": requested, "applied": applied,
             "effective": effective, "preserved": dict(preservation)}
    if not _body_free(frame):
        raise ValueError("surface recompose frame must remain body-free")
    return frame
