from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import fitz
import pytest
from jsonschema import Draft202012Validator

from quality_rules import content_blocks, html_pdf_harness

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/docs-content-block-evidence-1.json").read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _block(identifier: str, bbox: list[float], *, parent: str | None = None, kind: str = "shape") -> dict:
    return {
        "id": identifier, "parent_id": parent, "kind": kind, "role": identifier,
        "member_ids": [],
        "render": {"page": 1, "bbox_pdf_points": bbox, "mapping": "source_anchor"},
    }


def _base(tmp_path: Path, blocks: list[dict], relations: list[dict]) -> tuple[dict, Path, dict]:
    source = tmp_path / "source.svg"; source.write_text("<svg/>", encoding="utf-8")
    artifact = tmp_path / "artifact.pdf"
    doc = fitz.open(); doc.new_page(width=300, height=200); doc.save(artifact); doc.close()
    manifest = {
        "schema_version": "docs-content-block-evidence-1",
        "producer_run_id": 42,
        "artifact_set_id": "a" * 64,
        "execution_identity": {"contract_id": "b" * 64, "policy_hash": "c" * 64,
                               "producer_run_id": 42, "artifact_set_id": "a" * 64},
        "source": {"path": str(source.resolve()), "sha256": _sha(source)},
        "artifact": {"path": str(artifact.resolve()), "sha256": _sha(artifact)},
        "blocks": blocks, "relations": relations, "review_candidates": [],
    }
    policy = {"relation_policy": {"status": "declared", "required_relation_ids": [item["id"] for item in relations]}}
    return manifest, artifact, policy


def _anchor(mode: str = "attached") -> dict:
    values = {"mode": mode, "axis": "vertical", "shared_container": "allowed", "containment_tolerance_pt": .5}
    if mode == "attached":
        values.update({"max_gap_pt": 3, "min_axis_overlap_ratio": .8, "max_width_delta_pt": 2,
                       "max_edge_delta_pt": 2, "max_center_delta_pt": 2})
    elif mode == "separate":
        values["min_gap_pt"] = 10
    return {"id": "anchor", "scope": "sibling", "type": "anchor_gap", "measurement_basis": "pdf_points",
            "ordered_member_ids": ["anchor-a", "anchor-b"], "constraints": {"anchor_gap": values}}


def _inset() -> dict:
    return {"id": "inset", "scope": "parent_child", "type": "content_inset", "measurement_basis": "pdf_points",
            "ordered_member_ids": ["outer", "inner-a", "inner-b"], "constraints": {"content_inset": {
                "minimum_pt": {"left": 10, "right": 10, "top": 8, "bottom": 8},
                "balance_axes": "both", "max_balance_delta_pt": 5, "require_contained": True,
                "containment_tolerance_pt": .5, "inner_envelope": "union"}}}


def _applied(source_order: bool = True) -> dict:
    return {"format": "html", "adapter": "html-dom-order-1", "ordered_primitive_ids": ["p1", "p2"],
            "source_order_pass": source_order, "source_evidence_sha256": "d" * 64}


def _connector(*, visible: float = 0, ambiguous: bool = False) -> dict:
    effective = {"measurement_basis": "unavailable" if ambiguous else "pdf_vector_plus_render_mask",
                 "status": "ambiguous" if ambiguous else "measured", "evidence_sha256": "e" * 64}
    if ambiguous:
        effective["ambiguity_reason"] = "flattened_path_unavailable"
    else:
        effective.update({"endpoint_distance_pt": .2, "boundary_intersection_distance_pt": .2,
                          "visible_length_inside_surface_pt": visible, "visible_area_inside_surface_pt2": visible})
    return {"id": "connector", "scope": "sibling", "type": "connector_route", "measurement_basis": "pdf_points",
            "ordered_member_ids": ["line", "surface"], "constraints": {"connector_route": {
                "mode": "behind_surface", "endpoint_tolerance_pt": 1, "boundary_intersection_tolerance_pt": 1,
                "max_visible_length_inside_surface_pt": .5, "max_visible_area_inside_surface_pt2": 1,
                "require_source_order_proof": True, "require_rendered_occlusion_evidence": True}},
            "applied": _applied(), "effective": effective}


def _layer(*, overlap: float = 20, visible: float = .99, source_order: bool = True) -> dict:
    return {"id": "layer", "scope": "sibling", "type": "layer_relation", "measurement_basis": "pdf_points",
            "ordered_member_ids": ["front", "back"], "constraints": {"layer_relation": {
                "mode": "front_of", "min_overlap_area_pt2": 4, "max_overlap_area_pt2": 80,
                "min_front_visible_ratio": .98, "max_occluded_ratio": .02,
                "source_order_required": True, "flattened_pdf_ambiguity": "review"}},
            "applied": _applied(source_order), "effective": {
                "measurement_basis": "pdf_vector_plus_render_mask", "status": "measured", "evidence_sha256": "f" * 64,
                "overlap_area_pt2": overlap, "front_visible_ratio": visible, "occluded_ratio": 1 - visible}}


def test_geometry_relations_positive_python_and_schema_parity(tmp_path: Path):
    blocks = [_block("anchor-a", [20, 20, 120, 30]), _block("anchor-b", [20, 32, 120, 42]),
              _block("outer", [10, 60, 190, 140], kind="surface"), _block("inner-a", [25, 72, 90, 100], parent="outer"),
              _block("inner-b", [100, 100, 175, 128], parent="outer"), _block("line", [10, 150, 190, 152], kind="line"),
              _block("surface", [70, 140, 130, 170], kind="surface"), _block("front", [30, 155, 90, 185]),
              _block("back", [50, 160, 120, 190], kind="surface")]
    relations = [_anchor(), _inset(), _connector(), _layer()]
    manifest, artifact, policy = _base(tmp_path, blocks, relations)
    assert content_blocks.validate_manifest(manifest, artifact=artifact) == []
    assert list(Draft202012Validator(SCHEMA).iter_errors(manifest)) == []
    result = content_blocks.evaluate_manifest(manifest, policy, artifact)
    assert not [item for item in result["issues"] if item["rule"] == "content_block_relation_mismatch"]
    measured = {item["id"]: item["status"] for item in result["relations"] if item["id"] in {"anchor", "inset", "connector", "layer"}}
    assert measured == {
        "anchor": "pass", "inset": "pass", "connector": "pass", "layer": "pass"}


def test_content_evidence_schema_accepts_opt_in_producer_metadata_and_optional_parent(tmp_path: Path):
    blocks = [_block("anchor-a", [20, 20, 120, 30]), _block("anchor-b", [20, 32, 120, 42])]
    blocks[0].pop("parent_id")
    manifest, artifact, _ = _base(tmp_path, blocks, [_anchor()])
    manifest["producer_family"] = "docs-svg-material-producer-1"
    manifest["producer_capabilities"] = ["shape", "anchor_gap"]
    assert content_blocks.validate_manifest(manifest, artifact=artifact) == []
    assert list(Draft202012Validator(SCHEMA).iter_errors(manifest)) == []
    manifest["producer_capabilities"].append("unsupported")
    assert list(Draft202012Validator(SCHEMA).iter_errors(manifest))


def test_geometry_relations_negative_merge_to_existing_hard_path(tmp_path: Path):
    blocks = [_block("anchor-a", [20, 20, 120, 30]), _block("anchor-b", [40, 45, 140, 55]),
              _block("outer", [10, 60, 190, 140], kind="surface"), _block("inner-a", [5, 62, 180, 138], parent="outer"),
              _block("line", [10, 150, 190, 152], kind="line"), _block("surface", [70, 140, 130, 170], kind="surface"),
              _block("front", [30, 155, 90, 185]), _block("back", [50, 160, 120, 190], kind="surface")]
    inset = _inset(); inset["ordered_member_ids"] = ["outer", "inner-a"]
    relations = [_anchor(), inset, _connector(visible=2), _layer(visible=.7, source_order=False)]
    manifest, artifact, policy = _base(tmp_path, blocks, relations)
    assert content_blocks.validate_manifest(manifest, artifact=artifact) == []
    result = content_blocks.evaluate_manifest(manifest, policy, artifact)
    failures = [item for item in result["issues"] if item["rule"] == "content_block_relation_mismatch"]
    assert {item["relation_id"] for item in failures} == {"anchor", "inset", "connector", "layer"}
    assert all(item.get("model_call") is False for item in failures)


def test_flattened_connector_is_review_not_hard_and_candidate_zero_does_not_invoke_model(tmp_path: Path):
    blocks = [_block("line", [10, 20, 190, 22], kind="line"), _block("surface", [70, 10, 130, 40], kind="surface")]
    manifest, artifact, policy = _base(tmp_path, blocks, [_connector(ambiguous=True)])
    result = content_blocks.evaluate_manifest(manifest, policy, artifact)
    assert not [item for item in result["issues"] if item["rule"] == "content_block_relation_mismatch"]
    review = next(item for item in result["issues"] if item["rule"] == "content_block_geometry_relation_ambiguous")
    assert review["reason_code"] == "flattened_path_unavailable"
    assert review["auto_repair"] is False and review["model_call"] is False


@pytest.mark.parametrize(("legacy", "generic", "mode"), [
    ("inside", "content_inset", "contained"),
    ("outside", "anchor_gap", "separate"),
    ("no_overlap", "layer_relation", "separate"),
])
def test_html_v4_relation_adapter_is_additive_and_body_free(legacy: str, generic: str, mode: str):
    frame = {"frame_id": "frame-a"}
    relation = {"target_id_hash": "1" * 16, "relation": legacy, "min_gap_pt": 2}
    normalized = html_pdf_harness.normalize_v4_relation(frame, relation)
    assert normalized["type"] == generic and normalized["mode"] == mode
    assert normalized["source_adapter"] == "html-wrap-plan-v4-readonly-1"
    assert "text" not in json.dumps(normalized)


@pytest.mark.parametrize("mutation", ["bool", "nan", "unknown", "unpaired", "identity"])
def test_python_and_schema_reject_malformed_geometry_state(tmp_path: Path, mutation: str):
    blocks = [_block("anchor-a", [20, 20, 120, 30]), _block("anchor-b", [20, 32, 120, 42])]
    manifest, artifact, _ = _base(tmp_path, blocks, [_anchor()])
    if mutation == "bool": manifest["relations"][0]["constraints"]["anchor_gap"]["max_gap_pt"] = True
    elif mutation == "nan": manifest["relations"][0]["constraints"]["anchor_gap"]["max_gap_pt"] = math.nan
    elif mutation == "unknown": manifest["relations"][0]["constraints"]["anchor_gap"]["unexpected"] = 1
    elif mutation == "unpaired": manifest["relations"][0]["applied"] = _applied()
    else: manifest["execution_identity"]["producer_run_id"] = 43
    python_failed = bool(content_blocks.validate_manifest(manifest, artifact=artifact))
    schema_failed = bool(list(Draft202012Validator(SCHEMA).iter_errors(manifest)))
    assert python_failed
    if mutation in {"bool", "unknown", "unpaired"}:
        assert schema_failed
    else:
        # Draft 2020-12 has no portable finite-number or cross-field equality
        # assertion; these semantic checks intentionally remain fail-closed in Python.
        assert not schema_failed
