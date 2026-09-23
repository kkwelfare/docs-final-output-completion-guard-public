"""Registered generic JSON→PDF producer for bounded wrap repair fixtures and harnesses.

This is a deterministic, product-independent producer. Its source owns one paragraph
and a layout split index. Repair changes only the split index, preserves the paragraph
identity, writes a sibling revision, and regenerates body-free plan/readback evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import fitz

from . import repair_controller

SOURCE_SCHEMA_VERSION = "docs-generic-pdf-layout-source-1"
ADAPTER_ID = "generic-pdf-wrap-v1"


def _line_hash(value: str) -> str:
    return hashlib.sha256("".join(value.split()).encode("utf-8")).hexdigest()


def _load_source(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise ValueError("generic producer source schema mismatch")
    paragraph = value.get("paragraph")
    identity = value.get("paragraph_sha256")
    if not isinstance(paragraph, str) or not paragraph.strip() or identity != hashlib.sha256(paragraph.encode("utf-8")).hexdigest():
        raise ValueError("generic producer paragraph identity mismatch")
    words = paragraph.split()
    split = value.get("split_index")
    if len(words) < 6 or not isinstance(split, int) or isinstance(split, bool) or not 1 <= split < len(words):
        raise ValueError("generic producer split index is invalid")
    return value


def render_source(source_path: Path, artifact_path: Path, plan_path: Path, *, split_index: int | None = None) -> dict[str, Any]:
    source = _load_source(source_path)
    words = source["paragraph"].split()
    split = source["split_index"] if split_index is None else split_index
    if not isinstance(split, int) or not 1 <= split < len(words):
        raise ValueError("render split index is invalid")
    lines = [" ".join(words[:split]), " ".join(words[split:])]
    artifact = artifact_path.resolve(); artifact.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(); page = doc.new_page(width=620, height=180)
    rect = fitz.Rect(30, 35, 590, 100)
    result = page.insert_textbox(rect, "\n".join(lines), fontsize=9, fontname="helv", lineheight=1.35)
    if result < 0:
        doc.close(); raise ValueError("generic producer text did not fit")
    doc.save(artifact); doc.close()
    frame = {
        "schema_version": "docs-wrap-plan-frame-1",
        "status": "pass", "page": 1, "frame_id": "generic-frame-1", "role": "body",
        "geometry": {"frame_bbox_pt": [30, 35, 560, 65], "safe_width_pt": 560, "font_size_pt": 9},
        "plan": {"line_hashes": [_line_hash(line) for line in lines], "line_paragraph_indices": [0, 0]},
    }
    plan = plan_path.resolve(); plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"artifact": repair_controller.handle(artifact), "wrap_plan": repair_controller.handle(plan), "source_content_identity_sha256": source["paragraph_sha256"]}


def _regenerate_evidence(old_evidence: Path, new_evidence: Path, old_artifact: Path, new_artifact: Path) -> None:
    new_evidence.mkdir(parents=True, exist_ok=True)
    new_hash = repair_controller.sha256(new_artifact)
    prefix = old_artifact.name + "."
    for path in old_evidence.iterdir():
        if not path.is_file() or not path.name.startswith(prefix) or path.suffix.lower() != ".json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("schema_version") != "docs-artifact-quality-evidence-1":
            continue
        value["artifact_path"] = str(new_artifact.resolve())
        value["artifact_sha256"] = new_hash
        target = new_evidence / path.name.replace(prefix, new_artifact.name + ".", 1)
        target.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("plan")
    if not isinstance(plan, dict) or repair_controller.validate_plan(plan):
        raise ValueError("generic producer received an invalid repair plan")
    if {(item["family"], item["operation"]) for item in plan["issues"]} != {("safe-wrap", "wrap_adjustment")}:
        raise ValueError("generic producer received an unsupported repair operation")
    source_path = Path(payload["source_path"]).resolve(strict=True)
    source = _load_source(source_path)
    words = source["paragraph"].split()
    split = len(words) // 2
    revision_root = Path(payload["revision_root"]).resolve(); revision_root.mkdir(parents=True, exist_ok=True)
    artifact = revision_root / "artifact.pdf"
    wrap_plan = revision_root / "wrap-plan.jsonl"
    rendered = render_source(source_path, artifact, wrap_plan, split_index=split)

    old_contract_path = Path(payload["contract_path"]).resolve(strict=True)
    contract = json.loads(old_contract_path.read_text(encoding="utf-8"))
    old_artifact = Path(contract["artifacts"][0]).resolve(strict=True)
    contract["artifacts"] = [str(artifact.resolve())]
    quality = contract.setdefault("artifact_quality", {})
    layout = quality.setdefault("layout_typography", {})
    layout["wrap_plan_receipt"] = str(wrap_plan.resolve())
    layout["wrap_plan_receipt_sha256"] = repair_controller.sha256(wrap_plan)
    old_evidence = Path(payload["evidence_root"]).resolve()
    new_evidence = revision_root / "evidence"
    _regenerate_evidence(old_evidence, new_evidence, old_artifact, artifact)
    contract_path = revision_root / "contract.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    output = {
        "schema_version": "docs-generic-pdf-repair-output-1",
        "adapter_id": ADAPTER_ID,
        "plan_hash": plan["plan_hash"],
        "source_sha256": repair_controller.sha256(source_path),
        "source_content_identity_sha256": source["paragraph_sha256"],
        "artifact": repair_controller.handle(artifact),
        "wrap_plan": repair_controller.handle(wrap_plan),
        "contract": repair_controller.handle(contract_path),
        "evidence_set_id": repair_controller.digest(sorted(
            [repair_controller.handle(path) for path in new_evidence.iterdir() if path.is_file()],
            key=lambda item: item["path"],
        )),
    }
    output_path = revision_root / "adapter-output.json"
    output_path.write_bytes(repair_controller.canonical(output) + b"\n")
    return {
        "contract_path": str(contract_path),
        "artifact_path": str(artifact),
        "adapter_output_path": str(output_path),
        "source_content_identity_sha256": rendered["source_content_identity_sha256"],
    }


def register() -> repair_controller.Adapter:
    return repair_controller.register_adapter(
        adapter_id=ADAPTER_ID,
        capabilities={("safe-wrap", "wrap_adjustment")},
        producer_kind="generic-json-pdf-layout",
        invoke=invoke,
        source_path=Path(__file__),
    )
