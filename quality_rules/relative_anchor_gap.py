from __future__ import annotations

"""Generic point-to-visible-envelope anchor-gap contract and measurement."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image

CONTRACT_SCHEMA_VERSION = "docs-relative-anchor-gap-contract-1"
EVIDENCE_SCHEMA_VERSION = "docs-relative-anchor-gap-evidence-1"
MEASUREMENT_MODEL = "point_to_visible_envelope"
IDENTITY_FIELDS = ("contract_id", "policy_hash", "producer_run_id", "artifact_set_id")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _number(value: Any, label: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (positive and result <= 0):
        raise ValueError(f"{label} is out of range")
    return result


def _point(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must contain two values")
    return (_number(value[0], label), _number(value[1], label))


def _bbox(value: Any, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label} must contain four values")
    result = tuple(_number(item, label) for item in value)
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError(f"{label} must have positive area")
    return result  # type: ignore[return-value]


def normalize_direction(value: Any) -> tuple[float, float]:
    x, y = _point(value, "direction_vector")
    length = math.hypot(x, y)
    if length <= 1e-12:
        raise ValueError("direction_vector must be non-zero")
    return (x / length, y / length)


def validate_execution_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise ValueError("relative anchor-gap execution identity must contain exactly four fields")
    if not _hash(value.get("contract_id")) or not _hash(value.get("policy_hash")):
        raise ValueError("relative anchor-gap contract/policy identity is invalid")
    run = value.get("producer_run_id")
    if isinstance(run, bool) or not isinstance(run, int) or run < 0 or not _hash(value.get("artifact_set_id")):
        raise ValueError("relative anchor-gap run/artifact-set identity is invalid")
    return {key: value[key] for key in IDENTITY_FIELDS}


def canonicalize_contract(declaration: Any) -> dict[str, Any]:
    if not isinstance(declaration, dict):
        raise ValueError("relative anchor-gap declaration must be an object")
    required = {
        "mode", "axis", "shared_container", "containment_tolerance_pt", "measurement_model",
        "annotation_id", "source_annotation_map_sha256", "anchor", "reference", "direction_vector",
        "target_gap_ratio", "tolerance", "no_contact", "contact_tolerance_pt", "facing",
    }
    if set(declaration) != required:
        raise ValueError("relative anchor-gap declaration fields are invalid")
    if declaration.get("mode") != "separate" or declaration.get("axis") != "horizontal":
        raise ValueError("relative anchor-gap initially requires separate horizontal mode")
    if declaration.get("shared_container") not in {"required", "allowed", "forbidden"}:
        raise ValueError("relative anchor-gap shared_container is invalid")
    if declaration.get("measurement_model") != MEASUREMENT_MODEL:
        raise ValueError("relative anchor-gap measurement_model is invalid")
    annotation_id = declaration.get("annotation_id")
    if not isinstance(annotation_id, str) or not annotation_id.strip():
        raise ValueError("relative anchor-gap annotation_id is invalid")
    if not _hash(declaration.get("source_annotation_map_sha256")):
        raise ValueError("relative anchor-gap source annotation hash is invalid")
    anchor, reference = declaration.get("anchor"), declaration.get("reference")
    if (not isinstance(anchor, dict) or set(anchor) != {"member_id", "point_kind"}
            or not isinstance(anchor.get("member_id"), str) or not anchor["member_id"].strip()
            or anchor.get("point_kind") != "tail_tip"):
        raise ValueError("relative anchor-gap anchor is invalid")
    if (not isinstance(reference, dict) or set(reference) != {"member_id", "envelope_kind", "dimension"}
            or not isinstance(reference.get("member_id"), str) or not reference["member_id"].strip()
            or reference.get("envelope_kind") != "visible_bbox"
            or reference.get("dimension") != "horizontal_width"):
        raise ValueError("relative anchor-gap reference is invalid")
    if anchor["member_id"] == reference["member_id"]:
        raise ValueError("relative anchor-gap members must differ")
    direction = normalize_direction(declaration.get("direction_vector"))
    target = _number(declaration.get("target_gap_ratio"), "target_gap_ratio", minimum=0)
    tolerance = _number(declaration.get("tolerance"), "tolerance", minimum=0)
    if target - tolerance < 0 or target + tolerance > 1:
        raise ValueError("relative anchor-gap acceptance range is out of range")
    containment = _number(declaration.get("containment_tolerance_pt"), "containment_tolerance_pt", minimum=0)
    contact = _number(declaration.get("contact_tolerance_pt"), "contact_tolerance_pt", minimum=0)
    if not isinstance(declaration.get("no_contact"), bool):
        raise ValueError("relative anchor-gap no_contact must be boolean")
    if declaration.get("facing") not in {"toward_anchor", "away_from_anchor", "neutral"}:
        raise ValueError("relative anchor-gap facing is invalid")
    return {
        "mode": "separate", "axis": "horizontal", "shared_container": declaration["shared_container"],
        "containment_tolerance_pt": containment, "measurement_model": MEASUREMENT_MODEL,
        "annotation_id": annotation_id.strip(), "source_annotation_map_sha256": declaration["source_annotation_map_sha256"],
        "anchor": {"member_id": anchor["member_id"].strip(), "point_kind": "tail_tip"},
        "reference": {"member_id": reference["member_id"].strip(), "envelope_kind": "visible_bbox", "dimension": "horizontal_width"},
        "direction_vector": [round(direction[0], 12), round(direction[1], 12)],
        "target_gap_ratio": target, "tolerance": tolerance, "no_contact": declaration["no_contact"],
        "contact_tolerance_pt": contact, "facing": declaration["facing"],
    }


def build_contract(declaration: Any) -> dict[str, Any]:
    policy = canonicalize_contract(declaration)
    policy_hash = hashlib.sha256(_canonical(policy)).hexdigest()
    contract = {"schema_version": CONTRACT_SCHEMA_VERSION, "policy": policy, "policy_hash": policy_hash}
    contract["contract_id"] = hashlib.sha256(_canonical(contract)).hexdigest()
    return contract


def normalize_annotation_geometry_ir(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("annotation geometry IR must be an object")
    required = {
        "annotation_id", "page", "bubble_block_id", "avatar_block_id", "tail_tip_pdf_points",
        "visible_avatar_bbox_pdf_points", "direction_vector", "target_gap_ratio", "tolerance",
        "reference_dimension", "facing", "no_contact", "contact_tolerance_pt", "safe_rect_pdf_points",
        "source_annotation_map_sha256",
    }
    if set(value) != required:
        raise ValueError("annotation geometry IR fields are invalid")
    for key in ("annotation_id", "bubble_block_id", "avatar_block_id"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"annotation geometry IR {key} is invalid")
    page = value.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("annotation geometry IR page is invalid")
    if value["bubble_block_id"] == value["avatar_block_id"]:
        raise ValueError("annotation geometry IR members must differ")
    if value.get("reference_dimension") != "visible_bbox_horizontal_width":
        raise ValueError("annotation geometry IR reference_dimension is invalid")
    if not isinstance(value.get("no_contact"), bool) or value.get("facing") not in {"toward_anchor", "away_from_anchor", "neutral"}:
        raise ValueError("annotation geometry IR orientation/contact state is invalid")
    if not _hash(value.get("source_annotation_map_sha256")):
        raise ValueError("annotation geometry IR source hash is invalid")
    tail = _point(value.get("tail_tip_pdf_points"), "tail_tip_pdf_points")
    bbox = _bbox(value.get("visible_avatar_bbox_pdf_points"), "visible_avatar_bbox_pdf_points")
    safe = _bbox(value.get("safe_rect_pdf_points"), "safe_rect_pdf_points")
    direction = normalize_direction(value.get("direction_vector"))
    target = _number(value.get("target_gap_ratio"), "target_gap_ratio", minimum=0)
    tolerance = _number(value.get("tolerance"), "tolerance", minimum=0)
    contact = _number(value.get("contact_tolerance_pt"), "contact_tolerance_pt", minimum=0)
    if target - tolerance < 0 or target + tolerance > 1:
        raise ValueError("annotation geometry IR ratio range is invalid")
    return {
        **{key: value[key] for key in ("annotation_id", "page", "bubble_block_id", "avatar_block_id")},
        "tail_tip_pdf_points": [*tail], "visible_avatar_bbox_pdf_points": [*bbox],
        "direction_vector": [round(direction[0], 12), round(direction[1], 12)],
        "target_gap_ratio": target, "tolerance": tolerance,
        "reference_dimension": "visible_bbox_horizontal_width", "facing": value["facing"],
        "no_contact": value["no_contact"], "contact_tolerance_pt": contact,
        "safe_rect_pdf_points": [*safe], "source_annotation_map_sha256": value["source_annotation_map_sha256"],
    }


def measure_geometry(*, tail_tip: Any, visible_avatar_bbox: Any, direction_vector: Any,
                     target_gap_ratio: Any, tolerance: Any, facing_expected: str,
                     facing_actual: str, no_contact: bool, contact_tolerance_pt: Any,
                     safe_rect: Any | None = None, intersection: bool = False) -> dict[str, Any]:
    anchor = _point(tail_tip, "tail_tip")
    bbox = _bbox(visible_avatar_bbox, "visible_avatar_bbox")
    direction = normalize_direction(direction_vector)
    target = _number(target_gap_ratio, "target_gap_ratio", minimum=0)
    allowed = _number(tolerance, "tolerance", minimum=0)
    contact_tolerance = _number(contact_tolerance_pt, "contact_tolerance_pt", minimum=0)
    if target - allowed < 0 or target + allowed > 1:
        raise ValueError("relative anchor-gap ratio range is invalid")
    if facing_expected not in {"toward_anchor", "away_from_anchor", "neutral"} or facing_actual not in {"toward_anchor", "away_from_anchor", "neutral"}:
        raise ValueError("relative anchor-gap facing metadata is missing or invalid")
    if not isinstance(no_contact, bool) or not isinstance(intersection, bool):
        raise ValueError("relative anchor-gap contact state must be boolean")
    corners = ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3]))
    anchor_projection = anchor[0] * direction[0] + anchor[1] * direction[1]
    projected = [x * direction[0] + y * direction[1] for x, y in corners]
    gap = min(projected) - anchor_projection
    width = bbox[2] - bbox[0]
    if width <= 0:
        raise ValueError("relative anchor-gap avatar width must be positive")
    gap_ratio = gap / width
    failures: list[str] = []
    if gap < 0:
        failures.append("relative_anchor_gap_negative")
    if gap_ratio < target - allowed - 1e-9 or gap_ratio > target + allowed + 1e-9:
        failures.append("relative_anchor_gap_ratio_out_of_range")
    contact = intersection or gap <= contact_tolerance + 1e-9
    if no_contact and contact:
        failures.append("relative_anchor_gap_contact")
    if facing_actual != facing_expected:
        failures.append("relative_anchor_gap_facing_mismatch")
    if safe_rect is not None:
        safe = _bbox(safe_rect, "safe_rect")
        if bbox[0] < safe[0] - 1e-9 or bbox[1] < safe[1] - 1e-9 or bbox[2] > safe[2] + 1e-9 or bbox[3] > safe[3] + 1e-9:
            failures.append("relative_anchor_gap_clip_overflow")
    return {
        "tail_tip_pdf_points": [round(anchor[0], 4), round(anchor[1], 4)],
        "avatar_bbox_pdf_points": [round(item, 4) for item in bbox],
        "avatar_width_pt": round(width, 4),
        "direction_vector": [round(direction[0], 12), round(direction[1], 12)],
        "gap_pt": round(gap, 4), "gap_ratio": round(gap_ratio, 8),
        "target_gap_ratio": target, "tolerance": allowed, "facing": facing_actual,
        "no_contact": not contact, "contact_distance_pt": round(max(0.0, gap), 4),
        "status": "fail" if failures else "pass", "failed_constraints": failures,
    }


def solve_translation(*, tail_tip: Any, visible_avatar_bbox: Any, direction_vector: Any,
                      target_gap_ratio: Any) -> dict[str, Any]:
    anchor = _point(tail_tip, "tail_tip")
    bbox = _bbox(visible_avatar_bbox, "visible_avatar_bbox")
    direction = normalize_direction(direction_vector)
    target = _number(target_gap_ratio, "target_gap_ratio", minimum=0)
    width = bbox[2] - bbox[0]
    desired = target * width
    corners = ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3]))
    current = min(x * direction[0] + y * direction[1] for x, y in corners) - (anchor[0] * direction[0] + anchor[1] * direction[1])
    delta = desired - current
    return {"translation_pdf_points": [round(delta * direction[0], 4), round(delta * direction[1], 4)],
            "desired_gap_pt": round(desired, 4), "avatar_width_pt": round(width, 4)}


def visible_alpha_bbox(image: Image.Image, *, alpha_threshold: int = 1) -> tuple[int, int, int, int]:
    if isinstance(alpha_threshold, bool) or not isinstance(alpha_threshold, int) or not 0 <= alpha_threshold <= 255:
        raise ValueError("alpha_threshold is invalid")
    alpha = image.convert("RGBA").getchannel("A")
    mask = alpha.point(lambda value: 255 if value >= alpha_threshold else 0)
    bbox = mask.getbbox()
    if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("visible alpha envelope is empty")
    return bbox


def measure_png(*, artifact: Path, avatar_search_bbox_pixels: Any, tail_tip_pixels: Any,
                direction_vector: Any, target_gap_ratio: Any, tolerance: Any,
                facing_expected: str, facing_actual: str, no_contact: bool,
                contact_tolerance_pt: Any, points_per_pixel: Any, alpha_threshold: int = 1) -> dict[str, Any]:
    artifact = artifact.resolve(strict=True)
    scale = _number(points_per_pixel, "points_per_pixel", positive=True)
    search = _bbox(avatar_search_bbox_pixels, "avatar_search_bbox_pixels")
    with Image.open(artifact) as image:
        image.load()
        width, height = image.size
        if search[0] < 0 or search[1] < 0 or search[2] > width or search[3] > height:
            raise ValueError("avatar search bbox is outside PNG")
        crop = image.crop(tuple(int(round(item)) for item in search))
        visible = visible_alpha_bbox(crop, alpha_threshold=alpha_threshold)
    bbox_px = (search[0] + visible[0], search[1] + visible[1], search[0] + visible[2], search[1] + visible[3])
    tail_px = _point(tail_tip_pixels, "tail_tip_pixels")
    measured = measure_geometry(
        tail_tip=[tail_px[0] * scale, tail_px[1] * scale],
        visible_avatar_bbox=[item * scale for item in bbox_px], direction_vector=direction_vector,
        target_gap_ratio=target_gap_ratio, tolerance=tolerance, facing_expected=facing_expected,
        facing_actual=facing_actual, no_contact=no_contact, contact_tolerance_pt=contact_tolerance_pt,
    )
    measured.update({"measurement_basis": "png_alpha_mask", "artifact_path": str(artifact),
                     "artifact_sha256": sha256(artifact), "pixel_sha256": hashlib.sha256(Image.open(artifact).convert("RGBA").tobytes()).hexdigest(),
                     "points_per_pixel": scale})
    return measured


def measure_pdf(*, artifact: Path, page: int, avatar_search_bbox_pdf_points: Any,
                tail_tip_pdf_points: Any, direction_vector: Any, target_gap_ratio: Any,
                tolerance: Any, facing_expected: str, facing_actual: str, no_contact: bool,
                contact_tolerance_pt: Any, render_scale: float = 4.0, alpha_threshold: int = 8) -> dict[str, Any]:
    import fitz
    artifact = artifact.resolve(strict=True)
    search = _bbox(avatar_search_bbox_pdf_points, "avatar_search_bbox_pdf_points")
    scale = _number(render_scale, "render_scale", positive=True)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page is invalid")
    with fitz.open(artifact) as document:
        if page > len(document):
            raise ValueError("page is outside PDF")
        pdf_page = document[page - 1]
        rect = fitz.Rect(*search)
        if not pdf_page.rect.contains(rect):
            raise ValueError("avatar search bbox is outside PDF page")
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=True)
        image = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        visible = visible_alpha_bbox(image, alpha_threshold=alpha_threshold)
    bbox = (search[0] + visible[0] / scale, search[1] + visible[1] / scale,
            search[0] + visible[2] / scale, search[1] + visible[3] / scale)
    measured = measure_geometry(
        tail_tip=tail_tip_pdf_points, visible_avatar_bbox=bbox, direction_vector=direction_vector,
        target_gap_ratio=target_gap_ratio, tolerance=tolerance, facing_expected=facing_expected,
        facing_actual=facing_actual, no_contact=no_contact, contact_tolerance_pt=contact_tolerance_pt,
    )
    measured.update({"measurement_basis": "pdf_render_mask", "artifact_path": str(artifact),
                     "artifact_sha256": sha256(artifact), "page": page, "render_scale": scale})
    return measured


def build_evidence(*, execution_identity: Any, annotation_id: str, source_path: Path,
                   source_annotation_map_sha256: str, artifact: Path, format_name: str,
                   requested: dict[str, Any], applied: dict[str, Any], effective: dict[str, Any]) -> dict[str, Any]:
    identity = validate_execution_identity(execution_identity)
    source_path = source_path.resolve(strict=True)
    artifact = artifact.resolve(strict=True)
    if not isinstance(annotation_id, str) or not annotation_id.strip() or not _hash(source_annotation_map_sha256):
        raise ValueError("relative anchor-gap annotation/source identity is invalid")
    if sha256(source_path) != source_annotation_map_sha256:
        raise ValueError("relative anchor-gap source annotation hash drift")
    if format_name not in {"svg", "pptx", "pdf", "png"}:
        raise ValueError("relative anchor-gap evidence format is invalid")
    if not isinstance(requested, dict) or not isinstance(applied, dict) or not isinstance(effective, dict):
        raise ValueError("relative anchor-gap requested/applied/effective records are required")
    record = {
        "schema_version": EVIDENCE_SCHEMA_VERSION, "execution_identity": identity,
        "annotation_id": annotation_id.strip(), "source": {"path": str(source_path), "sha256": source_annotation_map_sha256},
        "artifact": {"path": str(artifact), "sha256": sha256(artifact)}, "format": format_name,
        "requested": requested, "applied": applied, "effective": effective,
    }
    if any(key.lower() in {"text", "body", "content", "payload", "raw"} for key in _walk_keys(record)):
        raise ValueError("relative anchor-gap evidence must remain body-free")
    record["evidence_sha256"] = hashlib.sha256(_canonical(record)).hexdigest()
    return record


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def validate_format_parity(records: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(records, list) or len(records) < 2:
        return ["relative anchor-gap format parity requires at least two records"]
    first = records[0]
    keys = ("execution_identity", "annotation_id", "source")
    formats: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            errors.append("relative anchor-gap format evidence schema mismatch")
            continue
        if any(record.get(key) != first.get(key) for key in keys):
            errors.append("relative anchor-gap format identity drift")
        format_name = record.get("format")
        if format_name in formats:
            errors.append("relative anchor-gap duplicate format evidence")
        formats.add(format_name)
        effective = record.get("effective")
        if not isinstance(effective, dict) or effective.get("status") != "pass":
            errors.append("relative anchor-gap format evidence is not pass")
    return list(dict.fromkeys(errors))
