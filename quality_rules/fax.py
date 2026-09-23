"""FAX extension: real PDF render geometry, reduction, binary metadata and reviewed legibility."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

from .common import contains_forbidden_key, sha256

RULES = ("FAX-RENDER-01", "FAX-REDUCED-01", "FAX-BINARY-01", "FAX-BINARY-META-01", "FAX-LEGIBILITY-01")
LEGIBILITY_CHECKS = frozenset({"black-fill", "thin-text", "rules", "images", "key-information"})


def applies(contract: dict[str, Any]) -> bool:
    quality = contract.get("artifact_quality", {})
    return contract.get("artifact_kind") == "fax_dm" or "fax" in (quality.get("extensions") or [])


def applies_to_artifact(contract: dict[str, Any], artifact: Path) -> bool:
    return applies(contract) and artifact.suffix.lower() == ".pdf"


def _evidence_path(item: Any, rule_id: str) -> tuple[Path | None, list[str]]:
    evidence = item.get("evidence") if isinstance(item, dict) else None
    if not isinstance(evidence, dict):
        return None, [f"{rule_id}: evidence missing"]
    path = Path(str(evidence.get("path", "")))
    if not path.is_absolute() or not path.is_file() or evidence.get("sha256") != sha256(path):
        return None, [f"{rule_id}: evidence missing/drift"]
    return path, []


def _open_image(path: Path, *, size: tuple[int, int] | None = None, binary: bool = False) -> list[str]:
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG": return ["not PNG"]
            if size is not None and image.size != size: return ["size mismatch"]
            if binary and image.mode != "1": return ["not 1-bit"]
    except Exception:
        return ["invalid image"]
    return []


def _render_contract(contract: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    fax = contract.get("render_contract", {}).get("fax")
    if not isinstance(fax, dict):
        return None, ["FAX-RENDER-01: task render contract missing"]
    binary = fax.get("binary")
    threshold = binary.get("threshold") if isinstance(binary, dict) else None
    if fax.get("original_size") is not True or fax.get("reduced_scale") != 0.5:
        return None, ["FAX-REDUCED-01: original/0.5 render contract required"]
    if not isinstance(binary, dict) or binary.get("mode") != "1-bit" or not isinstance(threshold, int) or not 0 <= threshold <= 255:
        return None, ["FAX-BINARY-META-01: explicit task binary mode/threshold required"]
    return fax, []


def _valid_provenance(data: dict[str, Any], artifact: Path, binary_path: Path) -> list[str]:
    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        return ["FAX-LEGIBILITY-01: review provenance missing"]
    kind = provenance.get("type")
    if kind in {"reviewer", "checker"}:
        if not isinstance(provenance.get("id"), str) or not provenance["id"].strip():
            return ["FAX-LEGIBILITY-01: review provenance ID missing"]
        return []
    if kind != "approved-reuse":
        return ["FAX-LEGIBILITY-01: invalid review provenance"]
    prior = provenance.get("prior_evidence")
    if not isinstance(prior, dict):
        return ["FAX-LEGIBILITY-01: prior approved evidence missing"]
    prior_path = Path(str(prior.get("path", "")))
    if not prior_path.is_absolute() or not prior_path.is_file() or prior.get("sha256") != sha256(prior_path):
        return ["FAX-LEGIBILITY-01: prior approved evidence missing/drift"]
    try:
        approved = json.loads(prior_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["FAX-LEGIBILITY-01: prior approved evidence invalid"]
    approved_provenance = approved.get("provenance") if isinstance(approved, dict) else None
    approved_binary = approved.get("binary_render") if isinstance(approved, dict) else None
    if (
        not isinstance(approved, dict)
        or approved.get("schema_version") != "docs-fax-legibility-evidence-1"
        or approved.get("status") != "pass"
        or approved.get("review_status") != "pass"
        or not isinstance(approved_provenance, dict)
        or approved_provenance.get("type") not in {"reviewer", "checker"}
        or not isinstance(approved_provenance.get("id"), str)
        or not approved_provenance["id"].strip()
        or approved.get("artifact_sha256") != sha256(artifact)
        or not isinstance(approved_binary, dict)
        or approved_binary.get("sha256") != sha256(binary_path)
    ):
        return ["FAX-LEGIBILITY-01: prior approval is not hash-identical"]
    return []


def _validate_legibility(path: Path, artifact: Path, binary_path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["FAX-LEGIBILITY-01: structured review JSON required"]
    if not isinstance(data, dict) or data.get("schema_version") != "docs-fax-legibility-evidence-1" or contains_forbidden_key(data):
        return ["FAX-LEGIBILITY-01: invalid/body-bearing review evidence"]
    if data.get("rule_id") != "FAX-LEGIBILITY-01" or data.get("status") != "pass" or data.get("review_status") != "pass":
        return ["FAX-LEGIBILITY-01: failed or unresolved manual review"]
    if data.get("artifact_path") != str(artifact.resolve()) or data.get("artifact_sha256") != sha256(artifact):
        return ["FAX-LEGIBILITY-01: artifact mismatch"]
    binary = data.get("binary_render")
    if not isinstance(binary, dict) or binary.get("path") != str(binary_path.resolve()) or binary.get("sha256") != sha256(binary_path):
        return ["FAX-LEGIBILITY-01: binary render mismatch"]
    checks = data.get("checks")
    if not isinstance(checks, list) or {item.get("id") for item in checks if isinstance(item, dict)} != LEGIBILITY_CHECKS:
        return ["FAX-LEGIBILITY-01: review coverage mismatch"]
    if any(item.get("status") != "pass" for item in checks):
        return ["FAX-LEGIBILITY-01: legibility failure"]
    return _valid_provenance(data, artifact, binary_path)


def validate_entry(contract: dict[str, Any], entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    artifact = Path(str(entry.get("artifact_path", "")))
    if not applies_to_artifact(contract, artifact):
        return []
    errors: list[str] = []
    fax_contract, contract_errors = _render_contract(contract)
    errors.extend(contract_errors)
    if fax_contract is None or not artifact.is_file():
        return errors
    rules = {item.get("id"): item for item in entry.get("rules", []) if isinstance(item, dict)}
    paths: dict[str, Path] = {}
    for rule_id in RULES:
        item = rules.get(rule_id)
        if not item or item.get("status") != "pass":
            errors.append(f"{rule_id}: missing/failed")
            continue
        path, path_errors = _evidence_path(item, rule_id)
        errors.extend(path_errors)
        if path is not None:
            paths[rule_id] = path

    try:
        with fitz.open(artifact) as document:
            if len(document) != 1:
                errors.append("FAX-RENDER-01: FAX PDF must be one page")
                expected_original = None
            else:
                page = document[0].rect
                expected_original = (round(page.width * 2), round(page.height * 2))
    except Exception:
        errors.append("FAX-RENDER-01: unreadable PDF")
        expected_original = None

    original = paths.get("FAX-RENDER-01")
    reduced = paths.get("FAX-REDUCED-01")
    binary = paths.get("FAX-BINARY-01")
    if original and expected_original:
        errors.extend(f"FAX-RENDER-01: {item}" for item in _open_image(original, size=expected_original))
    expected_reduced: tuple[int, int] | None = None
    if original:
        with Image.open(original) as image:
            expected_reduced = (max(1, image.width // 2), max(1, image.height // 2))
    if reduced and expected_reduced:
        errors.extend(f"FAX-REDUCED-01: {item}" for item in _open_image(reduced, size=expected_reduced))
    if binary and expected_reduced:
        errors.extend(f"FAX-BINARY-01: {item}" for item in _open_image(binary, size=expected_reduced, binary=True))

    meta = rules.get("FAX-BINARY-META-01", {}).get("binary")
    expected_meta = fax_contract["binary"]
    if not isinstance(meta, dict) or meta.get("mode") != expected_meta["mode"] or meta.get("threshold") != expected_meta["threshold"]:
        errors.append("FAX-BINARY-META-01: binary metadata mismatch")
    meta_path = paths.get("FAX-BINARY-META-01")
    if binary and meta_path and meta_path.resolve() != binary.resolve():
        errors.append("FAX-BINARY-META-01: binary evidence path mismatch")
    legibility = paths.get("FAX-LEGIBILITY-01")
    if legibility and binary:
        errors.extend(_validate_legibility(legibility, artifact, binary))
    return errors
