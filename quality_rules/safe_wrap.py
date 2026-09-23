"""Canonical deterministic Japanese boundary classification and global safe-wrap planning."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import ImageFont

from quality_rules.cjk_font_selector import select_cjk_font

SAFE_RATIO_DEFAULT = 0.90
SAFE_RATIO_MIN = 0.90
SAFE_RATIO_MAX = 0.92
CONTINUATION_MIN_RATIO = 0.30
CONTINUATION_REPLAN_RATIO = 0.45
_DEFAULT_SELECTION = select_cjk_font()
DEFAULT_FONT_PATH = Path(_DEFAULT_SELECTION.path or "/nonexistent/docs-profile-ud-font")
DEFAULT_FONT_FAMILY = _DEFAULT_SELECTION.family
OPENING = frozenset("（［【「『〈《〔｛([{<")
FORBIDDEN_LINE_HEAD = frozenset(
    "）］】」』〉》〕｝)]}>、。，．！？!?：；:;ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々〻ー〜…‥"
)
SENTENCE = frozenset("、。！？!?")
SYMBOL_BREAK_AFTER = frozenset("：:／/・；;")
BULLET_PREFIXES = frozenset("・●○■□◆◇▶▷►▸‣⁃-–—※")
BRACKET_PAIRS = {
    "（": "）", "［": "］", "【": "】", "「": "」", "『": "』",
    "〈": "〉", "《": "》", "〔": "〕", "｛": "｝",
    "(": ")", "[": "]", "{": "}", "<": ">",
}
PARTICLE_TOKENS = tuple(sorted({
    "ので", "から", "より", "まで", "だけ", "しか", "でも", "ながら",
    "は", "が", "を", "に", "へ", "と", "や", "も", "の", "で",
}, key=len, reverse=True))
AUXILIARY_HEADS = (
    "する", "した", "して", "します", "される", "させる", "させられる",
    "れる", "られる", "ます", "ました", "ません", "です", "でした", "ない", "たい",
)
FORBIDDEN_LEFT_SUFFIXES = ("し", "させ", "られ", "れ", "まし", "でし")
PROTECTED = re.compile(
    r"https?://[^\s]+"
    r"|[（［【「『〈《〔｛(\[{<][^）］】」』〉》〕｝)\]}>\r\n]*[）］】」』〉》〕｝)\]}>]"
    r"|[A-Za-z][A-Za-z0-9._/+:#?=&%-]*"
    r"|\d+(?:[.,:/-]\d+)*"
    r"|[一-龯々〆ヵヶァ-ヴー]{1,24}する"
    r"|[一-龯々]{1,6}[ぁ-ゖ]{1,8}(?:させられる|させる|られる|れる|ました|ません|ます|でした|です|ない|たい)"
)


def _is_cjk(character: str) -> bool:
    return bool(character) and ("\u3000" <= character <= "\u9fff" or "\uff00" <= character <= "\uffef")


def _is_kanji(character: str) -> bool:
    return bool(character) and ("一" <= character <= "龯" or character in "々〆ヵヶ")


def _is_hiragana(character: str) -> bool:
    return bool(character) and "ぁ" <= character <= "ゖ"


@dataclass(frozen=True)
class BoundaryDecision:
    allowed: bool
    kind: str
    rule_ids: tuple[str, ...] = ()
    preference: float = 30.0


@dataclass(frozen=True)
class WrapResult:
    status: str
    lines: list[str]
    rule_ids: list[str]
    measured_widths: list[float]
    safe_width: float
    font_family: str
    font_status: str
    font_path: str = ""
    font_sha256: str = ""
    line_height: float = 0.0
    total_height: float = 0.0
    initial_implicit_wrap_candidates: tuple[dict[str, Any], ...] = ()
    implicit_wrap_candidates: tuple[dict[str, Any], ...] = ()
    break_indices: tuple[int, ...] = ()
    width_ratios: tuple[float, ...] = ()
    boundary_kinds: tuple[str, ...] = ()
    plan_cost: float = 0.0
    line_capacities: tuple[float, ...] = ()


def validate_ratio(value: float | None) -> float:
    ratio = SAFE_RATIO_DEFAULT if value is None else float(value)
    if not SAFE_RATIO_MIN <= ratio <= SAFE_RATIO_MAX:
        raise ValueError("SAFE-WRAP-RATIO-01")
    return ratio


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_cjk_font(installed_families: list[str] | None = None, *, font_path: Path | None = None) -> dict[str, str]:
    del installed_families
    selection = select_cjk_font()
    if font_path is not None and Path(font_path).resolve() != Path(selection.path or "/nonexistent").resolve():
        return {"family": "", "status": "ud-unavailable", "path": str(Path(font_path).resolve()),
                "sha256": "", "priority": "none",
                "fallback_decision": "requested_font_path_not_selected_by_shared_selector; generation_must_stop"}
    return {"family": selection.family, "status": selection.status, "path": selection.path,
            "sha256": selection.sha256, "priority": selection.priority,
            "fallback_decision": selection.fallback_decision}


def _metric_font(font: dict[str, str], font_size: float) -> ImageFont.FreeTypeFont:
    path = Path(str(font.get("path", "")))
    if not path.is_file():
        raise OSError("CJK-FONT-UNAVAILABLE-01")
    return ImageFont.truetype(str(path), size=max(1, round(float(font_size) * 96 / 72)))


def _width(value: str, metric_font: ImageFont.FreeTypeFont) -> float:
    return metric_font.getlength(value) * 72 / 96


def _line_height(metric_font: ImageFont.FreeTypeFont, line_spacing: float = 1.0) -> float:
    ascent, descent = metric_font.getmetrics()
    return (ascent + descent) * 72 / 96 * float(line_spacing)


def _protected_boundaries(text: str) -> set[int]:
    blocked: set[int] = set()
    for found in PROTECTED.finditer(text):
        blocked.update(range(found.start() + 1, found.end()))
    return blocked


def _explicit_compatibility_ids(left: str, right: str) -> list[str]:
    left, right = left.rstrip(), right.lstrip()
    if not left or not right:
        return []
    rules: list[str] = []
    combined = left + right
    if left[-1].isdigit() and right[0].isdigit():
        rules.append("SAFE-WRAP-DIGIT-SPLIT-01")
    if left[-1].isascii() and right[0].isascii() and left[-1].isalpha() and right[0].isalpha():
        rules.append("SAFE-WRAP-URL-SPLIT-01" if re.fullmatch(r"https?://[^\s]+", combined) else "SAFE-WRAP-ENGLISH-SPLIT-01")
    elif re.fullmatch(r"https?://[^\s]+", combined):
        rules.append("SAFE-WRAP-URL-SPLIT-01")
    if right.startswith("する") and re.match(r"[一-龯々〆ヵヶァ-ヴー]$", left[-1]):
        rules.append("SAFE-WRAP-NOUN-SURU-SPLIT-01")
    if right.startswith(("ます", "ました", "ません", "です", "でした")) or left.endswith(("まし", "でし")):
        rules.append("SAFE-WRAP-POLITE-FORM-SPLIT-01")
    elif right.startswith(("れる", "られる", "ない", "たい", "させる", "させられる")) or left.endswith(("し", "させ", "られ", "れ")):
        rules.append("SAFE-WRAP-VERB-AUX-SPLIT-01")
    for opening, closing in BRACKET_PAIRS.items():
        if left.count(opening) > left.count(closing) and right.count(closing) > right.count(opening):
            rules.append("SAFE-WRAP-BRACKET-PAIR-BOUNDARY-01")
            break
    return rules


def classify_boundary(text: str, index: int, *, protected: set[int] | None = None) -> BoundaryDecision:
    """Single canonical classifier used by producer planning and source validation."""
    if index <= 0 or index >= len(text):
        return BoundaryDecision(False, "edge", ("SAFE-WRAP-NATURAL-BREAK-01",))
    protected = _protected_boundaries(text) if protected is None else protected
    left, right = text[:index].rstrip(), text[index:].lstrip()
    if not left or not right:
        return BoundaryDecision(False, "empty", ("SAFE-WRAP-NATURAL-BREAK-01",))
    compatibility = _explicit_compatibility_ids(left, right)
    if index in protected:
        return BoundaryDecision(False, "protected-token", tuple(compatibility or ["SAFE-WRAP-PROTECTED-TOKEN-01"]))
    if left[-1] in OPENING:
        return BoundaryDecision(False, "opening-bracket-end", tuple(compatibility or ["SAFE-WRAP-OPENING-BRACKET-END-01"]))
    if right[0] in FORBIDDEN_LINE_HEAD:
        return BoundaryDecision(False, "forbidden-line-head", tuple(compatibility or ["SAFE-WRAP-FORBIDDEN-LINE-HEAD-01"]))
    if any(right.startswith(token) for token in AUXILIARY_HEADS) or any(left.endswith(token) for token in FORBIDDEN_LEFT_SUFFIXES):
        return BoundaryDecision(False, "verb-auxiliary", tuple(compatibility or ["SAFE-WRAP-FORBIDDEN-PAIR-01"]))
    stranded = next((token for token in PARTICLE_TOKENS if right.startswith(token)), None)
    if stranded:
        return BoundaryDecision(False, "stranded-particle", ("SAFE-WRAP-STRANDED-PARTICLE-01",))
    if _is_kanji(left[-1]) and _is_hiragana(right[0]):
        return BoundaryDecision(False, "kanji-okurigana", tuple(compatibility or ["SAFE-WRAP-KANJI-OKURIGANA-01"]))
    if _is_hiragana(left[-1]) and _is_hiragana(right[0]):
        return BoundaryDecision(False, "contiguous-hiragana", tuple(compatibility or ["SAFE-WRAP-CONTIGUOUS-HIRAGANA-01"]))
    left_particle = next((token for token in PARTICLE_TOKENS if left.endswith(token)), None)
    if left_particle and len(right) <= 4 and _is_cjk(right[0]):
        return BoundaryDecision(False, "short-predicate-isolation", ("SAFE-WRAP-PREDICATE-ISOLATION-01",))
    if left[-1] in SENTENCE:
        return BoundaryDecision(True, "sentence", preference=0.0)
    if left[-1] in SYMBOL_BREAK_AFTER:
        if index == 1 and text[0] in BULLET_PREFIXES:
            return BoundaryDecision(False, "bullet-prefix", ("SAFE-WRAP-BULLET-PREFIX-01",))
        return BoundaryDecision(True, "symbol-after", preference=1.0)
    if text[index - 1].isspace() or text[index].isspace():
        return BoundaryDecision(True, "space", preference=2.0)
    if left_particle:
        # Attributive の normally belongs with the following noun phrase; keep
        # it available, but prefer another equally balanced particle boundary.
        return BoundaryDecision(True, "particle-ending", preference=6.0 if left_particle == "の" else 4.0)
    if right[0] in OPENING:
        return BoundaryDecision(True, "before-opening", preference=8.0)
    if _is_cjk(left[-1]) and _is_cjk(right[0]):
        # CJK adjacency alone does not establish a Japanese phrase boundary.
        # The planner emits a break only when a deterministic rule proved it safe.
        return BoundaryDecision(False, "ambiguous-japanese", tuple(compatibility or ["SAFE-WRAP-AMBIGUOUS-JA-BOUNDARY-01"]))
    return BoundaryDecision(False, "ambiguous", tuple(compatibility or ["SAFE-WRAP-NATURAL-BREAK-01"]))


def classify_explicit_boundary(left: str, right: str) -> BoundaryDecision:
    combined = left.rstrip() + right.lstrip()
    index = len(left.rstrip())
    decision = classify_boundary(combined, index)
    if decision.allowed:
        return decision
    aliases = _explicit_compatibility_ids(left, right)
    return BoundaryDecision(False, decision.kind, tuple(dict.fromkeys(aliases + list(decision.rule_ids))), decision.preference)


def _text_hash(value: str) -> str:
    """Hash normalized text so rendered-boundary evidence stays body-free."""
    return hashlib.sha256("".join(value.split()).encode("utf-8")).hexdigest()


def classify_rendered_line_boundaries(
    lines: list[str], *, paragraph_indices: list[int] | None = None,
    explicit_break_indices: list[int] | None = None,
) -> dict[str, Any]:
    """Classify actual rendered line joins with the canonical boundary rules.

    ``lines`` must come from renderer glyph readback, not source-side wrapping.
    ``paragraph_indices`` prevents intentional paragraph boundaries from being
    treated as automatic line wraps. ``explicit_break_indices`` names 1-based
    boundaries already proven by an exact producer-plan/render line-hash match;
    those planned breaks are not reclassified as implicit renderer wrapping.
    The returned evidence contains hashes and rule identifiers only; it is safe
    to embed in body-free receipts.
    """
    if paragraph_indices is None:
        paragraph_indices = [0] * len(lines)
    if (len(paragraph_indices) != len(lines)
            or any(not isinstance(value, int) or isinstance(value, bool) for value in paragraph_indices)):
        return {
            "status": "block", "reason_codes": ["SAFE-WRAP-RENDERED-PARAGRAPH-MAP-01"],
            "line_count": len(lines), "boundary_count": 0, "boundaries": [],
        }
    if explicit_break_indices is None:
        explicit_break_indices = []
    if (not isinstance(explicit_break_indices, list)
            or any(not isinstance(value, int) or isinstance(value, bool)
                   or value < 1 or value >= len(lines) for value in explicit_break_indices)
            or len(set(explicit_break_indices)) != len(explicit_break_indices)):
        return {
            "status": "block", "reason_codes": ["SAFE-WRAP-RENDERED-EXPLICIT-BREAK-MAP-01"],
            "line_count": len(lines), "boundary_count": 0, "boundaries": [],
        }
    explicit_breaks = set(explicit_break_indices)
    boundaries: list[dict[str, Any]] = []
    reasons: list[str] = []
    for index, (left, right) in enumerate(zip(lines, lines[1:])):
        if paragraph_indices[index] != paragraph_indices[index + 1]:
            continue
        if index + 1 in explicit_breaks:
            continue
        decision = classify_explicit_boundary(left, right)
        record = {
            "boundary_index": index + 1,
            "left_line_hash": _text_hash(left),
            "right_line_hash": _text_hash(right),
            "allowed": decision.allowed,
            "kind": decision.kind,
            "rule_ids": list(decision.rule_ids),
        }
        boundaries.append(record)
        if not decision.allowed:
            reasons.extend(decision.rule_ids or ("SAFE-WRAP-RENDERED-BOUNDARY-01",))
    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "pass" if not reasons else "block",
        "reason_codes": reasons,
        "line_count": len(lines),
        "boundary_count": len(boundaries),
        "boundaries": boundaries,
    }


def _candidate(line_index: int, width: float, safe_width: float) -> dict[str, Any]:
    return {"id": f"implicit-wrap-{line_index + 1}", "line": line_index + 1,
            "measured_width": round(width, 4), "safe_width": round(safe_width, 4),
            "rule_id": "SAFE-WRAP-IMPLICIT-OVERFLOW-01"}


def _blocked_result(rule_id: str, *, safe_width: float, family: str, status: str, path: str, digest: str) -> WrapResult:
    return WrapResult("block", [], [rule_id], [], safe_width, family, status, path, digest)


def _plan_line(text: str, *, width: Any, safe_width: float,
               line_capacities: tuple[float, ...] | None = None,
               max_lines: int | None = None) -> tuple[list[str], list[int], list[str], float, list[float]] | None:
    """Plan with the canonical boundary graph and optional per-line capacities.

    ``line_capacities`` describes the text width available on each display line.
    When fewer capacities than lines are supplied, the last capacity is reused.
    This keeps prefix and hanging-indent geometry in the planner without adding a
    second boundary classifier or phrase-specific exception table.

    Eligible sentence/symbol-after boundaries are ordered lexicographically by
    their first reachable position. A later punctuation or semantic boundary is
    considered only when the earlier candidate cannot lead to a width- and
    line-count-feasible complete plan.
    """
    capacities = tuple(float(value) for value in (line_capacities or (safe_width,)))
    if not capacities or any(value <= 0 for value in capacities) or (max_lines is not None and max_lines < 1):
        return None
    capacity_for = lambda line_index: capacities[min(line_index, len(capacities) - 1)]
    if width(text) <= capacity_for(0):
        return [text], [], [], 0.0, [capacity_for(0)]
    protected = _protected_boundaries(text)
    decisions = {index: classify_boundary(text, index, protected=protected) for index in range(1, len(text))}
    preferred_symbol_kinds = {"sentence", "symbol-after"}
    positions = [0] + [index for index, decision in decisions.items() if decision.allowed] + [len(text)]
    positions = sorted(set(positions))
    # (position, line_count) -> (priority_path, cost, previous_position,
    #                           previous_count, boundary_kind)
    # priority_path is compared before visual-balance cost. Preferred boundaries
    # retain their absolute index so the earliest feasible one wins; semantic
    # alternatives share a tier key and therefore remain cost-balanced.
    states: dict[tuple[int, int], tuple[tuple[tuple[int, int], ...], float, int | None, int | None, str]] = {
        (0, 0): ((), 0.0, None, None, "start")
    }
    for start in positions[:-1]:
        current_states = [(key, value) for key, value in states.items() if key[0] == start]
        has_preferred_after = any(
            index > start and decision.allowed and decision.kind in preferred_symbol_kinds
            for index, decision in decisions.items()
        )
        for (position, count), (base_path, base_cost, _, _, _) in current_states:
            for end in positions:
                if end <= start:
                    continue
                if max_lines is not None and count + 1 > max_lines:
                    continue
                segment = text[start:end].strip()
                measured = width(segment)
                capacity = capacity_for(count)
                if not segment or measured > capacity + 1e-7:
                    continue
                if line_capacities is not None and len(segment.rstrip("、。，．！？!?：；:;")) <= 1:
                    # Prefix-aware planning evaluates the body independently;
                    # do not accept a nominally two-glyph line that contains
                    # only one semantic body character plus punctuation.
                    continue
                ratio = measured / capacity
                if end != len(text) and ratio < CONTINUATION_MIN_RATIO:
                    # A preferred boundary that would leave the current display
                    # line below the declared minimum width is not yet reachable;
                    # continue to the next eligible candidate. This applies to
                    # the first line as well as continuation lines.
                    continue
                decision = decisions.get(end)
                preference = 0.0 if end == len(text) else float(decision.preference if decision else 30.0)
                cost = base_cost + ((1.0 - ratio) ** 2 * 100.0) + preference
                if count > 0 and ratio < CONTINUATION_REPLAN_RATIO:
                    cost += (CONTINUATION_REPLAN_RATIO - ratio) * 800.0
                if end == len(text) and count == 0:
                    cost = 0.0
                path = base_path
                if end != len(text):
                    if decision and decision.kind in preferred_symbol_kinds:
                        path += ((0, end),)
                    elif has_preferred_after:
                        path += ((1, 0),)
                    else:
                        path += ((0, 0),)
                key = (end, count + 1)
                candidate_rank = (path, cost)
                old = states.get(key)
                if old is None or candidate_rank < (old[0], old[1]):
                    states[key] = (path, cost, position, count, decision.kind if decision else "end")
    finals = [(key, value) for key, value in states.items() if key[0] == len(text)]
    if not finals:
        return None
    (end, line_count), (_, cost, previous, previous_count, _) = min(
        finals, key=lambda item: (item[1][0], item[1][1], item[0][1])
    )
    cuts: list[int] = []
    kinds: list[str] = []
    key = (end, line_count)
    while key != (0, 0):
        _, _, prev, prev_count, kind = states[key]
        if prev is None or prev_count is None:
            break
        if key[0] != len(text):
            kinds.append(kind)
        if prev > 0:
            cuts.append(prev)
        key = (prev, prev_count)
    cuts = sorted(cuts)
    kinds = [decisions[cut].kind for cut in cuts]
    boundaries = [0] + cuts + [len(text)]
    lines = [text[a:b].strip() for a, b in zip(boundaries, boundaries[1:])]
    used_capacities = [capacity_for(index) for index in range(len(lines))]
    return lines, cuts, kinds, float(cost), used_capacities


def preflight(text: str, *, frame_width: float, margins: tuple[float, float], font_size: float,
              max_lines: int, max_height: float | None = None, safe_ratio: float | None = None,
              font: dict[str, str] | None = None, line_spacing: float = 1.0,
              paragraph_after: float = 0.0,
              line_capacities: tuple[float, ...] | None = None) -> WrapResult:
    ratio = validate_ratio(safe_ratio)
    effective_width = float(frame_width) - float(margins[0]) - float(margins[1])
    safe_width = effective_width * ratio
    font = font or resolve_cjk_font()
    family, status = str(font.get("family", "")), str(font.get("status", "no-cjk-font"))
    path, digest = str(font.get("path", "")), str(font.get("sha256", ""))
    if safe_width <= 0 or font_size <= 0 or max_lines < 1 or line_spacing <= 0 or paragraph_after < 0 or (max_height is not None and max_height <= 0) or (line_capacities is not None and (not line_capacities or any(float(value) <= 0 for value in line_capacities))):
        return _blocked_result("SAFE-WRAP-GEOMETRY-01", safe_width=safe_width, family=family, status=status, path=path, digest=digest)
    if "\u3000" in text:
        return _blocked_result("SAFE-WRAP-FULLWIDTH-SPACE-01", safe_width=safe_width, family=family, status=status, path=path, digest=digest)
    if not family or status != "ud-installed":
        return _blocked_result("CJK-FONT-UNAVAILABLE-01", safe_width=safe_width, family=family, status=status, path=path, digest=digest)
    try:
        metric_font = _metric_font(font, font_size)
        width = lambda value: _width(value, metric_font)
        line_height = _line_height(metric_font, line_spacing)
    except OSError:
        return _blocked_result("CJK-FONT-UNAVAILABLE-01", safe_width=safe_width, family=family, status=status, path=path, digest=digest)

    source_lines = text.splitlines() or [text]
    source_widths = [width(line) for line in source_lines]
    initial_candidates = tuple(_candidate(index, measured, safe_width) for index, measured in enumerate(source_widths) if measured > safe_width)
    lines: list[str] = []
    break_indices: list[int] = []
    boundary_kinds: list[str] = []
    plan_cost = 0.0
    used_capacities: list[float] = []
    offset = 0
    for source_line in source_lines:
        if not source_line.strip():
            lines.append("")
            offset += 1
            continue
        remaining_lines = max_lines - len(lines)
        planned = _plan_line(source_line.strip(), width=width, safe_width=safe_width,
                             line_capacities=line_capacities, max_lines=remaining_lines)
        if planned is None and remaining_lines < max_lines:
            planned = _plan_line(source_line.strip(), width=width, safe_width=safe_width,
                                 line_capacities=line_capacities)
        elif planned is None:
            # Preserve the established MAX-LINES diagnostic when no plan can fit
            # the declared line cap, while still letting feasible later symbol
            # candidates satisfy that cap before this fallback is considered.
            unrestricted = _plan_line(source_line.strip(), width=width, safe_width=safe_width,
                                      line_capacities=line_capacities)
            if unrestricted is not None:
                planned = unrestricted
        if planned is None:
            return WrapResult("block", lines, ["SAFE-WRAP-NATURAL-BREAK-01"], [width(line) for line in lines],
                              safe_width, family, status, path, digest, line_height, len(lines) * line_height,
                              initial_candidates, (_candidate(len(lines), width(source_line), safe_width),),
                              tuple(break_indices), tuple(width(line) / safe_width for line in lines if safe_width), tuple(boundary_kinds), plan_cost)
        planned_lines, cuts, kinds, cost, planned_capacities = planned
        lines.extend(planned_lines)
        break_indices.extend(offset + cut for cut in cuts)
        boundary_kinds.extend(kinds)
        plan_cost += cost
        used_capacities.extend(planned_capacities)
        offset += len(source_line) + 1

    widths = [width(line) for line in lines]
    if not used_capacities:
        used_capacities = [safe_width for _ in widths]
    width_ratios = tuple(measured / capacity for measured, capacity in zip(widths, used_capacities))
    remaining_candidates = tuple(_candidate(index, measured, safe_width) for index, measured in enumerate(widths) if measured > safe_width)
    paragraph_count = max(1, len(source_lines))
    total_height = len(lines) * line_height + max(0, paragraph_count - 1) * float(paragraph_after)
    rules: list[str] = []
    if remaining_candidates:
        rules.append("SAFE-WRAP-IMPLICIT-OVERFLOW-01")
    if len(lines) > max_lines:
        rules.append("SAFE-WRAP-MAX-LINES-01")
    if max_height is not None and total_height > float(max_height):
        rules.append("SAFE-WRAP-MAX-HEIGHT-01")
    if any(len(line.strip()) == 1 for line in lines):
        rules.append("SAFE-WRAP-ONE-CHAR-LINE-01")
    if any(line[:1] in FORBIDDEN_LINE_HEAD for line in lines):
        rules.append("SAFE-WRAP-FORBIDDEN-LINE-HEAD-01")
    if any(line[-1:] in OPENING for line in lines):
        rules.append("SAFE-WRAP-OPENING-BRACKET-END-01")
    for index in range(len(source_lines) - 1):
        decision = classify_explicit_boundary(source_lines[index], source_lines[index + 1])
        if not decision.allowed:
            rules.extend(decision.rule_ids)
            rules.append("SAFE-WRAP-FORBIDDEN-PAIR-01")
    rules = list(dict.fromkeys(rules))
    return WrapResult("pass" if not rules else "block", lines, rules, widths, safe_width, family, status,
                      path, digest, line_height, total_height, initial_candidates, remaining_candidates,
                      tuple(break_indices), width_ratios, tuple(boundary_kinds), plan_cost,
                      tuple(used_capacities))


def producer_preflight(text: str, **geometry: Any) -> dict[str, Any]:
    result = preflight(text, **geometry)
    font_decision = geometry.get("font") or resolve_cjk_font()
    split_required = any(rule_id in {"SAFE-WRAP-MAX-LINES-01", "SAFE-WRAP-MAX-HEIGHT-01"} for rule_id in result.rule_ids)
    return {
        "status": result.status, "rule_ids": result.rule_ids, "explicit_text": "\n".join(result.lines),
        "line_count": len(result.lines), "measured_widths": result.measured_widths,
        "width_ratios": list(result.width_ratios), "safe_width": result.safe_width,
        "line_height": result.line_height, "total_height": result.total_height,
        "break_indices": list(result.break_indices), "boundary_kinds": list(result.boundary_kinds),
        "plan_cost": result.plan_cost,
        "line_capacities": list(result.line_capacities),
        "initial_implicit_wrap_candidates": list(result.initial_implicit_wrap_candidates),
        "implicit_wrap_candidates": list(result.implicit_wrap_candidates),
        "font_size": geometry.get("font_size"), "line_spacing": geometry.get("line_spacing", 1.0),
        "paragraph_after": geometry.get("paragraph_after", 0.0),
        "recommended_action": "split-frame-or-slide" if split_required else None,
        "font": {"family": result.font_family, "status": result.font_status, "path": result.font_path,
                 "sha256": result.font_sha256, "priority": font_decision.get("priority", "none"),
                 "fallback_decision": font_decision.get("fallback_decision", "font_decision_missing; generation_must_stop")},
    }
