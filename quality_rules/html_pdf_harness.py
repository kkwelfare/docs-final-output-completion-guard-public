"""Canonical HTML-to-PDF safe-wrap producer and rendered-boundary adapter.

Receipts contain hashes, geometry, font decisions, and rule identifiers only.
Clear source text remains in HTML and is never copied into a receipt.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
from pathlib import Path
from typing import Any

import fitz

from quality_rules import execution_contract, rendered_readback, safe_wrap, universal_visible_text_contract


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _line_hash(value: str) -> str:
    return hashlib.sha256("".join(value.split()).encode("utf-8")).hexdigest()


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < float(value) < float("inf")


def _finite_nonnegative(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) < float("inf")


def _padding(value: Any, margins_pt: tuple[float, float]) -> tuple[float, float, float, float]:
    if value is None:
        return 0.0, float(margins_pt[1]), 0.0, float(margins_pt[0])
    if _finite_nonnegative(value):
        return (float(value),) * 4
    if (isinstance(value, (list, tuple)) and len(value) == 4
            and all(_finite_nonnegative(item) for item in value)):
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    raise ValueError("HTML-PDF-ALIGNMENT-CONTRACT-01")


def _layout_declaration(*, role: str, single_line: bool, vertical_align: str | None,
                        horizontal_align: str | None, padding_pt: Any,
                        margins_pt: tuple[float, float], alignment_tolerance_pt: float,
                        relations: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(single_line, bool) or not isinstance(role, str) or not role.strip():
        raise ValueError("HTML-PDF-ALIGNMENT-CONTRACT-01")
    recognized = role in {"pill", "badge", "button", "metric"}
    declared = single_line or vertical_align is not None or horizontal_align is not None or padding_pt is not None
    if recognized and single_line:
        vertical_align = vertical_align or "center"
        horizontal_align = horizontal_align or "center"
        declared = True
    if vertical_align not in {None, "start", "center", "end"} or horizontal_align not in {None, "start", "center", "end"}:
        raise ValueError("HTML-PDF-ALIGNMENT-CONTRACT-01")
    if not _finite_positive(alignment_tolerance_pt):
        raise ValueError("HTML-PDF-ALIGNMENT-CONTRACT-01")
    padding = _padding(padding_pt, margins_pt)
    alignment = None
    if declared:
        alignment = {
            "single_line": single_line,
            "vertical_align": vertical_align or "start",
            "horizontal_align": horizontal_align or "start",
            "padding_pt": [round(item, 4) for item in padding],
            "tolerance_pt": round(float(alignment_tolerance_pt), 4),
        }
    normalized_relations: list[dict[str, Any]] = []
    if relations is not None:
        if not isinstance(relations, list) or not relations:
            raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
        seen: set[str] = set()
        for raw in relations:
            if not isinstance(raw, dict):
                raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
            target_id, target_kind = raw.get("target_id"), raw.get("target_kind")
            bbox, relation, minimum = raw.get("target_bbox_pt"), raw.get("relation"), raw.get("min_gap_pt", 0)
            if (not isinstance(target_id, str) or not target_id.strip() or target_id != target_id.strip()
                    or target_id in seen or target_kind not in {"surface", "image", "shape"}
                    or relation not in {"inside", "outside", "no_overlap"}
                    or not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                    or not all(_finite_nonnegative(item) for item in bbox)
                    or float(bbox[2]) <= 0 or float(bbox[3]) <= 0 or not _finite_nonnegative(minimum)):
                raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
            seen.add(target_id)
            normalized_relations.append({
                "target_id_hash": hashlib.sha256(target_id.encode()).hexdigest()[:16],
                "target_kind": target_kind,
                "target_bbox_pt": [round(float(item), 4) for item in bbox],
                "relation": relation,
                "min_gap_pt": round(float(minimum), 4),
            })
    return alignment, normalized_relations


def normalize_v4_relation(frame: dict[str, Any], relation: dict[str, Any]) -> dict[str, Any]:
    """Map a legacy v4 bbox relation to the additive generic relation vocabulary.

    The adapter is body-free and read-only: it never rewrites a v4 receipt.  It
    intentionally records the legacy radial gap as adapter evidence rather than
    inventing a directional caller contract.
    """
    if (not isinstance(frame, dict) or not isinstance(relation, dict)
            or relation.get("relation") not in {"inside", "outside", "no_overlap"}
            or not isinstance(relation.get("target_id_hash"), str)
            or len(relation["target_id_hash"]) != 16
            or not _finite_nonnegative(relation.get("min_gap_pt"))):
        raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
    frame_hash = hashlib.sha256(str(frame.get("frame_id", "")).encode()).hexdigest()[:16]
    relation_id = hashlib.sha256(f"{frame_hash}:{relation['target_id_hash']}:{relation['relation']}".encode()).hexdigest()[:16]
    mapping = {
        "inside": ("content_inset", "contained"),
        "outside": ("anchor_gap", "separate"),
        "no_overlap": ("layer_relation", "separate"),
    }
    generic_type, mode = mapping[relation["relation"]]
    return {
        "schema_version": "docs-html-v4-generic-relation-adapter-1",
        "relation_id_hash": relation_id,
        "ordered_member_id_hashes": [frame_hash, relation["target_id_hash"]],
        "type": generic_type, "mode": mode,
        "requested_min_gap_pt": round(float(relation["min_gap_pt"]), 4),
        "measurement_basis": "pdf_points",
        "source_adapter": "html-wrap-plan-v4-readonly-1",
    }


def _spacing_declaration(*, role: str, requested_line_step_pt: float | None,
                         applied_line_step_pt: float, spacing_group_id: str | None,
                         spacing_index: int | None, inter_frame_item_step_pt: float | None,
                         inter_frame_item_step_applied_pt: float | None,
                         spacing_tolerance_pt: float, spacing_mode: str) -> dict[str, Any] | None:
    declared = (requested_line_step_pt is not None or spacing_group_id is not None or spacing_index is not None
                or inter_frame_item_step_pt is not None or inter_frame_item_step_applied_pt is not None)
    if not declared:
        return None
    if role not in {"body", "bullet-list"} or spacing_mode not in {"enforced", "shadow"}:
        raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
    if not _finite_positive(requested_line_step_pt) or not _finite_positive(applied_line_step_pt):
        raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
    if not _finite_positive(spacing_tolerance_pt):
        raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
    if float(requested_line_step_pt) < 12.0:
        raise ValueError("HTML-PDF-SPACING-MINIMUM-01")
    result: dict[str, Any] = {
        "mode": spacing_mode,
        "intra_frame_line_step": {
            "requested_pt": round(float(requested_line_step_pt), 4),
            "applied_pt": round(float(applied_line_step_pt), 4),
            "tolerance_pt": round(float(spacing_tolerance_pt), 4),
        },
    }
    applied_item_step = inter_frame_item_step_pt if inter_frame_item_step_applied_pt is None else inter_frame_item_step_applied_pt
    group_values = (spacing_group_id, spacing_index, inter_frame_item_step_pt, applied_item_step)
    if any(value is not None for value in group_values):
        if not isinstance(spacing_group_id, str) or not spacing_group_id.strip():
            raise ValueError("HTML-PDF-SPACING-GROUP-01")
        if not isinstance(spacing_index, int) or isinstance(spacing_index, bool) or spacing_index < 0:
            raise ValueError("HTML-PDF-SPACING-GROUP-01")
        if not _finite_positive(inter_frame_item_step_pt) or not _finite_positive(applied_item_step):
            raise ValueError("HTML-PDF-SPACING-GROUP-01")
        result.update({
            "spacing_group_id": spacing_group_id.strip(),
            "spacing_index": spacing_index,
            "inter_frame_item_step": {
                "requested_pt": round(float(inter_frame_item_step_pt), 4),
                "applied_pt": round(float(applied_item_step), 4),
                "tolerance_pt": round(float(spacing_tolerance_pt), 4),
            },
        })
    return result


def plan_html_frame(text: str, *, frame_id: str, page: int, frame_bbox_pt: tuple[float, float, float, float],
                    margins_pt: tuple[float, float], font_size_pt: float, max_lines: int,
                    max_height_pt: float | None = None, role: str = "body",
                    universal_text_item: dict[str, Any] | None = None,
                    intra_frame_line_step_pt: float | None = None,
                    intra_frame_line_step_applied_pt: float | None = None,
                    spacing_group_id: str | None = None, spacing_index: int | None = None,
                    inter_frame_item_step_pt: float | None = None,
                    inter_frame_item_step_applied_pt: float | None = None,
                    spacing_tolerance_pt: float = 0.75, spacing_mode: str = "enforced",
                    single_line: bool = False, vertical_align: str | None = None,
                    horizontal_align: str | None = None, padding_pt: Any = None,
                    alignment_tolerance_pt: float = 1.0,
                    relations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Plan one visible HTML frame with actual geometry and optional v3 spacing."""
    universal_state = None
    if universal_text_item is not None:
        universal = universal_visible_text_contract.apply_contract([universal_text_item])
        if len(universal["visible_records"]) != 1 or universal["visible_records"][0]["explicit_text"] != text:
            raise ValueError("HTML text differs from universal visible text result")
        record = universal["visible_records"][0]
        universal_state = {"policy_id": universal_visible_text_contract.POLICY_ID,
                           "item_id": record["id"], "status": record["status"],
                           "unsupported_fact_count": record["unsupported_fact_count"]}
    alignment, normalized_relations = _layout_declaration(
        role=role, single_line=single_line, vertical_align=vertical_align,
        horizontal_align=horizontal_align, padding_pt=padding_pt, margins_pt=margins_pt,
        alignment_tolerance_pt=alignment_tolerance_pt, relations=relations,
    )
    _, _, frame_width, frame_height = frame_bbox_pt
    effective_margins = margins_pt
    if alignment is not None:
        padding = alignment["padding_pt"]
        if padding[1] + padding[3] >= frame_width or padding[0] + padding[2] >= frame_height:
            raise ValueError("HTML-PDF-ALIGNMENT-CONTRACT-01")
        effective_margins = (padding[3], padding[1])
    base_result = safe_wrap.producer_preflight(
        text, frame_width=frame_width, margins=effective_margins, font_size=font_size_pt,
        max_lines=max_lines, max_height=max_height_pt,
    )
    applied_line_step = intra_frame_line_step_pt if intra_frame_line_step_applied_pt is None else intra_frame_line_step_applied_pt
    if intra_frame_line_step_applied_pt is not None and intra_frame_line_step_pt is None:
        raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
    if intra_frame_line_step_pt is not None and base_result["status"] == "pass":
        natural_step = float(base_result["line_height"])
        if natural_step <= 0 or not _finite_positive(applied_line_step):
            raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
        result = safe_wrap.producer_preflight(
            text, frame_width=frame_width, margins=effective_margins, font_size=font_size_pt,
            max_lines=max_lines, max_height=max_height_pt,
            line_spacing=float(applied_line_step) / natural_step,
        )
    else:
        result = base_result
    spacing = _spacing_declaration(
        role=role, requested_line_step_pt=intra_frame_line_step_pt,
        applied_line_step_pt=float(result.get("line_height") or 0),
        spacing_group_id=spacing_group_id, spacing_index=spacing_index,
        inter_frame_item_step_pt=inter_frame_item_step_pt,
        inter_frame_item_step_applied_pt=inter_frame_item_step_applied_pt,
        spacing_tolerance_pt=spacing_tolerance_pt, spacing_mode=spacing_mode,
    )
    explicit_lines = result["explicit_text"].split("\n") if result["status"] == "pass" else []
    if alignment is not None and alignment["single_line"] and len(explicit_lines) != 1:
        raise ValueError("HTML-PDF-SINGLE-LINE-01")
    return {
        "status": result["status"], "frame_id": frame_id, "page": page,
        "geometry": {"frame_bbox_pt": list(frame_bbox_pt), "frame_width_pt": frame_width,
                     "margins_pt": list(effective_margins), "safe_width_pt": result["safe_width"],
                     "font_size_pt": font_size_pt, "max_lines": max_lines,
                     "max_height_pt": max_height_pt},
        "plan": {"line_hashes": [_line_hash(line) for line in explicit_lines if line],
                 "line_count": result["line_count"], "widths_pt": result["measured_widths"],
                 "width_ratios": result["width_ratios"], "line_height_pt": result["line_height"],
                 "total_height_pt": result["total_height"], "break_indices": result["break_indices"],
                 "boundary_kinds": result["boundary_kinds"],
                 "line_paragraph_indices": [0] * len(explicit_lines)},
        "font": result["font"],
        "role": role,
        **({"alignment": alignment} if alignment is not None else {}),
        **({"relations": normalized_relations} if normalized_relations else {}),
        **({"spacing": spacing} if spacing is not None else {}),
        **({"universal_text": universal_state} if universal_state is not None else {}),
        "explicit_lines": explicit_lines,
    }


def _font_css(font: dict[str, Any]) -> str:
    font_path = Path(str(font.get("path", "")))
    if not font_path.is_file() or sha256(font_path) != font.get("sha256"):
        raise ValueError("CJK-FONT-UNAVAILABLE-01")
    return "@font-face{font-family:'%s';src:url('file://%s') format('truetype');}" % (
        font["family"], font_path,
    )


def _frame_markup(planned: dict[str, Any], index: int) -> tuple[str, str]:
    font = planned["font"]
    x, y, frame_width, frame_height = planned["geometry"]["frame_bbox_pt"]
    left_margin, right_margin = planned["geometry"]["margins_pt"]
    role = planned.get("role", "body")
    line_height = (universal_visible_text_contract.minimum_line_spacing_points(
        role, planned["plan"]["line_height_pt"],
    ) if role in universal_visible_text_contract.VISIBLE_ROLES else planned["plan"]["line_height_pt"])
    selector = f"#safe-wrap-frame-{index}"
    css = (selector + "{position:absolute;z-index:1;left:%spt;top:%spt;width:%spt;height:%spt;box-sizing:border-box;"
           "padding-left:%spt;padding-right:%spt;font-family:'%s';font-size:%spt;line-height:%spt;}" %
           (x, y, frame_width, frame_height, left_margin, right_margin, font["family"],
            planned["geometry"]["font_size_pt"], line_height))
    alignment = planned.get("alignment")
    if isinstance(alignment, dict):
        top, right, bottom, left = alignment["padding_pt"]
        css += (selector + "{padding:%spt %spt %spt %spt;display:flex;flex-direction:column;"
                "justify-content:%s;align-items:%s;text-align:%s;}" % (
                    top, right, bottom, left,
                    {"start": "flex-start", "center": "center", "end": "flex-end"}[alignment["vertical_align"]],
                    {"start": "flex-start", "center": "center", "end": "flex-end"}[alignment["horizontal_align"]],
                    {"start": "left", "center": "center", "end": "right"}[alignment["horizontal_align"]],
                ))
    lines = "".join('<div class="safe-wrap-line">%s</div>' % html.escape(line)
                    for line in planned.pop("explicit_lines"))
    markup = '<div id="safe-wrap-frame-%s" data-frame-id-hash="%s">%s</div>' % (
        index, hashlib.sha256(str(planned["frame_id"]).encode()).hexdigest()[:16], lines,
    )
    return css, markup


def _surface_markup(frames: list[dict[str, Any]]) -> tuple[str, str]:
    declarations: dict[str, dict[str, Any]] = {}
    for frame in frames:
        for relation in frame.get("relations", []):
            target_hash = relation["target_id_hash"]
            previous = declarations.get(target_hash)
            if (previous is not None and
                    (previous["target_kind"], previous["target_bbox_pt"]) !=
                    (relation["target_kind"], relation["target_bbox_pt"])):
                raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
            declarations[target_hash] = relation
    css_parts, markup_parts = [], []
    for index, relation in enumerate(declarations.values(), start=1):
        x, y, width, height = relation["target_bbox_pt"]
        target_kind = relation["target_kind"]
        selector = f"#safe-wrap-surface-{index}"
        css_parts.append(selector + "{position:absolute;z-index:0;left:%spt;top:%spt;width:%spt;height:%spt;"
                         "box-sizing:border-box;background:#e6e9ed;%s}" % (
                             x, y, width, height,
                             "border:0.5pt solid #c5cbd3;" if target_kind in {"shape", "surface"} else "",
                         ))
        element = "div" if target_kind != "image" else "div"
        markup_parts.append('<%s id="safe-wrap-surface-%s" data-target-id-hash="%s"></%s>' %
                            (element, index, relation["target_id_hash"], element))
    return "".join(css_parts), "".join(markup_parts)


_SEMANTIC_SURFACE_STYLE = {
    "none": {"fill": "transparent", "border": "transparent", "border_width_pt": 0.0},
    "subtle": {"fill": "#f8fafc", "border": "#d7dde5", "border_width_pt": 0.5},
    "standard": {"fill": "#eef2f6", "border": "#9aa4b2", "border_width_pt": 0.75},
    "strong": {"fill": "#e1e7ee", "border": "#5f6b7a", "border_width_pt": 1.0},
}


_SEMANTIC_SURFACE_ELEVATION_STYLE = {
    "none": "none",
    "low": "0 1pt 3pt rgba(31,41,55,.14)",
    "standard": "0 2pt 6pt rgba(31,41,55,.18)",
}


def _semantic_surface_markup(
    *, contract_path: Path, bindings: list[dict[str, Any]], plan_log_path: Path,
    page_size_pt: tuple[float, float],
) -> tuple[str, str]:
    """Resolve explicit contract regions onto body-free HTML primitives.

    The producer reads intent and tier only from the execution contract.  It never
    inspects visible text.  A flat/topology-none region may deliberately name a
    primitive so the rendered QA path can report SURFACE-IDENTITY-01.
    """
    contract, errors = execution_contract.load_contract(contract_path, require_artifacts=False)
    if contract is None:
        raise ValueError("invalid execution contract: " + "; ".join(errors))
    requested = contract["declaration"]["policy"]
    declaration = requested.get("semantic_surface_policy")
    if not isinstance(declaration, dict):
        raise ValueError("execution contract does not declare semantic_surface_policy")
    declared_ids = {item["region_id"] for item in declaration["regions"]}
    if not isinstance(bindings, list) or len(bindings) != len(declared_ids):
        raise ValueError("semantic surface bindings must cover every declared region exactly once")
    plan_log_path = plan_log_path.resolve()
    plan_log_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    css_parts: list[str] = []
    markup_parts: list[str] = []
    seen_regions: set[str] = set()
    seen_primitives: set[str] = set()
    page_width, page_height = map(float, page_size_pt)
    for index, raw in enumerate(bindings, start=1):
        if not isinstance(raw, dict) or set(raw) != {"region_id", "caller_role", "primitive_id", "bbox_pt"}:
            raise ValueError("semantic surface binding is malformed")
        region_id = raw.get("region_id")
        caller_role = raw.get("caller_role")
        primitive_id = raw.get("primitive_id")
        bbox = raw.get("bbox_pt")
        if (not isinstance(region_id, str) or not region_id.strip() or region_id != region_id.strip()
                or region_id in seen_regions or region_id not in declared_ids
                or not isinstance(caller_role, str) or not caller_role.strip() or caller_role != caller_role.strip()
                or primitive_id is not None and (not isinstance(primitive_id, str) or not primitive_id.strip()
                                                  or primitive_id != primitive_id.strip())):
            raise ValueError("semantic surface binding identity is malformed")
        if primitive_id is None:
            if bbox is not None:
                raise ValueError("semantic surface primitive bbox requires primitive identity")
        elif (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
              or not all(_finite_nonnegative(value) for value in bbox)
              or float(bbox[2]) <= 0 or float(bbox[3]) <= 0):
            raise ValueError("semantic surface primitive geometry is malformed")
        seen_regions.add(region_id)
        concrete, applied = execution_contract.resolve_semantic_surface_region(
            requested, region_id=region_id, caller_role=caller_role,
        )
        effective = dict(applied)
        effective["schema_version"] = execution_contract.SEMANTIC_SURFACE_EFFECTIVE_SCHEMA_VERSION
        expected_count = 0 if concrete["topology"] == "none" or concrete["surface_tier"] == "flat" else 1
        hard_failures: list[dict[str, Any]] = []
        actual_count = 0 if primitive_id is None else 1
        if actual_count != expected_count or primitive_id in seen_primitives:
            hard_failures.append({
                "rule_id": "SURFACE-IDENTITY-01", "region_id": region_id,
                "expected_primitive_count": expected_count, "actual_primitive_count": actual_count,
                "duplicate_primitive": bool(primitive_id in seen_primitives),
            })
        bbox_xyxy = None
        style_key = concrete["visual_weight"]
        # A caller-supplied primitive for flat/topology-none is an intentional
        # negative fixture. Render it as a visible neutral primitive so PDF
        # readback can prove the extra identity rather than only trusting DOM.
        if primitive_id is not None and expected_count == 0:
            style_key = "subtle"
        style = _SEMANTIC_SURFACE_STYLE[style_key]
        shadow_applied = concrete["elevation_class"] != "none"
        computed_shadow = _SEMANTIC_SURFACE_ELEVATION_STYLE[concrete["elevation_class"]]
        if primitive_id is not None:
            seen_primitives.add(primitive_id)
            x, y, width, height = map(float, bbox)
            bbox_xyxy = [round(x, 4), round(y, 4), round(x + width, 4), round(y + height, 4)]
            if x < 0 or y < 0 or x + width > page_width or y + height > page_height:
                hard_failures.append({"rule_id": "SURFACE-CONTAINMENT-01", "region_id": region_id,
                                      "primitive_id": primitive_id, "bbox_pt": bbox_xyxy})
            selector = f"#semantic-surface-{index}"
            shadow = "box-shadow:%s;" % computed_shadow
            css_parts.append(
                selector + "{position:absolute;z-index:0;left:%spt;top:%spt;width:%spt;height:%spt;"
                "box-sizing:border-box;background:%s;border:%spt solid %s;border-radius:4pt;%s}" %
                (x, y, width, height, style["fill"], style["border_width_pt"], style["border"], shadow)
            )
            markup_parts.append(
                '<div id="semantic-surface-%s" data-semantic-surface-region="%s" '
                'data-semantic-surface-primitive="%s" data-semantic-surface-tier="%s" '
                'data-semantic-surface-topology="%s"></div>' %
                (index, html.escape(region_id, quote=True), html.escape(primitive_id, quote=True),
                 concrete["surface_tier"], concrete["topology"])
            )
        records.append({
            "schema_version": "docs-html-semantic-surface-plan-log-1",
            "frame_id": f"html-surface:{region_id}", "status": "pass",
            "surface_application": {
                "region_id": region_id, "page_index": concrete["page_index"],
                "primitive_ids": [] if primitive_id is None else [primitive_id],
                "primitive_count": actual_count, "bbox_pt": bbox_xyxy,
                "fill_applied": primitive_id is not None and style["fill"] != "transparent",
                "border_applied": primitive_id is not None and style["border_width_pt"] > 0,
                "shadow_applied": primitive_id is not None and shadow_applied,
                "effect_application": {
                    "adapter": "html-semantic-surface-shadow-1",
                    "ownership_scope": "shadow-only",
                    "selector_hash": hashlib.sha256(f"#semantic-surface-{index}".encode()).hexdigest(),
                    "declared_box_shadow": computed_shadow,
                    "computed_box_shadow": computed_shadow,
                    "computed_status": "source-controlled",
                },
                "hard_failures": hard_failures,
            },
            "execution_contract": {
                "contract_id": contract["contract_id"], "policy_hash": contract["policy_hash"],
                "producer_run_id": contract["producer_run_id"], "requested": requested,
                "selected_token_ids": [concrete["selected_token_id"]],
                "applied": applied, "effective": effective,
            },
        })
    if seen_regions != declared_ids:
        raise ValueError("semantic surface bindings differ from declared regions")
    plan_log_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for record in records), encoding="utf-8",
    )
    return "".join(css_parts), "".join(markup_parts)


def _validate_planned_spacing(frames: list[dict[str, Any]]) -> None:
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for frame in frames:
        spacing = frame.get("spacing")
        if not isinstance(spacing, dict):
            continue
        intra = spacing.get("intra_frame_line_step")
        if not isinstance(intra, dict) or any(not _finite_positive(intra.get(key)) for key in ("requested_pt", "applied_pt", "tolerance_pt")):
            raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
        if abs(float(intra["requested_pt"]) - float(intra["applied_pt"])) > float(intra["tolerance_pt"]) + 1e-9:
            raise ValueError("HTML-PDF-SPACING-PLANNED-DRIFT-01")
        group_id = spacing.get("spacing_group_id")
        if group_id is not None:
            groups.setdefault((int(frame["page"]), str(group_id)), []).append(frame)
    for members in groups.values():
        ordered = sorted(members, key=lambda item: int(item["spacing"]["spacing_index"]))
        indices = [int(item["spacing"]["spacing_index"]) for item in ordered]
        if len(ordered) < 2 or indices != list(range(indices[0], indices[0] + len(indices))):
            raise ValueError("HTML-PDF-SPACING-GROUP-01")
        for previous, current in zip(ordered, ordered[1:]):
            step = current["spacing"].get("inter_frame_item_step")
            if not isinstance(step, dict):
                raise ValueError("HTML-PDF-SPACING-GROUP-01")
            planned = float(current["geometry"]["frame_bbox_pt"][1]) - float(previous["geometry"]["frame_bbox_pt"][1])
            step["planned_effective_pt"] = round(planned, 4)
            if abs(planned - float(step["applied_pt"])) > float(step["tolerance_pt"]) + 1e-9:
                raise ValueError("HTML-PDF-SPACING-PLANNED-DRIFT-01")


def _validate_planned_layout(frames: list[dict[str, Any]]) -> None:
    declared = False
    targets: dict[str, tuple[str, tuple[float, ...]]] = {}
    for frame in frames:
        alignment = frame.get("alignment")
        if isinstance(alignment, dict):
            declared = True
            padding = alignment.get("padding_pt")
            bbox = frame.get("geometry", {}).get("frame_bbox_pt")
            if (alignment.get("single_line") not in {True, False}
                    or alignment.get("vertical_align") not in {"start", "center", "end"}
                    or alignment.get("horizontal_align") not in {"start", "center", "end"}
                    or not isinstance(padding, list) or len(padding) != 4
                    or not all(_finite_nonnegative(item) for item in padding)
                    or not _finite_positive(alignment.get("tolerance_pt"))
                    or not isinstance(bbox, list) or len(bbox) != 4
                    or float(padding[1]) + float(padding[3]) >= float(bbox[2])
                    or float(padding[0]) + float(padding[2]) >= float(bbox[3])):
                raise ValueError("HTML-PDF-ALIGNMENT-CONTRACT-01")
        relations = frame.get("relations")
        if relations is not None:
            declared = True
            if not isinstance(relations, list) or not relations:
                raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
            for relation in relations:
                if (not isinstance(relation, dict) or relation.get("target_kind") not in {"surface", "image", "shape"}
                        or relation.get("relation") not in {"inside", "outside", "no_overlap"}
                        or not isinstance(relation.get("target_id_hash"), str) or len(relation["target_id_hash"]) != 16
                        or not isinstance(relation.get("target_bbox_pt"), list) or len(relation["target_bbox_pt"]) != 4
                        or not all(_finite_nonnegative(item) for item in relation["target_bbox_pt"])
                        or not _finite_nonnegative(relation.get("min_gap_pt"))):
                    raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
                identity = (relation["target_kind"], tuple(map(float, relation["target_bbox_pt"])))
                previous = targets.setdefault(relation["target_id_hash"], identity)
                if previous != identity:
                    raise ValueError("HTML-PDF-RELATION-CONTRACT-01")
    if not declared:
        raise ValueError("HTML-PDF-LAYOUT-CONTRACT-01")


def write_controlled_html_frames(frames: list[dict[str, Any]], html_path: Path, receipt_path: Path,
                                 *, page_size_pt: tuple[float, float] = (612.0, 792.0),
                                 semantic_surface_contract_path: Path | None = None,
                                 semantic_surface_bindings: list[dict[str, Any]] | None = None,
                                 semantic_surface_plan_log_path: Path | None = None) -> dict[str, Any]:
    """Write frame-aware explicit-line HTML and a legacy v2 or enforced v3 plan."""
    if not frames:
        raise ValueError("HTML-PDF-GEOMETRY-01")
    planned_frames = [plan_html_frame(**frame) for frame in frames]
    if any(frame["status"] != "pass" or frame["page"] != 1 for frame in planned_frames):
        raise ValueError("HTML-SAFE-WRAP-BLOCK")
    spacing_declared = any(isinstance(frame.get("spacing"), dict) for frame in planned_frames)
    layout_declared = any(isinstance(frame.get("alignment"), dict) or frame.get("relations") for frame in planned_frames)
    if layout_declared:
        _validate_planned_layout(planned_frames)
    if spacing_declared:
        if not all(isinstance(frame.get("spacing"), dict) for frame in planned_frames):
            raise ValueError("HTML-PDF-SPACING-CONTRACT-01")
        _validate_planned_spacing(planned_frames)
    fonts = {(frame["font"]["family"], frame["font"]["path"], frame["font"]["sha256"])
             for frame in planned_frames}
    if len(fonts) != 1:
        raise ValueError("CJK-FONT-UNAVAILABLE-01")
    font_css = _font_css(planned_frames[0]["font"])
    surface_css, surface_markup = _surface_markup(planned_frames)
    semantic_values = (semantic_surface_contract_path, semantic_surface_bindings, semantic_surface_plan_log_path)
    if any(value is not None for value in semantic_values) and not all(value is not None for value in semantic_values):
        raise ValueError("semantic surfaces require contract, bindings, and plan log together")
    semantic_css, semantic_markup = "", ""
    if semantic_surface_contract_path is not None:
        semantic_css, semantic_markup = _semantic_surface_markup(
            contract_path=semantic_surface_contract_path,
            bindings=semantic_surface_bindings or [],
            plan_log_path=semantic_surface_plan_log_path,  # type: ignore[arg-type]
            page_size_pt=page_size_pt,
        )
    css_parts, markup_parts = [], []
    for index, frame in enumerate(planned_frames, start=1):
        css, markup = _frame_markup(frame, index)
        css_parts.append(css); markup_parts.append(markup)
    page_width, page_height = page_size_pt
    css = ("@page{size:%spt %spt;margin:0}html,body{width:%spt;height:%spt;margin:0;}body{position:relative;}" %
           (page_width, page_height, page_width, page_height) + font_css + surface_css + semantic_css + "".join(css_parts) +
           ".safe-wrap-line{white-space:nowrap;overflow:hidden;}")
    html_path.write_text("<!doctype html><meta charset=utf-8><style>%s</style>%s" %
                         (css, semantic_markup + surface_markup + "".join(markup_parts)), encoding="utf-8")
    version = 4 if layout_declared else 3 if spacing_declared else 2
    receipt = {"schema_version": f"docs-html-pdf-wrap-plan-{version}",
               "producer": f"html-pdf-canonical-{version}",
               "status": "planned", "source_sha256": sha256(html_path),
               "page_size_pt": list(page_size_pt), "frames": planned_frames,
               **({"spacing_contract": {"mode": "enforced" if any(frame.get("spacing", {}).get("mode") == "enforced" for frame in planned_frames) else "shadow",
                                         "spacing_pass": None}} if spacing_declared else {}),
               **({"layout_contract": {"mode": "enforced", "relation_sink": "AQ-RELATION-01",
                                        "alignment_pass": None, "relation_pass": None}}
                  if layout_declared else {})}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def write_controlled_html(text: str, html_path: Path, receipt_path: Path, **kwargs: Any) -> dict[str, Any]:
    """Write one explicit no-wrap HTML frame plus a compatible v1 plan receipt."""
    planned = plan_html_frame(text, **kwargs)
    if planned["status"] != "pass":
        raise ValueError("HTML-SAFE-WRAP-BLOCK")
    font = planned["font"]
    font_css = _font_css(font)
    css_frame, markup = _frame_markup(planned, 1)
    css = ("@page{size:612pt 792pt;margin:0}body{margin:0;}" + font_css + css_frame +
           ".safe-wrap-line{white-space:nowrap;overflow:hidden;}")
    html_path.write_text("<!doctype html><meta charset=utf-8><style>%s</style>%s" % (css, markup), encoding="utf-8")
    receipt = {"schema_version": "docs-html-pdf-wrap-plan-1", "producer": "html-pdf-canonical-1",
               "status": "pass", "source_sha256": sha256(html_path), **planned}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _receipt_frames(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("schema_version") == "docs-html-pdf-wrap-plan-1":
        return [data]
    if data.get("schema_version") in {"docs-html-pdf-wrap-plan-2", "docs-html-pdf-wrap-plan-3", "docs-html-pdf-wrap-plan-4"} and isinstance(data.get("frames"), list):
        return [item for item in data["frames"] if isinstance(item, dict)]
    return []


def _point_inside(bbox: tuple[float, float, float, float], frame_bbox: list[float]) -> bool:
    x, y, width, height = map(float, frame_bbox)
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return x - .5 <= center_x <= x + width + .5 and y - .5 <= center_y <= y + height + .5


def _rendered_frame_lines(page: fitz.Page, frame: dict[str, Any]) -> list[dict[str, Any]]:
    glyph_lines: list[dict[str, Any]] = []
    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = [{"c": str(char.get("c", "")), "bbox": tuple(map(float, char["bbox"]))}
                     for span in line.get("spans", []) for char in span.get("chars", [])
                     if isinstance(char, dict) and isinstance(char.get("bbox"), (list, tuple)) and len(char["bbox"]) == 4]
            selected = [char for char in chars if _point_inside(char["bbox"], frame["geometry"]["frame_bbox_pt"])]
            if not selected:
                continue
            selected.sort(key=lambda item: item["bbox"][0])
            bbox = (min(item["bbox"][0] for item in selected), min(item["bbox"][1] for item in selected),
                    max(item["bbox"][2] for item in selected), max(item["bbox"][3] for item in selected))
            glyph_lines.append({"text": "".join(item["c"] for item in selected), "bbox": bbox,
                                "origin_y": sum((item["bbox"][1] + item["bbox"][3]) / 2 for item in selected) / len(selected)})
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(glyph_lines, key=lambda value: (value["origin_y"], value["bbox"][0])):
        if groups and abs(item["origin_y"] - groups[-1][0]["origin_y"]) <= .75:
            groups[-1].append(item)
        else:
            groups.append([item])
    result = []
    for group in groups:
        ordered = sorted(group, key=lambda value: value["bbox"][0])
        result.append({"text": "".join(item["text"] for item in ordered),
                       "bbox_pdf_points": [round(min(item["bbox"][0] for item in ordered), 3),
                                           round(min(item["bbox"][1] for item in ordered), 3),
                                           round(max(item["bbox"][2] for item in ordered), 3),
                                           round(max(item["bbox"][3] for item in ordered), 3)],
                       "origin_y_pdf_points": round(sum(item["origin_y"] for item in ordered) / len(ordered), 4)})
    return result


def _xyxy(box: list[float] | tuple[float, ...]) -> tuple[float, float, float, float]:
    x, y, width, height = map(float, box)
    return x, y, x + width, y + height


def _bbox_union(boxes: list[list[float]]) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _surface_candidates(page: fitz.Page, target_kind: str) -> list[tuple[float, float, float, float]]:
    candidates: list[tuple[float, float, float, float]] = []
    if target_kind == "image":
        for block in page.get_text("rawdict").get("blocks", []):
            bbox = block.get("bbox")
            if block.get("type") == 1 and isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                candidates.append(tuple(map(float, bbox)))
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is not None:
                candidates.append((float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)))
    except (AttributeError, RuntimeError, ValueError):
        pass
    return candidates


def _match_surface(page: fitz.Page, relation: dict[str, Any]) -> tuple[float, float, float, float] | None:
    expected = _xyxy(relation["target_bbox_pt"])
    candidates = _surface_candidates(page, relation["target_kind"])
    if not candidates:
        return None
    distance = lambda box: sum(abs(left - right) for left, right in zip(box, expected))
    selected = min(candidates, key=distance)
    return selected if distance(selected) <= 4.0 else None


def _relation_measurement(glyph: tuple[float, float, float, float], target: tuple[float, float, float, float],
                          relation: str, min_gap_pt: float) -> tuple[float, float, bool]:
    overlap_x = max(0.0, min(glyph[2], target[2]) - max(glyph[0], target[0]))
    overlap_y = max(0.0, min(glyph[3], target[3]) - max(glyph[1], target[1]))
    overlap_area = overlap_x * overlap_y
    if relation == "inside":
        inset = min(glyph[0] - target[0], glyph[1] - target[1], target[2] - glyph[2], target[3] - glyph[3])
        return inset, overlap_area, inset >= min_gap_pt - .01
    dx = max(target[0] - glyph[2], glyph[0] - target[2], 0.0)
    dy = max(target[1] - glyph[3], glyph[1] - target[3], 0.0)
    gap = math.hypot(dx, dy) if overlap_area == 0 else -min(overlap_x, overlap_y)
    return gap, overlap_area, overlap_area <= .0001 and gap >= min_gap_pt - .01


def scan_rendered_pdf_boundaries(pdf_path: Path, frames: list[dict[str, Any]]) -> dict[str, Any]:
    """Read real PDF glyph positions and classify every same-paragraph line join."""
    frame_results: list[dict[str, Any]] = []
    reasons: list[str] = []
    with fitz.open(pdf_path) as document:
        for frame in frames:
            page_number = frame.get("page")
            if not isinstance(page_number, int) or page_number < 1 or page_number > len(document):
                reasons.append("HTML-PDF-RENDERED-PAGE-01"); continue
            lines = _rendered_frame_lines(document[page_number - 1], frame)
            texts = [line["text"] for line in lines]
            plan = frame.get("plan") if isinstance(frame.get("plan"), dict) else {}
            paragraph_indices = plan.get("line_paragraph_indices")
            if not (isinstance(paragraph_indices, list) and len(paragraph_indices) == len(texts)):
                paragraph_indices = [0] * len(texts)
            boundary = safe_wrap.classify_rendered_line_boundaries(texts, paragraph_indices=paragraph_indices)
            planned_hashes = plan.get("line_hashes", [])
            rendered_hashes = [_line_hash(text) for text in texts]
            parity = rendered_hashes == planned_hashes
            frame_reasons = list(boundary["reason_codes"])
            spacing_checks: list[dict[str, Any]] = []
            alignment_check = None
            relation_checks: list[dict[str, Any]] = []
            glyph_bbox = _bbox_union([line["bbox_pdf_points"] for line in lines])
            alignment = frame.get("alignment")
            if isinstance(alignment, dict):
                if glyph_bbox is None:
                    frame_reasons.append("HTML-PDF-ALIGNMENT-EVIDENCE-01")
                else:
                    x, y, width, height = map(float, frame["geometry"]["frame_bbox_pt"])
                    top, right, bottom, left = map(float, alignment["padding_pt"])
                    content = (x + left, y + top, x + width - right, y + height - bottom)
                    horizontal_delta = (glyph_bbox[0] + glyph_bbox[2] - content[0] - content[2]) / 2
                    vertical_delta = (glyph_bbox[1] + glyph_bbox[3] - content[1] - content[3]) / 2
                    tolerance = float(alignment["tolerance_pt"])
                    horizontal_pass = alignment["horizontal_align"] != "center" or abs(horizontal_delta) <= tolerance + .01
                    vertical_pass = alignment["vertical_align"] != "center" or abs(vertical_delta) <= tolerance + .01
                    alignment_check = {
                        "single_line": alignment["single_line"],
                        "horizontal_align": alignment["horizontal_align"],
                        "vertical_align": alignment["vertical_align"],
                        "content_bbox_pdf_points": [round(value, 3) for value in content],
                        "glyph_bbox_pdf_points": [round(value, 3) for value in glyph_bbox],
                        "horizontal_center_delta_pt": round(horizontal_delta, 4),
                        "vertical_center_delta_pt": round(vertical_delta, 4),
                        "tolerance_pt": tolerance,
                        "alignment_pass": horizontal_pass and vertical_pass and (not alignment["single_line"] or len(lines) == 1),
                        "measurement_basis": "pdf_glyph_bbox",
                    }
                    if not alignment_check["alignment_pass"]:
                        frame_reasons.append("HTML-PDF-ALIGNMENT-DRIFT-01")
            if frame.get("relations"):
                if glyph_bbox is None:
                    frame_reasons.append("HTML-PDF-RELATION-EVIDENCE-01")
                else:
                    rendered_page = document[page_number - 1]
                    for relation in frame["relations"]:
                        target_bbox = _match_surface(rendered_page, relation)
                        if target_bbox is None:
                            frame_reasons.append("HTML-PDF-RELATION-EVIDENCE-01")
                            relation_checks.append({
                                "target_id_hash": relation["target_id_hash"], "target_kind": relation["target_kind"],
                                "relation": relation["relation"], "min_gap_pt": relation["min_gap_pt"],
                                "measurement_basis": "pdf_surface_and_glyph_bbox", "sample_count": 0,
                                "relation_pass": False,
                            })
                            continue
                        effective_gap, overlap_area, relation_pass = _relation_measurement(
                            glyph_bbox, target_bbox, relation["relation"], float(relation["min_gap_pt"]),
                        )
                        relation_checks.append({
                            "target_id_hash": relation["target_id_hash"], "target_kind": relation["target_kind"],
                            "relation": relation["relation"], "min_gap_pt": relation["min_gap_pt"],
                            "target_bbox_pdf_points": [round(value, 3) for value in target_bbox],
                            "glyph_bbox_pdf_points": [round(value, 3) for value in glyph_bbox],
                            "effective_gap_pt": round(effective_gap, 4), "overlap_area_pt2": round(overlap_area, 4),
                            "measurement_basis": "pdf_surface_and_glyph_bbox", "sample_count": 1,
                            "relation_pass": relation_pass,
                        })
                        if not relation_pass:
                            frame_reasons.append("HTML-PDF-RELATION-DRIFT-01")
            spacing = frame.get("spacing")
            if isinstance(spacing, dict):
                intra = spacing.get("intra_frame_line_step")
                if not isinstance(intra, dict):
                    frame_reasons.append("HTML-PDF-SPACING-EVIDENCE-01")
                else:
                    origins = [float(line["origin_y_pdf_points"]) for line in lines]
                    deltas = [current - previous for previous, current in zip(origins, origins[1:])]
                    effective = sum(deltas) / len(deltas) if deltas else float(intra.get("applied_pt", 0))
                    tolerance = float(intra.get("tolerance_pt", 0))
                    spacing_pass = bool(deltas) and all(abs(value - float(intra.get("applied_pt", 0))) <= tolerance + .01 for value in deltas)
                    if len(lines) == 1:
                        spacing_pass = abs(float(intra.get("requested_pt", 0)) - float(intra.get("applied_pt", 0))) <= tolerance + 1e-9
                    check = {"kind": "intra_frame_line_step",
                             "requested_pt": float(intra.get("requested_pt", 0)),
                             "applied_pt": float(intra.get("applied_pt", 0)),
                             "effective_pt": round(effective, 4), "tolerance_pt": tolerance,
                             "measurement_basis": "pdf_glyph_origin" if deltas else "planned_single_line",
                             "sample_count": len(deltas), "spacing_pass": spacing_pass}
                    spacing_checks.append(check)
                    if spacing.get("mode") == "enforced" and not spacing_pass:
                        frame_reasons.append("HTML-PDF-SPACING-DRIFT-01")
            if not parity:
                frame_reasons.append("HTML-PDF-PLANNED-BREAK-PARITY-01")
            reasons.extend(frame_reasons)
            frame_results.append({
                "frame_id_hash": hashlib.sha256(str(frame.get("frame_id", "")).encode()).hexdigest()[:16],
                "page": page_number,
                "status": "pass" if not frame_reasons else "block",
                "reason_codes": list(dict.fromkeys(frame_reasons)),
                "rendered_line_count": len(lines),
                "rendered_line_hashes": rendered_hashes,
                "line_bboxes_pdf_points": [line["bbox_pdf_points"] for line in lines],
                "line_origins_y_pdf_points": [line["origin_y_pdf_points"] for line in lines],
                "planned_break_parity": parity,
                "boundary_check": boundary,
                **({"alignment_check": alignment_check} if alignment_check is not None else {}),
                **({"relation_checks": relation_checks} if frame.get("relations") else {}),
                **({"spacing_group_id_hash": hashlib.sha256(str(spacing.get("spacing_group_id")).encode()).hexdigest()[:16],
                    "spacing_index": spacing.get("spacing_index"), "spacing_checks": spacing_checks}
                   if isinstance(spacing, dict) else {}),
            })
    groups: dict[tuple[int, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for frame, result in zip(frames, frame_results):
        spacing = frame.get("spacing")
        if isinstance(spacing, dict) and isinstance(spacing.get("spacing_group_id"), str):
            groups.setdefault((int(frame.get("page", 0)), spacing["spacing_group_id"]), []).append((frame, result))
    for members in groups.values():
        ordered = sorted(members, key=lambda pair: int(pair[0]["spacing"]["spacing_index"]))
        for (previous_frame, previous_result), (current_frame, current_result) in zip(ordered, ordered[1:]):
            step = current_frame["spacing"].get("inter_frame_item_step")
            previous_origins = previous_result.get("line_origins_y_pdf_points", [])
            current_origins = current_result.get("line_origins_y_pdf_points", [])
            if not isinstance(step, dict) or not previous_origins or not current_origins:
                spacing_pass, effective, tolerance = False, 0.0, float(step.get("tolerance_pt", 0)) if isinstance(step, dict) else 0.0
            else:
                effective = float(current_origins[0]) - float(previous_origins[0])
                tolerance = float(step.get("tolerance_pt", 0))
                spacing_pass = abs(effective - float(step.get("applied_pt", 0))) <= tolerance + .01
            current_result.setdefault("spacing_checks", []).append({
                "kind": "inter_frame_item_step",
                "requested_pt": float(step.get("requested_pt", 0)) if isinstance(step, dict) else 0.0,
                "applied_pt": float(step.get("applied_pt", 0)) if isinstance(step, dict) else 0.0,
                "effective_pt": round(effective, 4), "tolerance_pt": tolerance,
                "measurement_basis": "pdf_glyph_origin", "sample_count": 1 if previous_origins and current_origins else 0,
                "spacing_pass": spacing_pass,
            })
            if current_frame["spacing"].get("mode") == "enforced" and not spacing_pass:
                current_result["reason_codes"] = list(dict.fromkeys([*current_result["reason_codes"], "HTML-PDF-SPACING-DRIFT-01"]))
                current_result["status"] = "block"
                reasons.append("HTML-PDF-SPACING-DRIFT-01")
    reasons = list(dict.fromkeys(reasons))
    spacing_declared = any(isinstance(frame.get("spacing"), dict) for frame in frames)
    spacing_pass = all(all(check.get("spacing_pass") is True for check in result.get("spacing_checks", []))
                       and bool(result.get("spacing_checks")) for frame, result in zip(frames, frame_results)
                       if isinstance(frame.get("spacing"), dict)) if spacing_declared else None
    layout_declared = any(isinstance(frame.get("alignment"), dict) or frame.get("relations") for frame in frames)
    alignment_pass = all(result.get("alignment_check", {}).get("alignment_pass") is True
                         for frame, result in zip(frames, frame_results) if isinstance(frame.get("alignment"), dict))
    relation_pass = all(all(check.get("relation_pass") is True for check in result.get("relation_checks", []))
                        and bool(result.get("relation_checks"))
                        for frame, result in zip(frames, frame_results) if frame.get("relations"))
    return {"schema_version": "docs-html-pdf-rendered-boundaries-2" if layout_declared else "docs-html-pdf-rendered-boundaries-1",
            "pdf_sha256": sha256(pdf_path), "status": "pass" if not reasons else "block",
            "reason_codes": reasons, "frames": frame_results,
            **({"spacing_pass": spacing_pass} if spacing_declared else {}),
            **({"alignment_pass": alignment_pass, "relation_pass": relation_pass,
                "relation_sink": "AQ-RELATION-01"} if layout_declared else {})}


def bind_rendered_pdf(receipt_path: Path, source_path: Path, pdf_path: Path) -> dict[str, Any]:
    """Bind a plan to both authoritative HTML and the reopened rendered PDF."""
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("HTML-PDF-RECEIPT-01") from error
    source_reasons = validate_html_receipt(receipt_path, source_path)
    if source_reasons:
        raise ValueError(source_reasons[0])
    rendered = scan_rendered_pdf_boundaries(pdf_path, _receipt_frames(data))
    data["rendered_pdf_sha256"] = rendered["pdf_sha256"]
    data["rendered_boundary_check"] = rendered
    if data.get("schema_version") == "docs-html-pdf-wrap-plan-3":
        contract = data.get("spacing_contract")
        if not isinstance(contract, dict):
            contract = {"mode": "enforced"}
            data["spacing_contract"] = contract
        contract["spacing_pass"] = rendered.get("spacing_pass") is True
    if data.get("schema_version") == "docs-html-pdf-wrap-plan-4":
        contract = data.get("layout_contract")
        if not isinstance(contract, dict):
            contract = {"mode": "enforced", "relation_sink": "AQ-RELATION-01"}
            data["layout_contract"] = contract
        contract["alignment_pass"] = rendered.get("alignment_pass") is True
        contract["relation_pass"] = rendered.get("relation_pass") is True
        if isinstance(data.get("spacing_contract"), dict):
            data["spacing_contract"]["spacing_pass"] = rendered.get("spacing_pass") is True
    data["status"] = rendered["status"]
    data["reason_codes"] = rendered["reason_codes"]
    receipt_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return data


def validate_html_receipt(receipt_path: Path, source_path: Path,
                          rendered_pdf_path: Path | None = None) -> list[str]:
    """Fail closed for missing, stale, noncanonical, or unbound HTML plans."""
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["HTML-PDF-RECEIPT-01"]
    frames = _receipt_frames(data) if isinstance(data, dict) else []
    if not frames or data.get("status") not in {"planned", "pass"}:
        return list(dict.fromkeys(data.get("reason_codes", []) if isinstance(data, dict) else [])) or ["HTML-PDF-RECEIPT-01"]
    if data.get("source_sha256") != sha256(source_path):
        return ["HTML-PDF-SOURCE-01"]
    is_v3 = data.get("schema_version") == "docs-html-pdf-wrap-plan-3"
    is_v4 = data.get("schema_version") == "docs-html-pdf-wrap-plan-4"
    if is_v3 or (is_v4 and isinstance(data.get("spacing_contract"), dict)):
        contract = data.get("spacing_contract")
        if not isinstance(contract, dict) or contract.get("mode") not in {"enforced", "shadow"}:
            return ["HTML-PDF-SPACING-CONTRACT-01"]
        try:
            _validate_planned_spacing(frames)
        except ValueError as error:
            return [str(error)]
        if contract.get("mode") == "enforced" and data.get("status") == "pass" and contract.get("spacing_pass") is not True:
            return ["HTML-PDF-SPACING-EVIDENCE-01"]
    if is_v4:
        layout_contract = data.get("layout_contract")
        if (not isinstance(layout_contract, dict) or layout_contract.get("mode") != "enforced"
                or layout_contract.get("relation_sink") != "AQ-RELATION-01"):
            return ["HTML-PDF-LAYOUT-CONTRACT-01"]
        try:
            _validate_planned_layout(frames)
        except ValueError as error:
            return [str(error)]
        if data.get("status") == "pass" and (layout_contract.get("alignment_pass") is not True
                                               or layout_contract.get("relation_pass") is not True):
            return ["HTML-PDF-LAYOUT-EVIDENCE-01"]
    selected = safe_wrap.resolve_cjk_font()
    for frame in frames:
        font = frame.get("font")
        if not isinstance(font, dict) or any(font.get(key) != selected.get(key) for key in ("family", "status", "path", "sha256")):
            return ["CJK-FONT-UNAVAILABLE-01"]
        geometry, plan = frame.get("geometry"), frame.get("plan")
        if not isinstance(geometry, dict) or not isinstance(plan, dict) or not plan.get("line_hashes"):
            return ["HTML-PDF-GEOMETRY-01"]
        widths = plan.get("widths_pt", [])
        if not isinstance(widths, list) or any(float(width) > float(geometry.get("safe_width_pt", 0)) + .01 for width in widths):
            return ["SAFE-WRAP-IMPLICIT-OVERFLOW-01"]
    if rendered_pdf_path is not None:
        if data.get("status") != "pass" or data.get("rendered_pdf_sha256") != sha256(rendered_pdf_path):
            return ["HTML-PDF-RENDERED-PDF-01"]
        rendered = data.get("rendered_boundary_check")
        if not isinstance(rendered, dict) or rendered.get("status") != "pass" or rendered.get("pdf_sha256") != sha256(rendered_pdf_path):
            return list(dict.fromkeys(rendered.get("reason_codes", []) if isinstance(rendered, dict) else [])) or ["SAFE-WRAP-RENDERED-BOUNDARY-01"]
        if (is_v3 or is_v4) and isinstance(data.get("spacing_contract"), dict) and data["spacing_contract"].get("mode") == "enforced" and rendered.get("spacing_pass") is not True:
            return ["HTML-PDF-SPACING-EVIDENCE-01"]
        if is_v4 and (rendered.get("schema_version") != "docs-html-pdf-rendered-boundaries-2"
                      or rendered.get("alignment_pass") is not True or rendered.get("relation_pass") is not True
                      or rendered.get("relation_sink") != "AQ-RELATION-01"):
            return ["HTML-PDF-LAYOUT-EVIDENCE-01"]
    return []


def _semantic_surface_records(plan_log_path: Path) -> list[dict[str, Any]]:
    try:
        records = [json.loads(line) for line in plan_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("semantic surface plan log is unreadable or malformed") from error
    if not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("semantic surface plan log has no valid frame records")
    region_ids: list[str] = []
    for record in records:
        frame = record.get("execution_contract")
        applied = frame.get("applied") if isinstance(frame, dict) else None
        effective = frame.get("effective") if isinstance(frame, dict) else None
        if (not isinstance(applied, dict)
                or applied.get("schema_version") != execution_contract.SEMANTIC_SURFACE_APPLIED_SCHEMA_VERSION
                or not isinstance(effective, dict)
                or effective.get("schema_version") != execution_contract.SEMANTIC_SURFACE_EFFECTIVE_SCHEMA_VERSION):
            raise ValueError("semantic surface plan log contains a non-surface frame")
        region_ids.append(str(applied.get("region_id", "")))
    if any(not region_id for region_id in region_ids) or len(set(region_ids)) != len(region_ids):
        raise ValueError("semantic surface plan log region IDs must be unique")
    return records


def _drawing_measurement(page: fitz.Page, bbox: list[float]) -> dict[str, Any]:
    expected = tuple(map(float, bbox))
    candidates: list[tuple[float, dict[str, Any], tuple[float, float, float, float]]] = []
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is None:
                continue
            observed = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
            distance = sum(abs(left - right) for left, right in zip(observed, expected))
            candidates.append((distance, drawing, observed))
    except (AttributeError, RuntimeError, ValueError):
        candidates = []
    candidates.sort(key=lambda item: item[0])
    matched = [item for item in candidates if item[0] <= 6.0]
    if not matched:
        return {"surface_present": False, "matched_primitive_count": 0,
                "measurement_basis": "pdf_vector_drawing_bbox"}
    _, selected, observed = matched[0]
    return {
        "surface_present": True, "matched_primitive_count": len(matched),
        "bbox_pdf_points": [round(value, 3) for value in observed],
        "bbox_delta_pt": round(sum(abs(left - right) for left, right in zip(observed, expected)), 4),
        "fill_present": selected.get("fill") is not None,
        "border_present": selected.get("color") is not None and float(selected.get("width") or 0) > 0,
        "shadow_measurement": "pixel_relative_strength_via_rendered_readback",
        "measurement_basis": "pdf_vector_drawing_bbox_fill_border",
    }


def _semantic_sequence_candidates(records: list[dict[str, Any]], artifact_sha256: str) -> list[dict[str, Any]]:
    requested = records[0]["execution_contract"]["requested"]
    declaration = requested.get("semantic_surface_policy") if isinstance(requested, dict) else None
    policy = declaration.get("sequence_policy") if isinstance(declaration, dict) else None
    if not isinstance(policy, dict):
        return []
    prominent = set(policy.get("prominent_tiers", []))
    limit = policy.get("max_consecutive_pages")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return []
    by_scope: dict[str, list[tuple[int, int, str]]] = {}
    for record in records:
        state = record["execution_contract"]["effective"]
        scope, index = state.get("sequence_scope_id"), state.get("sequence_index")
        if isinstance(scope, str) and isinstance(index, int) and state.get("surface_tier") in prominent:
            by_scope.setdefault(scope, []).append((index, int(state["page_index"]), state["region_id"]))
    candidates: list[dict[str, Any]] = []
    for scope, values in sorted(by_scope.items()):
        pages = sorted({page for _, page, _ in sorted(values)})
        runs: list[list[int]] = []
        for page in pages:
            if runs and page == runs[-1][-1] + 1:
                runs[-1].append(page)
            else:
                runs.append([page])
        for run in runs:
            if len(run) <= limit:
                continue
            region_ids = sorted({region for _, page, region in values if page in run})
            seed = json.dumps([artifact_sha256, scope, run, region_ids], separators=(",", ":")).encode()
            candidates.append({
                "id": "surface-" + hashlib.sha256(seed).hexdigest()[:16],
                "rule": "semantic_surface_prominent_tier_sequence",
                "rule_id": "SURFACE-SEQUENCE-REVIEW-01",
                "page": run[0] + 1, "shape_ids": region_ids,
                "candidate_bbox_pdf_points": [0.0, 0.0, 1.0, 1.0],
                "affected_pages": [page + 1 for page in run], "affected_count": len(run),
                "example_shape_ids": region_ids[:8],
            })
    return candidates


def finalize_html_semantic_surfaces(
    *, contract_path: Path, plan_log_path: Path, html_path: Path, pdf_path: Path,
    producer_receipt_path: Path, composition_evidence_path: Path, readback_dir: Path,
) -> dict[str, Path]:
    """Bind HTML/PDF artifacts, producer receipt, PDF/PNG readback, and L1 evidence."""
    contract, errors = execution_contract.load_contract(contract_path, require_artifacts=False)
    if contract is None:
        raise ValueError("invalid execution contract: " + "; ".join(errors))
    records = _semantic_surface_records(plan_log_path)
    requested = contract["declaration"]["policy"]
    for record in records:
        frame = record.get("execution_contract")
        if (not isinstance(frame, dict)
                or frame.get("contract_id") != contract["contract_id"]
                or frame.get("policy_hash") != contract["policy_hash"]
                or frame.get("producer_run_id") != contract["producer_run_id"]
                or frame.get("requested") != requested):
            raise ValueError("producer frame execution identity/policy mismatch")
    bound = execution_contract.bind_artifacts(contract, [html_path, pdf_path])
    execution_contract.write_contract(contract_path, bound)
    receipt = {
        "schema_version": execution_contract.PRODUCER_RECEIPT_SCHEMA_VERSION,
        "status": "pass", "execution_identity": execution_contract.identity(bound),
        "contract": execution_contract.file_handle(contract_path),
        "plan_log": execution_contract.file_handle(plan_log_path),
        "requested": requested,
        "applied": execution_contract.aggregate_producer_policy(records, "applied"),
        "effective": execution_contract.aggregate_producer_policy(records, "effective"),
        "artifacts": bound["artifacts"], "frame_count": len(records),
    }
    producer_receipt_path = producer_receipt_path.resolve()
    producer_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    producer_receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    validated, receipt_errors = execution_contract.validate_producer_receipt(producer_receipt_path, bound)
    if validated is None:
        raise ValueError("producer receipt self-check failed: " + "; ".join(receipt_errors))
    regions: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        for page_index, page in enumerate(document):
            pages.append({"page": page_index + 1,
                          "page_size_pt": [round(page.rect.width, 3), round(page.rect.height, 3)]})
        for record in records:
            application = record["surface_application"]
            state = record["execution_contract"]
            region_id = application["region_id"]
            page_index = application["page_index"]
            hard_failures.extend(application.get("hard_failures", []))
            primitive_ids = application.get("primitive_ids", [])
            bbox = application.get("bbox_pt")
            measurement = None
            if primitive_ids:
                if not isinstance(page_index, int) or page_index < 0 or page_index >= len(document):
                    hard_failures.append({"rule_id": "SURFACE-CONTAINMENT-01", "region_id": region_id,
                                          "expected_page_index": page_index})
                elif not isinstance(bbox, list):
                    hard_failures.append({"rule_id": "SURFACE-GEOMETRY-01", "region_id": region_id})
                else:
                    measurement = _drawing_measurement(document[page_index], bbox)
                    if not measurement["surface_present"]:
                        hard_failures.append({"rule_id": "SURFACE-COVERAGE-01", "region_id": region_id,
                                              "primitive_id": primitive_ids[0]})
                    elif measurement.get("bbox_delta_pt", 999) > 6.0:
                        hard_failures.append({"rule_id": "SURFACE-GEOMETRY-01", "region_id": region_id,
                                              "primitive_id": primitive_ids[0]})
            regions.append({
                "region_id": region_id, "page_index": page_index,
                "shape_id": primitive_ids[0] if len(primitive_ids) == 1 else None,
                "bbox_pt": bbox, "requested": state["applied"],
                "applied": state["applied"], "effective": state["effective"],
                "primitive_application": {
                    "fill_applied": application["fill_applied"],
                    "border_applied": application["border_applied"],
                    "shadow_applied": application["shadow_applied"],
                },
                **({"rendered_primitive": measurement} if measurement is not None else {}),
            })
    artifact_digest = sha256(pdf_path)
    evidence = {
        "schema_version": "docs-composition-evidence-2",
        "extractor": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
        "source": {"path": str(html_path.resolve()), "sha256": sha256(html_path)},
        "artifact": {"path": str(pdf_path.resolve()), "sha256": artifact_digest},
        "pages": pages, "hard_pairs": [],
        "review_candidates": _semantic_sequence_candidates(records, artifact_digest),
        "actual_metrics_pt": {},
        "limitations": [
            "HTML surface identity uses caller-declared region and primitive IDs only.",
            "PDF vector readback measures bbox, fill, and border; shadow strength uses existing rendered pixel readback.",
        ],
        "semantic_surfaces": {
            "schema_version": "docs-semantic-surface-composition-evidence-1",
            "execution_identity": execution_contract.identity(bound),
            "regions": regions, "HARD_FAIL": hard_failures,
        },
    }
    composition_evidence_path = composition_evidence_path.resolve()
    composition_evidence_path.parent.mkdir(parents=True, exist_ok=True)
    composition_evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest = rendered_readback.build_manifest(
        pdf_path, readback_dir,
        producer_run_id=bound["producer_run_id"], artifact_set_id=bound["artifact_set_id"],
        execution_contract_path=contract_path, producer_receipt_path=producer_receipt_path,
    )
    if manifest is None:
        raise ValueError("HTML semantic surface PDF readback is unavailable")
    enriched = rendered_readback.attach_surface_readback(manifest, composition_evidence_path)
    return {
        "contract": contract_path.resolve(), "producer_receipt": producer_receipt_path,
        "composition_evidence": composition_evidence_path, "rendered_readback": enriched,
    }
