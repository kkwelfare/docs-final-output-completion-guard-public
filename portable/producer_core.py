"""Portable producer core: local checker first, renderer second, provider never authoritative."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .evidence import EvidenceBoundary, assert_body_free, canonical_hash, sha256_path
from .format_adapters import FormatInspection, inspect_source
from .host_adapter import HostAdapter, sha256_path as host_sha256_path
from .renderer_protocol import FakeRenderer, RenderPlan, Renderer

PRODUCER_SCHEMA_VERSION = "docs-producer-receipt-2"
RULESET_ID = "docs-quality-ruleset-v3"
STYLE_PRESET_ID = "docs-style-default-v1"


@dataclass(frozen=True)
class ProducerRequest:
    source_path: Path
    output_root: Path
    host_manifest: Path | None = None
    style_preset_path: Path | None = None
    ruleset_path: Path | None = None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"metadata file is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"metadata file must contain an object: {path}")
    return value


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _metadata_paths(request: ProducerRequest) -> tuple[Path, Path]:
    root = _package_root()
    style = Path(request.style_preset_path).resolve() if request.style_preset_path else root / "presets" / "style-default-v1.json"
    ruleset = Path(request.ruleset_path).resolve() if request.ruleset_path else root / "presets" / "ruleset-default-v1.json"
    return style, ruleset


def _validate_metadata(style_path: Path, ruleset_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    style = _read_json(style_path)
    ruleset = _read_json(ruleset_path)
    preset_id = style.get("preset_id")
    if (not isinstance(preset_id, str) or not preset_id.startswith("docs-style-")
            or not preset_id.endswith("-v1") or style.get("schema_version") != "docs-style-preset-1"):
        raise ValueError("unsupported style preset")
    if ruleset.get("ruleset_id") != RULESET_ID or ruleset.get("schema_version") != "docs-ruleset-1":
        raise ValueError("unsupported quality ruleset")
    rule_ids = ruleset.get("rule_ids")
    if not isinstance(rule_ids, list) or not rule_ids or any(not isinstance(item, str) or not item for item in rule_ids):
        raise ValueError("quality ruleset must declare rule identifiers")
    return style, ruleset


def _checker_summary(host: HostAdapter, inspection: FormatInspection) -> tuple[dict[str, Any], str, str]:
    checker_path, core_path = host.checker_paths()
    checker_sha = host_sha256_path(checker_path)
    core_sha = host_sha256_path(core_path)
    if inspection.structural_facts.get("checker_eligible"):
        checker = host.load_checker()
        report = checker.check_path(inspection.path, apply=False)
        result = {
            "status": "block" if report.get("changed") else "pass",
            "authoritative": True,
            "changed": bool(report.get("changed")),
            "rewrite_count": int(report.get("rewrite_count", 0)),
            "rule_ids": [str(item) for item in (report.get("rules") or [])],
            "protected": bool(report.get("protected")),
        }
    else:
        result = {
            "status": "pass",
            "authoritative": True,
            "changed": False,
            "rewrite_count": 0,
            "rule_ids": ["FORMAT-STRUCTURE-01"],
            "protected": True,
        }
    result["checker_path"] = str(checker_path)
    result["checker_sha256"] = checker_sha
    result["core_sha256"] = core_sha
    return result, checker_sha, core_sha


def produce(request: ProducerRequest, *, renderer: Renderer | None = None) -> dict[str, Any]:
    source = Path(request.source_path).resolve(strict=True)
    output_root = Path(request.output_root).resolve()
    if not source.is_file():
        raise ValueError("source_path must be a regular file")
    style_path, ruleset_path = _metadata_paths(request)
    style, ruleset = _validate_metadata(style_path, ruleset_path)
    inspection = inspect_source(source)
    host = HostAdapter.from_manifest(request.host_manifest, candidate_root=_package_root())
    checker, checker_sha, core_sha = _checker_summary(host, inspection)
    local_rule_ids = list(dict.fromkeys([*ruleset["rule_ids"], *checker["rule_ids"]]))
    style_sha = sha256_path(style_path)
    ruleset_sha = sha256_path(ruleset_path)
    status = "pass" if checker["status"] == "pass" else "block"
    renderer_impl = renderer or FakeRenderer()
    rendered: dict[str, Any]
    render_hash: str | None = None
    if status == "pass":
        plan = RenderPlan(
            format_name=inspection.format_name,
            source_sha256=inspection.source_sha256,
            line_count=inspection.line_count,
            style_preset_id=style["preset_id"],
            ruleset_id=ruleset["ruleset_id"],
            rule_ids=tuple(local_rule_ids),
        )
        artifact = renderer_impl.render(plan, output_root / "render")
        rendered = {
            "status": "pass",
            "name": artifact.name,
            "contract_version": renderer_impl.contract_version,
            "artifact": artifact.as_dict(),
        }
        render_hash = artifact.sha256
    else:
        rendered = {
            "status": "skipped",
            "name": renderer_impl.name,
            "contract_version": renderer_impl.contract_version,
            "reason": "authoritative local checker blocked before rendering",
        }
    evidence = EvidenceBoundary(
        schema_version="docs-typed-evidence-boundary-1",
        source_sha256=inspection.source_sha256,
        checker_sha256=checker_sha,
        ruleset_id=ruleset["ruleset_id"],
        ruleset_sha256=ruleset_sha,
        style_preset_id=style["preset_id"],
        style_preset_sha256=style_sha,
        renderer_sha256=render_hash,
        provider_mode=host.provider_mode,
        image_transport="forbidden",
    ).as_dict()
    receipt: dict[str, Any] = {
        "schema_version": PRODUCER_SCHEMA_VERSION,
        "status": status,
        "source": {
            "path": str(inspection.path),
            "sha256": inspection.source_sha256,
            "size_bytes": inspection.size_bytes,
            "format": inspection.format_name,
            "line_count": inspection.line_count,
            "structural_facts": inspection.structural_facts,
        },
        "style_preset": {
            "id": style["preset_id"],
            "version": style["version"],
            "sha256": style_sha,
        },
        "ruleset": {
            "id": ruleset["ruleset_id"],
            "version": ruleset["version"],
            "sha256": ruleset_sha,
            "rule_ids": local_rule_ids,
        },
        "quality": checker,
        "renderer": rendered,
        "host": host.describe(),
        "provider": {
            "mode": host.provider_mode,
            "called": False,
            "authoritative": False,
            "payloads_persisted": False,
            "image_transport": "forbidden",
        },
        "evidence_boundary": evidence,
        "receipt_identity": canonical_hash({
            "source": inspection.source_sha256,
            "checker": checker_sha,
            "core": core_sha,
            "style": style_sha,
            "ruleset": ruleset_sha,
            "renderer": render_hash,
        }),
    }
    assert_body_free(receipt)
    output_root.mkdir(parents=True, exist_ok=True)
    receipt_path = output_root / "producer-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt["receipt_path"] = str(receipt_path)
    return receipt
