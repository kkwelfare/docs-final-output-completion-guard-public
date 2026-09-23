from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import common, composition, execution_contract, html_pdf_harness, rendered_readback, safe_wrap


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_checker():
    spec = importlib.util.spec_from_file_location("html_semantic_surface_checker", ROOT / "check_docs_artifact_quality.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha(path)}


def _rules(suite: Path) -> dict:
    return {
        "AQ-CHANGE-01": {"approved_scope_ids": ["scope"], "preserved_field_ids": ["preserved"]},
        "AQ-IDENTITY-01": {"identities": []}, "AQ-ASSET-01": {"assets": []},
        "AQ-AUTHORITY-01": {"owner_id": "owner"},
        "AQ-BOUNDARY-01": {"clipped": False, "overflow": False}, "AQ-RELATION-01": {"relations": []},
        "AQ-SCOPE-01": {"protected_region_ids": ["protected"], "baseline_sha256": "a" * 64},
        "AQ-VISIBLE-01": {"criterion_ids": ["v1", "v2", "v3", "v4", "v5"], "image_evidence_sha256": "b" * 64},
        "AQ-FIXTURE-01": {"suite": _handle(suite), "expected_rule_failures": ["none"]},
        "AQ-LOCAL-GLOBAL-01": {"required": False},
    }


def _manual_evidence(rule: str, contract: dict, artifact: Path) -> dict:
    spec = contract["rule_contracts"][rule]
    if rule == "AQ-CHANGE-01":
        checks = {"id": "change-contract", "observed_scope_ids": spec["approved_scope_ids"], "changed_preserved_field_ids": []}
    elif rule == "AQ-IDENTITY-01": checks = {"id": "identity-contract", "identities": []}
    elif rule == "AQ-ASSET-01": checks = {"id": "asset-contract", "assets": []}
    elif rule == "AQ-AUTHORITY-01": checks = {"id": "authority-contract", "observed_owner_ids": ["owner"], "duplicate_owner_ids": []}
    elif rule == "AQ-BOUNDARY-01": checks = {"id": "boundary-contract", "clipped": False, "overflow": False, "clipped_count": 0, "overflow_count": 0, "image_cut_count": 0}
    elif rule == "AQ-RELATION-01": checks = {"id": "relation-contract", "relations": []}
    elif rule == "AQ-SCOPE-01": checks = {"id": "scope-contract", "protected_region_ids": ["protected"], "baseline_sha256": "a" * 64}
    elif rule == "AQ-VISIBLE-01": checks = {"id": "visible-contract", "criterion_ids": ["v1", "v2", "v3", "v4", "v5"], "image_evidence_sha256": "b" * 64}
    elif rule == "AQ-FIXTURE-01": checks = {"id": "fixture-contract", "suite": spec["suite"], "observed_rule_failures": ["none"]}
    else: checks = {"id": "local-global-contract", "required": False}
    checks["status"] = "pass"
    return {"schema_version": "docs-artifact-quality-evidence-1", "provenance": {"kind": "checker", "id": "fixture"},
            "rule_id": rule, "status": "pass", "review_status": "pass",
            "artifact_path": str(artifact.resolve()), "artifact_sha256": _sha(artifact), "checks": [checks]}


def _chrome() -> str:
    binary = shutil.which("google-chrome") or shutil.which("chromium")
    assert binary, "headless Chrome is required"
    return binary


def _policy() -> dict:
    return {
        "renderer_policy": "fresh-readback",
        "semantic_surface_policy": {
            "schema_version": "docs-semantic-surface-policy-1",
            "tokens": [
                {"id": "surface-flat", "surface_tier": "flat", "topology": "none",
                 "container_scope": "none", "visual_weight": "none", "elevation_class": "none"},
                {"id": "surface-light", "surface_tier": "light-group", "topology": "shared-group",
                 "container_scope": "group", "visual_weight": "subtle", "elevation_class": "none"},
                {"id": "surface-card", "surface_tier": "card", "topology": "per-item",
                 "container_scope": "semantic-unit", "visual_weight": "standard", "elevation_class": "low"},
                {"id": "surface-band", "surface_tier": "emphasis", "topology": "band",
                 "container_scope": "semantic-unit", "visual_weight": "strong", "elevation_class": "none"},
            ],
            "regions": [
                {"region_id": "region-flat", "caller_role": "generic ordinary role",
                 "intent_class": "ordinary", "page_index": 0, "selected_token_id": "surface-flat",
                 "sequence_scope_id": "generic-sequence", "sequence_index": 0},
                {"region_id": "region-light", "caller_role": "generic group role",
                 "intent_class": "sequence", "page_index": 0, "selected_token_id": "surface-light",
                 "semantic_group_id": "group-light", "sequence_scope_id": "generic-sequence", "sequence_index": 1},
                {"region_id": "region-card", "caller_role": "generic unit role",
                 "intent_class": "enumerate", "page_index": 0, "selected_token_id": "surface-card",
                 "semantic_group_id": "group-card", "sequence_scope_id": "generic-sequence", "sequence_index": 2},
                {"region_id": "region-band", "caller_role": "generic emphasis role",
                 "intent_class": "memorize", "page_index": 0, "selected_token_id": "surface-band",
                 "semantic_group_id": "group-band", "sequence_scope_id": "generic-sequence", "sequence_index": 3},
            ],
            "sequence_policy": {
                "mode": "review", "prominent_tiers": ["card", "emphasis"],
                "max_consecutive_pages": 2, "model_call_if_zero_candidates": False,
            },
        },
    }


def _issue(root: Path, run_id: int) -> Path:
    source = root / "source-seed.bin"
    source.write_bytes(b"generic-source")
    contract = execution_contract.issue_contract(
        source=source, policy=_policy(), scope_ids=["html-semantic-surface"],
        acceptance_ids=["html-semantic-surface"], producer_run_id=run_id,
    )
    path = root / "execution-contract.json"
    path.write_text(json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    return path


def _frames() -> list[dict]:
    rows = [
        ("Generic label A", "copy-flat", 52),
        ("Generic label B", "copy-light", 212),
        ("Generic label C", "copy-card", 352),
        ("Generic label D", "copy-band", 492),
    ]
    return [{
        "text": text, "frame_id": frame_id, "page": 1,
        "frame_bbox_pt": (64, top, 360, 48), "margins_pt": (8, 8),
        "font_size_pt": 16, "max_lines": 2,
    } for text, frame_id, top in rows]


def _bindings(*, negative_flat: bool = False) -> list[dict]:
    return [
        {"region_id": "region-flat", "caller_role": "generic ordinary role",
         "primitive_id": "surface-flat-independent" if negative_flat else None,
         "bbox_pt": [36, 36, 500, 90] if negative_flat else None},
        {"region_id": "region-light", "caller_role": "generic group role",
         "primitive_id": "surface-light", "bbox_pt": [36, 176, 500, 100]},
        {"region_id": "region-card", "caller_role": "generic unit role",
         "primitive_id": "surface-card", "bbox_pt": [36, 316, 500, 100]},
        {"region_id": "region-band", "caller_role": "generic emphasis role",
         "primitive_id": "surface-band", "bbox_pt": [36, 456, 500, 100]},
    ]


def _produce(root: Path, *, negative_flat: bool, run_id: int) -> dict[str, Path]:
    root.mkdir()
    contract = _issue(root, run_id)
    html = root / "surface.html"
    wrap = root / "wrap-plan.json"
    plan = root / "semantic-surface-plan.jsonl"
    pdf = root / "surface.pdf"
    html_pdf_harness.write_controlled_html_frames(
        _frames(), html, wrap,
        semantic_surface_contract_path=contract,
        semantic_surface_bindings=_bindings(negative_flat=negative_flat),
        semantic_surface_plan_log_path=plan,
    )
    completed = subprocess.run([
        _chrome(), "--headless", "--no-sandbox", "--disable-gpu",
        "--print-to-pdf=" + str(pdf), html.as_uri(),
    ], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    html_pdf_harness.bind_rendered_pdf(wrap, html, pdf)
    outputs = html_pdf_harness.finalize_html_semantic_surfaces(
        contract_path=contract, plan_log_path=plan, html_path=html, pdf_path=pdf,
        producer_receipt_path=root / "producer-receipt.json",
        composition_evidence_path=root / "composition-evidence.json",
        readback_dir=root / "readback",
    )
    return {"html": html, "wrap": wrap, "plan": plan, "pdf": pdf, **outputs}


def test_html_semantic_surface_positive_real_pdf_png_and_existing_l1_path(tmp_path):
    result = _produce(tmp_path / "positive", negative_flat=False, run_id=2210)
    contract, contract_errors = execution_contract.load_contract(result["contract"], require_artifacts=True)
    assert contract_errors == [] and contract is not None
    producer, producer_errors = execution_contract.validate_producer_receipt(result["producer_receipt"], contract)
    assert producer_errors == [] and producer is not None
    evidence = json.loads(result["composition_evidence"].read_text(encoding="utf-8"))
    surface = evidence["semantic_surfaces"]
    assert surface["HARD_FAIL"] == [] and len(surface["regions"]) == 4
    visible = [row for row in surface["regions"] if row["shape_id"] is not None]
    assert len(visible) == 3
    assert all(row["rendered_primitive"]["surface_present"] for row in visible)
    assert all(row["rendered_primitive"]["fill_present"] for row in visible)
    assert next(row for row in visible if row["region_id"] == "region-card")["primitive_application"]["shadow_applied"] is True
    manifest = json.loads(result["rendered_readback"].read_text(encoding="utf-8"))
    assert manifest["surface_readback"]["status"] == "pass"
    assert manifest["surface_readback"]["surface_presence_count"] == 3
    assert rendered_readback.validate_handle(
        {"path": str(result["rendered_readback"]), "sha256": _sha(result["rendered_readback"])},
        result["pdf"], producer_run_id=contract["producer_run_id"], artifact_set_id=contract["artifact_set_id"],
        execution_contract_path=result["contract"], producer_receipt_path=result["producer_receipt"],
    ) == []
    rows = composition.evaluate({
        "quality_foundation": {"spacing_tokens_pt": {"S": 8}},
        "artifact_profile": {"id": "html-pdf", "values": {}},
        "composition_evidence": {"path": str(result["composition_evidence"]), "sha256": _sha(result["composition_evidence"])},
    }, artifact_kind="pdf_html", baseline_statuses=[{"status": "pass"}],
       source=result["html"], artifact=result["pdf"])
    assert next(row for row in rows if row["rule_id"] == "L1-SURFACE-01")["status"] == "pass"
    wrap_schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-2.json").read_text(encoding="utf-8"))
    producer_schema = json.loads((ROOT / "schemas/docs-producer-execution-receipt-1.json").read_text(encoding="utf-8"))
    readback_schema = json.loads((ROOT / "schemas/docs-rendered-readback-1.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(wrap_schema).iter_errors(json.loads(result["wrap"].read_text()))) == []
    assert list(Draft202012Validator(producer_schema).iter_errors(producer)) == []
    assert list(Draft202012Validator(readback_schema).iter_errors(manifest)) == []
    serialized = "".join(path.read_text(encoding="utf-8") for path in
                         (result["plan"], result["producer_receipt"], result["composition_evidence"], result["rendered_readback"]))
    assert "Generic label" not in serialized and '"text"' not in serialized.lower()


def test_html_semantic_surface_flat_independent_primitive_is_identity_hard_fail(tmp_path):
    result = _produce(tmp_path / "negative", negative_flat=True, run_id=2211)
    evidence = json.loads(result["composition_evidence"].read_text(encoding="utf-8"))
    failures = evidence["semantic_surfaces"]["HARD_FAIL"]
    assert {row["rule_id"] for row in failures} == {"SURFACE-IDENTITY-01"}
    assert any(row["rule_id"] == "SURFACE-IDENTITY-01" and row["region_id"] == "region-flat" for row in failures)
    readback = json.loads(result["rendered_readback"].read_text(encoding="utf-8"))["surface_readback"]
    assert readback["status"] == "HARD_FAIL"
    assert any(row["rule_id"] == "SURFACE-IDENTITY-01" for row in readback["HARD_FAIL"])


def test_declared_html_composition_evidence_reaches_existing_aq_layout_surface_readback(tmp_path):
    result = _produce(tmp_path / "checker", negative_flat=False, run_id=2212)
    checker = _load_checker()
    wrap = json.loads(result["wrap"].read_text(encoding="utf-8"))
    quality_contract = {
        "schema_version": "docs-artifact-quality-contract-1",
        "artifact_kind": "pdf_html",
        "artifacts": [str(result["pdf"].resolve()), str(result["html"].resolve())],
        "authoritative_source": {**_handle(result["html"]), "readback_mode": "editable-html"},
        "visible_criteria": [{"id": f"v{i}"} for i in range(1, 6)],
        "rule_contracts": _rules(Path(__file__)),
        "execution_contract": {
            "path": str(result["contract"].resolve()),
            "producer_receipt": str(result["producer_receipt"].resolve()),
        },
        "artifact_quality": {
            "required": True,
            "layout_typography": {
                "required": True, "expected_cjk_fonts": [safe_wrap.resolve_cjk_font()["family"]],
                "wrap_plan_receipt": str(result["wrap"].resolve()),
                "wrap_plan_receipt_sha256": _sha(result["wrap"]),
            },
            "html_pdf": {"required": True, "source_path": str(result["html"].resolve()),
                         "receipt_path": str(result["wrap"].resolve())},
            "quality_foundation": {"spacing_tokens_pt": {"S": 8},
                                   "semantic_regions": {"minimum_gutter_pt": 0}},
            "artifact_profile": {"id": "html-pdf", "values": {}},
            "composition_evidence": _handle(result["composition_evidence"]),
        },
    }
    assert wrap["status"] == "pass"
    contract_path = tmp_path / "quality-contract.json"
    contract_path.write_text(json.dumps(quality_contract), encoding="utf-8")
    evidence_dir = tmp_path / "quality-evidence"
    evidence_dir.mkdir()
    for rule in common.CONTRACT_RULES:
        (evidence_dir / f"{result['pdf'].name}.{rule}.json").write_text(
            json.dumps(_manual_evidence(rule, quality_contract, result["pdf"])), encoding="utf-8",
        )
    receipt, code = checker.build_receipt(contract_path, evidence_dir=evidence_dir)
    pdf_entry = next(entry for entry in receipt["entries"] if entry["artifact"]["path"] == str(result["pdf"].resolve()))
    layout = next(rule for rule in pdf_entry["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert code == 2 and receipt["status"] == "block"
    assert layout["status"] == "fail"
    layout_scan = json.loads(Path(layout["evidence"]["path"]).read_text(encoding="utf-8"))
    assert layout_scan["hard_issue_count"] == 0
    assert layout_scan["surface_readback"]["path"] == pdf_entry["actual_delivery_readback"]["path"]
    readback_path = Path(pdf_entry["actual_delivery_readback"]["path"])
    readback = json.loads(readback_path.read_text(encoding="utf-8"))
    assert readback["surface_readback"]["status"] == "pass"
    assert readback["surface_readback"]["surface_presence_count"] == 3
    assert next(row for row in receipt["quality_layers"] if row["rule_id"] == "L1-SURFACE-01")["status"] == "pass"


def test_policy_absent_legacy_bytes_and_receipt_shape_are_unchanged(tmp_path):
    html = tmp_path / "legacy.html"
    receipt = tmp_path / "legacy.receipt.json"
    result = html_pdf_harness.write_controlled_html_frames([{
        "text": "Generic legacy label", "frame_id": "legacy", "page": 1,
        "frame_bbox_pt": (36, 36, 200, 60), "margins_pt": (6, 6),
        "font_size_pt": 14, "max_lines": 2,
    }], html, receipt)
    assert _sha(html) == "86d8d5253542a238ccd76a60939073593a8127a784aff297762954a427fd6ce5"
    assert _sha(receipt) == "26910b555ce43df61d871fa25be67a0e02a9631b808b5770d7a126a0adab992e"
    assert result["schema_version"] == "docs-html-pdf-wrap-plan-2"
    assert not any(key.startswith("semantic_surface") for key in result)
    assert not list(tmp_path.glob("*semantic*"))
