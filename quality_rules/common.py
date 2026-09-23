"""Fail-closed contract-versus-measured rules for user-facing docs artifacts.

Rule evidence is structured, body-free JSON.  The receipt stores only the evidence
handle; measured values remain in the hashed evidence document.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image

from . import rendered_readback
from . import content_blocks
from . import jev_overlap
from . import jev_post_render

RULES = (
    "AQ-SOURCE-01", "AQ-CHANGE-01", "AQ-IDENTITY-01", "AQ-DELIVERY-01",
    "AQ-ASSET-01", "AQ-AUTHORITY-01", "AQ-BOUNDARY-01", "AQ-RELATION-01",
    "AQ-LOCAL-GLOBAL-01", "AQ-SCOPE-01", "AQ-VISIBLE-01", "AQ-HASH-01",
    "AQ-STATUS-01", "AQ-FIXTURE-01",
)
CONTRACT_RULES = (
    "AQ-CHANGE-01", "AQ-IDENTITY-01", "AQ-ASSET-01", "AQ-AUTHORITY-01",
    "AQ-BOUNDARY-01", "AQ-RELATION-01", "AQ-SCOPE-01", "AQ-VISIBLE-01",
    "AQ-FIXTURE-01", "AQ-LOCAL-GLOBAL-01",
)
EVIDENCE_RULES = frozenset(CONTRACT_RULES)
COMMON_APPLICABILITY_SCHEMA_VERSION = "docs-artifact-quality-common-rule-applicability-1"
COMMON_EVIDENCE_SCHEMA_VERSION = "docs-artifact-quality-common-evidence-1"
# These aliases keep the additive contract path readable to callers that used
# the shorter names during the candidate design phase.  Emitted records always
# use the canonical values above.
APPLICABILITY_SCHEMA_VERSION = COMMON_APPLICABILITY_SCHEMA_VERSION
COMMON_EVIDENCE_MANIFEST_SCHEMA_VERSION = COMMON_EVIDENCE_SCHEMA_VERSION
ADDITIVE_CONTRACT_SCHEMA_VERSIONS = frozenset({"docs-artifact-quality-contract-2"})
_APPLICABILITY_FIELD_NAMES = (
    "common_rule_applicability", "rule_applicability", "applicability",
)
_REASON_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


def uses_common_rule_applicability(contract: Any) -> bool:
    """Return whether the additive common-rule disposition route is selected."""
    return (
        isinstance(contract, dict)
        and (
            contract.get("schema_version") in ADDITIVE_CONTRACT_SCHEMA_VERSIONS
            or any(name in contract for name in _APPLICABILITY_FIELD_NAMES)
        )
    )


def _applicability_container(contract: Any) -> dict[str, Any] | None:
    if not isinstance(contract, dict):
        return None
    for name in _APPLICABILITY_FIELD_NAMES:
        value = contract.get(name)
        if isinstance(value, dict):
            return value
    return None


def _applicability_rules(container: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    value = container.get("rules")
    if value is None:
        value = container.get("declarations")
    if value is None:
        # Accept a direct rule map as a compatibility input, but never emit it.
        value = {key: item for key, item in container.items() if key in CONTRACT_RULES}
    return value if isinstance(value, dict) else None


def _declaration_value(spec: Any, *names: str) -> Any:
    if not isinstance(spec, dict):
        return None
    for name in names:
        if name in spec:
            return spec[name]
    return None


def _normalized_declaration(spec: Any) -> dict[str, Any] | None:
    """Normalize accepted declaration aliases without weakening validation."""
    if not isinstance(spec, dict):
        return None
    applicability = _declaration_value(spec, "applicability", "status")
    provenance = _declaration_value(spec, "decision_provenance", "provenance")
    return {
        "applicability": applicability,
        "reason_code": spec.get("reason_code"),
        "reason": spec.get("reason"),
        "basis": spec.get("basis"),
        "decision_provenance": provenance,
    }


def common_rule_declarations(contract: Any) -> dict[str, dict[str, Any]]:
    """Return normalized declarations; malformed or legacy contracts return {}."""
    container = _applicability_container(contract)
    rules = _applicability_rules(container)
    if rules is None:
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for rule_id in CONTRACT_RULES:
        declaration = _normalized_declaration(rules.get(rule_id))
        if declaration is not None:
            normalized[rule_id] = declaration
    return normalized


def validate_common_rule_applicability(contract: Any) -> list[str]:
    """Validate the explicit additive per-common-rule applicability contract."""
    if not uses_common_rule_applicability(contract):
        return []
    errors: list[str] = []
    container = _applicability_container(contract)
    if container is None:
        return ["AQ-COMMON-APPLICABILITY-01: common_rule_applicability object required"]
    if container.get("schema_version") not in {
        COMMON_APPLICABILITY_SCHEMA_VERSION,
        "docs-common-rule-applicability-1",
    }:
        errors.append("AQ-COMMON-APPLICABILITY-01: unsupported applicability schema")
    rules = _applicability_rules(container)
    if rules is None:
        return errors + ["AQ-COMMON-APPLICABILITY-01: rules object required"]
    unknown = sorted(set(rules) - set(CONTRACT_RULES))
    errors.extend(f"AQ-COMMON-APPLICABILITY-01: unknown rule declaration {rule}" for rule in unknown)
    missing = [rule for rule in CONTRACT_RULES if rule not in rules]
    errors.extend(f"{rule}: applicability declaration missing" for rule in missing)
    for rule_id in CONTRACT_RULES:
        if rule_id not in rules:
            continue
        declaration = _normalized_declaration(rules[rule_id])
        if declaration is None:
            errors.append(f"{rule_id}: applicability declaration must be an object")
            continue
        applicability = declaration["applicability"]
        if applicability not in {"applicable", "not_applicable"}:
            errors.append(f"{rule_id}: applicability must be applicable or not_applicable")
        reason_code = declaration["reason_code"]
        if not isinstance(reason_code, str) or not reason_code.strip() or not _REASON_CODE_RE.fullmatch(reason_code):
            errors.append(f"{rule_id}: machine-readable reason_code required")
        for field in ("reason", "basis"):
            value = declaration[field]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{rule_id}: {field} is required")
        provenance = declaration["decision_provenance"]
        if (
            not isinstance(provenance, dict)
            or not isinstance(provenance.get("kind"), str)
            or not provenance["kind"].strip()
            or not isinstance(provenance.get("id"), str)
            or not provenance["id"].strip()
        ):
            errors.append(f"{rule_id}: decision_provenance.kind and id are required")
    return errors


FORBIDDEN_KEYS = frozenset({
    "text", "body", "original", "rewritten", "response_text", "value",
    "actual_value", "expected_value", "raw", "payload", "content",
})
UNMEASURED_VISUAL_BOOLEAN_KEYS = frozenset({
    "no_clipping", "no_overlap", "visual_pass", "manual_visual_pass", "looks_good",
})
HASH_KEYS = frozenset({"sha256", "baseline_sha256", "image_evidence_sha256"})
TARGET_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".html", ".htm",
    ".pptx", ".docx", ".xlsx", ".odt", ".ods",
})
LINE_SPACING_PRESERVE = (
    "text", "explicit_breaks", "font_size", "character_spacing", "word_spacing",
    "font_family", "font_weight", "horizontal_geometry", "hanging_indent",
)


def validate_line_spacing_policy(value: Any) -> list[str]:
    """Validate the narrow opt-in contract; absence leaves legacy behavior unchanged."""
    if value is None:
        return []
    prefix = "AQ-LAYOUT-01: line_spacing_policy"
    if not isinstance(value, dict):
        return [f"{prefix} must be an object"]
    enabled = value.get("enabled")
    if enabled is False:
        return []
    if enabled is not True:
        return [f"{prefix}.enabled must be boolean"]
    if value.get("mode") not in {"shadow", "enforced"}:
        return [f"{prefix}.mode must be shadow or enforced"]
    if value.get("measurement_basis") != "rendered_baseline_to_baseline_over_font_size":
        return [f"{prefix}.measurement_basis is unsupported"]
    if value.get("target_roles") != ["body"] or not _ids(value.get("target_region_ids")):
        return [f"{prefix} requires target_roles=['body'] and unique target_region_ids"]
    ratios, maximum = value.get("candidate_ratios"), value.get("requested_max_ratio", 2.0)
    numeric = lambda item: isinstance(item, (int, float)) and not isinstance(item, bool)
    if (not isinstance(ratios, list) or not ratios or not all(numeric(item) and float(item) > 0 for item in ratios)
            or [float(item) for item in ratios] != sorted(float(item) for item in ratios)
            or len(set(float(item) for item in ratios)) != len(ratios)):
        return [f"{prefix}.candidate_ratios must be positive, unique, and ordered"]
    if not numeric(maximum) or not 0 < float(maximum) <= 2.0 or any(float(item) > float(maximum) for item in ratios):
        return [f"{prefix} candidate ratio exceeds requested/approved maximum"]
    if value.get("selection") != "manual_accepted_candidate":
        return [f"{prefix}.selection must be manual_accepted_candidate"]
    preserve = value.get("preserve")
    if not isinstance(preserve, list) or len(preserve) != len(LINE_SPACING_PRESERVE) or set(preserve) != set(LINE_SPACING_PRESERVE):
        return [f"{prefix}.preserve must contain the complete preservation set"]
    manifest = value.get("evidence_manifest")
    if manifest is not None:
        return _absolute_hash_handle(manifest, f"{prefix}.evidence_manifest")
    return []


def target_artifacts(contract: Any) -> list[Path]:
    """Select quality targets without treating ordinary text as output art."""
    if not isinstance(contract, dict):
        return []
    quality = contract.get("artifact_quality")
    explicit_values = quality.get("rule_targets", []) if isinstance(quality, dict) else []
    explicit = {
        str(Path(value).resolve())
        for value in explicit_values
        if isinstance(value, str) and Path(value).is_absolute()
    }
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [
        Path(value)
        for value in artifacts
        if isinstance(value, str)
        and Path(value).is_absolute()
        and (Path(value).suffix.lower() in TARGET_SUFFIXES or str(Path(value).resolve()) in explicit)
    ]


def validate_pdf_layout_contract(contract: Any) -> list[str]:
    """Validate the existing layout route; strict v2 fields are opt-in compatible.

    A contract that declares ``finalization_manifest`` selects the strict quality
    gate. Older valid docs contracts continue to use the established AQ-LAYOUT-01
    path and non-docs completions never reach this validator.
    """
    pdf_targets = [path for path in target_artifacts(contract) if path.suffix.lower() == ".pdf"]
    if not pdf_targets:
        return []
    quality = contract.get("artifact_quality") if isinstance(contract, dict) else None
    layout = quality.get("layout_typography") if isinstance(quality, dict) else None
    if not isinstance(layout, dict):
        return ["AQ-LAYOUT-01: PDF artifact requires artifact_quality.layout_typography object"]
    if layout.get("required") is not True:
        return ["AQ-LAYOUT-01: PDF artifact requires artifact_quality.layout_typography.required=true"]
    policy_errors = validate_line_spacing_policy(layout.get("line_spacing_policy"))
    if policy_errors:
        return policy_errors
    source_spec = contract.get("authoritative_source") if isinstance(contract, dict) else None
    source_path = source_spec.get("path") if isinstance(source_spec, dict) else None
    content_policy_errors = content_blocks.validate_policy(
        layout.get("content_block_policy"), source_path=source_path
    )
    if content_policy_errors:
        return content_policy_errors
    jev_overlap_errors = jev_overlap.validate_config(layout.get("jev_overlap"))
    visual_errors = jev_post_render.validate_config(layout.get("jev_post_render_review"))
    if visual_errors:
        return visual_errors
    if jev_overlap_errors:
        return jev_overlap_errors
    for design_key in jev_overlap.DESIGN_CONFIG_KEYS:
        if design_key in layout:
            design_errors = jev_overlap.validate_design_config(
                layout.get(design_key),
                contract_schema_version=contract.get("schema_version"),
            )
            if design_errors:
                return design_errors
    strict = isinstance(contract.get("finalization_manifest"), dict)
    if strict or layout.get("geometry_contract_required") is True:
        if layout.get("measurement_basis") != "pdf_points":
            return ["AQ-LAYOUT-01: geometry measurement_basis must be pdf_points"]
        if "minimum_gap_px" in layout or not isinstance(layout.get("minimum_gap_pt"), (int, float)):
            return ["AQ-LAYOUT-01: geometry contract requires minimum_gap_pt and rejects minimum_gap_px"]
        dead_space = layout.get("dead_space")
        if not isinstance(dead_space, dict):
            return ["AQ-LAYOUT-01: strict geometry contract requires dead_space thresholds"]
        ratio = dead_space.get("bottom_blank_ratio_max")
        footer = dead_space.get("last_content_to_footer_max_pt")
        ratio_ok = isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= float(ratio) <= 1
        footer_ok = isinstance(footer, (int, float)) and not isinstance(footer, bool) and float(footer) >= 0
        if not ratio_ok and not footer_ok:
            return ["AQ-LAYOUT-01: dead_space requires bottom_blank_ratio_max or last_content_to_footer_max_pt"]
    if strict:
        cjk = layout.get("cjk_contract")
        if not isinstance(cjk, dict) or cjk.get("required") is not True:
            return ["AQ-LAYOUT-01: strict PDF contract requires cjk_contract.required=true"]
        if not isinstance(layout.get("wrap_plan_receipt"), str) or not layout["wrap_plan_receipt"].startswith("/"):
            return ["AQ-LAYOUT-01: strict PDF contract requires an absolute wrap_plan_receipt"]
        if not isinstance(layout.get("wrap_plan_receipt_sha256"), str) or not _hash(layout["wrap_plan_receipt_sha256"]):
            return ["AQ-LAYOUT-01: strict PDF contract requires wrap_plan_receipt_sha256"]
        regions = layout.get("cjk_appearance_regions")
        if not isinstance(regions, list) or not regions:
            return ["AQ-LAYOUT-01: strict PDF contract requires CJK appearance coverage regions"]
    return []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in FORBIDDEN_KEYS or contains_forbidden_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_forbidden_key(item) for item in value)
    return False


def contains_unmeasured_visual_boolean(value: Any) -> bool:
    """Reject caller-asserted visual outcomes that contain no measurement."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in UNMEASURED_VISUAL_BOOLEAN_KEYS and isinstance(item, bool):
                return True
            if contains_unmeasured_visual_boolean(item):
                return True
    if isinstance(value, list):
        return any(contains_unmeasured_visual_boolean(item) for item in value)
    return False


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _ids(value: Any) -> bool:
    return isinstance(value, list) and len(value) == len(set(value)) and all(isinstance(item, str) and item for item in value)


def _absolute_hash_handle(value: Any, label: str, *, must_exist: bool = True) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}: path/hash handle missing"]
    path_value, digest = value.get("path"), value.get("sha256")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute() or not _hash(digest):
        return [f"{label}: absolute path and sha256 required"]
    path = Path(path_value)
    if must_exist and (not path.is_file() or digest != sha256(path)):
        return [f"{label}: path/hash drift"]
    return []


def _one_check(data: dict[str, Any], check_id: str, rule_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    checks = data.get("checks")
    if not isinstance(checks, list) or len(checks) != 1 or not isinstance(checks[0], dict) or checks[0].get("id") != check_id:
        return None, [f"{rule_id}: exactly one {check_id} measured check required"]
    return checks[0], []


def _rule_contract(contract: dict[str, Any], rule_id: str) -> dict[str, Any]:
    rules = contract.get("rule_contracts")
    value = rules.get(rule_id) if isinstance(rules, dict) else None
    return value if isinstance(value, dict) else {}


def _validate_rule_contract(rule_id: str, spec: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    error = lambda message: [f"{rule_id}: {message}"]
    if rule_id == "AQ-CHANGE-01":
        if not _ids(spec.get("approved_scope_ids")) or not _ids(spec.get("preserved_field_ids")):
            return error("approved_scope_ids and preserved_field_ids must be unique ID arrays")
    elif rule_id == "AQ-IDENTITY-01":
        values = spec.get("identities")
        if not isinstance(values, list) or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) or not _hash(x.get("sha256")) for x in values) or len({x["id"] for x in values}) != len(values):
            return error("identities require unique id/sha256 declarations")
    elif rule_id == "AQ-ASSET-01":
        values = spec.get("assets")
        if not isinstance(values, list) or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) or not _hash(x.get("sha256")) for x in values) or len({x["id"] for x in values}) != len(values):
            return error("assets require unique id/sha256 declarations")
    elif rule_id == "AQ-AUTHORITY-01":
        if not isinstance(spec.get("owner_id"), str) or not spec["owner_id"]:
            return error("owner_id is required")
    elif rule_id == "AQ-BOUNDARY-01":
        if spec.get("clipped") is not False or spec.get("overflow") is not False:
            return error("clipped=false and overflow=false are required")
        for key in ("max_clipped_count", "max_overflow_count", "max_image_cut_count"):
            if key in spec and (not isinstance(spec[key], int) or isinstance(spec[key], bool) or spec[key] < 0):
                return error(f"{key} must be a non-negative integer")
    elif rule_id == "AQ-RELATION-01":
        values = spec.get("relations")
        allowed = {"no-contact", "no-overlap", "gap", "overlap"}
        if not isinstance(values, list) or len({x.get("id") for x in values if isinstance(x, dict)}) != len(values):
            return error("relations require unique IDs")
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("type") not in allowed or not isinstance(item.get("no_contact"), bool) or not isinstance(item.get("no_overlap"), bool):
                return error("relation id/type/no_contact/no_overlap required")
            for key in ("min_gap_px", "max_gap_px", "max_overlap_px", "min_gap_pt", "max_gap_pt", "max_overlap_pt"):
                if key in item and (not isinstance(item[key], (int, float)) or isinstance(item[key], bool) or item[key] < 0):
                    return error(f"relation {key} must be non-negative")
    elif rule_id == "AQ-SCOPE-01":
        if not _ids(spec.get("protected_region_ids")) or not _hash(spec.get("baseline_sha256")):
            return error("protected_region_ids and baseline_sha256 required")
    elif rule_id == "AQ-VISIBLE-01":
        expected = [item.get("id") for item in contract.get("visible_criteria", []) if isinstance(item, dict)]
        if not _ids(spec.get("criterion_ids")) or spec.get("criterion_ids") != expected or not _hash(spec.get("image_evidence_sha256")):
            return error("criterion IDs must match visible_criteria and image evidence hash is required")
    elif rule_id == "AQ-FIXTURE-01":
        if _absolute_hash_handle(spec.get("suite"), rule_id) or not _ids(spec.get("expected_rule_failures")):
            return error("suite path/hash and expected_rule_failures required")
    elif rule_id == "AQ-LOCAL-GLOBAL-01":
        if not isinstance(spec.get("required"), bool):
            return error("required boolean is required")
        if spec["required"]:
            for prefix in ("crop", "whole_artifact"):
                if not isinstance(spec.get(f"{prefix}_id"), str) or not spec[f"{prefix}_id"] or not _hash(spec.get(f"{prefix}_sha256")):
                    return error(f"{prefix} ID/hash required")
    return []


def validate_contract(contract: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(contract, dict):
        return ["AQ-SOURCE-01: contract must be object"]
    if contract.get("schema_version") not in {
        "docs-artifact-quality-contract-1", *ADDITIVE_CONTRACT_SCHEMA_VERSIONS,
    }:
        errors.append("AQ-SCOPE-01: unsupported contract schema")
    quality = contract.get("artifact_quality")
    if not isinstance(quality, dict) or quality.get("required") is not True:
        errors.append("AQ-SCOPE-01: artifact_quality.required must be true")
    elif "rule_targets" in quality:
        rule_targets = quality.get("rule_targets")
        if not isinstance(rule_targets, list) or not all(isinstance(item, str) and Path(item).is_absolute() for item in rule_targets):
            errors.append("AQ-SCOPE-01: artifact_quality.rule_targets must contain absolute paths")
        elif any(item not in (contract.get("artifacts") or []) for item in rule_targets):
            errors.append("AQ-SCOPE-01: artifact_quality.rule_targets must be declared artifacts")
    if not isinstance(contract.get("artifact_kind"), str) or not contract["artifact_kind"]:
        errors.append("AQ-DELIVERY-01: artifact_kind is required")
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or not all(isinstance(item, str) and Path(item).is_absolute() for item in artifacts):
        errors.append("AQ-DELIVERY-01: absolute user-facing artifacts are required")
    if len(set(artifacts or [])) != len(artifacts or []):
        errors.append("AQ-AUTHORITY-01: duplicate artifact authority")
    source = contract.get("authoritative_source")
    if not isinstance(source, dict) or not isinstance(source.get("readback_mode"), str):
        errors.append("AQ-SOURCE-01: source readback mode required")
    else:
        errors.extend(_absolute_hash_handle(source, "AQ-SOURCE-01"))
    criteria = contract.get("visible_criteria")
    if not isinstance(criteria, list) or not 5 <= len(criteria) <= 8:
        errors.append("AQ-VISIBLE-01: 5-8 visible criteria required")
    elif not all(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] for item in criteria):
        errors.append("AQ-VISIBLE-01: identified visible criteria required")
    elif len({item["id"] for item in criteria}) != len(criteria):
        errors.append("AQ-VISIBLE-01: duplicate visible criterion id")
    declarations = contract.get("rule_contracts")
    if not isinstance(declarations, dict):
        errors.extend(f"{rule}: contract declaration missing" for rule in CONTRACT_RULES)
    else:
        for rule in CONTRACT_RULES:
            spec = declarations.get(rule)
            if not isinstance(spec, dict):
                errors.append(f"{rule}: contract declaration missing")
            else:
                errors.extend(_validate_rule_contract(rule, spec, contract))
    errors.extend(validate_common_rule_applicability(contract))
    if contains_forbidden_key(contract):
        errors.append("AQ-IDENTITY-01: contract contains a forbidden clear-value/body field")
    errors.extend(validate_pdf_layout_contract(contract))
    return errors


def _load_evidence(rule_id: str, evidence: Any, artifact: Path) -> tuple[dict[str, Any] | None, list[str]]:
    errors = _absolute_hash_handle(evidence, rule_id)
    if errors:
        return None, errors
    path = Path(evidence["path"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, [f"{rule_id}: evidence must be structured JSON"]
    if not isinstance(data, dict) or data.get("schema_version") != "docs-artifact-quality-evidence-1":
        return None, [f"{rule_id}: invalid evidence schema"]
    if contains_forbidden_key(data):
        return None, [f"{rule_id}: evidence contains forbidden clear-value/body field"]
    if contains_unmeasured_visual_boolean(data):
        return None, [f"{rule_id}: handwritten visual pass boolean is not deterministic render/geometry evidence"]
    provenance = data.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") not in {"reviewer", "checker"} or not isinstance(provenance.get("id"), str) or not provenance["id"]:
        return None, [f"{rule_id}: reviewer/checker provenance required"]
    if data.get("rule_id") != rule_id or data.get("status") != "pass" or data.get("review_status") != "pass":
        return None, [f"{rule_id}: evidence not passed or unresolved/manual-review"]
    if data.get("artifact_path") != str(artifact.resolve()) or data.get("artifact_sha256") != sha256(artifact):
        return None, [f"{rule_id}: evidence artifact mismatch"]
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks or any(not isinstance(item, dict) or item.get("status") != "pass" for item in checks):
        return None, [f"{rule_id}: measured checks missing/failed"]
    return data, []


def _pairs(values: Any, hash_key: str = "sha256") -> list[tuple[Any, Any]] | None:
    if not isinstance(values, list):
        return None
    pairs = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _hash(item.get(hash_key)):
            return None
        pairs.append((item["id"], item[hash_key]))
    return pairs


def _real_image(handle_value: Any, label: str) -> tuple[tuple[int, int] | None, list[str]]:
    errors = _absolute_hash_handle(handle_value, label)
    if errors:
        return None, errors
    try:
        with Image.open(handle_value["path"]) as image:
            image.verify()
        with Image.open(handle_value["path"]) as image:
            return image.size, []
    except Exception:
        return None, [f"{label}: render handle is not a valid image"]


def validate_rule_evidence(rule_id: str, data: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    spec = _rule_contract(contract, rule_id)
    check_ids = {
        "AQ-CHANGE-01": "change-contract", "AQ-IDENTITY-01": "identity-contract",
        "AQ-ASSET-01": "asset-contract", "AQ-AUTHORITY-01": "authority-contract",
        "AQ-BOUNDARY-01": "boundary-contract", "AQ-RELATION-01": "relation-contract",
        "AQ-SCOPE-01": "scope-contract", "AQ-VISIBLE-01": "visible-contract",
        "AQ-FIXTURE-01": "fixture-contract", "AQ-LOCAL-GLOBAL-01": "local-global-contract",
    }
    check, errors = _one_check(data, check_ids[rule_id], rule_id)
    if errors or check is None:
        return errors
    mismatch = lambda message: [f"{rule_id}: {message}"]
    if rule_id == "AQ-CHANGE-01":
        if check.get("observed_scope_ids") != spec.get("approved_scope_ids") or check.get("changed_preserved_field_ids") != []:
            return mismatch("approved scope or preserved fields mismatch")
    elif rule_id == "AQ-IDENTITY-01":
        if _pairs(check.get("identities")) != _pairs(spec.get("identities")):
            return mismatch("identity ID/hash mismatch")
    elif rule_id == "AQ-ASSET-01":
        if _pairs(check.get("assets")) != _pairs(spec.get("assets")):
            return mismatch("asset ID/hash mismatch")
    elif rule_id == "AQ-AUTHORITY-01":
        owners = check.get("observed_owner_ids")
        duplicates = check.get("duplicate_owner_ids")
        if owners != [spec.get("owner_id")] or duplicates != []:
            return mismatch("single observed owner/duplicate mismatch")
    elif rule_id == "AQ-BOUNDARY-01":
        if check.get("clipped") is not spec.get("clipped") or check.get("overflow") is not spec.get("overflow"):
            return mismatch("clipped/overflow mismatch")
        for measured, limit in (("clipped_count", "max_clipped_count"), ("overflow_count", "max_overflow_count"), ("image_cut_count", "max_image_cut_count")):
            value = check.get(measured)
            maximum = spec.get(limit, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > maximum:
                return mismatch(f"{measured} exceeds contract")
    elif rule_id == "AQ-RELATION-01":
        measured = check.get("relations")
        expected = spec.get("relations")
        if not isinstance(measured, list) or len(measured) != len(expected):
            return mismatch("relation coverage mismatch")
        by_id = {item.get("id"): item for item in measured if isinstance(item, dict)}
        if len(by_id) != len(measured) or set(by_id) != {item["id"] for item in expected}:
            return mismatch("relation IDs mismatch")
        for relation in expected:
            seen = by_id[relation["id"]]
            if seen.get("type") != relation["type"]:
                return mismatch("relation type mismatch")
            if relation["no_contact"] and seen.get("contact") is not False:
                return mismatch("no_contact violated")
            if relation["no_overlap"] and seen.get("overlap") is not False:
                return mismatch("no_overlap violated")
            basis = relation.get("measurement_basis", "px")
            gap_key, overlap_key = ("gap_pt", "overlap_pt") if basis == "pdf_points" else ("gap_px", "overlap_px")
            gap, overlap = seen.get(gap_key), seen.get(overlap_key)
            if not isinstance(gap, (int, float)) or isinstance(gap, bool) or not isinstance(overlap, (int, float)) or isinstance(overlap, bool):
                return mismatch("measured gap/overlap required")
            if basis == "pdf_points":
                if "min_gap_pt" in relation and gap < relation["min_gap_pt"]: return mismatch("gap below contract")
                if "max_gap_pt" in relation and gap > relation["max_gap_pt"]: return mismatch("gap above contract")
                if "max_overlap_pt" in relation and overlap > relation["max_overlap_pt"]: return mismatch("overlap above contract")
            else:
                if "min_gap_px" in relation and gap < relation["min_gap_px"]: return mismatch("gap below contract")
                if "max_gap_px" in relation and gap > relation["max_gap_px"]: return mismatch("gap above contract")
                if "max_overlap_px" in relation and overlap > relation["max_overlap_px"]: return mismatch("overlap above contract")
    elif rule_id == "AQ-SCOPE-01":
        if check.get("protected_region_ids") != spec.get("protected_region_ids") or check.get("baseline_sha256") != spec.get("baseline_sha256"):
            return mismatch("protected-region IDs or baseline hash mismatch")
    elif rule_id == "AQ-VISIBLE-01":
        if check.get("criterion_ids") != spec.get("criterion_ids") or check.get("image_evidence_sha256") != spec.get("image_evidence_sha256"):
            return mismatch("coverage/image evidence hash mismatch")
    elif rule_id == "AQ-FIXTURE-01":
        suite = check.get("suite")
        if suite != spec.get("suite") or _absolute_hash_handle(suite, rule_id):
            return mismatch("fixture suite path/hash mismatch")
        if check.get("observed_rule_failures") != spec.get("expected_rule_failures"):
            return mismatch("expected rule failures mismatch")
    elif rule_id == "AQ-LOCAL-GLOBAL-01":
        if check.get("required") is not spec.get("required"):
            return mismatch("required state mismatch")
        if spec.get("required"):
            crop, whole = check.get("crop"), check.get("whole_artifact")
            if not isinstance(crop, dict) or not isinstance(whole, dict):
                return mismatch("crop and whole-artifact render handles required")
            if crop.get("id") != spec.get("crop_id") or crop.get("sha256") != spec.get("crop_sha256") or whole.get("id") != spec.get("whole_artifact_id") or whole.get("sha256") != spec.get("whole_artifact_sha256"):
                return mismatch("crop/whole ID/hash mismatch")
            crop_size, crop_errors = _real_image({"path": crop.get("path"), "sha256": crop.get("sha256")}, rule_id)
            whole_size, whole_errors = _real_image({"path": whole.get("path"), "sha256": whole.get("sha256")}, rule_id)
            if crop_errors or whole_errors:
                return crop_errors + whole_errors
            if crop_size is None or whole_size is None or crop_size[0] >= whole_size[0] or crop_size[1] >= whole_size[1]:
                return mismatch("crop must be a real smaller crop of a whole-artifact render")
    return []


def _evidence_failure_disposition(evidence: Any) -> str:
    if evidence is None:
        return "missing_evidence"
    if not isinstance(evidence, dict) or not isinstance(evidence.get("path"), str) or not isinstance(evidence.get("sha256"), str):
        return "invalid_evidence"
    return "invalid_evidence"


def evaluate_common_rule(
    rule_id: str, rule: dict[str, Any], contract: dict[str, Any], artifact: Path,
) -> dict[str, Any]:
    """Evaluate one additive common rule without converting declarations to evidence."""
    declarations = common_rule_declarations(contract)
    declaration = declarations.get(rule_id)
    declaration_errors = [
        error for error in validate_common_rule_applicability(contract)
        if error.startswith(f"{rule_id}:") or error.startswith("AQ-COMMON-APPLICABILITY-01:")
    ]
    if declaration_errors:
        return {
            "status": "fail",
            "disposition": "invalid_applicability",
            "evidence": None,
            "errors": declaration_errors,
            "declaration": declaration,
        }
    if declaration is None:
        return {
            "status": "fail",
            "disposition": "missing_applicability",
            "evidence": None,
            "errors": [f"{rule_id}: applicability declaration missing"],
            "declaration": None,
        }
    if declaration.get("applicability") == "not_applicable":
        return {
            "status": "NOT_APPLICABLE",
            "disposition": "not_applicable",
            "evidence": None,
            "errors": [],
            "declaration": declaration,
        }
    if declaration.get("applicability") != "applicable":
        return {
            "status": "fail",
            "disposition": "invalid_applicability",
            "evidence": None,
            "errors": [f"{rule_id}: applicability declaration is invalid"],
            "declaration": declaration,
        }
    if rule.get("status") != "pass":
        return {
            "status": "fail",
            "disposition": "rule_failed",
            "evidence": rule.get("evidence"),
            "errors": [f"{rule_id}: failed/unresolved"],
            "declaration": declaration,
        }
    evidence = rule.get("evidence")
    data, load_errors = _load_evidence(rule_id, evidence, artifact)
    if data is not None and not load_errors:
        load_errors = validate_rule_evidence(rule_id, data, contract)
    if load_errors:
        return {
            "status": "fail",
            "disposition": _evidence_failure_disposition(evidence),
            "evidence": evidence,
            "errors": [
                f"{rule_id}: {_evidence_failure_disposition(evidence)}",
                *load_errors,
            ],
            "declaration": declaration,
        }
    return {
        "status": "pass",
        "disposition": "evidence_validated",
        "evidence": evidence,
        "errors": [],
        "declaration": declaration,
    }


def build_common_evidence_manifest(
    contract_path: Path,
    artifact: Path,
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    execution_identity: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Build the body-free, hash-bound additive common-evidence manifest."""
    contract_handle = handle(contract_path)
    artifact_handle = handle(artifact) if artifact.is_file() else {"path": str(artifact.resolve()), "sha256": ""}
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        value = {
            "rule_id": row.get("rule_id"),
            "applicability": row.get("applicability"),
            "status": row.get("status"),
            "disposition": row.get("disposition"),
            "reason_code": row.get("reason_code"),
            "reason": row.get("reason"),
            "basis": row.get("basis"),
            "decision_provenance": row.get("decision_provenance"),
        }
        if row.get("evidence") is not None:
            value["evidence"] = row["evidence"]
        normalized_rows.append(value)
    return {
        "schema_version": COMMON_EVIDENCE_SCHEMA_VERSION,
        "output_path": str(output_path.resolve()),
        "contract": contract_handle,
        "contract_path": contract_handle["path"],
        "contract_sha256": contract_handle["sha256"],
        "artifact": artifact_handle,
        "artifact_path": artifact_handle["path"],
        "artifact_sha256": artifact_handle["sha256"],
        "execution_identity": execution_identity,
        "provenance": provenance,
        "rules": normalized_rows,
    }


def validate_common_evidence_manifest(
    manifest: Any,
    contract: dict[str, Any],
    artifact: Path,
    output_path: Path,
) -> list[str]:
    """Validate generated common evidence and preserve failure dispositions."""
    if not isinstance(manifest, dict):
        return ["AQ-COMMON-EVIDENCE-01: common evidence manifest must be an object"]
    errors: list[str] = []
    if manifest.get("schema_version") != COMMON_EVIDENCE_SCHEMA_VERSION:
        errors.append("AQ-COMMON-EVIDENCE-01: unsupported common evidence schema")
    if manifest.get("output_path") != str(output_path.resolve()):
        errors.append("AQ-COMMON-EVIDENCE-01: output path mismatch")
    expected_contract = handle(Path(contract.get("__contract_path", ""))) if contract.get("__contract_path") else None
    contract_handle = manifest.get("contract")
    if not isinstance(contract_handle, dict) or not isinstance(contract_handle.get("path"), str) or not isinstance(contract_handle.get("sha256"), str):
        errors.append("AQ-COMMON-EVIDENCE-01: contract hash handle missing")
    else:
        if manifest.get("contract_path") != contract_handle.get("path") or manifest.get("contract_sha256") != contract_handle.get("sha256"):
            errors.append("AQ-COMMON-EVIDENCE-01: contract hash fields drift")
        if expected_contract is not None and contract_handle != expected_contract:
            errors.append("AQ-COMMON-EVIDENCE-01: contract hash mismatch")
    artifact_handle = manifest.get("artifact")
    actual_artifact = handle(artifact) if artifact.is_file() else {"path": str(artifact.resolve()), "sha256": ""}
    if artifact_handle != actual_artifact:
        errors.append("AQ-COMMON-EVIDENCE-01: artifact hash mismatch")
    if manifest.get("artifact_path") != actual_artifact["path"] or manifest.get("artifact_sha256") != actual_artifact["sha256"]:
        errors.append("AQ-COMMON-EVIDENCE-01: artifact hash fields drift")
    identity = manifest.get("execution_identity")
    if not isinstance(identity, dict) or not isinstance(identity.get("checker"), dict):
        errors.append("AQ-COMMON-EVIDENCE-01: execution identity/checker handle missing")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") != "checker" or not isinstance(provenance.get("id"), str) or not provenance["id"]:
        errors.append("AQ-COMMON-EVIDENCE-01: checker provenance missing")
    declarations = common_rule_declarations(contract)
    rows = manifest.get("rules")
    if not isinstance(rows, list) or len(rows) != len(CONTRACT_RULES):
        errors.append("AQ-COMMON-EVIDENCE-01: common rule coverage mismatch")
        return errors
    by_rule = {row.get("rule_id"): row for row in rows if isinstance(row, dict)}
    if len(by_rule) != len(rows) or set(by_rule) != set(CONTRACT_RULES):
        errors.append("AQ-COMMON-EVIDENCE-01: common rule IDs mismatch")
        return errors
    for rule_id in CONTRACT_RULES:
        row = by_rule[rule_id]
        declaration = declarations.get(rule_id)
        if declaration is None:
            errors.append(f"{rule_id}: manifest lacks a valid applicability declaration")
            continue
        for field in ("applicability", "reason_code", "reason", "basis", "decision_provenance"):
            if row.get(field) != declaration.get(field):
                errors.append(f"{rule_id}: manifest declaration drift for {field}")
        applicability = declaration.get("applicability")
        disposition = row.get("disposition")
        if applicability == "not_applicable":
            if row.get("status") != "NOT_APPLICABLE" or disposition != "not_applicable" or row.get("evidence") is not None:
                errors.append(f"{rule_id}: not_applicable manifest disposition is invalid")
        elif applicability == "applicable":
            if row.get("status") not in {"pass", "fail"}:
                errors.append(f"{rule_id}: applicable manifest status is invalid")
            if disposition not in {"evidence_validated", "missing_evidence", "invalid_evidence", "rule_failed"}:
                errors.append(f"{rule_id}: applicable manifest disposition is invalid")
        else:
            errors.append(f"{rule_id}: manifest applicability is invalid")
    return errors


def _common_evidence_handle_error(contract: dict[str, Any], entry: dict[str, Any], artifact: Path) -> list[str]:
    value = entry.get("common_evidence")
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not isinstance(value.get("sha256"), str):
        return ["AQ-COMMON-EVIDENCE-01: generated common evidence handle missing"]
    path = Path(value["path"])
    if not path.is_file() or value["sha256"] != sha256(path):
        return ["AQ-COMMON-EVIDENCE-01: generated common evidence handle drift"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["AQ-COMMON-EVIDENCE-01: generated common evidence is unreadable"]
    contract_for_manifest = dict(contract)
    contract_for_manifest["__contract_path"] = contract.get("__contract_path", "")
    return validate_common_evidence_manifest(manifest, contract_for_manifest, artifact, path)


def validate_entry(contract: dict[str, Any], entry: Any) -> list[str]:
    errors = validate_contract(contract)
    if not isinstance(entry, dict):
        return errors + ["AQ-STATUS-01: receipt entry must be object"]
    artifact = Path(str(entry.get("artifact_path", "")))
    if not artifact.is_absolute() or not artifact.is_file():
        errors.append("AQ-DELIVERY-01: artifact missing")
        return errors
    if entry.get("artifact_sha256") != sha256(artifact):
        errors.append("AQ-HASH-01: artifact hash drift")
    declared = {str(Path(item).resolve()) for item in contract.get("artifacts", []) if isinstance(item, str)}
    if str(artifact.resolve()) not in declared:
        errors.append("AQ-DELIVERY-01: artifact not declared")
    errors.extend(rendered_readback.validate_handle(entry.get("actual_delivery_readback"), artifact))
    rules = {item.get("id"): item for item in entry.get("rules", []) if isinstance(item, dict)}
    additive = uses_common_rule_applicability(contract)
    for rule_id in RULES:
        rule = rules.get(rule_id)
        if not rule:
            errors.append(f"{rule_id}: missing")
            continue
        if rule.get("class") not in {"deterministic", "visual", "semantic"}:
            errors.append(f"{rule_id}: failed/unresolved")
            continue
        if additive and rule_id in EVIDENCE_RULES:
            # Preserve the disposition already emitted by the checker when a
            # malformed or missing evidence handle caused a fail.  Re-evaluating
            # a failed entry as ``rule_failed`` would erase the required
            # missing_evidence/invalid_evidence distinction in validation.
            evaluation_rule = dict(rule)
            if rule.get("disposition") in {"missing_evidence", "invalid_evidence"}:
                evaluation_rule["status"] = "pass"
            evaluation = evaluate_common_rule(rule_id, evaluation_rule, contract, artifact)
            expected_status = evaluation["status"]
            if rule.get("status") != expected_status:
                errors.append(f"{rule_id}: {evaluation['disposition']} status mismatch")
            if rule.get("disposition") != evaluation["disposition"]:
                errors.append(f"{rule_id}: {evaluation['disposition']} disposition mismatch")
            errors.extend(evaluation["errors"])
            continue
        if rule.get("status") != "pass":
            errors.append(f"{rule_id}: failed/unresolved")
            continue
        if rule_id in EVIDENCE_RULES:
            data, load_errors = _load_evidence(rule_id, rule.get("evidence"), artifact)
            errors.extend(load_errors)
            if data is not None:
                errors.extend(validate_rule_evidence(rule_id, data, contract))
    if additive:
        contract_for_manifest = dict(contract)
        if contract.get("__contract_path"):
            contract_for_manifest["__contract_path"] = contract["__contract_path"]
        errors.extend(_common_evidence_handle_error(contract_for_manifest, entry, artifact))
    return errors
