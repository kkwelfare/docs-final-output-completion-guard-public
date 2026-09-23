from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quality_rules import execution_contract, format_relation_adapters as adapters
from quality_rules import repair_controller, semantic_surface


def _region(region_id: str, role: str, tier: str, topology: str, page: int, bbox):
    state = {
        "region_id": region_id, "caller_role": role, "intent_class": "ordinary",
        "page_index": page, "selected_token_id": "token", "surface_tier": tier,
        "topology": topology, "container_scope": "none" if topology == "none" else "semantic-unit",
        "visual_weight": "none" if topology == "none" else "standard", "elevation_class": "none",
        "sequence_scope_id": "main", "sequence_index": page,
    }
    if topology != "none":
        state["semantic_group_id"] = "group-" + region_id
    return {"region_id": region_id, "page_index": page, "shape_id": None if bbox is None else "s-" + region_id,
            "bbox_pt": bbox, "requested": copy.deepcopy(state), "applied": copy.deepcopy(state),
            "effective": copy.deepcopy(state)}


def test_semantic_surface_ir_metrics_roles_and_classification():
    regions = [
        _region("r-code", "code", "card", "per-item", 0, [0, 0, 80, 90]),
        _region("r-warning", "warning", "card", "per-item", 1, [0, 0, 80, 90]),
        _region("r-question", "question", "card", "per-item", 2, [0, 0, 80, 90]),
        _region("r-answer", "answer", "flat", "none", 3, None),
        _region("r-custom", "caller-specific role", "flat", "none", 4, None),
    ]
    ir = semantic_surface.build_ir(
        regions=regions, page_sizes={index: [100, 100] for index in range(1, 6)},
        line_boxes={index: [[5, 5, 30, 15]] for index in range(1, 6)}, hard_failures=[],
        vertical_occupancy=lambda *_: 0.10, bottom_blank_ratio=lambda *_: 0.80,
        thresholds={"max_surface_area_ratio": 0.50, "min_vertical_occupancy": 0.35,
                    "max_bottom_blank_ratio": 0.45, "max_consecutive_prominent_pages": 2},
    )
    assert ir["schema_version"] == "docs-semantic-surface-ir-1"
    assert {item["role_class"] for item in ir["semantic_blocks"]} == {
        "code", "warning", "question", "answer", "caller-defined",
    }
    assert ir["metrics"]["max_surface_area_ratio"] == 0.72
    assert {item["rule"] for item in ir["REVIEW_CANDIDATE"]} == {
        "semantic_surface_area_ratio", "semantic_surface_sparse_container",
        "semantic_surface_consecutive_prominent_pages",
    }
    assert ir["HARD_FAIL"] == [] and ir["status"] == "REVIEW_CANDIDATE"
    assert all(item["classification"] == "INFO" for item in ir["INFO"])
    assert '"text"' not in json.dumps(ir, sort_keys=True).lower()


def test_card_existence_is_not_failure_and_ambiguous_block_is_hard_fail():
    card = _region("valid", "code", "card", "per-item", 0, [10, 10, 30, 30])
    ir = semantic_surface.build_ir(
        regions=[card], page_sizes={1: [100, 100]}, line_boxes={1: [[12, 12, 28, 28]]},
        hard_failures=[], vertical_occupancy=lambda *_: 0.8, bottom_blank_ratio=lambda *_: 0.1,
    )
    assert ir["status"] == "INFO" and ir["HARD_FAIL"] == []
    ambiguous = copy.deepcopy(card); ambiguous["effective"].pop("caller_role")
    blocked = semantic_surface.build_ir(
        regions=[ambiguous], page_sizes={1: [100, 100]}, line_boxes={1: []},
        hard_failures=[], vertical_occupancy=lambda *_: 0.0, bottom_blank_ratio=lambda *_: 1.0,
    )
    assert blocked["status"] == "HARD_FAIL"
    assert blocked["HARD_FAIL"][0]["rule_id"] == "SURFACE-IR-SCHEMA-01"


def test_surface_metric_thresholds_are_additive_and_schema_valid(tmp_path):
    policy = {
        "semantic_surface_policy": {
            "schema_version": "docs-semantic-surface-policy-1",
            "tokens": [{"id": "flat", "surface_tier": "flat", "topology": "none",
                        "container_scope": "none", "visual_weight": "none", "elevation_class": "none"}],
            "regions": [{"region_id": "r", "caller_role": "answer", "intent_class": "ordinary",
                         "page_index": 0, "selected_token_id": "flat"}],
            "metric_thresholds": {"max_surface_area_ratio": 0.6, "min_vertical_occupancy": 0.35,
                                  "max_bottom_blank_ratio": 0.45, "max_consecutive_prominent_pages": 2},
        }
    }
    source = tmp_path / "source"; source.write_bytes(b"source")
    artifact = tmp_path / "artifact"; artifact.write_bytes(b"artifact")
    contract = execution_contract.bind_artifacts(execution_contract.issue_contract(
        source=source, policy=policy, scope_ids=["surface"], acceptance_ids=["ir"], producer_run_id=1,
    ), [artifact])
    schema = json.loads((Path(__file__).parents[1] / "schemas/docs-production-execution-contract-1.json").read_text())
    assert execution_contract.validate_contract(contract) == []
    assert list(Draft202012Validator(schema).iter_errors(contract)) == []
    invalid = copy.deepcopy(policy)
    invalid["semantic_surface_policy"]["metric_thresholds"]["max_surface_area_ratio"] = True
    with pytest.raises(ValueError, match="finite ratio"):
        execution_contract.canonicalize_policy(invalid)


def test_format_adapter_surface_recompose_boundary_and_preservation(tmp_path):
    evidence = adapters.write_source_evidence(
        format_name="svg", adapter="generic-svg", output_path=tmp_path / "source-evidence.json",
        primitives=[{"primitive_id": "old", "member_id": "r1", "source_order": 0,
                     "page": 1, "bbox_pdf_points": [0, 0, 20, 20]}],
    )
    ir = {"schema_version": "docs-semantic-surface-ir-1",
          "semantic_blocks": [{"region_id": "r1"}], "surfaces": [], "INFO": [],
          "REVIEW_CANDIDATE": [], "HARD_FAIL": []}
    preserved = {key: "a" * 64 for key in (
        "content_sha256_before", "content_sha256_after", "text_order_sha256_before",
        "text_order_sha256_after", "cjk_contract_sha256_before", "cjk_contract_sha256_after")}
    frame = adapters.build_surface_recompose_frame(
        execution_identity={"contract_id": "1" * 64, "policy_hash": "2" * 64,
                            "producer_run_id": 1, "artifact_set_id": "3" * 64},
        format_name="svg", adapter="generic-svg", semantic_surface_ir=ir,
        selected_region_ids=["r1"], source_evidence_path=evidence,
        replacement_primitives=[{"region_id": "r1", "primitive_id": "new", "bbox_pdf_points": [0, 0, 40, 20], "source_order": 0}],
        preservation=preserved,
    )
    assert frame["schema_version"] == "docs-format-surface-recompose-frame-1"
    assert frame["requested"]["selected_region_ids"] == ["r1"]
    with pytest.raises(ValueError, match="ambiguous semantic block"):
        adapters.build_surface_recompose_frame(
            execution_identity=frame["execution_identity"], format_name="svg", adapter="generic-svg",
            semantic_surface_ir=ir, selected_region_ids=["missing"], source_evidence_path=evidence,
            replacement_primitives=[], preservation=preserved,
        )


def test_registered_surface_recompose_capability_and_output_validation(tmp_path):
    adapter = repair_controller.register_adapter(
        adapter_id="surface-fixture", capabilities={("semantic-surface", "surface_recompose")},
        producer_kind="fixture", invoke=lambda _: {}, source_path=Path(__file__),
    )
    plan = {"plan_hash": "b" * 64}
    preserved = {key: "c" * 64 for key in (
        "content_sha256_before", "content_sha256_after", "text_order_sha256_before",
        "text_order_sha256_after", "cjk_contract_sha256_before", "cjk_contract_sha256_after")}
    output = {"schema_version": "docs-surface-recompose-adapter-output-1", "status": "pass",
              "plan_hash": plan["plan_hash"], "adapter_id": adapter.adapter_id,
              "adapter_fingerprint": adapter.fingerprint, "preserved": preserved,
              "requested": {"schema_version": "docs-surface-recompose-requested-1"},
              "applied": {"schema_version": "docs-surface-recompose-applied-1"},
              "effective": {"schema_version": "docs-surface-recompose-effective-1"}}
    path = tmp_path / "output.json"; path.write_text(json.dumps(output))
    assert repair_controller.validate_surface_recompose_output(path, plan=plan, adapter=adapter) == []
    output["preserved"]["text_order_sha256_after"] = "d" * 64
    path.write_text(json.dumps(output))
    assert "text_order_sha256 drift" in " ".join(
        repair_controller.validate_surface_recompose_output(path, plan=plan, adapter=adapter))
