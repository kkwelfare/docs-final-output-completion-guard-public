"""Deterministic PDF layout/typography candidate extraction for docs QA.

This module emits only body-free identifiers, page numbers, measurements, and
hash-bound rendered-evidence handles. Japanese wrapping and CJK glyph suitability
remain visual-review candidates; mechanical defects block the enabled receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz

from . import content_blocks, cjk_font_selector, safe_wrap

CIRCLED = frozenset("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")
PARTICLES = frozenset("はがをにへとやもののでからよりまでだけしかでもまた")
PUNCTUATION = frozenset("、。！？）］】」』")
OPENING = frozenset("（［【「『")
CLOSING = frozenset("）］】」』")
REVIEW_CANDIDATE_RULES = frozenset({
    "short_final_line_candidate",
    "split_inside_word_candidate",
    "manual_break_anomaly_candidate",
    "automatic_wrap_anomaly_candidate",
    "cjk_glyph_or_region_appearance_anomaly_candidate",
    "deterministic_coverage_gap_candidate",
    "line_spacing_candidate",
    "content_block_spacing_candidate",
    "content_block_intra_spacing_candidate",
    "content_block_mapping_ambiguous",
    "content_block_vertical_association_unspecified",
})
LINE_SPACING_DEFECTS = frozenset({
    "line_spacing_overflow", "line_spacing_clipping", "line_spacing_overlap",
    "line_spacing_text_reflow_mismatch", "line_spacing_vertical_distribution_block",
    "line_spacing_contract_limit_exceeded", "line_spacing_preservation_mismatch",
})


def _is_bold(flags: int) -> bool:
    return bool(flags & 16)  # PDF span bold flag


def _norm_font(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _cjk(text: str) -> bool:
    return any("\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" for ch in text)


def _gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float]:
    horizontal = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    vertical = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return horizontal, vertical


def _union_bbox(items: list[dict[str, Any]]) -> list[float]:
    boxes = [item["bbox"] for item in items]
    return [round(min(box[0] for box in boxes), 3), round(min(box[1] for box in boxes), 3), round(max(box[2] for box in boxes), 3), round(max(box[3] for box in boxes), 3)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_records(manifest: Path | None) -> dict[int, dict[str, Any]]:
    if manifest is None or not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    records: dict[int, dict[str, Any]] = {}
    for item in data.get("renders", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get("page"), int):
            continue
        raw_path = item.get("path")
        path = Path(raw_path) if isinstance(raw_path, str) else Path()
        if path.is_absolute() and path.is_file() and item.get("sha256") == _sha256(path):
            records[item["page"]] = item
    return records


def _attach_render_evidence(issue: dict[str, Any], renders: dict[int, dict[str, Any]]) -> None:
    record = renders.get(issue["page"])
    bbox = issue.get("bbox_pdf_points")
    if record is None or not isinstance(bbox, list):
        return
    scale = 2.0
    issue["render_evidence"] = {
        "path": record["path"],
        "sha256": record["sha256"],
        "page": issue["page"],
        "width": record.get("width"),
        "height": record.get("height"),
        "render_scale": scale,
        "bbox_pdf_points": bbox,
        "bbox_render_pixels": [round(value * scale, 3) for value in bbox],
    }


def _line_hash(value: str) -> str:
    return hashlib.sha256("".join(value.split()).encode("utf-8")).hexdigest()


def _line_spacing_manifest(artifact: Path, policy: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load body-free candidate renders; source mutation remains producer-owned."""
    if not isinstance(policy, dict) or policy.get("enabled") is not True or policy.get("evidence_manifest") is None:
        return [], []
    handle = policy["evidence_manifest"]
    path = Path(str(handle.get("path", ""))) if isinstance(handle, dict) else Path()
    failure = lambda rule, page=1, **extra: {"rule": rule, "page": page, **extra}
    if not path.is_absolute() or not path.is_file() or handle.get("sha256") != _sha256(path):
        return [], [failure("line_spacing_preservation_mismatch", reason_code="evidence_manifest_drift")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], [failure("line_spacing_preservation_mismatch", reason_code="evidence_manifest_malformed")]
    if data.get("schema_version") != "docs-line-spacing-candidates-1" or data.get("artifact_sha256") != _sha256(artifact):
        return [], [failure("line_spacing_preservation_mismatch", reason_code="artifact_identity_drift")]
    targets, maximum = set(policy.get("target_region_ids", [])), float(policy.get("requested_max_ratio", 2.0))
    candidates, defects, measured = [], [], set()
    for raw in data.get("candidates", []):
        if not isinstance(raw, dict) or raw.get("region_id") not in targets or raw.get("role") != "body":
            continue
        page, bbox, render = raw.get("page"), raw.get("bbox_pdf_points"), raw.get("render_evidence")
        numbers = [raw.get(key) for key in ("source_candidate_ratio", "control_ratio", "rendered_ratio_min", "rendered_ratio_max")]
        numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
        if (not isinstance(page, int) or page < 1 or not isinstance(bbox, list) or len(bbox) != 4
                or not all(numeric(value) for value in [*bbox, *numbers])
                or not isinstance(raw.get("line_count"), int) or raw["line_count"] < 0 or not isinstance(render, dict)):
            defects.append(failure("line_spacing_preservation_mismatch", reason_code="candidate_evidence_malformed")); continue
        source_ratio, control_ratio, low, high = map(float, numbers)
        allowed_ratios = {float(value) for value in policy.get("candidate_ratios", [])}
        region_hash = hashlib.sha256(str(raw["region_id"]).encode()).hexdigest()[:16]
        render_path = Path(str(render.get("path", "")))
        if not render_path.is_absolute() or not render_path.is_file() or render.get("sha256") != _sha256(render_path):
            defects.append(failure("line_spacing_preservation_mismatch", page, region_id_hash=region_hash, reason_code="render_identity_drift")); continue
        if raw.get("candidate_kind") != "control" and (source_ratio > maximum + 1e-9 or source_ratio not in allowed_ratios):
            defects.append(failure("line_spacing_contract_limit_exceeded", page, region_id_hash=region_hash, source_candidate_ratio=source_ratio)); continue
        if raw.get("candidate_kind") != "control" and source_ratio <= control_ratio + 1e-9:
            continue
        preservation = raw.get("preservation_diff")
        if not isinstance(preservation, dict) or any(value is not False for value in preservation.values()):
            defects.append(failure("line_spacing_preservation_mismatch", page, region_id_hash=region_hash)); continue
        raw_defects = raw.get("defects", [])
        if not isinstance(raw_defects, list) or any(item not in LINE_SPACING_DEFECTS for item in raw_defects):
            defects.append(failure("line_spacing_preservation_mismatch", page, region_id_hash=region_hash, reason_code="unknown_defect")); continue
        if raw_defects:
            defects.extend(failure(item, page, region_id_hash=region_hash, source_candidate_ratio=source_ratio) for item in raw_defects); continue
        if raw["line_count"] < 2:
            defects.append(failure("line_spacing_text_reflow_mismatch", page, region_id_hash=region_hash,
                                   source_candidate_ratio=source_ratio)); continue
        key = (region_hash, round(low, 3), round(high, 3))
        if key in measured: continue
        measured.add(key)
        candidates.append({"rule": "line_spacing_candidate", "page": page,
            "bbox_pdf_points": [round(float(value), 3) for value in bbox], "region_id_hash": region_hash,
            "candidate_kind": raw.get("candidate_kind", "trial"), "evidence_label": raw.get("evidence_label", "heuristic"),
            "render_sha256": render.get("sha256"),
            "measurement": {"line_count": raw["line_count"], "control_ratio": control_ratio,
                "source_candidate_ratio": source_ratio, "rendered_ratio_min": low, "rendered_ratio_max": high,
                "requested_max_ratio": maximum}, "preservation_diff": preservation, "render_evidence": render})
    return candidates, defects


def _wrap_plan_records(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a body-free producer JSONL plan and verify its optional declared hash."""
    raw = config.get("wrap_plan_receipt")
    if not isinstance(raw, str) or not raw:
        return [], []
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return [], [{"rule": "wrap_plan_receipt_missing", "page": 1}]
    expected = config.get("wrap_plan_receipt_sha256")
    actual = _sha256(path)
    if isinstance(expected, str) and expected != actual:
        return [], [{"rule": "wrap_plan_receipt_hash_mismatch", "page": 1}]
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("status") != "pass":
                continue
            schema_version = item.get("schema_version")
            if schema_version == "docs-html-pdf-wrap-plan-3":
                spacing_contract = item.get("spacing_contract")
                rendered = item.get("rendered_boundary_check")
                if (not isinstance(spacing_contract, dict) or spacing_contract.get("mode") not in {"enforced", "shadow"}
                        or (spacing_contract.get("mode") == "enforced" and
                            (spacing_contract.get("spacing_pass") is not True or not isinstance(rendered, dict)
                             or rendered.get("spacing_pass") is not True))):
                    return [], [{"rule": "html_pdf_spacing_contract", "page": 1,
                                 "reason_code": "spacing_pass_missing_or_false"}]
            rendered_layout_by_frame: dict[str, dict[str, Any]] = {}
            if schema_version == "docs-html-pdf-wrap-plan-4":
                layout_contract = item.get("layout_contract")
                rendered = item.get("rendered_boundary_check")
                if (not isinstance(layout_contract, dict) or layout_contract.get("mode") != "enforced"
                        or layout_contract.get("relation_sink") != "AQ-RELATION-01"
                        or layout_contract.get("alignment_pass") is not True or layout_contract.get("relation_pass") is not True
                        or not isinstance(rendered, dict) or rendered.get("schema_version") != "docs-html-pdf-rendered-boundaries-2"
                        or rendered.get("alignment_pass") is not True or rendered.get("relation_pass") is not True
                        or rendered.get("relation_sink") != "AQ-RELATION-01"):
                    return [], [{"rule": "html_pdf_layout_relation_contract", "page": 1,
                                 "reason_code": "alignment_or_relation_pass_missing_or_false"}]
                spacing_contract = item.get("spacing_contract")
                if (isinstance(spacing_contract, dict) and spacing_contract.get("mode") == "enforced"
                        and (spacing_contract.get("spacing_pass") is not True or rendered.get("spacing_pass") is not True)):
                    return [], [{"rule": "html_pdf_spacing_contract", "page": 1,
                                 "reason_code": "spacing_pass_missing_or_false"}]
                rendered_layout_by_frame = {
                    str(value.get("frame_id_hash")): value for value in rendered.get("frames", [])
                    if isinstance(value, dict) and isinstance(value.get("frame_id_hash"), str)
                }
            candidates = item.get("frames") if schema_version in {"docs-html-pdf-wrap-plan-2", "docs-html-pdf-wrap-plan-3", "docs-html-pdf-wrap-plan-4"} else [item]
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("status") != "pass":
                    continue
                page = candidate.get("page")
                match = re.search(r"slide-(\d+)", str(candidate.get("frame_id", "")))
                bbox = candidate.get("geometry", {}).get("frame_bbox_pt")
                # OOXML receipts retain slide-N compatibility. HTML producers declare a
                # numeric page explicitly, so every frame traverses the same parity and
                # rendered-boundary verifier without a parallel classifier.
                if not isinstance(page, int) and match:
                    page = int(match.group(1))
                if not isinstance(page, int) or not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                frame_hash = hashlib.sha256(str(candidate.get("frame_id", "")).encode()).hexdigest()[:16]
                rendered_layout = rendered_layout_by_frame.get(frame_hash)
                records.append({**candidate, "page": page,
                                **({"rendered_layout": rendered_layout} if rendered_layout is not None else {})})
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], [{"rule": "wrap_plan_receipt_malformed", "page": 1}]
    prefix = str(config.get("wrap_plan_frame_prefix", ""))
    if prefix:
        records = [item for item in records if str(item.get("frame_id", "")).startswith(prefix)]
    return records, []


def _point_inside(bbox: tuple[float, float, float, float], frame: list[float]) -> bool:
    x, y, width, height = [float(value) for value in frame]
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return x - 0.5 <= center_x <= x + width + 0.5 and y - 0.5 <= center_y <= y + height + 0.5


def _visual_baseline_lines(items: list[dict[str, Any]], tolerance_pt: float = 0.75) -> list[dict[str, Any]]:
    """Coalesce extractor fragments that share one rendered baseline."""
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda value: (float(value["origin_y"]), float(value["bbox"][0]))):
        if groups and abs(float(item["origin_y"]) - float(groups[-1][0]["origin_y"])) <= tolerance_pt:
            groups[-1].append(item)
        else:
            groups.append([item])
    result: list[dict[str, Any]] = []
    for group in groups:
        ordered = sorted(group, key=lambda value: float(value["bbox"][0]))
        boxes = [value["bbox"] for value in ordered]
        result.append({
            **ordered[0],
            "text": "".join(str(value["text"]) for value in ordered),
            "bbox": (
                min(float(box[0]) for box in boxes), min(float(box[1]) for box in boxes),
                max(float(box[2]) for box in boxes), max(float(box[3]) for box in boxes),
            ),
            "origin_y": sum(float(value["origin_y"]) for value in ordered) / len(ordered),
            "font_size": max(float(value.get("font_size") or 0) for value in ordered),
            "chars": [char for value in ordered for char in value.get("chars", [])],
        })
    return result


def _glyph_x(line: dict[str, Any], start_index: int = 0) -> float | None:
    chars = line.get("chars")
    if not isinstance(chars, list) or start_index < 0 or start_index >= len(chars):
        return None
    for item in chars[start_index:]:
        if not isinstance(item, dict) or not str(item.get("c", "")).strip():
            continue
        bbox = item.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            return float(bbox[0])
    return None


def _role_body_start_index(text: str) -> int | None:
    positions = [value for value in (text.find("："), text.find(":")) if value >= 0]
    if not positions:
        return None
    index = min(positions) + 1
    while index < len(text) and text[index].isspace():
        index += 1
    return index if index < len(text) else None


def _plan_parity_issues(page_no: int, blocks: list[dict[str, Any]], records: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    x_tolerance = float(config.get("continuation_x_tolerance_pt", 2.5))
    baseline_min = float(config.get("baseline_font_ratio_min", 1.30))
    baseline_max = float(config.get("baseline_font_ratio_max", 1.42))
    exempt_roles = {
        str(value) for value in config.get("safe_wrap_exempt_roles", [])
        if isinstance(value, str) and value
    }
    for record in (item for item in records if item["page"] == page_no):
        geometry = record["geometry"]
        plan = record.get("plan", {})
        actual = _visual_baseline_lines([
            item for item in blocks if _point_inside(item["bbox"], geometry["frame_bbox_pt"])
        ])
        planned_hashes = plan.get("line_hashes", [])
        actual_hashes = [_line_hash(item["text"]) for item in actual]
        base = {"page": page_no, "frame_id_hash": hashlib.sha256(str(record.get("frame_id", "")).encode()).hexdigest()[:16]}
        role = record.get("role")
        if role in exempt_roles:
            continue
        paragraph_indices = plan.get("line_paragraph_indices")
        if not (isinstance(paragraph_indices, list) and len(paragraph_indices) == len(actual)
                and all(isinstance(value, int) and not isinstance(value, bool) for value in paragraph_indices)):
            paragraph_indices = [0] * len(actual)
        explicit_break_indices = []
        if actual_hashes == planned_hashes:
            explicit_break_indices = [
                index for index in range(1, len(actual))
                if paragraph_indices[index] == paragraph_indices[index - 1]
            ]
        boundary_check = safe_wrap.classify_rendered_line_boundaries(
            [str(item["text"]) for item in actual], paragraph_indices=paragraph_indices,
            explicit_break_indices=explicit_break_indices,
        )
        for boundary in boundary_check["boundaries"]:
            if boundary["allowed"]:
                continue
            issues.append({"rule": "rendered_japanese_boundary", **base,
                           "boundary_index": boundary["boundary_index"],
                           "boundary_kind": boundary["kind"],
                           "reason_codes": boundary["rule_ids"]})
        if actual_hashes != planned_hashes:
            issues.append({"rule": "planned_break_parity", **base, "planned_line_count": len(planned_hashes), "actual_line_count": len(actual_hashes)})
            continue
        continuation_indices = [
            index for index in range(1, len(actual))
            if paragraph_indices[index] == paragraph_indices[index - 1]
        ]
        safe_width = float(geometry.get("safe_width_pt") or 0)
        actual_ratios = [max(0.0, item["bbox"][2] - item["bbox"][0]) / safe_width for item in actual] if safe_width > 0 else []
        continuation_ratios = [actual_ratios[index] for index in continuation_indices]
        if continuation_ratios and any(ratio < 0.30 for ratio in continuation_ratios):
            issues.append({"rule": "rendered_orphan_ratio", **base, "minimum_ratio": round(min(continuation_ratios), 4)})
        if role in {"bullet-list", "role-list"} and len(actual) > 1:
            ratios = []
            for index in continuation_indices:
                previous, current = actual[index - 1], actual[index]
                font_size = float(current.get("font_size") or geometry.get("font_size_pt") or 0)
                if font_size > 0:
                    ratios.append((float(current["origin_y"]) - float(previous["origin_y"])) / font_size)
            if ratios and any(value < baseline_min or value > baseline_max for value in ratios):
                issues.append({"rule": "rendered_baseline_ratio", **base, "minimum": round(min(ratios), 4), "maximum": round(max(ratios), 4)})
        paragraph_indents = plan.get("paragraph_hanging_indents_pt")
        if role == "role-list" and continuation_indices:
            valid_indents = (
                isinstance(paragraph_indents, list)
                and paragraph_indents
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) >= 0 for value in paragraph_indents)
            )
            if not valid_indents:
                issues.append({"rule": "paragraph_hanging_indents_missing", **base})
            else:
                deviations = []
                for paragraph_index in sorted(set(paragraph_indices[index] for index in continuation_indices)):
                    paragraph_lines = [
                        index for index, value in enumerate(paragraph_indices) if value == paragraph_index
                    ]
                    if paragraph_index >= len(paragraph_indents) or len(paragraph_lines) < 2:
                        deviations.append(x_tolerance + 1.0)
                        continue
                    first_index = paragraph_lines[0]
                    body_index = _role_body_start_index(str(actual[first_index]["text"]))
                    first_body_x = _glyph_x(actual[first_index], body_index if body_index is not None else -1)
                    for continuation_index in paragraph_lines[1:]:
                        continuation_x = _glyph_x(actual[continuation_index], 0)
                        if first_body_x is None or continuation_x is None:
                            deviations.append(x_tolerance + 1.0)
                        else:
                            deviations.append(abs(continuation_x - first_body_x))
                if deviations and max(deviations) > x_tolerance:
                    issues.append({"rule": "rendered_continuation_x", **base, "maximum_deviation_pt": round(max(deviations), 4)})
    return issues


def scan_pdf(artifact: Path, config: dict[str, Any], *, render_manifest: Path | None = None) -> dict[str, Any]:
    """Scan a PDF; output contains no extracted text and is safe to receipt."""
    issues: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    min_gap = float(config.get("minimum_gap_pt", config.get("minimum_gap_px", 0)))
    measurement_basis = "pdf_points"
    dead_space = config.get("dead_space") if isinstance(config.get("dead_space"), dict) else {}
    expected_value = config.get("expected_cjk_fonts")
    expected_declared = (
        isinstance(expected_value, list)
        and bool(expected_value)
        and all(isinstance(item, str) and bool(item.strip()) for item in expected_value)
    )
    expected_fonts = [_norm_font(x) for x in expected_value] if expected_declared else []
    short_limit = max(1, int(config.get("short_final_line_max_chars", 4)))
    auto_page_ratio = float(config.get("automatic_wrap_page_ratio", 0.60))
    narrow_card_ratio = float(config.get("narrow_card_exemption_ratio", 0.40))
    wrap_candidates_enabled = config.get("required") is True or config.get("wrap_candidates_required") is True
    cjk_appearance_regions = config.get("cjk_appearance_regions", [])
    if not isinstance(cjk_appearance_regions, list):
        cjk_appearance_regions = []
    coverage_gaps = config.get("deterministic_coverage_gaps", [])
    if not isinstance(coverage_gaps, list):
        coverage_gaps = []
    source_image_count = config.get("source_image_count")
    source_images_absent = isinstance(source_image_count, int) and not isinstance(source_image_count, bool) and source_image_count == 0
    renders = _render_records(render_manifest)
    plan_records, plan_receipt_issues = _wrap_plan_records(config)
    issues.extend(plan_receipt_issues)
    html_pdf_alignment: list[dict[str, Any]] = []
    html_pdf_relations: list[dict[str, Any]] = []
    for record in plan_records:
        rendered_layout = record.get("rendered_layout")
        if not isinstance(rendered_layout, dict):
            continue
        alignment = rendered_layout.get("alignment_check")
        if isinstance(alignment, dict):
            html_pdf_alignment.append({
                "page": record["page"],
                "frame_id_hash": rendered_layout.get("frame_id_hash"),
                "horizontal_center_delta_pt": alignment.get("horizontal_center_delta_pt"),
                "vertical_center_delta_pt": alignment.get("vertical_center_delta_pt"),
                "tolerance_pt": alignment.get("tolerance_pt"),
                "measurement_basis": alignment.get("measurement_basis"),
                "status": "pass" if alignment.get("alignment_pass") is True else "block",
            })
        for relation in rendered_layout.get("relation_checks", []):
            if not isinstance(relation, dict):
                continue
            html_pdf_relations.append({
                "page": record["page"],
                "frame_id_hash": rendered_layout.get("frame_id_hash"),
                "target_id_hash": relation.get("target_id_hash"),
                "target_kind": relation.get("target_kind"),
                "relation": relation.get("relation"),
                "minimum_gap_pt": relation.get("min_gap_pt"),
                "effective_gap_pt": relation.get("effective_gap_pt"),
                "overlap_area_pt2": relation.get("overlap_area_pt2"),
                "measurement_basis": relation.get("measurement_basis"),
                "sink": "AQ-RELATION-01",
                "status": "pass" if relation.get("relation_pass") is True else "block",
            })
    first_cjk_page: int | None = None
    with fitz.open(artifact) as pdf:
        for page_no, page in enumerate(pdf, start=1):
            blocks: list[dict[str, Any]] = []
            for block_i, block in enumerate(page.get_text("rawdict").get("blocks", [])):
                if block.get("type") == 1:
                    # LibreOffice may rasterize deterministic vector/background
                    # shapes into PDF image blocks. When the source audit declares
                    # zero image objects, composition/source geometry already owns
                    # their coverage and these are not unknown visual assets.
                    if source_images_absent:
                        continue
                    bbox = block.get("bbox")
                    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                        issue = {
                            "rule": "deterministic_coverage_gap_candidate",
                            "coverage_kind": "image",
                            "page": page_no,
                            "bbox_pdf_points": [round(float(value), 3) for value in bbox],
                        }
                        _attach_render_evidence(issue, renders)
                        issues.append(issue)
                    continue
                if block.get("type") != 0:
                    continue
                for line_i, line in enumerate(block.get("lines", [])):
                    spans = [s for s in line.get("spans", []) if isinstance(s.get("chars"), list) and s.get("chars")]
                    if not spans:
                        continue
                    chars = [
                        {"c": str(char.get("c", "")), "bbox": tuple(float(value) for value in char.get("bbox", ())) }
                        for span in spans for char in span.get("chars", [])
                        if isinstance(char, dict) and isinstance(char.get("bbox"), (list, tuple)) and len(char.get("bbox")) == 4
                    ]
                    text = "".join(char["c"] for char in chars)
                    if not text:
                        continue
                    bbox = tuple(float(x) for x in line["bbox"])
                    blocks.append({
                        "id": f"p{page_no}-b{block_i}-l{line_i}",
                        "block": f"p{page_no}-b{block_i}",
                        "line": line_i,
                        "bbox": bbox,
                        "font": _norm_font(str(spans[0].get("font", ""))),
                        "font_size": float(spans[0].get("size", 0) or 0),
                        "origin_y": float((spans[0].get("origin") or [bbox[0], bbox[3]])[1]),
                        "bold": all(_is_bold(int(s.get("flags", 0))) for s in spans),
                        "text": text,
                        "chars": chars,
                    })
                    if first_cjk_page is None and _cjk(text):
                        first_cjk_page = page_no

            # Exact collisions remain candidates even when PyMuPDF groups the
            # colliding draws into one block. Minimum-gap checks skip same-block
            # line rhythm so normal lines are not treated as separate objects.
            for i, left in enumerate(blocks):
                for right in blocks[i + 1:]:
                    hx, vy = _gap(left["bbox"], right["bbox"])
                    same_block_line_rhythm = (
                        left["block"] == right["block"]
                        and abs(left["origin_y"] - right["origin_y"]) > 0.5
                    )
                    if hx == 0 and vy == 0 and not same_block_line_rhythm:
                        issues.append({"rule": "overlap", "page": page_no, "items": [left["id"], right["id"]]})
                    elif left["block"] != right["block"] and min_gap > 0 and hx == 0 and 0 < vy < min_gap:
                        issues.append({"rule": "minimum_gap", "page": page_no, "items": [left["id"], right["id"]], "gap_pt": round(vy, 3)})

            by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in blocks:
                by_block[item["block"]].append(item)

            # Mixed family/weight is checked only within a multi-item circled-number
            # procedure list. This excludes intentional heading/body differences.
            for key, items in by_block.items():
                list_items = [item for item in items if item["text"] and item["text"][0] in CIRCLED]
                if len(list_items) < 2:
                    continue
                fonts = {item["font"] for item in list_items}
                weights = {item["bold"] for item in list_items}
                if len(fonts) > 1:
                    issues.append({"rule": "font_family_inconsistent", "page": page_no, "block": key, "font_count": len(fonts)})
                if len(weights) > 1:
                    issues.append({"rule": "font_weight_inconsistent", "page": page_no, "block": key, "weight_count": len(weights)})

            for item in blocks:
                text = item["text"]
                bbox = item["bbox"]
                if bbox[0] < -0.5 or bbox[1] < -0.5 or bbox[2] > float(page.rect.width) + 0.5 or bbox[3] > float(page.rect.height) + 0.5:
                    issues.append({"rule": "clipping_or_out_of_page", "page": page_no, "item": item["id"], "bbox_pdf_points": [round(x, 3) for x in bbox]})
                if text and text[0] in CIRCLED and item["bold"]:
                    issues.append({"rule": "circled_number_bold", "page": page_no, "item": item["id"]})
                if _cjk(text) and expected_fonts and not any(font in item["font"] for font in expected_fonts):
                    issues.append({"rule": "cjk_font_fallback", "page": page_no, "item": item["id"]})
                if "\ufffd" in text:
                    issue = {"rule": "cjk_glyph_or_region_appearance_anomaly_candidate", "page": page_no,
                             "item": item["id"], "bbox_pdf_points": [round(x, 3) for x in bbox]}
                    _attach_render_evidence(issue, renders)
                    issues.append(issue)
                if len(text) == 1 and (text in PARTICLES or text in PUNCTUATION):
                    issues.append({"rule": "orphan_particle_or_punctuation", "page": page_no, "item": item["id"]})
                if text and text[-1] in OPENING:
                    issues.append({"rule": "opening_bracket_at_line_end", "page": page_no, "item": item["id"]})
                if text and text[0] in CLOSING:
                    issues.append({"rule": "closing_bracket_at_line_start", "page": page_no, "item": item["id"]})
                if text and text[0] in CIRCLED and len(text) == 1:
                    issues.append({"rule": "circled_number_separated", "page": page_no, "item": item["id"]})

            for items in by_block.values():
                items.sort(key=lambda item: item["line"])
                block_width = max(item["bbox"][2] for item in items) - min(item["bbox"][0] for item in items)
                labelled_card = (len(items) >= 2 and len(items[0]["text"].strip()) <= 12
                                 and abs(items[0]["bbox"][0] - items[1]["bbox"][0]) > 20)
                intended_bullet = bool(items[0]["text"].lstrip().startswith(("•", "・", "-")))
                if (not wrap_candidates_enabled or len(items) < 2 or all(item["bold"] for item in items)
                        or labelled_card or intended_bullet or block_width < float(page.rect.width) * narrow_card_ratio):
                    continue  # headings, labels, intended bullets, and narrow cards
                for previous, current in zip(items, items[1:]):
                    left = previous["text"].rstrip()
                    right = current["text"].lstrip()
                    left_token = left.rsplit(" ", 1)[-1] if left else ""
                    right_token = right.split(" ", 1)[0] if right else ""
                    # PDF extraction does not preserve authoring wrap intent. Limit
                    # deterministic split-word candidates to two short Latin fragments;
                    # ordinary word-boundary wrapping and no-space Japanese are excluded.
                    if (left_token.isascii() and right_token.isascii() and left_token.isalpha() and right_token.isalpha()
                            and 2 <= len(left_token) <= 4 and 2 <= len(right_token) <= 4
                            and len(left_token) + len(right_token) <= 8):
                        issue = {"rule": "split_inside_word_candidate", "page": page_no, "items": [previous["id"], current["id"]], "bbox_pdf_points": _union_bbox([previous, current])}
                        _attach_render_evidence(issue, renders)
                        issues.append(issue)
                final = items[-1]
                previous = items[-2]
                final_text = final["text"].strip()
                previous_text = previous["text"].strip()
                final_width = max(0.0, final["bbox"][2] - final["bbox"][0])
                previous_width = max(0.0, previous["bbox"][2] - previous["bbox"][0])
                intended_short_sentence = bool(previous_text and previous_text[-1] in "。！？.!?")
                if (not intended_short_sentence and len(final_text) <= short_limit
                        and len(previous_text) >= max(12, short_limit * 2)
                        and final_width <= previous_width * 0.45):
                    bbox = _union_bbox([previous, final])
                    short_issue = {"rule": "short_final_line_candidate", "page": page_no, "items": [previous["id"], final["id"]], "bbox_pdf_points": bbox}
                    _attach_render_evidence(short_issue, renders)
                    issues.append(short_issue)
                    mode_rule = "automatic_wrap_anomaly_candidate" if previous_width >= float(page.rect.width) * auto_page_ratio else "manual_break_anomaly_candidate"
                    mode_issue = {"rule": mode_rule, "page": page_no, "items": [previous["id"], final["id"]], "bbox_pdf_points": bbox}
                    _attach_render_evidence(mode_issue, renders)
                    issues.append(mode_issue)
            for region in cjk_appearance_regions:
                if not isinstance(region, dict) or region.get("page") != page_no:
                    continue
                bbox = region.get("bbox_pdf_points", region.get("bbox"))
                if (not isinstance(bbox, list) or len(bbox) != 4
                        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in bbox)):
                    continue
                issue = {"rule": "cjk_glyph_or_region_appearance_anomaly_candidate", "page": page_no,
                         "bbox_pdf_points": [round(float(value), 3) for value in bbox]}
                _attach_render_evidence(issue, renders)
                issues.append(issue)
            for gap in coverage_gaps:
                if (not isinstance(gap, dict) or gap.get("page") != page_no
                        or gap.get("kind") not in {"image", "table"}):
                    continue
                bbox = gap.get("bbox_pdf_points", gap.get("bbox"))
                if (not isinstance(bbox, list) or len(bbox) != 4
                        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in bbox)):
                    continue
                issue = {
                    "rule": "deterministic_coverage_gap_candidate",
                    "coverage_kind": gap["kind"],
                    "page": page_no,
                    "bbox_pdf_points": [round(float(value), 3) for value in bbox],
                }
                _attach_render_evidence(issue, renders)
                issues.append(issue)
            issues.extend(_plan_parity_issues(page_no, blocks, plan_records, config))
            page_height = float(page.rect.height)
            last_content_bottom = max((float(item["bbox"][3]) for item in blocks), default=0.0)
            bottom_blank_pt = max(0.0, page_height - last_content_bottom)
            bottom_blank_ratio = bottom_blank_pt / page_height if page_height > 0 else 1.0
            ratio_max = dead_space.get("bottom_blank_ratio_max")
            footer_max = dead_space.get("last_content_to_footer_max_pt")
            if (isinstance(ratio_max, (int, float)) and not isinstance(ratio_max, bool)
                    and bottom_blank_ratio > float(ratio_max) + 1e-9):
                issues.append({"rule": "dead_space_bottom_blank", "page": page_no,
                               "bottom_blank_ratio": round(bottom_blank_ratio, 6),
                               "bottom_blank_ratio_max": float(ratio_max)})
            if (isinstance(footer_max, (int, float)) and not isinstance(footer_max, bool)
                    and bottom_blank_pt > float(footer_max) + 0.01):
                issues.append({"rule": "dead_space_last_content_to_footer", "page": page_no,
                               "last_content_to_footer_pt": round(bottom_blank_pt, 3),
                               "last_content_to_footer_max_pt": float(footer_max)})
            pages.append({"page": page_no, "line_count": len(blocks),
                          "measurement_basis": measurement_basis,
                          "page_height_pt": round(page_height, 3),
                          "last_content_bottom_pt": round(last_content_bottom, 3),
                          "bottom_blank_pt": round(bottom_blank_pt, 3),
                          "bottom_blank_ratio": round(bottom_blank_ratio, 6)})
    if first_cjk_page is not None and not expected_declared:
        issues.append({"rule": "expected_cjk_fonts_required", "page": first_cjk_page})
    if first_cjk_page is not None and config.get("cjk_font_evidence_required") is True:
        for reason in cjk_font_selector.validate_artifact_font_evidence(
            config.get("cjk_font_evidence"), artifact, expected_value if expected_declared else []
        ):
            issues.append({"rule": reason, "page": first_cjk_page})
    cjk_contract = config.get("cjk_contract")
    if first_cjk_page is not None and isinstance(cjk_contract, dict) and cjk_contract.get("required") is True:
        if not plan_records:
            issues.append({"rule": "cjk_wrap_plan_coverage_missing", "page": first_cjk_page})
        if not cjk_appearance_regions:
            issues.append({"rule": "cjk_appearance_coverage_missing", "page": first_cjk_page})
    line_policy = config.get("line_spacing_policy")
    line_candidates, line_defects = _line_spacing_manifest(artifact, line_policy)
    line_mode = line_policy.get("mode") if isinstance(line_policy, dict) else None
    line_spacing_evidence = {"enabled": isinstance(line_policy, dict) and line_policy.get("enabled") is True,
                             "mode": line_mode, "candidate_count": len(line_candidates),
                             "defect_count": len(line_defects), "candidates": line_candidates,
                             "defects": line_defects}
    if line_mode == "enforced":
        issues.extend(line_defects)
        issues.extend(line_candidates)
    content_policy = config.get("content_block_policy")
    content_policy_errors = content_blocks.validate_policy(content_policy)
    content_evidence = None
    content_relations: list[dict[str, Any]] = []
    if content_policy_errors:
        issues.extend({"rule": "content_block_preservation_mismatch", "page": 1, "reason_code": error}
                      for error in content_policy_errors)
    elif content_blocks.enabled(content_policy):
        content_evidence, content_issues, content_relations = content_blocks.scan_content_blocks(artifact, content_policy)
        # Content-block candidates use the same hash-bound rendered evidence
        # handles as line-spacing candidates before entering the shared review
        # receipt. Geometry-only evidence remains body-free.
        for item in content_issues:
            if isinstance(item.get("page"), int) and isinstance(item.get("bbox_pdf_points"), list):
                _attach_render_evidence(item, renders)
        issues.extend(content_issues)
    visible_relations = [
        relation for relation in content_relations
        if isinstance(relation, dict) and relation.get("type") in {"content_inset", "nested_inset"}
    ]
    visible_inset_required = config.get("visible_inset_required") is True
    visible_inset_coverage_missing = visible_inset_required and (
        not content_blocks.enabled(content_policy) or not visible_relations
    )
    if visible_inset_coverage_missing:
        issues.append({
            "rule": "content_block_relation_coverage_missing",
            "page": 1,
            "reason_code": (
                "content_block_policy_missing_or_disabled"
                if not content_blocks.enabled(content_policy)
                else "visible_inset_relation_missing"
            ),
        })
    artifact_digest = _sha256(artifact)
    for issue in issues:
        review_candidate = issue["rule"] in REVIEW_CANDIDATE_RULES
        issue["severity"] = "review" if review_candidate else "hard"
        issue["bucket"] = "REVIEW_CANDIDATE" if review_candidate else "HARD_FAIL"
        identity = {key: value for key, value in issue.items() if key not in {"render_evidence", "severity", "id"}}
        seed = json.dumps([artifact_digest, identity], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        issue["id"] = "pdf-" + hashlib.sha256(seed).hexdigest()[:16]
    scan_identity = [{key: value for key, value in issue.items()
                      if key not in {"render_evidence", "scan_hash", "issue_id", "page_index", "bbox", "candidate_type"}}
                     for issue in issues]
    scan_seed = json.dumps([artifact_digest, scan_identity, pages], ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    scan_hash = hashlib.sha256(scan_seed).hexdigest()
    for issue in issues:
        if issue["bucket"] != "REVIEW_CANDIDATE":
            continue
        issue["issue_id"] = issue["id"]
        issue["scan_hash"] = scan_hash
        issue["page_index"] = issue["page"] - 1
        issue["bbox"] = issue.get("bbox_pdf_points")
        issue["candidate_type"] = issue["rule"]
    hard_count = sum(issue["severity"] == "hard" for issue in issues)
    review_count = sum(issue["severity"] == "review" for issue in issues)
    status = "block" if hard_count else "review" if review_count else "pass"
    visible_inset_status = (
        "block" if visible_inset_coverage_missing else
        "not_applicable" if not visible_relations else
        "pass" if all(relation.get("status") == "pass" for relation in visible_relations) else
        "indeterminate" if any(relation.get("status") == "review" for relation in visible_relations) else
        "block"
    )
    return {
        "schema_version": "docs-layout-typography-scan-3",
        "artifact_sha256": artifact_digest,
        "measurement_basis": measurement_basis,
        "scan_hash": scan_hash,
        "status": status,
        "hard_issue_count": hard_count,
        "review_candidate_count": review_count,
        "HARD_FAIL": [issue for issue in issues if issue["bucket"] == "HARD_FAIL"],
        "REVIEW_CANDIDATE": [issue for issue in issues if issue["bucket"] == "REVIEW_CANDIDATE"],
        "issues": issues,
        **({"content_block_evidence": content_evidence} if content_evidence is not None else {}),
        **({"content_block_relations": [*content_relations, *html_pdf_relations]}
           if content_blocks.enabled(content_policy) or html_pdf_relations else {}),
        "visible_inset_status": visible_inset_status,
        **({"html_pdf_alignment": html_pdf_alignment} if html_pdf_alignment else {}),
        **({"line_spacing_evidence": line_spacing_evidence} if line_spacing_evidence["enabled"] else {}),
        "pages": pages,
    }
