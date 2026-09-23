from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import delivery_workflow as workflow


def load_guard():
    spec = importlib.util.spec_from_file_location("delivery_workflow_guard_fixture", ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_file(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def harness_record(tmp_path: Path):
    source = write_file(tmp_path / "source.md", b"source")
    output = tmp_path / "rebuilt.pdf"
    record = workflow.issue_workflow(
        task_id="t_fixture", mode="harness_development", strategy="source_first_rebuild",
        source=source, output_path=output,
    )
    ledger = workflow.create_candidate_ledger(tmp_path / "harness-ledger.jsonl", record)
    record = workflow.attach_ledger(record, ledger)
    write_file(output, b"new artifact")
    return workflow.transition(record, to_state="submitted", result_artifact=output), ledger


def deadline_record(tmp_path: Path, *, candidate: bool = True):
    source = write_file(tmp_path / "source.md", b"source")
    artifact = write_file(tmp_path / "existing.pdf", b"before")
    before_hash = workflow.sha256(artifact)
    record = workflow.issue_workflow(
        task_id="t_fixture", mode="deadline_delivery", strategy="existing_artifact_repair",
        source=source, output_path=artifact, baseline_artifact=artifact,
    )
    ledger = workflow.create_candidate_ledger(tmp_path / "deadline-ledger.jsonl", record)
    if candidate:
        artifact.write_bytes(b"after")
        readback = write_file(tmp_path / "readback.json", b'{"status":"pass"}')
        workflow.append_candidate(
            ledger, record, issue_family="alignment", target_ids=["1" * 64],
            artifact_before_sha256=before_hash, artifact_after_sha256=workflow.sha256(artifact),
            before_geometry={"bbox_pt": [0, 0, 20, 8]}, after_geometry={"bbox_pt": [0, 1, 20, 9]},
            repair_operation="alignment_adjustment", renderer_readback=readback,
            genericization_candidate=True,
        )
    record = workflow.attach_ledger(record, ledger)
    if not candidate:
        artifact.write_bytes(b"submitted without candidates")
    return workflow.transition(record, to_state="submitted", result_artifact=artifact), ledger


def test_fixture_inventory_covers_required_routes_and_failures():
    value = json.loads((ROOT / "tests/fixtures/delivery-workflow-fixtures.json").read_text(encoding="utf-8"))
    ids = {item["id"] for item in value["cases"]}
    assert value["schema_version"] == "docs-delivery-workflow-fixtures-1"
    assert {
        "harness-source-first-new-identity-pass", "deadline-existing-artifact-repair-pass",
        "deadline-source-repair-pass", "submission-required-block", "record-tamper-block",
        "ledger-body-block", "ledger-pii-block", "cross-task-block", "mode-strategy-conflict-block",
        "artifact-hash-drift-block", "premature-hardening-block", "legacy-without-workflow-pass-through",
        "candidate-zero-pass", "bounded-hardening-pass",
    } == ids


def test_harness_requires_source_first_and_new_artifact_identity(tmp_path):
    source = write_file(tmp_path / "source.md", b"source")
    record = workflow.issue_workflow(
        task_id="t_fixture", mode="harness_development", strategy="source_first_rebuild",
        source=source, output_path=tmp_path / "new.pdf",
    )
    assert workflow.validate_workflow(record) == []
    with pytest.raises(ValueError, match="mode/strategy conflict"):
        workflow.issue_workflow(
            task_id="t_fixture", mode="harness_development", strategy="existing_artifact_repair",
            source=source, output_path=tmp_path / "new.pdf",
        )
    with pytest.raises(ValueError, match="new artifact identity"):
        workflow.issue_workflow(
            task_id="t_fixture", mode="harness_development", strategy="source_first_rebuild",
            source=source, output_path=source,
        )


def test_deadline_modes_require_incremental_existing_artifact_or_source_repair(tmp_path):
    source = write_file(tmp_path / "source.md", b"source")
    artifact = write_file(tmp_path / "artifact.pdf", b"artifact")
    existing = workflow.issue_workflow(
        task_id="t_fixture", mode="deadline_delivery", strategy="existing_artifact_repair",
        source=source, output_path=artifact, baseline_artifact=artifact,
    )
    source_repair = workflow.issue_workflow(
        task_id="t_fixture", mode="deadline_delivery", strategy="source_repair",
        source=source, output_path=source,
    )
    assert workflow.validate_workflow(existing) == []
    assert workflow.validate_workflow(source_repair) == []
    with pytest.raises(ValueError, match="mode/strategy conflict"):
        workflow.issue_workflow(
            task_id="t_fixture", mode="deadline_delivery", strategy="source_first_rebuild",
            source=source, output_path=artifact,
        )


def test_deadline_candidate_is_hash_chained_body_free_and_anonymized(tmp_path):
    submitted, ledger = deadline_record(tmp_path)
    snapshot, errors = workflow.ledger_snapshot(ledger, submitted)
    assert errors == [] and snapshot is not None
    assert len(snapshot["entries"]) == 1
    entry = snapshot["entries"][0]
    assert entry["previous_entry_hash"] == snapshot["header"]["header_hash"]
    assert entry["repair"]["producer_mutation"] is False
    assert workflow.validate_workflow(submitted, expected_task_id="t_fixture", require_completion=True) == []


def test_candidate_zero_is_valid_and_submittable(tmp_path):
    submitted, ledger = deadline_record(tmp_path, candidate=False)
    snapshot, errors = workflow.ledger_snapshot(ledger, submitted)
    assert errors == [] and snapshot == {"schema_version": workflow.LEDGER_SCHEMA_VERSION, "header": snapshot["header"], "entries": []}
    assert workflow.validate_workflow(submitted, require_completion=True) == []


def test_submission_gate_and_no_skipped_state_transitions(tmp_path):
    source = write_file(tmp_path / "source.md", b"source")
    output = tmp_path / "new.pdf"
    record = workflow.issue_workflow(
        task_id="t_fixture", mode="harness_development", strategy="source_first_rebuild",
        source=source, output_path=output,
    )
    assert "delivery workflow has not reached submitted state" in workflow.validate_workflow(record, require_completion=True)
    with pytest.raises(ValueError, match="state transition"):
        workflow.transition(record, to_state="hardening_ready")


def test_record_tamper_mode_conflict_and_cross_task_fail_closed(tmp_path):
    submitted, _ = harness_record(tmp_path)
    tampered = copy.deepcopy(submitted); tampered["state"] = "hardened"
    assert any("tamper" in error for error in workflow.validate_workflow(tampered))
    conflicted = copy.deepcopy(submitted); conflicted["strategy"] = "source_repair"; conflicted = workflow._seal(conflicted)
    assert "delivery workflow mode/strategy conflict" in workflow.validate_workflow(conflicted)
    assert "delivery workflow cross-task identity mismatch" in workflow.validate_workflow(submitted, expected_task_id="t_other")


def test_ledger_tamper_body_pii_and_cross_task_fail_closed(tmp_path):
    submitted, ledger = deadline_record(tmp_path)
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    body = copy.deepcopy(records); body[1]["body"] = "forbidden"
    assert any("schema mismatch" in error or "body" in error for error in workflow.validate_ledger_records(body, submitted))
    pii = copy.deepcopy(records); pii[1]["target_ids"] = ["person@example.com"]
    assert any("PII" in error or "anonymized" in error for error in workflow.validate_ledger_records(pii, submitted))
    cross = copy.deepcopy(records); cross[0]["task_id"] = "t_other"
    assert any("cross-task" in error for error in workflow.validate_ledger_records(cross, submitted))


def test_artifact_and_ledger_hash_drift_fail_closed(tmp_path):
    submitted, ledger = deadline_record(tmp_path)
    Path(submitted["result_artifact"]["path"]).write_bytes(b"drift")
    assert any("result artifact path/hash/size drift" in error for error in workflow.validate_workflow(submitted))

    clean, clean_ledger = deadline_record(tmp_path / "clean")
    clean_ledger.write_bytes(clean_ledger.read_bytes() + b"\n")
    assert any("candidate ledger path/hash/size drift" in error for error in workflow.validate_workflow(clean))


def test_source_repair_can_submit_same_source_identity_without_generic_hardening(tmp_path):
    source = write_file(tmp_path / "source.md", b"before")
    before = workflow.sha256(source)
    record = workflow.issue_workflow(
        task_id="t_fixture", mode="deadline_delivery", strategy="source_repair",
        source=source, output_path=source,
    )
    ledger = workflow.create_candidate_ledger(tmp_path / "source-ledger.jsonl", record)
    source.write_bytes(b"after")
    readback = write_file(tmp_path / "source-readback.json", b'{"status":"pass"}')
    workflow.append_candidate(
        ledger, record, issue_family="safe-wrap", target_ids=["5" * 64],
        artifact_before_sha256=before, artifact_after_sha256=workflow.sha256(source),
        before_geometry={"bbox_pt": [0, 0, 50, 20]}, after_geometry={"bbox_pt": [0, 0, 48, 20]},
        repair_operation="wrap_adjustment", renderer_readback=readback,
        genericization_candidate=False,
    )
    submitted = workflow.transition(workflow.attach_ledger(record, ledger), to_state="submitted", result_artifact=source)
    assert workflow.validate_workflow(submitted, require_completion=True) == []


def test_append_after_submission_and_premature_hardening_are_rejected(tmp_path):
    submitted, ledger = deadline_record(tmp_path)
    with pytest.raises(ValueError, match="only during deadline-delivery repair"):
        workflow.append_candidate(
            ledger, submitted, issue_family="alignment", target_ids=["2" * 64],
            artifact_before_sha256="3" * 64, artifact_after_sha256="4" * 64,
            before_geometry={"bbox_pt": [0, 0, 1, 1]}, after_geometry={"bbox_pt": [0, 0, 1, 1]},
            repair_operation="alignment_adjustment", renderer_readback=Path(submitted["result_artifact"]["path"]),
            genericization_candidate=False,
        )
    with pytest.raises(ValueError, match="state transition"):
        workflow.transition(submitted, to_state="hardened", hardening_receipt=ledger)


def test_bounded_post_delivery_hardening_maps_only_submitted_candidates(tmp_path):
    submitted, ledger = deadline_record(tmp_path)
    ready = workflow.transition(submitted, to_state="hardening_ready")
    snapshot, _ = workflow.ledger_snapshot(ledger, ready)
    candidate_id = snapshot["entries"][0]["candidate_id"]
    receipt = workflow.write_hardening_receipt(
        ready, ledger, [{"candidate_id": candidate_id, "mapped_family": "alignment"}],
        tmp_path / "hardening.json",
    )
    hardened = workflow.transition(ready, to_state="hardened", hardening_receipt=receipt)
    assert workflow.validate_workflow(hardened, require_completion=True) == []
    with pytest.raises(ValueError, match="consume exactly"):
        workflow.write_hardening_receipt(ready, ledger, [], tmp_path / "bad-hardening.json")


def test_candidate_zero_bounded_hardening_is_valid(tmp_path):
    submitted, ledger = deadline_record(tmp_path, candidate=False)
    ready = workflow.transition(submitted, to_state="hardening_ready")
    receipt = workflow.write_hardening_receipt(ready, ledger, [], tmp_path / "zero-hardening.json")
    hardened = workflow.transition(ready, to_state="hardened", hardening_receipt=receipt)
    assert workflow.validate_workflow(hardened) == []


def test_workflow_and_ledger_snapshots_validate_against_schemas(tmp_path):
    submitted, ledger = deadline_record(tmp_path)
    workflow_schema = json.loads((ROOT / "schemas/docs-delivery-workflow-1.json").read_text(encoding="utf-8"))
    ledger_schema = json.loads((ROOT / "schemas/docs-delivery-candidate-ledger-1.json").read_text(encoding="utf-8"))
    snapshot, errors = workflow.ledger_snapshot(ledger, submitted)
    assert errors == [] and snapshot is not None
    assert list(Draft202012Validator(workflow_schema).iter_errors(submitted)) == []
    assert list(Draft202012Validator(ledger_schema).iter_errors(snapshot)) == []


def test_live_completion_hook_enforces_opt_in_workflow_and_legacy_remains_unchanged(tmp_path, monkeypatch):
    guard = load_guard()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")
    source = write_file(tmp_path / "source.md", b"source")
    in_progress = workflow.issue_workflow(
        task_id="t_fixture", mode="harness_development", strategy="source_first_rebuild",
        source=source, output_path=tmp_path / "new.pdf",
    )
    contract = workflow.write_workflow(tmp_path / "in-progress.json", in_progress)
    blocked = guard._pre_tool_call(
        tool_name="kanban_complete", args={"metadata": {"verification": {"delivery_workflow": {"contract": str(contract)}}}}
    )
    assert "has not reached submitted state" in blocked["message"]
    legacy = guard._pre_tool_call(tool_name="kanban_complete", args={})
    assert "final-output guard" in legacy["message"] and "delivery workflow" not in legacy["message"]


def test_valid_live_completion_workflow_reaches_existing_receipt_gate(tmp_path, monkeypatch):
    guard = load_guard(); monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")
    submitted, _ = harness_record(tmp_path)
    contract = workflow.write_workflow(tmp_path / "submitted.json", submitted)
    result = guard._pre_tool_call(
        tool_name="kanban_complete", args={"metadata": {"verification": {"delivery_workflow": {"contract": str(contract)}}}}
    )
    assert result["message"] == "docs final-output guard: completion metadata must include verification.final_output_filter.receipts"
