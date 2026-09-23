"""Independent, hash-bound quality gate for already-rendered PDF artifacts.

This gate deliberately evaluates PDF bytes, not production receipts or source
lineage.  It reuses the candidate's deterministic ``layout_typography``
extractor for the common PDF observations, then adds a small receipt-oriented
measurement layer that gives every finding a stable page/element/region key.
Production provenance (including safe-wrap coverage) is reported separately and
is never converted into a PDF defect.

The command has two modes::

    python artifact_pdf_quality_gate.py scan --pdf input.pdf --output receipt.json
    python artifact_pdf_quality_gate.py verify --pdf input.pdf --receipt receipt.json

A receipt is not a visual certification.  Image regions, ambiguous drawing
layers, meaning/order, and renderer-specific appearance remain explicit review
or unsupported boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fitz

try:
    from quality_rules import layout_typography
except ImportError:  # pragma: no cover - supports direct invocation from elsewhere
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from quality_rules import layout_typography  # type: ignore[no-redef]


SCHEMA_VERSION = "docs-pdf-artifact-quality-gate-1"
POLICY_SCHEMA_VERSION = "docs-pdf-artifact-quality-policy-1"
GATE_ID = "independent-pdf-artifact-quality-v1"
COMMON_REQUIRED_RULES = ("bounds", "clipping", "overlap", "font", "line_geometry")
KNOWN_RULES = frozenset((*COMMON_REQUIRED_RULES, "composition", "image_regions"))
STATUSES = frozenset({"pass", "fail", "review", "unsupported"})
REVIEW_ASSESSMENT_SCHEMA_VERSION = "docs-pdf-review-assessment-1"
REVIEW_MANIFEST_SCHEMA_VERSION = "docs-pdf-review-resolution-1"
FINDING_ID_PATTERN = re.compile(r"^pdf-finding-[a-f0-9]{16}$")
REVIEW_REQUIRED_BINDINGS = (
    "artifact",
    "source_pptx",
    "policy",
    "gate",
    "raw_receipt",
    "target_selection",
    "prior_disposition",
)
REVIEW_REJECT_ON = frozenset({
    "PDF bytes differ",
    "policy file bytes differ",
    "evidence missing or bytes differ",
    "source PPTX or prior disposition differs",
    "finding ID/page/bbox mapping differs",
    "duplicate or omitted target IDs",
})
REVIEW_LIMITATIONS = [
    "Assesses only explicitly bound reviewed occurrences and preserved decisions; this is not a universal quality certificate.",
    "Raw PDF gate findings and statuses remain authoritative; the overlay never waives hard failures or required unsupported checks.",
    "Any bound PDF, policy, source, evidence, prior-disposition, or finding-mapping change requires a fresh assessment; partial reuse is not automatic.",
    "Visual review content is not rerun by this consumer; supplied evidence is verified by exact bytes and schema-level mappings only.",
]

DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": POLICY_SCHEMA_VERSION,
    "required_rules": [*COMMON_REQUIRED_RULES, "composition"],
    "optional_rules": ["image_regions"],
    "rules": {
        "bounds": {"tolerance_pt": 0.5},
        "clipping": {"tolerance_pt": 0.5},
        "overlap": {"minimum_area_pt2": 0.0, "line_tolerance_pt": 0.75},
        "font": {"require_font_name": True, "expected_fonts": []},
        "line_geometry": {"baseline_tolerance_pt": 0.75, "overlap_tolerance_pt": 0.5},
        "composition": {"enabled": True, "container_containment_ratio": 0.98},
        "image_regions": {"enabled": True},
    },
}

CAPABILITIES = {
    "text_bounds": "deterministic PyMuPDF text-line geometry in PDF points",
    "clipping": "deterministic text-line containment against each PDF page",
    "overlap": "deterministic text-line pair intersection, excluding normal same-block line rhythm",
    "font": "deterministic extracted font-name and optional expected-font comparison",
    "line_geometry": "deterministic baseline and line-box measurements",
    "composition": "deterministic vector-drawing containment heuristic plus explicit ambiguous-layer review",
    "image_regions": "image-region identity/bounds only; pixels and semantic appearance are unsupported",
    "production_provenance": "not evaluated by this gate",
}

LIMITS = {
    "meaning_and_information_order": "not evaluated; docs/content checks remain authoritative",
    "renderer_equality": "not required and never used as a quality pass",
    "powerpoint_or_browser_rendering": "not evaluated",
    "image_region_appearance": "review/unsupported without an independent pixel/visual contract",
    "font_glyph_appearance": "font metadata is measured; glyph appearance requires review",
    "safe_wrap_provenance": "unknown unless a separate production receipt is checked by the production gate",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _object_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _handle(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    return {"path": str(resolved), "sha256": sha256(resolved), "size_bytes": resolved.stat().st_size}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _bbox(value: Any) -> list[float] | None:
    if isinstance(value, fitz.Rect):
        value = list(value)
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_number(item) for item in value):
        return None
    return [round(float(item), 3) for item in value]


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection(a: list[float], b: list[float]) -> list[float] | None:
    value = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
    return value if value[2] > value[0] and value[3] > value[1] else None


def _intersection_area(a: list[float], b: list[float]) -> float:
    value = _intersection(a, b)
    return 0.0 if value is None else _area(value)


def _norm_font(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _contains(outer: list[float], inner: list[float], tolerance: float = 0.0) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _is_cjk(value: str) -> bool:
    return any("\u3000" <= char <= "\u9fff" or "\uff00" <= char <= "\uffef" for char in value)


def _status_result(rule: str, status: str, *, required: bool, measured: Any = None,
                   findings: list[dict[str, Any]] | None = None, reason: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid rule status: {status}")
    result: dict[str, Any] = {"rule": rule, "required": required, "status": status}
    if measured is not None:
        result["measured"] = measured
    if findings:
        result["finding_ids"] = [item["finding_id"] for item in findings]
    if reason:
        result["reason"] = reason
    return result


def _finding(rule: str, status: str, *, page: int | None = None, element_id: str | None = None,
             region_id: str | None = None, bbox: list[float] | None = None,
             measurement: dict[str, Any] | None = None, reason: str | None = None,
             related_elements: list[str] | None = None, source: str = "direct-pdf") -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid finding status: {status}")
    identity = {
        "rule": rule,
        "page": page,
        "element_id": element_id,
        "region_id": region_id,
        "bbox_pdf_points": bbox,
        "related_elements": related_elements or [],
    }
    value: dict[str, Any] = {
        "finding_id": "pdf-finding-" + _object_hash(identity)[:16],
        "rule": rule,
        "status": status,
        "source": source,
    }
    if page is not None:
        value["page"] = page
    if element_id is not None:
        value["element_id"] = element_id
    if region_id is not None:
        value["region_id"] = region_id
    if bbox is not None:
        value["bbox_pdf_points"] = bbox
    if related_elements:
        value["related_elements"] = related_elements
    if measurement is not None:
        value["measurement"] = measurement
    if reason:
        value["reason"] = reason
    return value


def _validate_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("PDF quality policy must be a JSON object")
    policy = json.loads(json.dumps(value, ensure_ascii=False))
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError(f"policy schema_version must be {POLICY_SCHEMA_VERSION}")
    required = policy.get("required_rules")
    optional = policy.get("optional_rules", [])
    if not isinstance(required, list) or not required or any(not isinstance(item, str) for item in required):
        raise ValueError("policy required_rules must be a non-empty list of strings")
    if not isinstance(optional, list) or any(not isinstance(item, str) for item in optional):
        raise ValueError("policy optional_rules must be a list of strings")
    enabled = set(required) | set(optional)
    unknown = sorted(enabled - KNOWN_RULES)
    if unknown:
        raise ValueError("policy contains unsupported rule(s): " + ", ".join(unknown))
    missing_common = [rule for rule in COMMON_REQUIRED_RULES if rule not in required]
    if missing_common:
        raise ValueError("common rule(s) cannot be globally disabled: " + ", ".join(missing_common))
    if set(required) & set(optional):
        raise ValueError("a rule cannot be both required and optional")
    rules = policy.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("policy rules must be an object")
    for rule in enabled:
        if rule not in rules or not isinstance(rules[rule], dict):
            raise ValueError(f"policy rule configuration is missing for {rule}")
    for rule in COMMON_REQUIRED_RULES:
        if rules[rule].get("enabled") is False:
            raise ValueError(f"common rule cannot be disabled: {rule}")
    return policy


def load_policy(path: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None:
        policy = _validate_policy(DEFAULT_POLICY)
        return policy, {"path": None, "sha256": _object_hash(policy), "size_bytes": len(_canonical(policy))}
    source = path.resolve(strict=True)
    value = json.loads(source.read_text(encoding="utf-8"))
    policy = _validate_policy(value)
    return policy, _handle(source)


def _policy_rule(policy: dict[str, Any], rule: str) -> tuple[bool, bool, dict[str, Any]]:
    required = rule in policy["required_rules"]
    enabled = required or rule in policy.get("optional_rules", [])
    return enabled, required, dict(policy["rules"].get(rule, {}))


def _extract_pdf(artifact: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """Return text lines, image regions, page metadata, and vector drawings."""
    text_lines: list[dict[str, Any]] = []
    image_regions: list[dict[str, Any]] = []
    pages: dict[int, dict[str, Any]] = {}
    drawings: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with fitz.open(artifact) as document:
        for page_no, page in enumerate(document, start=1):
            page_width, page_height = float(page.rect.width), float(page.rect.height)
            pages[page_no] = {"page": page_no, "width_pt": round(page_width, 3), "height_pt": round(page_height, 3)}
            raw = page.get_text("rawdict")
            for block_index, block in enumerate(raw.get("blocks", [])):
                block_type = block.get("type")
                block_box = _bbox(block.get("bbox"))
                if block_type == 1:
                    image_regions.append({
                        "region_id": f"p{page_no}-image-b{block_index}",
                        "page": page_no,
                        "kind": "image",
                        "bbox_pdf_points": block_box,
                    })
                    continue
                if block_type != 0:
                    continue
                for line_index, line in enumerate(block.get("lines", [])):
                    spans = [span for span in line.get("spans", []) if isinstance(span, dict)]
                    chars = [char for span in spans for char in span.get("chars", []) if isinstance(char, dict)]
                    text = "".join(str(char.get("c", "")) for char in chars)
                    if not text:
                        text = "".join(str(span.get("text", "")) for span in spans)
                    line_box = _bbox(line.get("bbox"))
                    if line_box is None or not text:
                        continue
                    font_names = sorted({str(span.get("font", "")).strip() for span in spans if str(span.get("font", "")).strip()})
                    normalized_fonts = sorted({_norm_font(font) for font in font_names if _norm_font(font)})
                    glyph_boxes = [
                        _bbox(char.get("bbox"))
                        for char in chars
                        if _bbox(char.get("bbox")) is not None
                    ]
                    origin = spans[0].get("origin") if spans else None
                    baseline_y = float(origin[1]) if isinstance(origin, (list, tuple)) and len(origin) > 1 and _number(origin[1]) else line_box[3]
                    text_lines.append({
                        "element_id": f"p{page_no}-text-b{block_index}-l{line_index}",
                        "region_id": f"p{page_no}-text-b{block_index}",
                        "page": page_no,
                        "kind": "text",
                        "block_id": f"p{page_no}-text-b{block_index}",
                        "line_index": line_index,
                        "bbox_pdf_points": line_box,
                        "width_pt": round(max(0.0, line_box[2] - line_box[0]), 3),
                        "height_pt": round(max(0.0, line_box[3] - line_box[1]), 3),
                        "baseline_y_pt": round(baseline_y, 3),
                        "font_names": font_names,
                        "fonts": normalized_fonts,
                        "font_size_pt": round(max((float(span.get("size", 0) or 0) for span in spans), default=0.0), 3),
                        "char_count": len(text),
                        "has_cjk": _is_cjk(text),
                        "replacement_glyph_count": text.count("\ufffd"),
                        "glyph_count": len(glyph_boxes),
                    })
            try:
                for drawing_index, drawing in enumerate(page.get_drawings()):
                    drawing_box = _bbox(drawing.get("rect"))
                    if drawing_box is None:
                        continue
                    drawings[page_no].append({
                        "region_id": f"p{page_no}-drawing-{drawing_index}",
                        "page": page_no,
                        "kind": "drawing",
                        "bbox_pdf_points": drawing_box,
                        "filled": drawing.get("fill") is not None,
                        "stroked": drawing.get("color") is not None,
                        "area_pt2": round(_area(drawing_box), 3),
                    })
            except Exception:
                # Vector drawing extraction is an explicit composition coverage
                # boundary; the text checks remain usable and fail closed at the
                # composition rule if it was required.
                pages[page_no]["drawings_unavailable"] = True
            image_count = len(page.get_images(full=True))
            pages[page_no]["image_count"] = image_count
            pages[page_no]["text_element_count"] = sum(item["page"] == page_no for item in text_lines)
            pages[page_no]["drawing_count"] = len(drawings.get(page_no, []))
            pages[page_no]["image_region_count"] = sum(item["page"] == page_no for item in image_regions)
    return text_lines, image_regions, pages, dict(drawings)


def _bounds_and_clipping(policy: dict[str, Any], text_lines: list[dict[str, Any]], pages: dict[int, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    enabled_bounds, required_bounds, bounds_config = _policy_rule(policy, "bounds")
    enabled_clip, required_clip, clip_config = _policy_rule(policy, "clipping")
    findings_bounds: list[dict[str, Any]] = []
    findings_clip: list[dict[str, Any]] = []
    if not enabled_bounds:
        bounds_result = _status_result("bounds", "unsupported", required=False, reason="policy_disabled")
    elif not text_lines:
        bounds_result = _status_result("bounds", "unsupported", required=required_bounds, reason="no_extractable_text_lines")
    else:
        malformed = [item for item in text_lines if _bbox(item.get("bbox_pdf_points")) is None or item["width_pt"] <= 0 or item["height_pt"] <= 0]
        for item in malformed:
            findings_bounds.append(_finding("bounds", "fail", page=item["page"], element_id=item["element_id"], region_id=item["region_id"], bbox=item.get("bbox_pdf_points"), measurement={"width_pt": item.get("width_pt"), "height_pt": item.get("height_pt")}, reason="non_positive_or_malformed_text_bounds"))
        bounds_result = _status_result("bounds", "fail" if findings_bounds else "pass", required=required_bounds, measured={"text_element_count": len(text_lines), "malformed_count": len(malformed)}, findings=findings_bounds)
    if not enabled_clip:
        clip_result = _status_result("clipping", "unsupported", required=False, reason="policy_disabled")
    elif not text_lines:
        clip_result = _status_result("clipping", "unsupported", required=required_clip, reason="no_extractable_text_lines")
    else:
        tolerance = float(clip_config.get("tolerance_pt", 0.5))
        for item in text_lines:
            page = pages[item["page"]]
            page_box = [0.0, 0.0, page["width_pt"], page["height_pt"]]
            box = item["bbox_pdf_points"]
            if not _contains(page_box, box, tolerance):
                findings_clip.append(_finding("clipping", "fail", page=item["page"], element_id=item["element_id"], region_id=item["region_id"], bbox=box, measurement={"page_width_pt": page["width_pt"], "page_height_pt": page["height_pt"], "tolerance_pt": tolerance}, reason="text_bounds_outside_page"))
        clip_result = _status_result("clipping", "fail" if findings_clip else "pass", required=required_clip, measured={"text_element_count": len(text_lines), "clipped_count": len(findings_clip), "tolerance_pt": tolerance}, findings=findings_clip)
    return bounds_result, clip_result, findings_bounds, findings_clip


def _overlap_rule(policy: dict[str, Any], text_lines: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enabled, required, config = _policy_rule(policy, "overlap")
    if not enabled:
        return _status_result("overlap", "unsupported", required=False, reason="policy_disabled"), []
    if len(text_lines) < 2:
        return _status_result("overlap", "unsupported", required=required, measured={"text_element_count": len(text_lines)}, reason="fewer_than_two_text_lines"), []
    minimum_area = float(config.get("minimum_area_pt2", 0.0))
    line_tolerance = float(config.get("line_tolerance_pt", 0.75))
    findings: list[dict[str, Any]] = []
    pair_count = 0
    for index, left in enumerate(text_lines):
        for right in text_lines[index + 1:]:
            if left["page"] != right["page"]:
                continue
            pair_count += 1
            area = _intersection_area(left["bbox_pdf_points"], right["bbox_pdf_points"])
            same_block_line_rhythm = left["block_id"] == right["block_id"] and abs(left["baseline_y_pt"] - right["baseline_y_pt"]) > line_tolerance
            if area <= minimum_area or same_block_line_rhythm:
                continue
            intersection = _intersection(left["bbox_pdf_points"], right["bbox_pdf_points"])
            findings.append(_finding("overlap", "fail", page=left["page"], region_id=left["region_id"], bbox=intersection, related_elements=sorted([left["element_id"], right["element_id"]]), measurement={"overlap_area_pt2": round(area, 3), "left_bbox_pdf_points": left["bbox_pdf_points"], "right_bbox_pdf_points": right["bbox_pdf_points"], "line_tolerance_pt": line_tolerance}, reason="text_line_intersection"))
    result = _status_result("overlap", "fail" if findings else "pass", required=required, measured={"pair_count": pair_count, "overlap_count": len(findings), "minimum_area_pt2": minimum_area}, findings=findings)
    return result, findings


def _font_rule(policy: dict[str, Any], text_lines: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enabled, required, config = _policy_rule(policy, "font")
    if not enabled:
        return _status_result("font", "unsupported", required=False, reason="policy_disabled"), []
    if not text_lines:
        return _status_result("font", "unsupported", required=required, reason="no_extractable_text_lines"), []
    expected = [_norm_font(str(item)) for item in config.get("expected_fonts", []) if isinstance(item, str) and item.strip()]
    require_name = config.get("require_font_name", True) is True
    findings: list[dict[str, Any]] = []
    missing = 0
    fallback = 0
    for item in text_lines:
        if require_name and not item["fonts"]:
            missing += 1
            findings.append(_finding("font", "unsupported", page=item["page"], element_id=item["element_id"], region_id=item["region_id"], bbox=item["bbox_pdf_points"], measurement={"font_names": item["font_names"]}, reason="font_name_unavailable"))
        if expected and item["has_cjk"] and not any(candidate in font for candidate in expected for font in item["fonts"]):
            fallback += 1
            findings.append(_finding("font", "fail", page=item["page"], element_id=item["element_id"], region_id=item["region_id"], bbox=item["bbox_pdf_points"], measurement={"observed_fonts": item["fonts"], "expected_fonts": expected}, reason="cjk_font_does_not_match_policy"))
        if item["replacement_glyph_count"]:
            findings.append(_finding("font", "review", page=item["page"], element_id=item["element_id"], region_id=item["region_id"], bbox=item["bbox_pdf_points"], measurement={"replacement_glyph_count": item["replacement_glyph_count"]}, reason="replacement_glyph_requires_appearance_review"))
    statuses = {item["status"] for item in findings}
    status = "fail" if "fail" in statuses else "unsupported" if "unsupported" in statuses else "review" if "review" in statuses else "pass"
    result = _status_result("font", status, required=required, measured={"text_element_count": len(text_lines), "missing_font_count": missing, "expected_font_mismatch_count": fallback, "expected_fonts": expected}, findings=findings)
    return result, findings


def _line_geometry_rule(policy: dict[str, Any], text_lines: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    enabled, required, config = _policy_rule(policy, "line_geometry")
    if not enabled:
        return _status_result("line_geometry", "unsupported", required=False, reason="policy_disabled"), [], []
    if not text_lines:
        return _status_result("line_geometry", "unsupported", required=required, reason="no_extractable_text_lines"), [], []
    baseline_tolerance = float(config.get("baseline_tolerance_pt", 0.75))
    overlap_tolerance = float(config.get("overlap_tolerance_pt", 0.5))
    by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in text_lines:
        by_region[item["region_id"]].append(item)
    findings: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    for region_id, lines in sorted(by_region.items()):
        ordered = sorted(lines, key=lambda item: (item["line_index"], item["bbox_pdf_points"][1], item["element_id"]))
        deltas = [round(ordered[index]["baseline_y_pt"] - ordered[index - 1]["baseline_y_pt"], 3) for index in range(1, len(ordered))]
        box_overlaps = [round(_intersection_area(ordered[index - 1]["bbox_pdf_points"], ordered[index]["bbox_pdf_points"]), 3) for index in range(1, len(ordered))]
        measurement = {"region_id": region_id, "page": ordered[0]["page"], "line_count": len(ordered), "baseline_y_pt": [item["baseline_y_pt"] for item in ordered], "baseline_deltas_pt": deltas, "adjacent_overlap_area_pt2": box_overlaps, "baseline_tolerance_pt": baseline_tolerance, "overlap_tolerance_pt": overlap_tolerance}
        measurements.append(measurement)
        for index, delta in enumerate(deltas, start=1):
            if delta <= baseline_tolerance:
                findings.append(_finding("line_geometry", "review", page=ordered[index]["page"], element_id=ordered[index]["element_id"], region_id=region_id, bbox=ordered[index]["bbox_pdf_points"], measurement={"baseline_delta_pt": delta, "previous_element_id": ordered[index - 1]["element_id"], "baseline_tolerance_pt": baseline_tolerance}, reason="non_increasing_or_too_close_baseline_requires_review"))
            elif box_overlaps[index - 1] > overlap_tolerance:
                findings.append(_finding("line_geometry", "fail", page=ordered[index]["page"], element_id=ordered[index]["element_id"], region_id=region_id, bbox=ordered[index]["bbox_pdf_points"], measurement={"adjacent_overlap_area_pt2": box_overlaps[index - 1], "overlap_tolerance_pt": overlap_tolerance}, reason="adjacent_line_boxes_overlap"))
    statuses = {item["status"] for item in findings}
    status = "fail" if "fail" in statuses else "review" if "review" in statuses else "pass"
    result = _status_result("line_geometry", status, required=required, measured={"region_count": len(measurements), "regions": measurements}, findings=findings)
    return result, findings, measurements


def _composition_rules(policy: dict[str, Any], text_lines: list[dict[str, Any]], image_regions: list[dict[str, Any]], pages: dict[int, dict[str, Any]], drawings: dict[int, list[dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    enabled, required, config = _policy_rule(policy, "composition")
    image_enabled, image_required, image_config = _policy_rule(policy, "image_regions")
    findings: list[dict[str, Any]] = []
    drawing_measurements: list[dict[str, Any]] = []
    image_measurements: list[dict[str, Any]] = []
    if enabled:
        if any(page.get("drawings_unavailable") for page in pages.values()):
            composition_result = _status_result("composition", "unsupported", required=required, reason="vector_drawing_extractor_unavailable")
        else:
            containment_ratio = float(config.get("container_containment_ratio", 0.98))
            for page_no, page_drawings in sorted(drawings.items()):
                for drawing in page_drawings:
                    for text in (item for item in text_lines if item["page"] == page_no):
                        overlap = _intersection_area(drawing["bbox_pdf_points"], text["bbox_pdf_points"])
                        if overlap <= 0:
                            continue
                        text_area = max(1.0, _area(text["bbox_pdf_points"]))
                        contained_ratio = overlap / text_area
                        intentional = drawing["filled"] and contained_ratio >= containment_ratio
                        record = {"drawing_region_id": drawing["region_id"], "text_element_id": text["element_id"], "overlap_area_pt2": round(overlap, 3), "text_containment_ratio": round(contained_ratio, 4), "classification": "intentional_container_background_overlap" if intentional else "ambiguous_vector_overlap", "status": "pass" if intentional else "review"}
                        drawing_measurements.append({"page": page_no, **record})
                        if not intentional:
                            findings.append(_finding("composition", "review", page=page_no, element_id=text["element_id"], region_id=drawing["region_id"], bbox=_intersection(drawing["bbox_pdf_points"], text["bbox_pdf_points"]), measurement=record, reason="vector_layer_overlap_requires_composition_review"))
            composition_status = "review" if findings else "pass"
            composition_result = _status_result("composition", composition_status, required=required, measured={"drawing_count": sum(len(value) for value in drawings.values()), "text_drawing_overlap_count": len(drawing_measurements), "ambiguous_overlap_count": len(findings)}, findings=findings)
    else:
        composition_result = _status_result("composition", "unsupported", required=False, reason="policy_disabled")
    if image_enabled:
        if not image_regions and not any(page.get("image_count", 0) for page in pages.values()):
            image_result = _status_result("image_regions", "pass", required=image_required, measured={"image_region_count": 0})
        else:
            for region in image_regions:
                image_measurements.append(region)
                findings.append(_finding("image_regions", "review", page=region["page"], region_id=region["region_id"], bbox=region.get("bbox_pdf_points"), measurement={"kind": "image", "bbox_available": region.get("bbox_pdf_points") is not None}, reason="image_region_appearance_unsupported"))
            missing_bbox = sum(item.get("bbox_pdf_points") is None for item in image_regions)
            # ``get_images(full=True)`` counts repeated resource references, not
            # rendered regions. One or more raw image blocks is the measurable
            # geometry; only a page with resources but no block is unlocated.
            unlocated = sum(1 for page_no in pages if pages[page_no].get("image_count", 0) and not pages[page_no].get("image_region_count", 0))
            if missing_bbox or unlocated:
                findings.append(_finding("image_regions", "unsupported", reason="image_region_geometry_unavailable", measurement={"missing_bbox_count": missing_bbox, "unlocated_image_count": unlocated}))
            image_result = _status_result("image_regions", "unsupported" if missing_bbox or unlocated else "review", required=image_required, measured={"image_region_count": len(image_regions), "missing_bbox_count": missing_bbox, "unlocated_image_count": unlocated}, findings=[item for item in findings if item["rule"] == "image_regions"], reason="pixel_and_semantic_appearance_not_measured")
    else:
        image_result = _status_result("image_regions", "unsupported", required=False, reason="policy_disabled")
    composition_measurements = {"drawings": drawing_measurements, "image_regions": image_measurements}
    return composition_result, image_result, findings, composition_measurements


def _reused_layout_extractor(artifact: Path, policy: dict[str, Any], observed_fonts: list[str]) -> dict[str, Any]:
    """Run the existing candidate extractor without production receipts."""
    config = {
        "required": False,
        "minimum_gap_pt": float(policy["rules"].get("overlap", {}).get("minimum_gap_pt", 0.0)),
    }
    font_config = policy["rules"].get("font", {})
    expected = font_config.get("expected_fonts")
    # The legacy extractor requires an expected-font declaration for CJK pages.
    # When the independent policy intentionally has no expectation, use the
    # fonts actually extracted from this PDF only to suppress that producer-side
    # provenance requirement; the independent font rule still reports metadata
    # and policy mismatches above.
    if not isinstance(expected, list) or not expected:
        expected = observed_fonts or ["Helvetica"]
    config["expected_cjk_fonts"] = expected
    try:
        scan = layout_typography.scan_pdf(artifact, config)
    except Exception as exc:
        return {"module": str(Path(layout_typography.__file__).resolve()), "status": "unsupported", "error": type(exc).__name__, "hard_issue_count": None, "review_candidate_count": None}
    return {
        "module": str(Path(layout_typography.__file__).resolve()),
        "module_sha256": sha256(Path(layout_typography.__file__).resolve()),
        "schema_version": scan.get("schema_version"),
        "status": scan.get("status"),
        "hard_issue_count": scan.get("hard_issue_count"),
        "review_candidate_count": scan.get("review_candidate_count"),
        "scan_hash": scan.get("scan_hash"),
        "measurement_basis": scan.get("measurement_basis"),
    }


def _append_extractor_reviews(findings: list[dict[str, Any]], artifact: Path, policy: dict[str, Any], observed_fonts: list[str]) -> list[dict[str, Any]]:
    """Preserve non-duplicate observations from the shared extractor as review."""
    config = {"required": False}
    expected = policy["rules"].get("font", {}).get("expected_fonts")
    config["expected_cjk_fonts"] = expected if isinstance(expected, list) and expected else (observed_fonts or ["Helvetica"])
    try:
        scan = layout_typography.scan_pdf(artifact, config)
    except Exception:
        return findings
    duplicate = {"overlap", "clipping_or_out_of_page", "font_family_inconsistent", "font_weight_inconsistent", "cjk_font_fallback", "deterministic_coverage_gap_candidate"}
    for issue in scan.get("issues", []):
        if not isinstance(issue, dict) or issue.get("rule") in duplicate:
            continue
        rule = str(issue.get("rule", "layout_typography_observation"))
        page = issue.get("page") if isinstance(issue.get("page"), int) else None
        bbox = _bbox(issue.get("bbox_pdf_points"))
        element_id = str(issue.get("item")) if isinstance(issue.get("item"), str) else None
        related = sorted(str(item) for item in issue.get("items", []) if isinstance(item, str)) if isinstance(issue.get("items"), list) else None
        status = "fail" if issue.get("severity") == "hard" else "review"
        findings.append(_finding("layout_typography." + rule, status, page=page, element_id=element_id, bbox=bbox, related_elements=related, measurement={key: issue[key] for key in ("boundary_index", "gap_pt", "minimum_ratio", "minimum", "maximum") if key in issue}, reason="shared_layout_typography_observation", source="quality_rules.layout_typography"))
    return findings


def _aggregate_status(results: Iterable[dict[str, Any]]) -> str:
    values = list(results)
    if any(item.get("status") == "fail" for item in values):
        return "fail"
    if any(item.get("status") == "unsupported" and item.get("required") is True for item in values):
        return "unsupported"
    if any(item.get("status") == "review" for item in values):
        return "review"
    if any(item.get("status") == "unsupported" for item in values):
        return "unsupported"
    return "pass"


def build_receipt(artifact: Path, *, policy_path: Path | None = None) -> dict[str, Any]:
    artifact = artifact.resolve(strict=True)
    if artifact.suffix.lower() != ".pdf":
        raise ValueError("artifact must be a PDF")
    policy, policy_handle = load_policy(policy_path)
    text_lines, image_regions, pages, drawings = _extract_pdf(artifact)
    bounds_result, clip_result, bounds_findings, clip_findings = _bounds_and_clipping(policy, text_lines, pages)
    overlap_result, overlap_findings = _overlap_rule(policy, text_lines)
    font_result, font_findings = _font_rule(policy, text_lines)
    line_result, line_findings, line_measurements = _line_geometry_rule(policy, text_lines)
    composition_result, image_result, composition_findings, composition_measurements = _composition_rules(policy, text_lines, image_regions, pages, drawings)
    findings = [*bounds_findings, *clip_findings, *overlap_findings, *font_findings, *line_findings, *composition_findings]
    observed_fonts = sorted({font for item in text_lines for font in item.get("font_names", []) if font})
    findings = _append_extractor_reviews(findings, artifact, policy, observed_fonts)
    # Stable ordering makes the finding-set hash reproducible across runs.
    findings.sort(key=lambda item: (item.get("page", 0), item.get("rule", ""), item.get("finding_id", "")))
    rule_results = [bounds_result, clip_result, overlap_result, font_result, line_result, composition_result, image_result]
    rule_results.sort(key=lambda item: item["rule"])
    quality_status = _aggregate_status(rule_results)
    finding_set_hash = _object_hash(findings)
    artifact_handle = _handle(artifact)
    artifact_handle["page_count"] = len(pages)
    measurements = {
        "page_count": len(pages),
        "pages": [pages[key] for key in sorted(pages)],
        "text_elements": text_lines,
        "line_geometry": line_measurements,
        "composition": {"drawing_count": sum(len(value) for value in drawings.values()), "image_region_count": len(image_regions), **composition_measurements},
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "gate_id": GATE_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gate": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
        "artifact": artifact_handle,
        "policy": {"source": policy_handle, "sha256": _object_hash(policy), "snapshot": policy},
        "artifact_quality": {
            "status": quality_status,
            "rule_results": rule_results,
            "findings": findings,
            "finding_set_sha256": finding_set_hash,
            "measurements": measurements,
            "reused_extractors": [_reused_layout_extractor(artifact, policy, observed_fonts)],
            "capabilities": CAPABILITIES,
            "limits": LIMITS,
            "rendering_equality_required": False,
        },
        "production_provenance": {
            "status": "unknown",
            "safe_wrap": "unknown",
            "evaluated": False,
            "reason": "independent PDF gate does not consume production receipts or source lineage",
        },
    }
    return receipt


def _write_json(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_receipt(artifact: Path, receipt_path: Path, *, policy_path: Path | None = None) -> dict[str, Any]:
    artifact = artifact.resolve(strict=True)
    receipt_path = receipt_path.resolve(strict=True)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "receipt_unreadable", "error": type(exc).__name__}
    if not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "receipt_schema_mismatch"}
    try:
        policy, policy_handle = load_policy(policy_path)
        current_hash = sha256(artifact)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "verification_input_invalid", "error": type(exc).__name__}
    expected_artifact = receipt.get("artifact", {})
    if expected_artifact.get("path") != str(artifact) or expected_artifact.get("sha256") != current_hash:
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "artifact_hash_mismatch", "expected_sha256": expected_artifact.get("sha256"), "actual_sha256": current_hash}
    policy_data = receipt.get("policy", {})
    if policy_data.get("sha256") != _object_hash(policy):
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "policy_hash_mismatch", "expected_sha256": policy_data.get("sha256"), "actual_sha256": _object_hash(policy)}
    if policy_handle.get("path") != ((policy_data.get("source") or {}).get("path") if isinstance(policy_data.get("source"), dict) else None):
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "policy_source_mismatch"}
    quality = receipt.get("artifact_quality", {})
    findings = quality.get("findings")
    if not isinstance(findings, list) or quality.get("finding_set_sha256") != _object_hash(findings):
        return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": False, "status": "fail", "reason": "finding_set_hash_mismatch"}
    return {"schema_version": "docs-pdf-artifact-quality-verification-1", "valid": True, "status": "pass", "artifact_sha256": current_hash, "quality_status": quality.get("status"), "finding_count": len(findings)}


class ReviewOverlayError(ValueError):
    """Fail-closed error for a reviewed-disposition overlay."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _review_error(reason: str, **details: Any) -> None:
    raise ReviewOverlayError(reason, **details)


def _review_read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        _review_error(f"missing_{label}")
    if not resolved.is_file():
        _review_error(f"missing_{label}")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _review_error(f"unreadable_{label}", error=type(exc).__name__)
    if not isinstance(value, dict):
        _review_error(f"{label}_must_be_object")
    return value


def _review_binding(value: Any, label: str, *, check_hash: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict):
        _review_error(f"invalid_{label}_binding")
    path_value = value.get("path")
    expected = value.get("sha256")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        _review_error(f"invalid_{label}_path")
    if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
        _review_error(f"invalid_{label}_sha256")
    try:
        resolved = Path(path_value).resolve(strict=True)
    except (OSError, RuntimeError):
        _review_error(f"missing_{label}")
    if not resolved.is_file():
        _review_error(f"missing_{label}")
    actual = sha256(resolved) if check_hash else None
    if check_hash and actual != expected:
        _review_error(f"{label}_hash_mismatch", expected_sha256=expected, actual_sha256=actual)
    return {"path": str(resolved), "sha256": expected, "actual_sha256": actual}


def _review_binding_pair(value: Any, label: str) -> tuple[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not isinstance(value.get("sha256"), str):
        _review_error(f"invalid_{label}_binding")
    return str(Path(value["path"]).resolve()), value["sha256"]


def _review_index_findings(findings: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(findings, list):
        _review_error(f"invalid_{label}_findings")
    indexed: dict[str, dict[str, Any]] = {}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            _review_error(f"invalid_{label}_finding", index=index)
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str) or FINDING_ID_PATTERN.fullmatch(finding_id) is None:
            _review_error(f"invalid_{label}_finding_id", index=index)
        if finding_id in indexed:
            _review_error(f"duplicate_{label}_finding_id", finding_id=finding_id)
        indexed[finding_id] = finding
    return indexed


def _review_rect(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 4 and all(_number(item) for item in value)


def _review_mapping_matches(raw: dict[str, Any], prior: dict[str, Any], *, ignored: frozenset[str] = frozenset()) -> bool:
    """Compare stable raw finding fields; prior adds disposition metadata only."""
    return all(prior.get(key) == value for key, value in raw.items() if key not in ignored)


def _review_failure(reason: str, **details: Any) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_ASSESSMENT_SCHEMA_VERSION,
        "valid": False,
        "status": "reject",
        "reason": reason,
        "details": details,
        "reviewed_disposition_status": "not_assessed",
        "reviewed_disposition": {
            "status": "not_assessed",
            "resolved_count": 0,
            "normal_count": 0,
            "repaired_count": 0,
        },
        "raw_status": None,
        "assessed_scope": {
            "universal_quality_certificate": False,
            "scope": "No reviewed disposition was emitted because a required binding or raw-gate condition failed.",
        },
        "limitations": list(REVIEW_LIMITATIONS),
    }


def assess_review_overlay(artifact: Path, manifest_path: Path, *, policy_path: Path | None = None) -> dict[str, Any]:
    """Verify a supplied review-resolution manifest and assess its disposition overlay.

    The manifest is the only source of review paths.  This routine deliberately
    does not import a private review workspace, rerun visual review, or alter the
    raw receipt.  It verifies every declared byte binding and every prior/current
    finding mapping before emitting a separate disposition assessment.
    """
    try:
        manifest_path = manifest_path.resolve(strict=True)
        manifest = _review_read_json(manifest_path, "review_manifest")
        if manifest.get("schema_version") != REVIEW_MANIFEST_SCHEMA_VERSION:
            _review_error("review_manifest_schema_mismatch")
        missing = [key for key in (*REVIEW_REQUIRED_BINDINGS, "target_count", "resolutions", "preserved_prior_decisions", "invalidation_policy", "raw_gate_status", "reviewed_outcome") if key not in manifest]
        if missing:
            _review_error("review_manifest_missing_fields", fields=missing)

        bindings: dict[str, dict[str, Any]] = {}
        for key in REVIEW_REQUIRED_BINDINGS:
            # The gate binding identifies the implementation that produced the
            # raw receipt. Its path/hash pair must agree with that receipt, but
            # the current consumer is allowed to have a newer implementation.
            bindings[key] = _review_binding(manifest[key], key, check_hash=key != "gate")
        artifact_binding = bindings["artifact"]
        artifact_path = artifact.resolve(strict=True)
        if artifact_path != Path(artifact_binding["path"]):
            _review_error("artifact_path_mismatch")
        if sha256(artifact_path) != artifact_binding["sha256"]:
            _review_error("stale_pdf")

        policy_file = Path(bindings["policy"]["path"])
        if policy_path is not None and policy_path.resolve(strict=True) != policy_file:
            _review_error("policy_path_mismatch")
        policy, policy_handle = load_policy(policy_file)
        if policy_handle["sha256"] != bindings["policy"]["sha256"]:
            _review_error("policy_binding_mismatch")
        effective_policy_sha256 = _object_hash(policy)
        if manifest.get("policy_effective_sha256") != effective_policy_sha256:
            _review_error("policy_effective_hash_mismatch")

        raw_receipt_path = Path(bindings["raw_receipt"]["path"])
        raw_receipt = _review_read_json(raw_receipt_path, "raw_receipt")
        if raw_receipt.get("schema_version") != SCHEMA_VERSION:
            _review_error("raw_receipt_schema_mismatch")
        if _review_binding_pair(raw_receipt.get("artifact"), "raw_receipt_artifact") != _review_binding_pair(manifest["artifact"], "artifact"):
            _review_error("raw_receipt_artifact_mapping_mismatch")
        raw_policy = raw_receipt.get("policy")
        if not isinstance(raw_policy, dict) or _review_binding_pair(raw_policy.get("source"), "raw_receipt_policy_source") != _review_binding_pair(manifest["policy"], "policy"):
            _review_error("raw_receipt_policy_mapping_mismatch")
        if raw_policy.get("sha256") != effective_policy_sha256:
            _review_error("raw_receipt_effective_policy_mismatch")
        if _review_binding_pair(raw_receipt.get("gate"), "raw_receipt_gate") != _review_binding_pair(manifest["gate"], "gate"):
            _review_error("raw_receipt_gate_mapping_mismatch")

        raw_verify = verify_receipt(artifact_path, raw_receipt_path, policy_path=policy_file)
        if not raw_verify.get("valid"):
            _review_error("raw_receipt_invalid", verification=raw_verify)
        quality = raw_receipt.get("artifact_quality")
        if not isinstance(quality, dict):
            _review_error("raw_quality_missing")
        raw_status = quality.get("status")
        if raw_status not in STATUSES:
            _review_error("raw_status_invalid")
        if manifest.get("raw_gate_status") != raw_status:
            _review_error("raw_status_mismatch")
        raw_findings = quality.get("findings")
        raw_by_id = _review_index_findings(raw_findings, "raw")
        declared_raw_count = manifest.get("raw_finding_count", len(raw_by_id))
        if not isinstance(declared_raw_count, int) or declared_raw_count != len(raw_by_id):
            _review_error("raw_finding_count_mismatch")
        rule_results = quality.get("rule_results")
        if not isinstance(rule_results, list):
            _review_error("raw_rule_results_missing")
        for rule_result in rule_results:
            if not isinstance(rule_result, dict) or rule_result.get("status") not in STATUSES:
                _review_error("raw_rule_result_invalid")
            if rule_result.get("required") is True and rule_result.get("status") in {"fail", "unsupported"}:
                _review_error("raw_required_rule_not_eligible", rule=rule_result.get("rule"), status=rule_result.get("status"))
        hard_findings = [item for item in raw_by_id.values() if item.get("status") == "fail"]
        if hard_findings or raw_status == "fail":
            _review_error("raw_hard_failure_present", count=len(hard_findings))

        target_selection = _review_read_json(Path(bindings["target_selection"]["path"]), "target_selection")
        target_ids = target_selection.get("target_ids")
        target_count = manifest.get("target_count")
        if not isinstance(target_count, int) or target_count <= 0 or target_selection.get("count") != target_count or not isinstance(target_ids, list) or len(target_ids) != target_count:
            _review_error("target_count_mismatch")
        if len(set(target_ids)) != len(target_ids):
            _review_error("duplicate_target_ids")
        if any(not isinstance(item, str) or FINDING_ID_PATTERN.fullmatch(item) is None for item in target_ids):
            _review_error("invalid_target_id")

        prior_doc = _review_read_json(Path(bindings["prior_disposition"]["path"]), "prior_disposition")
        prior_by_id = _review_index_findings(prior_doc.get("findings"), "prior")
        repaired_ids = [item.get("repaired_raw_finding_id") for item in prior_by_id.values()]
        if len(set(repaired_ids)) != len(repaired_ids) or set(repaired_ids) != set(raw_by_id):
            _review_error("prior_raw_finding_set_mismatch")
        if prior_doc.get("repaired_finding_set_sha256") != quality.get("finding_set_sha256"):
            _review_error("prior_repaired_finding_set_mismatch")
        for finding_id, prior_finding in prior_by_id.items():
            current_id = prior_finding.get("repaired_raw_finding_id")
            current_finding = raw_by_id.get(current_id)
            if current_finding is None or not _review_mapping_matches(current_finding, prior_finding, ignored=frozenset({"finding_id", "region_id"})):
                _review_error("prior_raw_finding_mapping_changed", finding_id=finding_id)
        unknown_ids = {finding_id for finding_id, item in prior_by_id.items() if item.get("disposition_after") == "unknown"}
        if set(target_ids) != unknown_ids:
            _review_error("target_coverage_mismatch")

        resolutions = manifest.get("resolutions")
        if not isinstance(resolutions, list) or len(resolutions) != target_count:
            _review_error("resolution_count_mismatch")
        resolution_ids = [item.get("original_finding_id") if isinstance(item, dict) else None for item in resolutions]
        current_ids = [item.get("current_finding_id") if isinstance(item, dict) else None for item in resolutions]
        if len(set(resolution_ids)) != len(resolution_ids) or len(set(current_ids)) != len(current_ids):
            _review_error("duplicate_resolution_ids")
        if set(resolution_ids) != set(target_ids):
            _review_error("resolution_target_coverage_mismatch")

        evidence_binding_count = 0
        for index, resolution in enumerate(resolutions):
            if not isinstance(resolution, dict):
                _review_error("invalid_resolution", index=index)
            original_id = resolution.get("original_finding_id")
            current_id = resolution.get("current_finding_id")
            if not isinstance(original_id, str) or not isinstance(current_id, str) or FINDING_ID_PATTERN.fullmatch(original_id) is None or FINDING_ID_PATTERN.fullmatch(current_id) is None:
                _review_error("invalid_resolution_id", index=index)
            original = prior_by_id.get(original_id)
            current = raw_by_id.get(current_id)
            if original is None or current is None:
                _review_error("resolution_finding_missing", index=index)
            if resolution.get("original_disposition") != "unknown":
                _review_error("resolution_original_disposition_changed", finding_id=original_id)
            if resolution.get("verdict") != "intentional_normal":
                _review_error("target_not_resolved_as_normal", finding_id=original_id, verdict=resolution.get("verdict"))
            if not isinstance(resolution.get("reason"), str) or not resolution["reason"].strip():
                _review_error("blank_resolution_reason", finding_id=original_id)
            if resolution.get("current_finding_id") != original.get("repaired_raw_finding_id"):
                _review_error("original_current_mapping_changed", finding_id=original_id)
            if resolution.get("group_id") != original.get("group_id"):
                _review_error("group_mapping_changed", finding_id=original_id)
            context = resolution.get("context")
            if not isinstance(context, dict) or not _review_rect(context.get("bbox_pdf_points")) or not _review_rect(context.get("page_rect_pdf_points")):
                _review_error("resolution_context_invalid", finding_id=original_id)
            if context.get("page") != current.get("page") or context.get("region_id") != current.get("region_id") or context.get("bbox_pdf_points") != current.get("bbox_pdf_points"):
                _review_error("finding_context_mapping_changed", finding_id=original_id)
            if context.get("source_shape") != original.get("source_shape"):
                _review_error("source_shape_mapping_changed", finding_id=original_id)
            if context.get("asset_rgba_sha256") != original.get("rgba_identity_sha256"):
                _review_error("asset_mapping_changed", finding_id=original_id)
            if resolution.get("raw_rule") != current.get("rule") or resolution.get("raw_status") != current.get("status"):
                _review_error("raw_finding_status_mapping_changed", finding_id=original_id)
            evidence = resolution.get("evidence")
            if not isinstance(evidence, list) or len(evidence) < 3:
                _review_error("insufficient_resolution_evidence", finding_id=original_id)
            roles: set[str] = set()
            for evidence_index, evidence_item in enumerate(evidence):
                if not isinstance(evidence_item, dict) or not isinstance(evidence_item.get("role"), str):
                    _review_error("invalid_resolution_evidence", finding_id=original_id, evidence_index=evidence_index)
                roles.add(evidence_item["role"])
                _review_binding(evidence_item, f"evidence_{index}_{evidence_index}")
                evidence_binding_count += 1
            missing_roles = {"page_context", "viewed_contact_sheet", "region_crop"} - roles
            if missing_roles:
                _review_error("resolution_evidence_roles_missing", finding_id=original_id, roles=sorted(missing_roles))

        preserved = manifest.get("preserved_prior_decisions")
        if not isinstance(preserved, list):
            _review_error("preserved_prior_decisions_invalid")
        preserved_ids = [item.get("original_finding_id") if isinstance(item, dict) else None for item in preserved]
        expected_preserved = set(prior_by_id) - set(target_ids)
        if len(set(preserved_ids)) != len(preserved_ids) or set(preserved_ids) != expected_preserved:
            _review_error("preserved_prior_coverage_mismatch")
        disposition_counts: dict[str, int] = defaultdict(int)
        for item in preserved:
            if not isinstance(item, dict):
                _review_error("invalid_preserved_prior_decision")
            original_id = item.get("original_finding_id")
            original = prior_by_id.get(original_id)
            if original is None or item.get("disposition") not in {"intentional", "repaired"}:
                _review_error("invalid_preserved_prior_disposition", finding_id=original_id)
            if item.get("disposition") != original.get("disposition_after") or item.get("current_finding_id") != original.get("repaired_raw_finding_id") or item.get("invalidated") is not False:
                _review_error("preserved_prior_mapping_changed", finding_id=original_id)
            disposition_counts[item["disposition"]] += 1
        invalidation = manifest.get("invalidation_policy")
        if not isinstance(invalidation, dict) or invalidation.get("mode") != "conservative_exact_bytes" or not REVIEW_REJECT_ON.issubset(set(invalidation.get("reject_on", []))):
            _review_error("invalidation_policy_insufficient")
        if manifest.get("reviewed_outcome") != "all_target_occurrences_intentional_normal":
            _review_error("reviewed_outcome_mismatch")

        normal_count = len(resolutions) + disposition_counts.get("intentional", 0)
        repaired_count = disposition_counts.get("repaired", 0)
        resolved_count = normal_count + repaired_count
        preserved_count = len(preserved)
        manifest_sha256 = sha256(manifest_path)
        return {
            "schema_version": REVIEW_ASSESSMENT_SCHEMA_VERSION,
            "valid": True,
            "status": "pass",
            "manifest": {"path": str(manifest_path), "sha256": manifest_sha256},
            "raw_status": raw_status,
            "raw_gate_status": raw_status,
            "raw_finding_count": len(raw_by_id),
            "raw_gate_verification": raw_verify,
            "reviewed_disposition_status": "resolved",
            "reviewed_disposition": {
                "status": "resolved",
                "resolved_count": resolved_count,
                "normal_count": normal_count,
                "repaired_count": repaired_count,
                "resolved_normal_count": normal_count,
                "resolved_repaired_count": repaired_count,
                "target_count": len(resolutions),
                "preserved_prior_count": preserved_count,
                "outcome": manifest["reviewed_outcome"],
            },
            "assessed_scope": {
                "target_occurrences": len(resolutions),
                "preserved_prior_decisions": preserved_count,
                "raw_findings": len(raw_by_id),
                "evidence_bindings_verified": evidence_binding_count,
                "universal_quality_certificate": False,
                "scope": "Only the target occurrences and preserved prior decisions in the supplied manifest were assessed.",
            },
            "limitations": list(REVIEW_LIMITATIONS),
        }
    except ReviewOverlayError as exc:
        return _review_failure(exc.reason, **exc.details)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _review_failure("review_overlay_input_error", error=type(exc).__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent hash-bound PDF artifact quality gate")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    scan = subparsers.add_parser("scan", help="scan one PDF and write a fresh receipt")
    scan.add_argument("--pdf", required=True, type=Path)
    scan.add_argument("--output", required=True, type=Path)
    scan.add_argument("--policy", type=Path)
    verify = subparsers.add_parser("verify", help="verify a receipt against current PDF/policy bytes")
    verify.add_argument("--pdf", required=True, type=Path)
    verify.add_argument("--receipt", required=True, type=Path)
    verify.add_argument("--policy", type=Path)
    review = subparsers.add_parser("review", aliases=["assess-review"], help="verify and assess a supplied hash-bound review overlay")
    review.add_argument("--pdf", required=True, type=Path)
    review.add_argument("--manifest", "--review-manifest", dest="manifest", required=True, type=Path)
    review.add_argument("--policy", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "scan":
            receipt = build_receipt(args.pdf, policy_path=args.policy)
            _write_json(args.output, receipt)
            print(json.dumps({"status": receipt["artifact_quality"]["status"], "receipt": str(args.output.resolve()), "artifact_sha256": receipt["artifact"]["sha256"], "findings": len(receipt["artifact_quality"]["findings"])}, ensure_ascii=False))
            return 0 if receipt["artifact_quality"]["status"] == "pass" else 2
        if args.mode in {"review", "assess-review"}:
            result = assess_review_overlay(args.pdf, args.manifest, policy_path=args.policy)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result.get("valid") else 2
        result = verify_receipt(args.pdf, args.receipt, policy_path=args.policy)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("valid") else 2
    except (OSError, ValueError, fitz.FileDataError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "unsupported", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
