from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import common, generic_pdf_repair_adapter as generic, repair_controller as repair
import production_retry_guard as retry_guard


def test_fixture_inventory_covers_required_positive_and_negative_routes():
    value = json.loads((ROOT / "tests/fixtures/artifact-repair-fixtures.json").read_text(encoding="utf-8"))
    assert value["schema_version"] == "docs-artifact-repair-fixtures-1"
    assert {item["id"] for item in value["cases"]} == {
        "rendered-orphan-ratio-repair-pass", "unsupported-issue-block", "review-only-block",
        "missing-adapter-block", "tampered-plan-block", "stale-scan-block",
        "cross-task-identity-block", "max-attempt-bound-block", "no-progress-same-hash-block",
        "semantic-mutation-block", "old-root-write-block", "adapter-error-block",
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha(path)}


def _load_checker():
    spec = importlib.util.spec_from_file_location("repair_checker_fixture", ROOT / "check_docs_artifact_quality.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _source(tmp_path: Path) -> Path:
    paragraph = "Alpha! beta! gamma! delta! epsilon! zeta! eta! theta! iota! kappa! lambda! mu! nu! omega"
    value = {
        "schema_version": generic.SOURCE_SCHEMA_VERSION,
        "paragraph": paragraph,
        "paragraph_sha256": hashlib.sha256(paragraph.encode()).hexdigest(),
        "split_index": 13,
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _rule_contracts(suite: Path) -> dict:
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


def _contract(source: Path, artifact: Path, wrap_plan: Path, suite: Path) -> dict:
    return {
        "schema_version": "docs-artifact-quality-contract-1", "artifact_kind": "pdf_document",
        "artifacts": [str(artifact.resolve())],
        "authoritative_source": {**_handle(source), "readback_mode": "generic-json-source"},
        "visible_criteria": [{"id": f"v{i}"} for i in range(1, 6)],
        "rule_contracts": _rule_contracts(suite),
        "artifact_quality": {"required": True, "layout_typography": {
            "required": True, "wrap_plan_receipt": str(wrap_plan.resolve()),
            "wrap_plan_receipt_sha256": _sha(wrap_plan),
        }},
    }


def _evidence(rule: str, contract: dict, artifact: Path) -> dict:
    spec = contract["rule_contracts"][rule]
    checks = {
        "AQ-CHANGE-01": {"id": "change-contract", "observed_scope_ids": spec.get("approved_scope_ids"), "changed_preserved_field_ids": []},
        "AQ-IDENTITY-01": {"id": "identity-contract", "identities": []},
        "AQ-ASSET-01": {"id": "asset-contract", "assets": []},
        "AQ-AUTHORITY-01": {"id": "authority-contract", "observed_owner_ids": ["owner"], "duplicate_owner_ids": []},
        "AQ-BOUNDARY-01": {"id": "boundary-contract", "clipped": False, "overflow": False, "clipped_count": 0, "overflow_count": 0, "image_cut_count": 0},
        "AQ-RELATION-01": {"id": "relation-contract", "relations": []},
        "AQ-SCOPE-01": {"id": "scope-contract", "protected_region_ids": ["protected"], "baseline_sha256": "a" * 64},
        "AQ-VISIBLE-01": {"id": "visible-contract", "criterion_ids": ["v1", "v2", "v3", "v4", "v5"], "image_evidence_sha256": "b" * 64},
        "AQ-FIXTURE-01": {"id": "fixture-contract", "suite": spec.get("suite"), "observed_rule_failures": ["none"]},
        "AQ-LOCAL-GLOBAL-01": {"id": "local-global-contract", "required": False},
    }[rule]
    checks["status"] = "pass"
    return {
        "schema_version": "docs-artifact-quality-evidence-1", "provenance": {"kind": "checker", "id": "fixture"},
        "rule_id": rule, "status": "pass", "review_status": "pass",
        "artifact_path": str(artifact.resolve()), "artifact_sha256": _sha(artifact), "checks": [checks],
    }


def _fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = _source(tmp_path)
    artifact = tmp_path / "old-root.pdf"
    wrap_plan = tmp_path / "old-wrap-plan.jsonl"
    generic.render_source(source, artifact, wrap_plan)
    contract = _contract(source, artifact, wrap_plan, Path(__file__))
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    evidence = tmp_path / "evidence"; evidence.mkdir()
    for rule in common.CONTRACT_RULES:
        (evidence / f"{artifact.name}.{rule}.json").write_text(json.dumps(_evidence(rule, contract, artifact)), encoding="utf-8")
    return source, artifact, contract_path, evidence


def test_opt_in_fresh_process_repairs_rendered_orphan_and_final_checker_passes(tmp_path):
    source, artifact, contract, evidence = _fixture(tmp_path)
    source_data = json.loads(source.read_text())
    old_hash = _sha(artifact)
    initial = tmp_path / "initial-aq.json"
    controller = tmp_path / "controller.json"
    revision = tmp_path / "revisions"
    command = [
        sys.executable, str(ROOT / "check_docs_artifact_quality.py"),
        "--contract", str(contract), "--receipt", str(initial), "--evidence-dir", str(evidence),
        "--auto-repair", "--repair-adapter", generic.ADAPTER_ID,
        "--repair-source", str(source), "--repair-content-identity-sha256", source_data["paragraph_sha256"],
        "--repair-root", str(revision), "--repair-controller-receipt", str(controller),
        "--repair-task-id", "t_fixture", "--repair-producer-run-id", "fixture-run", "--repair-max-attempts", "1",
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    initial_data = json.loads(initial.read_text())
    layout = next(rule for rule in initial_data["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    scan = json.loads(Path(layout["evidence"]["path"]).read_text())
    assert initial_data["status"] == "block"
    assert {item["rule"] for item in scan["HARD_FAIL"]} == {"rendered_orphan_ratio"}
    controller_data = json.loads(controller.read_text())
    final_receipt = json.loads(Path(controller_data["final_aq_receipt"]["path"]).read_text())
    assert controller_data["status"] == "pass" and controller_data["checker_exit_code"] == 0
    assert final_receipt["status"] == "pass" and len(controller_data["attempts"]) == 1
    assert _sha(artifact) == old_hash
    assert Path(controller_data["final_artifact"]["path"]) != artifact
    assert controller_data["source_content_identity_sha256"] == source_data["paragraph_sha256"]
    assert repair.validate_controller_receipt(controller, expected_task_id="t_fixture", final_aq_receipt=Path(controller_data["final_aq_receipt"]["path"])) == []

    plan_path = Path(controller_data["attempts"][0]["plan"]["path"])
    plan = json.loads(plan_path.read_text())
    for schema_name, value in (
        ("docs-artifact-repair-plan-1.json", plan),
        ("docs-artifact-repair-controller-receipt-1.json", controller_data),
    ):
        schema = json.loads((ROOT / "schemas" / schema_name).read_text())
        assert list(Draft202012Validator(schema).iter_errors(value)) == []


def test_ordinary_checker_call_remains_read_only_and_backward_compatible(tmp_path):
    checker = _load_checker(); _, artifact, contract, evidence = _fixture(tmp_path)
    old_hash = _sha(artifact)
    receipt, code = checker.build_receipt(contract, evidence_dir=evidence)
    assert code == 2 and receipt["status"] == "block"
    assert _sha(artifact) == old_hash
    assert not (tmp_path / "revisions").exists()


def test_unsupported_review_only_missing_adapter_and_attempt_bound_fail_closed(tmp_path):
    checker = _load_checker(); source, _, contract, evidence = _fixture(tmp_path)
    receipt, _ = checker.build_receipt(contract, evidence_dir=evidence)
    receipt_path = tmp_path / "receipt.json"; receipt_path.write_text(json.dumps(receipt))
    generic.register()
    adapter = repair.get_adapter(generic.ADAPTER_ID)
    with pytest.raises(ValueError, match="max_attempts"):
        repair.run(task_id="t_fixture", contract_path=contract, source_path=source,
                   content_identity_sha256=json.loads(source.read_text())["paragraph_sha256"],
                   initial_aq_receipt=receipt_path, adapter_id=generic.ADAPTER_ID,
                   revision_root=tmp_path / "r", max_attempts=3, checker=lambda *_: ({}, 2),
                   evidence_root=evidence, output_receipt=tmp_path / "c.json", producer_run_id="r")
    repair._ADAPTERS.pop(generic.ADAPTER_ID)
    with pytest.raises(ValueError, match="not explicitly registered"):
        repair.get_adapter(generic.ADAPTER_ID)
    repair._ADAPTERS[generic.ADAPTER_ID] = adapter

    layout = next(rule for rule in receipt["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    scan_path = Path(layout["evidence"]["path"])
    scan = json.loads(scan_path.read_text())
    scan["HARD_FAIL"] = []; scan["issues"] = scan["REVIEW_CANDIDATE"]
    scan["hard_issue_count"] = 0; scan["status"] = "review"
    scan_path.write_text(json.dumps(scan)); layout["evidence"]["sha256"] = _sha(scan_path)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="not a blocking artifact scan"):
        repair.normalize_hard_failures(
            receipt_path, task_id="t_fixture", contract_path=contract, source_path=source,
            adapter=adapter, attempt=1, max_attempts=1, producer_run_id="r",
            new_revision_root=tmp_path / "new", content_identity_sha256=json.loads(source.read_text())["paragraph_sha256"],
        )


def test_plan_tamper_stale_scan_cross_task_and_retry_guard_binding(tmp_path):
    checker = _load_checker(); source, _, contract, evidence = _fixture(tmp_path)
    receipt, _ = checker.build_receipt(contract, evidence_dir=evidence)
    receipt_path = tmp_path / "receipt.json"; receipt_path.write_text(json.dumps(receipt))
    adapter = generic.register()
    identity = json.loads(source.read_text())["paragraph_sha256"]
    plan = repair.normalize_hard_failures(
        receipt_path, task_id="t_fixture", contract_path=contract, source_path=source,
        adapter=adapter, attempt=1, max_attempts=1, producer_run_id="r",
        new_revision_root=tmp_path / "new", content_identity_sha256=identity,
    )
    tampered = copy.deepcopy(plan); tampered["attempt"] = 2
    assert any("attempt" in item or "tamper" in item for item in repair.validate_plan(tampered, expected_task_id="t_fixture", adapter=adapter))
    assert "cross-task" in " ".join(repair.validate_plan(plan, expected_task_id="t_other", adapter=adapter))
    Path(plan["scan"]["path"]).write_bytes(Path(plan["scan"]["path"]).read_bytes() + b"\n")
    assert "scan handle drift" in " ".join(repair.validate_plan(plan, expected_task_id="t_fixture", adapter=adapter))


def test_unsupported_hard_fail_is_not_automatically_repaired(tmp_path):
    checker = _load_checker(); source, _, contract, evidence = _fixture(tmp_path)
    receipt, _ = checker.build_receipt(contract, evidence_dir=evidence)
    layout = next(rule for rule in receipt["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    scan_path = Path(layout["evidence"]["path"])
    scan = json.loads(scan_path.read_text())
    for collection in (scan["HARD_FAIL"], scan["issues"]):
        for issue in collection:
            issue["rule"] = "unknown_semantic_layout_change"
    scan_path.write_text(json.dumps(scan)); layout["evidence"]["sha256"] = _sha(scan_path)
    receipt_path = tmp_path / "unsupported-receipt.json"; receipt_path.write_text(json.dumps(receipt))
    adapter = generic.register()
    with pytest.raises(ValueError, match="unsupported HARD_FAIL issue"):
        repair.normalize_hard_failures(
            receipt_path, task_id="t_fixture", contract_path=contract, source_path=source,
            adapter=adapter, attempt=1, max_attempts=1, producer_run_id="r",
            new_revision_root=tmp_path / "new", content_identity_sha256=json.loads(source.read_text())["paragraph_sha256"],
        )


def test_hash_bound_plan_and_adapter_fingerprint_are_required_by_retry_guard(tmp_path):
    checker = _load_checker(); source, artifact, contract, evidence = _fixture(tmp_path)
    receipt, _ = checker.build_receipt(contract, evidence_dir=evidence)
    receipt_path = tmp_path / "receipt.json"; receipt_path.write_text(json.dumps(receipt))
    adapter = generic.register(); identity = json.loads(source.read_text())["paragraph_sha256"]
    plan = repair.normalize_hard_failures(
        receipt_path, task_id="t_fixture", contract_path=contract, source_path=source,
        adapter=adapter, attempt=1, max_attempts=1, producer_run_id="r",
        new_revision_root=tmp_path / "new", content_identity_sha256=identity,
    )
    plan_path = repair.write_plan(tmp_path / "plan.json", plan)
    state = {
        "schema_version": retry_guard.SCHEMA_VERSION, "policy_version": retry_guard.POLICY_VERSION,
        "task_id": "t_fixture", "status": "hard_fail", "baseline_status": "fail",
        "primary_delivery": "none", "effective_max_retries": 1, "retry_limit_source": "task.max_retries",
        "retry_count": 0, "production_fingerprint": "a" * 64,
        "repair_plan_path": str(plan_path), "admitted_adapter_fingerprint": adapter.fingerprint,
        "primary_artifacts": [{"path": str(artifact), "sha256": _sha(artifact)}],
    }
    blocked = retry_guard.evaluate_production_call("write_file", {}, state, task_id="t_fixture")
    assert blocked["decision"] == "block"
    allowed = retry_guard.evaluate_production_call("write_file", {
        "repair_plan_path": str(plan_path), "repair_adapter_fingerprint": adapter.fingerprint,
    }, state, task_id="t_fixture")
    assert allowed["decision"] == "allow" and "hash-bound" in allowed["reason"]


def test_semantic_mutation_no_progress_adapter_error_and_old_root_write_are_blocked(tmp_path):
    checker = _load_checker(); source, artifact, contract, evidence = _fixture(tmp_path)
    receipt, _ = checker.build_receipt(contract, evidence_dir=evidence)
    receipt_path = tmp_path / "receipt.json"; receipt_path.write_text(json.dumps(receipt))
    identity = json.loads(source.read_text())["paragraph_sha256"]

    def run_bad(adapter_id: str, invoke, pattern: str):
        repair.register_adapter(adapter_id=adapter_id, capabilities={("safe-wrap", "wrap_adjustment")}, producer_kind="negative", invoke=invoke, source_path=Path(__file__))
        with pytest.raises(ValueError, match=pattern):
            repair.run(task_id="t_fixture", contract_path=contract, source_path=source,
                       content_identity_sha256=identity, initial_aq_receipt=receipt_path,
                       adapter_id=adapter_id, revision_root=tmp_path / adapter_id, max_attempts=1,
                       checker=lambda *_: ({}, 2), evidence_root=evidence,
                       output_receipt=tmp_path / f"{adapter_id}.json", producer_run_id="r")

    def semantic(payload):
        out = payload["revision_root"] / "out.pdf"; out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"new")
        marker = payload["revision_root"] / "out.json"; marker.write_text("{}")
        return {"contract_path": str(contract), "artifact_path": str(out), "adapter_output_path": str(marker), "source_content_identity_sha256": "f" * 64}
    run_bad("negative-semantic", semantic, "semantic source identity changed")

    def no_progress(payload):
        out = payload["revision_root"] / "out.pdf"; out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(artifact.read_bytes())
        marker = payload["revision_root"] / "out.json"; marker.write_text("{}")
        return {"contract_path": str(contract), "artifact_path": str(out), "adapter_output_path": str(marker), "source_content_identity_sha256": identity}
    run_bad("negative-no-progress", no_progress, "same artifact hash")

    def old_root(payload):
        marker = payload["revision_root"] / "out.json"; marker.parent.mkdir(parents=True, exist_ok=True); marker.write_text("{}")
        return {"contract_path": str(contract), "artifact_path": str(artifact), "adapter_output_path": str(marker), "source_content_identity_sha256": identity}
    run_bad("negative-old-root", old_root, "old-root write")

    def adapter_error(_payload):
        raise RuntimeError("adapter exploded")
    run_bad("negative-adapter-error", adapter_error, "adapter exploded")
