from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quality_rules import color_contrast, execution_contract

ROOT = Path(__file__).resolve().parents[1]


def _color_token(token_id: str, kind: str, value: str) -> dict:
    return {"id": token_id, "kind": kind, "hex": value, "rgb": list(color_contrast.hex_to_rgb(value))}


def combined_policy() -> dict:
    return {
        "renderer_policy": "fresh-readback",
        "semantic_style_policy": {
            "schema_version": "docs-semantic-style-policy-1",
            "tokens": [
                {"id": "heading-before", "kind": "paragraph-spacing", "role": "heading",
                 "relation": "before", "value": {"unit": "pt", "amount": 12}},
            ],
        },
        "vertical_alignment_policy": {
            "schema_version": "docs-cell-vertical-alignment-policy-1",
            "tokens": [
                {"id": "generic-center", "target_role": "arbitrary-cell-role",
                 "vertical_align": "center", "center_tolerance_pt": 0.5},
            ],
        },
        "color_policy": {
            "schema_version": "docs-color-contrast-policy-1",
            "palette": {"palette_id": "neutral-palette", "palette_seed": 7},
            "tokens": [
                _color_token("surface-neutral", "surface", "#F4F4F4"),
                _color_token("ink-dark", "foreground", "#111111"),
                _color_token("edge-neutral", "border", "#777777"),
            ],
            "roles": [{
                "role": "arbitrary-surface-role", "foreground_mode": "body-near-black",
                "foreground_token_ids": ["ink-dark"], "surface_token_id": "surface-neutral",
                "border_token_id": "edge-neutral", "min_contrast_ratio": 4.5,
                "preferred_contrast_ratio": 7.0,
                "render_samples": [{"sample_id": "sample-neutral", "render_index": 1,
                                    "foreground_xy": [0.25, 0.5], "surface_xy": [0.75, 0.5]}],
            }],
        },
        "semantic_surface_policy": {
            "schema_version": "docs-semantic-surface-policy-1",
            "tokens": [
                {"id": "tier-emphasis", "surface_tier": "emphasis", "topology": "band",
                 "container_scope": "semantic-unit", "visual_weight": "strong", "elevation_class": "none"},
                {"id": "tier-card-items", "surface_tier": "card", "topology": "per-item",
                 "container_scope": "semantic-unit", "visual_weight": "standard", "elevation_class": "standard"},
                {"id": "tier-flat", "surface_tier": "flat", "topology": "none",
                 "container_scope": "none", "visual_weight": "none", "elevation_class": "none"},
                {"id": "tier-card-pair", "surface_tier": "card", "topology": "paired",
                 "container_scope": "semantic-unit", "visual_weight": "standard", "elevation_class": "low"},
                {"id": "tier-light", "surface_tier": "light-group", "topology": "shared-group",
                 "container_scope": "group", "visual_weight": "subtle", "elevation_class": "none"},
            ],
            "regions": [
                {"region_id": "region-emphasis", "caller_role": "arbitrary conclusion role",
                 "intent_class": "memorize", "page_index": 4, "selected_token_id": "tier-emphasis",
                 "semantic_group_id": "group-emphasis", "sequence_scope_id": "sequence-main", "sequence_index": 4},
                {"region_id": "region-flat", "caller_role": "arbitrary comparison role",
                 "intent_class": "compare", "page_index": 0, "selected_token_id": "tier-flat",
                 "sequence_scope_id": "sequence-main", "sequence_index": 0},
                {"region_id": "region-items", "caller_role": "arbitrary enumeration role",
                 "intent_class": "enumerate", "page_index": 3, "selected_token_id": "tier-card-items",
                 "semantic_group_id": "group-items", "sequence_scope_id": "sequence-main", "sequence_index": 3},
                {"region_id": "region-light", "caller_role": "arbitrary sequence role",
                 "intent_class": "sequence", "page_index": 1, "selected_token_id": "tier-light",
                 "semantic_group_id": "group-light", "sequence_scope_id": "sequence-main", "sequence_index": 1},
                {"region_id": "region-pair", "caller_role": "arbitrary contrast role",
                 "intent_class": "contrast", "page_index": 2, "selected_token_id": "tier-card-pair",
                 "semantic_group_id": "group-pair", "sequence_scope_id": "sequence-main", "sequence_index": 2},
            ],
            "sequence_policy": {
                "mode": "review", "prominent_tiers": ["emphasis", "card"],
                "max_consecutive_pages": 2, "model_call_if_zero_candidates": False,
            },
        },
    }


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.bin"
    path.write_bytes(b"source")
    return path


def _contract(tmp_path: Path) -> dict:
    contract = execution_contract.issue_contract(
        source=_source(tmp_path), policy=combined_policy(),
        scope_ids=["caller", "producer"], acceptance_ids=["surface-contract"], producer_run_id=2202,
    )
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")
    return execution_contract.bind_artifacts(contract, [artifact])


def _surface_records(policy: dict) -> list[dict]:
    records = []
    for index, region in enumerate(policy["semantic_surface_policy"]["regions"]):
        _, applied = execution_contract.resolve_semantic_surface_region(
            policy, region_id=region["region_id"], caller_role=region["caller_role"],
        )
        effective = copy.deepcopy(applied)
        effective["schema_version"] = "docs-semantic-surface-policy-effective-1"
        records.append({
            "frame_id": f"frame-{index}",
            "execution_contract": {
                "contract_id": "placeholder", "policy_hash": "placeholder", "producer_run_id": 2202,
                "requested": policy, "selected_token_ids": [region["selected_token_id"]],
                "applied": applied, "effective": effective,
            },
        })
    return records


def _contains_body_key(value) -> bool:
    forbidden = execution_contract.FORBIDDEN_BODY_KEYS
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or _contains_body_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_body_key(item) for item in value)
    return False


def test_combined_surface_policy_canonicalizes_resolves_and_matches_contract_schema(tmp_path):
    bound = _contract(tmp_path)
    policy = bound["declaration"]["policy"]
    surface = policy["semantic_surface_policy"]
    assert [item["id"] for item in surface["tokens"]] == sorted(item["id"] for item in surface["tokens"])
    assert [item["region_id"] for item in surface["regions"]] == sorted(item["region_id"] for item in surface["regions"])
    assert surface["sequence_policy"]["prominent_tiers"] == ["card", "emphasis"]
    concrete, applied = execution_contract.resolve_semantic_surface_region(
        policy, region_id="region-light", caller_role="arbitrary sequence role",
    )
    assert concrete["page_index"] == 1
    assert concrete["topology"] == "shared-group"
    assert concrete["container_scope"] == "group"
    assert applied["schema_version"] == "docs-semantic-surface-policy-applied-1"
    assert not _contains_body_key(applied)
    schema = json.loads((ROOT / "schemas/docs-production-execution-contract-1.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(bound)) == []


def test_surface_frames_aggregate_validate_receipt_and_match_receipt_schema(tmp_path):
    bound = _contract(tmp_path)
    policy = bound["declaration"]["policy"]
    records = _surface_records(policy)
    for record in records:
        frame = record["execution_contract"]
        frame["contract_id"] = bound["contract_id"]
        frame["policy_hash"] = bound["policy_hash"]
    applied = execution_contract.aggregate_producer_policy(records, "applied")
    effective = execution_contract.aggregate_producer_policy(records, "effective")
    assert applied["schema_version"] == "docs-semantic-surface-policy-frames-1"
    assert applied["state"] == "applied" and effective["state"] == "effective"
    assert not _contains_body_key(applied) and not _contains_body_key(effective)
    contract_path = execution_contract.write_contract(tmp_path / "contract.json", bound)
    plan_log = tmp_path / "plan.jsonl"
    plan_log.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8")
    receipt = {
        "schema_version": "docs-producer-execution-receipt-1", "status": "pass",
        "execution_identity": execution_contract.identity(bound),
        "contract": execution_contract.file_handle(contract_path),
        "plan_log": execution_contract.file_handle(plan_log),
        "requested": policy, "applied": applied, "effective": effective,
        "artifacts": bound["artifacts"], "frame_count": len(records),
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    loaded, errors = execution_contract.validate_producer_receipt(receipt_path, bound)
    assert errors == [] and loaded == receipt
    schema = json.loads((ROOT / "schemas/docs-producer-execution-receipt-1.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(receipt)) == []


@pytest.mark.parametrize("mutation", [
    lambda p: p["semantic_surface_policy"]["tokens"][0].update(surface_tier="unknown"),
    lambda p: p["semantic_surface_policy"]["tokens"][0].update(topology="unknown"),
    lambda p: p["semantic_surface_policy"]["tokens"][0].update(container_scope="unknown"),
    lambda p: p["semantic_surface_policy"]["tokens"][0].update(visual_weight="unknown"),
    lambda p: p["semantic_surface_policy"]["tokens"][0].update(elevation_class="unknown"),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(intent_class="unknown"),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(caller_role="   "),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(region_id=" duplicate "),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(page_index=True),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(page_index=float("nan")),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(page_index=float("inf")),
    lambda p: p["semantic_surface_policy"]["regions"][0].update(selected_token_id="missing"),
    lambda p: p["semantic_surface_policy"]["regions"].append(copy.deepcopy(p["semantic_surface_policy"]["regions"][0])),
    lambda p: p["semantic_surface_policy"]["tokens"].append(copy.deepcopy(p["semantic_surface_policy"]["tokens"][0])),
    lambda p: p["semantic_surface_policy"]["regions"][1].update(sequence_index=4),
    lambda p: p["semantic_surface_policy"]["regions"][0].pop("sequence_index"),
    lambda p: p["semantic_surface_policy"]["sequence_policy"].update(max_consecutive_pages=True),
    lambda p: p["semantic_surface_policy"]["sequence_policy"].update(model_call_if_zero_candidates=True),
])
def test_invalid_surface_declarations_fail_closed(mutation):
    policy = combined_policy()
    mutation(policy)
    with pytest.raises(ValueError, match="semantic surface"):
        execution_contract.canonicalize_policy(policy)


def test_flat_per_item_and_group_applicability_fail_closed():
    policy = combined_policy()
    token = policy["semantic_surface_policy"]["tokens"][2]
    token["topology"] = "per-item"
    with pytest.raises(ValueError, match="flat tier"):
        execution_contract.canonicalize_policy(policy)
    policy = combined_policy()
    policy["semantic_surface_policy"]["regions"][0].pop("semantic_group_id")
    with pytest.raises(ValueError, match="requires semantic_group_id"):
        execution_contract.canonicalize_policy(policy)
    policy = combined_policy()
    policy["semantic_surface_policy"]["regions"][1]["semantic_group_id"] = "not-applicable"
    with pytest.raises(ValueError, match="cannot declare semantic_group_id"):
        execution_contract.canonicalize_policy(policy)


def test_resolver_unknown_role_and_direct_drift_fail_closed():
    policy = execution_contract.canonicalize_policy(combined_policy())
    with pytest.raises(ValueError, match="unknown semantic surface region"):
        execution_contract.resolve_semantic_surface_region(policy, region_id="missing", caller_role="role")
    with pytest.raises(ValueError, match="role drift"):
        execution_contract.resolve_semantic_surface_region(policy, region_id="region-flat", caller_role="wrong")
    with pytest.raises(ValueError, match="resolution drift"):
        execution_contract.resolve_semantic_surface_region(
            policy, region_id="region-flat", caller_role="arbitrary comparison role",
            direct_values={"page_index": True},
        )


def test_noncanonical_collection_order_and_policy_hash_mutation_are_detected(tmp_path):
    bound = _contract(tmp_path)
    assert execution_contract.validate_contract(bound) == []
    drifted = copy.deepcopy(bound)
    drifted["declaration"]["policy"]["semantic_surface_policy"]["regions"].reverse()
    errors = execution_contract.validate_contract(drifted)
    assert "execution contract policy is not canonical" in errors
    assert "execution contract canonical hash mismatch" in errors
    mutated = copy.deepcopy(bound)
    mutated["declaration"]["policy"]["semantic_surface_policy"]["regions"][0]["page_index"] += 1
    errors = execution_contract.validate_contract(mutated)
    assert "execution contract policy hash mismatch" in errors


def test_surface_receipt_requested_and_state_drift_fail_closed(tmp_path):
    bound = _contract(tmp_path)
    policy = bound["declaration"]["policy"]
    records = _surface_records(policy)
    for record in records:
        frame = record["execution_contract"]
        frame["contract_id"] = bound["contract_id"]
        frame["policy_hash"] = bound["policy_hash"]
    contract_path = execution_contract.write_contract(tmp_path / "contract.json", bound)
    plan_log = tmp_path / "plan.jsonl"
    plan_log.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8")
    receipt = {
        "schema_version": "docs-producer-execution-receipt-1", "status": "pass",
        "execution_identity": execution_contract.identity(bound), "contract": execution_contract.file_handle(contract_path),
        "plan_log": execution_contract.file_handle(plan_log), "requested": copy.deepcopy(policy),
        "applied": execution_contract.aggregate_producer_policy(records, "applied"),
        "effective": execution_contract.aggregate_producer_policy(records, "effective"),
        "artifacts": bound["artifacts"], "frame_count": len(records),
    }
    receipt["requested"]["semantic_surface_policy"]["regions"][0]["page_index"] += 1
    receipt["effective"]["frames"][0]["policy"]["visual_weight"] = "strong"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    _, errors = execution_contract.validate_producer_receipt(receipt_path, bound)
    assert "producer receipt requested policy mismatch" in errors
    assert "producer receipt differs from producer plan log" in errors


def test_surface_plan_state_drift_from_requested_policy_fails_closed(tmp_path):
    bound = _contract(tmp_path)
    policy = bound["declaration"]["policy"]
    records = _surface_records(policy)
    for record in records:
        frame = record["execution_contract"]
        frame["contract_id"] = bound["contract_id"]
        frame["policy_hash"] = bound["policy_hash"]
    records[0]["execution_contract"]["applied"]["page_index"] += 1
    records[0]["execution_contract"]["effective"]["page_index"] += 1
    contract_path = execution_contract.write_contract(tmp_path / "contract.json", bound)
    plan_log = tmp_path / "plan.jsonl"
    plan_log.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8")
    receipt = {
        "schema_version": "docs-producer-execution-receipt-1", "status": "pass",
        "execution_identity": execution_contract.identity(bound), "contract": execution_contract.file_handle(contract_path),
        "plan_log": execution_contract.file_handle(plan_log), "requested": policy,
        "applied": execution_contract.aggregate_producer_policy(records, "applied"),
        "effective": execution_contract.aggregate_producer_policy(records, "effective"),
        "artifacts": bound["artifacts"], "frame_count": len(records),
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    _, errors = execution_contract.validate_producer_receipt(receipt_path, bound)
    assert "producer semantic surface applied drift from requested policy" in errors
    assert "producer semantic surface effective drift from requested policy" in errors


def test_malformed_new_state_cannot_use_legacy_schema_fallback():
    schema = json.loads((ROOT / "schemas/docs-producer-execution-receipt-1.json").read_text(encoding="utf-8"))
    base = {
        "schema_version": "docs-producer-execution-receipt-1", "status": "pass",
        "execution_identity": {"contract_id": "0" * 64, "policy_hash": "1" * 64, "producer_run_id": 1, "artifact_set_id": "2" * 64},
        "contract": {"path": "/tmp/contract", "sha256": "3" * 64, "size_bytes": 1},
        "plan_log": {"path": "/tmp/plan", "sha256": "4" * 64, "size_bytes": 1},
        "requested": {}, "applied": {"schema_version": "docs-semantic-surface-policy-frames-1", "state": "applied"},
        "effective": {"schema_version": "docs-semantic-surface-policy-frames-1", "state": "effective"},
        "artifacts": [{"path": "/tmp/a", "sha256": "5" * 64, "size_bytes": 1, "media_type": "application/octet-stream"}],
    }
    assert list(Draft202012Validator(schema).iter_errors(base))


def test_body_fields_and_selected_token_drift_are_rejected_by_aggregation():
    policy = execution_contract.canonicalize_policy(combined_policy())
    records = _surface_records(policy)
    records[0]["execution_contract"]["applied"]["body"] = "forbidden"
    with pytest.raises(ValueError, match="state schema"):
        execution_contract.aggregate_producer_policy(records, "applied")
    records = _surface_records(policy)
    records[0]["execution_contract"]["selected_token_ids"] = ["wrong"]
    with pytest.raises(ValueError, match="selected token drift"):
        execution_contract.aggregate_producer_policy(records, "applied")


def test_python_and_json_schema_agree_on_common_positive_and_negative_contracts(tmp_path):
    bound = _contract(tmp_path)
    schema = json.loads((ROOT / "schemas/docs-production-execution-contract-1.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert execution_contract.validate_contract(bound) == []
    assert list(validator.iter_errors(bound)) == []
    for mutate in (
        lambda p: p["declaration"]["policy"]["semantic_surface_policy"]["regions"][0].update(caller_role=" "),
        lambda p: p["declaration"]["policy"]["semantic_surface_policy"]["regions"][0].update(page_index=True),
        lambda p: p["declaration"]["policy"]["semantic_surface_policy"]["tokens"][0].update(topology="unknown"),
    ):
        candidate = copy.deepcopy(bound)
        mutate(candidate)
        assert execution_contract.validate_contract(candidate)
        assert list(validator.iter_errors(candidate))
