"""Deterministic, body-free visible-ink measurement for PDF relations.

The text layer is used only to select a bounded region.  The effective bbox is
computed from pixels in a high-resolution render after vector stroke exclusion.
All coordinates returned to callers are PDF points.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import fitz
from PIL import Image, ImageChops, ImageDraw

SCHEMA_VERSION = "docs-visible-ink-evidence-1"
DEFAULT_RENDER_SCALE = 4.0
DEFAULT_THRESHOLD = 32


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_number(item) for item in value):
        return None
    box = tuple(float(item) for item in value)
    return box if box[2] > box[0] and box[3] > box[1] else None


def _hash_payload(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _pixel_box(box: tuple[float, float, float, float], scale: float, width: int, height: int) -> tuple[int, int, int, int]:
    return (
        max(0, min(width, int(math.floor(box[0] * scale)))),
        max(0, min(height, int(math.floor(box[1] * scale)))),
        max(0, min(width, int(math.ceil(box[2] * scale)))),
        max(0, min(height, int(math.ceil(box[3] * scale)))),
    )


def _union(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    values = list(boxes)
    if not values:
        return None
    return min(x[0] for x in values), min(x[1] for x in values), max(x[2] for x in values), max(x[3] for x in values)


def _text_layer_boxes(page: fitz.Page, crop: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    clip = fitz.Rect(crop)
    boxes: list[tuple[float, float, float, float]] = []
    for block in page.get_text("rawdict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = [
                _bbox(char.get("bbox"))
                for span in line.get("spans", [])
                for char in span.get("chars", [])
                if isinstance(char, dict)
            ]
            char_boxes = [box for box in chars if box is not None]
            if char_boxes:
                boxes.extend(char_boxes)
    return boxes


def _drawing_strokes(page: fitz.Page, crop: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    clip = fitz.Rect(crop)
    result: list[tuple[float, float, float, float]] = []
    for drawing in page.get_drawings():
        if drawing.get("color") is None:
            continue
        rect = drawing.get("rect")
        if rect is None:
            continue
        raw = tuple(float(value) for value in rect)
        width = max(0.25, float(drawing.get("width", 0.0) or 0.0))
        # ``drawing.rect`` is the full envelope for a stroked rectangle.  Erasing
        # that envelope would erase all text inside the cell, so only thin
        # stroke-like drawing envelopes are removed here.
        if raw[2] - raw[0] > width * 4 and raw[3] - raw[1] > width * 4:
            continue
        expanded = fitz.Rect(raw[0] - width, raw[1] - width, raw[2] + width, raw[3] + width)
        intersection = expanded & clip
        box = _bbox(tuple(intersection))
        if box is not None:
            result.append(box)
    return result


def measure_pdf_visible_ink(
    artifact: Path | str,
    *,
    page: int,
    crop_bbox_pdf_points: list[float] | tuple[float, float, float, float],
    source_bbox_pdf_points: list[float] | tuple[float, float, float, float] | None = None,
    occurrence: int = 1,
    render_scale: float = DEFAULT_RENDER_SCALE,
    threshold: int = DEFAULT_THRESHOLD,
    min_confidence: float = 0.80,
    exclude_vector_strokes: bool = True,
    render_path: Path | str | None = None,
) -> dict[str, Any]:
    """Measure visible text ink inside one PDF crop.

    A non-measurable or low-confidence crop returns ``indeterminate`` and never
    fabricates a bbox.  ``render_path`` is optional; when omitted the render is
    hash-bound in memory and no user artifact is written.
    """
    path = Path(artifact).resolve()
    crop = _bbox(crop_bbox_pdf_points)
    source_box = _bbox(source_bbox_pdf_points) if source_bbox_pdf_points is not None else crop
    settings = {
        "render_scale": render_scale,
        "threshold": threshold,
        "min_confidence": min_confidence,
        "exclude_vector_strokes": exclude_vector_strokes,
    }
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact": {"path": str(path), "sha256": sha256(path) if path.is_file() else ""},
        "page": page,
        "occurrence": occurrence,
        "source_bbox_pdf_points": list(source_box) if source_box else None,
        "crop_bbox_pdf_points": list(crop) if crop else None,
        "mask_settings_sha256": _hash_payload(settings),
        "render_scale": render_scale,
        "status": "indeterminate",
        "confidence": 0.0,
    }
    if (not path.is_file() or crop is None or source_box is None or not isinstance(page, int)
            or isinstance(page, bool) or page < 1 or not isinstance(occurrence, int) or occurrence < 1
            or not _number(render_scale) or float(render_scale) < 2.0
            or not isinstance(threshold, int) or isinstance(threshold, bool) or not 1 <= threshold <= 254
            or not _number(min_confidence) or not 0 <= float(min_confidence) <= 1):
        base["ambiguity_reason"] = "visible_ink_input_invalid"
        return base
    try:
        with fitz.open(path) as document:
            if page > len(document):
                base["ambiguity_reason"] = "visible_ink_page_missing"
                return base
            pdf_page = document[page - 1]
            text_boxes = _text_layer_boxes(pdf_page, crop)
            text_layer_bbox = _union(text_boxes)
            if text_layer_bbox is None:
                base["ambiguity_reason"] = "visible_ink_text_layer_missing"
                return base
            pix = pdf_page.get_pixmap(matrix=fitz.Matrix(float(render_scale), float(render_scale)), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            render_bytes = image.tobytes()
            render_sha = hashlib.sha256(render_bytes).hexdigest()
            if render_path is not None:
                output = Path(render_path).resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                image.save(output, format="PNG")
                render_handle: dict[str, Any] = {"path": str(output), "sha256": sha256(output)}
            else:
                render_handle = {"path": None, "sha256": render_sha}
            crop_px = _pixel_box(crop, float(render_scale), image.width, image.height)
            cropped = image.crop(crop_px)
            base["crop_sha256"] = hashlib.sha256(cropped.tobytes()).hexdigest()
            base["render"] = render_handle
            base["crop_bbox_pixels"] = list(crop_px)
            base["text_layer_bbox_pdf_points"] = [round(value, 4) for value in text_layer_bbox]

            allowed = Image.new("1", cropped.size, 0)
            draw = ImageDraw.Draw(allowed)
            for box in text_boxes:
                px = _pixel_box(box, float(render_scale), image.width, image.height)
                draw.rectangle((px[0] - crop_px[0], px[1] - crop_px[1], px[2] - crop_px[0], px[3] - crop_px[1]), fill=1)
            if exclude_vector_strokes:
                for box in _drawing_strokes(pdf_page, crop):
                    px = _pixel_box(box, float(render_scale), image.width, image.height)
                    draw.rectangle((px[0] - crop_px[0], px[1] - crop_px[1], px[2] - crop_px[0], px[3] - crop_px[1]), fill=0)

            rgb = cropped.convert("RGB")
            corners = [rgb.getpixel(point) for point in ((0, 0), (max(0, rgb.width - 1), 0),
                       (0, max(0, rgb.height - 1)), (max(0, rgb.width - 1), max(0, rgb.height - 1)))]
            background_rgb = tuple(sorted(pixel[channel] for pixel in corners)[len(corners) // 2] for channel in range(3))
            background = Image.new("RGB", rgb.size, background_rgb)
            difference = ImageChops.difference(rgb, background).convert("L")
            ink = difference.point(lambda value: 255 if value >= threshold else 0, mode="1")
            ink = ImageChops.logical_and(ink, allowed)
            bbox_px = ink.getbbox()
            allowed_count = max(1, allowed.histogram()[-1])
            ink_count = ink.histogram()[-1]
            confidence = min(1.0, ink_count / max(1.0, allowed_count * 0.025))
            base["confidence"] = round(confidence, 6)
            base["ink_pixel_count"] = ink_count
            if bbox_px is None or ink_count == 0:
                base["ambiguity_reason"] = "visible_ink_mask_empty"
                return base
            visible = (
                crop[0] + bbox_px[0] / float(render_scale),
                crop[1] + bbox_px[1] / float(render_scale),
                crop[0] + bbox_px[2] / float(render_scale),
                crop[1] + bbox_px[3] / float(render_scale),
            )
            base["visible_ink_bbox_pdf_points"] = [round(value, 4) for value in visible]
            if confidence + 1e-9 < float(min_confidence):
                base["ambiguity_reason"] = "visible_ink_confidence_below_threshold"
                return base
            base["status"] = "measured"
            return base
    except Exception as exc:
        base["ambiguity_reason"] = f"visible_ink_measurement_error:{type(exc).__name__}"
        return base


def validate_evidence(value: Any, artifact: Path | str, *, member_id: str | None = None) -> list[str]:
    """Validate live identity and measured/indeterminate evidence semantics."""
    errors: list[str] = []
    path = Path(artifact).resolve()
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return ["visible_ink: unsupported evidence schema"]
    handle = value.get("artifact")
    if (not isinstance(handle, dict) or handle.get("path") != str(path)
            or not path.is_file() or handle.get("sha256") != sha256(path)):
        errors.append("visible_ink: artifact identity drift")
    if member_id is not None and value.get("member_id") != member_id:
        errors.append("visible_ink: member identity drift")
    if not isinstance(value.get("page"), int) or isinstance(value.get("page"), bool) or value.get("page", 0) < 1:
        errors.append("visible_ink: page identity invalid")
    if not isinstance(value.get("occurrence"), int) or isinstance(value.get("occurrence"), bool) or value.get("occurrence", 0) < 1:
        errors.append("visible_ink: occurrence identity invalid")
    for field in ("source_bbox_pdf_points", "crop_bbox_pdf_points", "text_layer_bbox_pdf_points"):
        if _bbox(value.get(field)) is None:
            errors.append(f"visible_ink: {field} invalid")
    status = value.get("status")
    if status not in {"measured", "indeterminate"}:
        errors.append("visible_ink: status invalid")
    if status == "measured":
        if _bbox(value.get("visible_ink_bbox_pdf_points")) is None:
            errors.append("visible_ink: measured bbox missing")
        if not _number(value.get("confidence")) or not 0 <= float(value["confidence"]) <= 1:
            errors.append("visible_ink: confidence invalid")
        if not all(isinstance(value.get(field), str) and len(value[field]) == 64 for field in ("crop_sha256", "mask_settings_sha256")):
            errors.append("visible_ink: crop/mask identity missing")
        render = value.get("render")
        if not isinstance(render, dict) or not isinstance(render.get("sha256"), str) or len(render["sha256"]) != 64:
            errors.append("visible_ink: render identity missing")
        elif render.get("path") is not None:
            render_path = Path(str(render["path"]))
            if not render_path.is_absolute() or not render_path.is_file() or sha256(render_path) != render["sha256"]:
                errors.append("visible_ink: render identity drift")
    elif not isinstance(value.get("ambiguity_reason"), str) or not value["ambiguity_reason"].strip():
        errors.append("visible_ink: indeterminate reason missing")
    return list(dict.fromkeys(errors))
