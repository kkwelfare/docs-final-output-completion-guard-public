from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fitz
import pytest
from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw

from quality_rules import format_relation_adapters, relative_anchor_gap, rendered_readback

ROOT = Path(__file__).resolve().parents[1]
READBACK_SCHEMA = json.loads((ROOT / "schemas/docs-rendered-readback-1.json").read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy(source_hash: str) -> dict:
    return {
        "mode": "separate", "axis": "horizontal", "shared_container": "allowed",
        "containment_tolerance_pt": 0.5, "measurement_model": "point_to_visible_envelope",
        "annotation_id": "annotation-01", "source_annotation_map_sha256": source_hash,
        "anchor": {"member_id": "bubble", "point_kind": "tail_tip"},
        "reference": {"member_id": "avatar", "envelope_kind": "visible_bbox", "dimension": "horizontal_width"},
        "direction_vector": [1, 0], "target_gap_ratio": 0.5, "tolerance": 0.05,
        "no_contact": True, "contact_tolerance_pt": 0.5, "facing": "toward_anchor",
    }


def _identity(contract: dict) -> dict:
    return {"contract_id": contract["contract_id"], "policy_hash": contract["policy_hash"],
            "producer_run_id": 3, "artifact_set_id": "d" * 64}


def _applied(format_name: str) -> dict:
    return {"format": format_name, "adapter": "fixture", "anchor_primitive_id": "tail",
            "reference_primitive_id": "visible", "source_evidence_sha256": "e" * 64}


def _png(path: Path) -> None:
    image = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((30, 20, 69, 59), fill=(20, 20, 20, 255))
    image.save(path)


def _pdf(path: Path) -> None:
    document = fitz.open(); page = document.new_page(width=100, height=80)
    page.draw_rect(fitz.Rect(30, 20, 70, 60), color=(0, 0, 0), fill=(0, 0, 0))
    document.save(path); document.close()


def test_independent_png_and_pdf_readback_reopen_final_artifacts(tmp_path: Path):
    source = tmp_path / "annotation-map.json"; source.write_text("{}", encoding="utf-8")
    contract = relative_anchor_gap.build_contract(_policy(_sha(source)))
    identity = _identity(contract)
    png = tmp_path / "final.png"; _png(png)
    png_evidence = rendered_readback.measure_relative_anchor_gap_artifact(
        artifact=png, output_path=tmp_path / "png-evidence.json", execution_identity=identity,
        annotation_id="annotation-01", source_annotation_map=source, requested=contract["policy"],
        applied=_applied("png"), tail_tip=[10, 40], avatar_search_bbox=[20, 10, 80, 70],
        facing_actual="toward_anchor", points_per_pixel=1.0,
    )
    png_record = json.loads(png_evidence.read_text(encoding="utf-8"))
    assert png_record["effective"]["avatar_bbox_pdf_points"] == [30.0, 20.0, 70.0, 60.0]
    assert png_record["effective"]["gap_ratio"] == 0.5 and png_record["effective"]["status"] == "pass"

    pdf = tmp_path / "final.pdf"; _pdf(pdf)
    pdf_evidence = rendered_readback.measure_relative_anchor_gap_artifact(
        artifact=pdf, output_path=tmp_path / "pdf-evidence.json", execution_identity=identity,
        annotation_id="annotation-01", source_annotation_map=source, requested=contract["policy"],
        applied=_applied("pdf"), tail_tip=[10, 40], avatar_search_bbox=[30, 20, 70, 60],
        facing_actual="toward_anchor", page=1,
    )
    pdf_record = json.loads(pdf_evidence.read_text(encoding="utf-8"))
    assert pdf_record["effective"]["gap_ratio"] == 0.5 and pdf_record["effective"]["status"] == "pass"
    assert relative_anchor_gap.validate_format_parity([png_record, pdf_record]) == []


def test_readback_attachment_requires_hash_bound_independent_evidence(tmp_path: Path):
    source = tmp_path / "annotation-map.json"; source.write_text("{}", encoding="utf-8")
    contract = relative_anchor_gap.build_contract(_policy(_sha(source))); identity = _identity(contract)
    artifact = tmp_path / "final.png"; _png(artifact)
    relative_evidence = rendered_readback.measure_relative_anchor_gap_artifact(
        artifact=artifact, output_path=tmp_path / "relative.json", execution_identity=identity,
        annotation_id="annotation-01", source_annotation_map=source, requested=contract["policy"],
        applied=_applied("png"), tail_tip=[10, 40], avatar_search_bbox=[20, 10, 80, 70],
        facing_actual="toward_anchor", points_per_pixel=1.0,
    )
    relation = {"id": "relative", "scope": "sibling", "type": "anchor_gap", "measurement_basis": "pdf_points",
                "ordered_member_ids": ["bubble", "avatar"], "constraints": {"anchor_gap": contract["policy"]}}
    primitives = [
        {"primitive_id": "tail", "member_id": "bubble", "source_order": 0, "page": 1, "bbox_pdf_points": [9, 39, 11, 41],
         "semantic_role": "tail_tip", "tail_tip_pdf_points": [10, 40], "annotation_id": "annotation-01", "source_annotation_map_sha256": _sha(source)},
        {"primitive_id": "visible", "member_id": "avatar", "source_order": 1, "page": 1, "bbox_pdf_points": [30, 20, 70, 60],
         "semantic_role": "visible_envelope", "visible_bbox_pdf_points": [30, 20, 70, 60], "annotation_id": "annotation-01", "source_annotation_map_sha256": _sha(source)},
    ]
    source_evidence = format_relation_adapters.write_source_evidence(format_name="svg", adapter="fixture", primitives=primitives, output_path=tmp_path / "source-evidence.json")
    effective = {"measurement_basis": "png_alpha_mask", "status": "measured", "tail_tip_pdf_points": [10, 40],
                 "avatar_bbox_pdf_points": [30, 20, 70, 60], "avatar_width_pt": 40, "direction_vector": [1, 0],
                 "gap_pt": 20, "gap_ratio": 0.5, "target_gap_ratio": 0.5, "tolerance": 0.05,
                 "facing": "toward_anchor", "no_contact": True, "contact_distance_pt": 20, "failed_constraints": []}
    frame = format_relation_adapters.build_relation_frame(
        relation=relation, format_name="svg", adapter="fixture", execution_identity=identity,
        source_evidence_path=source_evidence, primitives=primitives, render_metrics=effective,
        crop_sha256_by_primitive={"tail": "f" * 64, "visible": "e" * 64},
    )
    content_evidence = tmp_path / "content.json"
    content_evidence.write_text(json.dumps({
        "schema_version": "docs-content-block-evidence-1", "producer_run_id": 3, "artifact_set_id": "d" * 64,
        "execution_identity": identity, "source": {"path": str(source), "sha256": _sha(source)},
        "artifact": {"path": str(artifact), "sha256": _sha(artifact)},
        "blocks": [{"id": "bubble", "kind": "shape", "role": "anchor", "member_ids": []}, {"id": "avatar", "kind": "image", "role": "reference", "member_ids": []}],
        "relations": [frame["relation"]], "review_candidates": [],
    }), encoding="utf-8")
    manifest = rendered_readback.build_manifest(artifact, tmp_path / "readback")
    assert manifest is not None
    rendered_readback.attach_geometry_relation_readback(manifest, content_evidence, relative_evidence_paths=[relative_evidence])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(READBACK_SCHEMA).iter_errors(data)) == []
    assert rendered_readback.validate_handle({"path": str(manifest), "sha256": _sha(manifest)}, artifact, producer_run_id=3, artifact_set_id="d" * 64) == []

    tampered = json.loads(relative_evidence.read_text(encoding="utf-8")); tampered["effective"]["gap_ratio"] = 0.9
    relative_evidence.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="identity/hash drift"):
        fresh = rendered_readback.build_manifest(artifact, tmp_path / "fresh")
        assert fresh is not None
        rendered_readback.attach_geometry_relation_readback(fresh, content_evidence, relative_evidence_paths=[relative_evidence])
