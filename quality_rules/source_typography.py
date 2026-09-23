"""OOXML explicit-break validator backed by the canonical safe-wrap classifier."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

from quality_rules.safe_wrap import classify_explicit_boundary

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CIRCLED = frozenset("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")


def _compatibility_alias(rule_ids: tuple[str, ...], left: str, right: str) -> tuple[str, str]:
    """Keep historical output labels as aliases; classification stays canonical."""
    if "SAFE-WRAP-DIGIT-SPLIT-01" in rule_ids:
        return "manual_break_number_split", "hard"
    if "SAFE-WRAP-URL-SPLIT-01" in rule_ids or "SAFE-WRAP-ENGLISH-SPLIT-01" in rule_ids:
        return "manual_break_word_split", "hard"
    if left[-1:] in CIRCLED or right[:1] in CIRCLED:
        return "manual_break_circled_number_split", "hard"
    if any(rule in rule_ids for rule in (
        "SAFE-WRAP-OPENING-BRACKET-END-01", "SAFE-WRAP-FORBIDDEN-LINE-HEAD-01",
        "SAFE-WRAP-BRACKET-PAIR-BOUNDARY-01",
    )):
        return "manual_break_bracket_or_punctuation_split", "hard"
    if "SAFE-WRAP-STRANDED-PARTICLE-01" in rule_ids:
        return "manual_break_particle_split", "hard"
    return "manual_break_inside_japanese_phrase", "hard"


def _issue(path: Path, member: str, paragraph: int, break_index: int, left: str, right: str) -> dict[str, Any] | None:
    left, right = left.rstrip(), right.lstrip()
    if not left or not right:
        return None
    decision = classify_explicit_boundary(left, right)
    if decision.allowed:
        return None
    rule, severity = _compatibility_alias(decision.rule_ids, left, right)
    seed = json.dumps([str(path.resolve()), member, paragraph, break_index, rule, decision.kind], ensure_ascii=False).encode()
    return {
        "id": "src-" + hashlib.sha256(seed).hexdigest()[:16],
        "rule": rule,
        "severity": severity,
        "canonical_kind": decision.kind,
        "canonical_rule_ids": list(decision.rule_ids),
        "source_member": member,
        "paragraph": paragraph,
        "break_index": break_index,
    }


def _pptx_segments(paragraph: ET.Element) -> list[str | None]:
    values: list[str | None] = []
    for child in list(paragraph):
        if child.tag == f"{{{A_NS}}}br":
            values.append(None)
        else:
            text = "".join(node.text or "" for node in child.iter(f"{{{A_NS}}}t"))
            if text:
                values.append(text)
    return values


def _docx_segments(paragraph: ET.Element) -> list[str | None]:
    values: list[str | None] = []
    current = ""
    for node in paragraph.iter():
        if node.tag == f"{{{W_NS}}}t":
            current += node.text or ""
        elif node.tag in {f"{{{W_NS}}}br", f"{{{W_NS}}}cr"}:
            if current:
                values.append(current)
                current = ""
            values.append(None)
    if current:
        values.append(current)
    return values


def scan_ooxml(source: Path) -> dict[str, Any]:
    """Return body-free explicit-break locations; all judgments use safe_wrap."""
    issues: list[dict[str, Any]] = []
    suffix = source.suffix.lower()
    if suffix not in {".pptx", ".docx"} or not source.is_file():
        return {"schema_version": "docs-source-typography-scan-2", "status": "not_applicable", "issues": []}
    pattern = re.compile(r"ppt/slides/slide\d+\.xml$") if suffix == ".pptx" else re.compile(r"word/document\.xml$")
    namespace = A_NS if suffix == ".pptx" else W_NS
    paragraph_tag = f"{{{namespace}}}p"
    with zipfile.ZipFile(source) as archive:
        members = sorted(name for name in archive.namelist() if pattern.search(name))
        for member in members:
            root = ET.fromstring(archive.read(member))
            for paragraph_no, paragraph in enumerate(root.iter(paragraph_tag), start=1):
                segments = _pptx_segments(paragraph) if suffix == ".pptx" else _docx_segments(paragraph)
                for index, marker in enumerate(segments):
                    if marker is not None:
                        continue
                    left = "".join(x for x in segments[:index] if x is not None)
                    right = "".join(x for x in segments[index + 1:] if x is not None)
                    found = _issue(source, member, paragraph_no, index, left, right)
                    if found:
                        issues.append(found)
    status = "block" if any(item["severity"] == "hard" for item in issues) else "review" if issues else "pass"
    return {"schema_version": "docs-source-typography-scan-2", "status": status, "issues": issues}
