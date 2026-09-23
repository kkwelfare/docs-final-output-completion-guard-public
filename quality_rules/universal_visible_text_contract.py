"""Universal source-grounded contract for all user-visible producer text.

The contract runs before format adapters.  It keeps internal metadata out of the
language-completion path and returns one normalized record per visible meaning.
It never invents a missing fact: every emitted semantic field must be named in
``source_supported_fields`` or carried by a source-supplied explicit sentence.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any, Iterable

POLICY_SCHEMA_VERSION = "docs-universal-visible-text-policy-1"
POLICY_ID = "universal-explicit-v1"
ITEM_SCHEMA_VERSION = "docs-universal-visible-text-item-1"
RESULT_SCHEMA_VERSION = "docs-universal-visible-text-result-1"
FORMAT_ADAPTER_SCHEMA_VERSION = "docs-universal-visible-text-format-adapter-1"
VISIBLE_ROLES = frozenset({"body", "bullet-list", "role-list", "heading", "label"})
BODY_LIST_ROLES = frozenset({"body", "bullet-list", "role-list"})
REQUIRED_ACTION_FIELDS = frozenset({"action", "object"})
SEMANTIC_FIELDS = ("purpose", "object", "recipient", "means", "contact_content", "action")
META_KINDS = frozenset({"scope-only", "boundary-only", "permission-only", "authority-only", "uncertainty-meta"})
PRESERVED_CONSTRAINT_REASONS = frozenset({"safety", "legal", "contract"})
SUPPORTED_FORMATS = frozenset({"docx", "pdf", "svg", "png", "pptx", "html"})
_TERMINAL_SENTENCE = re.compile(r"[。！？!?]+")


def default_policy() -> dict[str, Any]:
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "default_visibility": "visible",
        "source_supported_only": True,
        "one_sentence_one_meaning": True,
        "suppressed_meta_kinds": sorted(META_KINDS),
        "preserved_constraint_reasons": sorted(PRESERVED_CONSTRAINT_REASONS),
        "body_list_minimum_line_spacing_twips": 240,
    }


def canonicalize_policy(value: Any | None = None) -> dict[str, Any]:
    raw = default_policy() if value is None else deepcopy(value)
    if not isinstance(raw, dict) or set(raw) != set(default_policy()):
        raise ValueError("universal visible text policy declaration is malformed")
    if raw.get("schema_version") != POLICY_SCHEMA_VERSION or raw.get("policy_id") != POLICY_ID:
        raise ValueError("universal visible text policy schema or policy_id mismatch")
    if raw.get("default_visibility") != "visible":
        raise ValueError("universal visible text default_visibility must be visible")
    if raw.get("source_supported_only") is not True or raw.get("one_sentence_one_meaning") is not True:
        raise ValueError("universal visible text safety invariants must be enabled")
    for key, expected in (("suppressed_meta_kinds", META_KINDS),
                          ("preserved_constraint_reasons", PRESERVED_CONSTRAINT_REASONS)):
        values = raw.get(key)
        if not isinstance(values, list) or set(values) != expected or len(values) != len(expected):
            raise ValueError(f"universal visible text {key} mismatch")
        raw[key] = sorted(values)
    twips = raw.get("body_list_minimum_line_spacing_twips")
    if isinstance(twips, bool) or not isinstance(twips, int) or twips != 240:
        raise ValueError("universal visible text body/list spacing must be 240 twips")
    return json.loads(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _clean(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a trimmed non-empty string")
    return value


def _single_meaning(text: str) -> bool:
    stripped = text.strip()
    matches = list(_TERMINAL_SENTENCE.finditer(stripped))
    if not matches:
        return "\n" not in stripped
    return len(matches) == 1 and matches[0].end() == len(stripped) and "\n" not in stripped


def _compose(fields: dict[str, str]) -> str:
    """Compose only source-supplied clauses; no field value is inferred here."""
    clauses: list[str] = []
    if fields.get("purpose"):
        clauses.append(f"{fields['purpose']}ため")
    if fields.get("recipient"):
        clauses.append(f"{fields['recipient']}に")
    if fields.get("object"):
        clauses.append(f"{fields['object']}を")
    if fields.get("means"):
        clauses.append(f"{fields['means']}で")
    if fields.get("contact_content"):
        clauses.append(fields["contact_content"])
    action = fields.get("action")
    if action:
        clauses.append(action)
    return "、".join(clauses).rstrip("。.!！?") + "。"


def apply_item(item: Any, policy: Any | None = None) -> dict[str, Any]:
    canonicalize_policy(policy)
    if not isinstance(item, dict):
        raise ValueError("universal visible text item must be an object")
    item_id = _clean(item.get("id"), "item.id")
    visibility = item.get("visibility", "visible")
    if visibility not in {"visible", "internal"}:
        raise ValueError("visibility must be visible or internal")
    role = item.get("role", "body")
    if role not in VISIBLE_ROLES:
        raise ValueError("visible text role is unknown")
    statement_kind = item.get("statement_kind", "action")
    if not isinstance(statement_kind, str) or not statement_kind.strip():
        raise ValueError("statement_kind must be a non-empty string")
    reason = item.get("constraint_reason")
    if reason is not None and reason not in PRESERVED_CONSTRAINT_REASONS:
        raise ValueError("constraint_reason is unknown")
    raw_supported = item.get("source_supported_fields", [])
    if (not isinstance(raw_supported, list) or len(set(raw_supported)) != len(raw_supported)
            or any(field not in SEMANTIC_FIELDS for field in raw_supported)):
        raise ValueError("source_supported_fields are malformed")
    supported = set(raw_supported)
    fields: dict[str, str] = {}
    unsupported: list[str] = []
    for field in SEMANTIC_FIELDS:
        value = _clean(item.get(field), field)
        if value is None:
            continue
        if field not in supported:
            unsupported.append(field)
        else:
            fields[field] = value
    explicit = _clean(item.get("explicit_text"), "explicit_text")
    explicit_supported = item.get("explicit_text_source_supported", explicit is not None)
    if not isinstance(explicit_supported, bool):
        raise ValueError("explicit_text_source_supported must be boolean")
    if unsupported or (explicit is not None and not explicit_supported):
        raise ValueError("visible text contains unsupported facts: " + ",".join(unsupported or ["explicit_text"]))
    concrete = REQUIRED_ACTION_FIELDS.issubset(fields) or (explicit is not None and bool(item.get("explicit_action_object", False)))
    constraint = reason in PRESERVED_CONSTRAINT_REASONS and concrete
    suppressed = visibility == "internal" or (statement_kind in META_KINDS and not constraint and not concrete)
    text = None if suppressed else (explicit or _compose(fields))
    if text is not None and (not text.strip() or not _single_meaning(text)):
        raise ValueError("visible text must contain one sentence with one meaning")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "id": item_id,
        "visibility": visibility,
        "role": role,
        "statement_kind": statement_kind,
        "status": "suppressed" if suppressed else "emit",
        "reason_code": ("visibility_internal" if visibility == "internal" else
                        "meta_without_concrete_action" if suppressed else
                        "preserved_constraint" if constraint else "source_supported_visible_text"),
        "explicit_text": text,
        "source_supported_fields": sorted(supported),
        "unsupported_fact_count": 0,
        "minimum_line_spacing_twips": 240 if role in BODY_LIST_ROLES else None,
    }


def apply_contract(items: Iterable[Any], policy: Any | None = None) -> dict[str, Any]:
    canonical = canonicalize_policy(policy)
    if isinstance(items, (str, bytes, dict)):
        raise ValueError("universal visible text items must be an iterable of objects")
    records = [apply_item(item, canonical) for item in items]
    visible = [record for record in records if record["status"] == "emit"]
    identifiers = [record["id"] for record in records]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("universal visible text item IDs must be unique")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "policy": canonical,
        "records": records,
        "visible_records": visible,
        "visible_texts": [record["explicit_text"] for record in visible],
        "unsupported_fact_count": 0,
        "boundary_only_visible_count": sum(
            record["statement_kind"] in META_KINDS and record["status"] == "emit"
            and record["reason_code"] != "preserved_constraint" for record in records
        ),
    }


def format_adapter_payload(format_name: str, result: Any) -> dict[str, Any]:
    normalized = str(format_name).strip().lower()
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError("universal visible text format adapter is unknown")
    if not isinstance(result, dict) or result.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ValueError("universal visible text result is malformed")
    records = result.get("visible_records")
    if not isinstance(records, list):
        raise ValueError("universal visible text visible_records are missing")
    return {
        "schema_version": FORMAT_ADAPTER_SCHEMA_VERSION,
        "format": normalized,
        "policy_id": POLICY_ID,
        "records": [
            {"id": item["id"], "role": item["role"], "explicit_text": item["explicit_text"],
             "minimum_line_spacing_twips": item["minimum_line_spacing_twips"]}
            for item in records
        ],
    }


def minimum_line_spacing_points(role: str, requested: float | int | None = None) -> float | None:
    if role not in VISIBLE_ROLES:
        raise ValueError("visible text role is unknown")
    if requested is not None and (isinstance(requested, bool) or not isinstance(requested, (int, float))
                                  or not math.isfinite(float(requested)) or float(requested) <= 0):
        raise ValueError("requested line spacing must be finite and positive")
    minimum = 12.0 if role in BODY_LIST_ROLES else None
    return max(float(requested), minimum) if requested is not None and minimum is not None else (float(requested) if requested is not None else minimum)


# Stable public alias used by producer entrypoints.
apply_universal_visible_text_contract = apply_contract
