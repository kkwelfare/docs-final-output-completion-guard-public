from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))
from quality_rules import common, finalization, layout_typography


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def write_manifest(path: Path, artifacts: list[Path]) -> dict:
    return finalization.write_manifest(path.resolve(), [str(item) for item in artifacts])


def write_receiver(path: Path, manifest: dict, copies: list[tuple[Path, Path]]) -> dict:
    value = {
        "status": "accepted",
        "artifact_set_id": manifest["artifact_set_id"],
        "copies": [
            {
                "canonical_path": str(source.resolve()),
                "copy_path": str(target.resolve()),
                "sha256": sha(target),
                "size_bytes": target.stat().st_size,
                "media_type": finalization.media_type(target),
            }
            for source, target in copies
        ],
    }
    write_json(path, value)
    return value


def load_checker():
    spec = importlib.util.spec_from_file_location("fixture_quality_checker", ROOT / "check_docs_artifact_quality.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_guard():
    spec = importlib.util.spec_from_file_location("fixture_completion_guard", ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def strict_layout(pdf: Path, wrap_plan: Path) -> dict:
    return {
        "artifacts": [str(pdf.resolve())],
        "finalization_manifest": {"path": "/synthetic/manifest.json", "receiver_receipt": "/synthetic/receiver.json"},
        "artifact_quality": {
            "required": True,
            "layout_typography": {
                "required": True,
                "measurement_basis": "pdf_points",
                "minimum_gap_pt": 2,
                "dead_space": {"bottom_blank_ratio_max": 0.25},
                "cjk_contract": {"required": True},
                "wrap_plan_receipt": str(wrap_plan.resolve()),
                "wrap_plan_receipt_sha256": sha(wrap_plan),
                "cjk_appearance_regions": [{"page": 1, "bbox_pdf_points": [0, 0, 20, 20]}],
            },
        },
    }


def fake_document(text: str = "English", bbox: tuple[float, float, float, float] = (10, 10, 80, 20), *, width: float = 300, height: float = 100):
    chars = []
    x = bbox[0]
    step = max(1.0, (bbox[2] - bbox[0]) / max(1, len(text)))
    for char in text:
        chars.append({"c": char, "bbox": [x, bbox[1], min(bbox[2], x + step), bbox[3]]})
        x += step

    class Page:
        rect = type("Rect", (), {"width": width, "height": height})()

        def get_text(self, kind):
            assert kind == "rawdict"
            return {"blocks": [{"type": 0, "lines": [{
                "bbox": list(bbox),
                "spans": [{"chars": chars, "font": "Helvetica", "flags": 0, "size": 10, "origin": [bbox[0], bbox[3]]}],
            }]}]}

    class Document:
        def __enter__(self):
            return [Page()]

        def __exit__(self, *args):
            return False

    return Document()


def test_acceptance_fixture_inventory_and_canonical_expectations():
    data = json.loads((FIXTURES / "finalization-acceptance-fixtures.json").read_text(encoding="utf-8"))
    expected_ids = {
        "hash_drift_after_scan_block", "wrong_target_receipt_block", "corrupt_receipt_block",
        "self_review_block", "review_null_or_missing_block", "unit_mismatch_block",
        "cjk_wrap_missing_block", "cjk_bad_break_block", "dead_space_profile_block",
        "receiver_copy_hash_drift_block", "normal_pdf_pass", "legacy_non_docs_pass",
    }
    assert data["schema_version"] == "docs-quality-gate-acceptance-fixtures-1"
    assert {case["id"] for case in data["cases"]} == expected_ids
    base = data["passing_base"]
    assert base["manifest_artifact_fields"] == ["canonical_path", "sha256", "size_bytes", "media_type"]
    assert base["manifest_fields"] == ["schema_version", "state", "artifacts", "artifact_set_id"]
    assert base["pdf_measurement_basis"] == "pdf_points"
    assert base["candidate_zero"] == {
        "candidate_count": 0, "required": False, "vision_model_invoked": False,
        "evidence": None, "status": "pass",
    }
    assert base["candidate_first_review"] == {"index": 0, "llm_invoked": False, "vision_invoked": False}


def test_deterministic_canonical_manifest_and_artifact_set_id(tmp_path):
    source = tmp_path / "source.pptx"
    checker = tmp_path / "checker.py"
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.png"
    source.write_bytes(b"source")
    checker.write_bytes(b"checker")
    a.write_bytes(b"pdf")
    b.write_bytes(b"png")
    path = tmp_path / "manifest.json"
    first = finalization.write_manifest(
        path.resolve(), [str(b), str(a)], source=str(source), checker=str(checker),
        created_by_task_id="t_producer", created_by_run_id=7, state="accepted",
    )
    first_bytes = path.read_bytes()
    second = finalization.write_manifest(
        path.resolve(), [str(a), str(b)], source=str(source), checker=str(checker),
        created_by_task_id="t_producer", created_by_run_id=7, state="accepted",
    )
    assert first == second and path.read_bytes() == first_bytes
    assert [item["canonical_path"] for item in first["artifacts"]] == sorted([str(a.resolve()), str(b.resolve())])
    assert [item["media_type"] for item in first["artifacts"]] == ["application/pdf", "image/png"]
    assert first["artifact_set_id"] == finalization.artifact_set_id(first["artifacts"])
    assert finalization.validate_manifest(path, [str(a), str(b)], require_identity=True) == (first, None)


def test_generic_handwritten_visual_boolean_is_not_measured_evidence(tmp_path):
    artifact = tmp_path / "artifact.pdf"
    artifact.write_bytes(b"fixture")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "schema_version": "docs-artifact-quality-evidence-1",
        "rule_id": "AQ-BOUNDARY-01", "status": "pass", "review_status": "pass",
        "artifact_path": str(artifact.resolve()), "artifact_sha256": sha(artifact),
        "provenance": {"kind": "checker", "id": "fixture"},
        "checks": [{"id": "boundary-contract", "status": "pass", "no_clipping": True}],
    }), encoding="utf-8")
    data, errors = common._load_evidence(
        "AQ-BOUNDARY-01", {"path": str(evidence), "sha256": sha(evidence)}, artifact
    )
    assert data is None
    assert errors == ["AQ-BOUNDARY-01: handwritten visual pass boolean is not deterministic render/geometry evidence"]


def test_generic_measured_boundary_evidence_remains_accepted(tmp_path):
    artifact = tmp_path / "artifact.pdf"
    artifact.write_bytes(b"fixture")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "schema_version": "docs-artifact-quality-evidence-1",
        "rule_id": "AQ-BOUNDARY-01", "status": "pass", "review_status": "pass",
        "artifact_path": str(artifact.resolve()), "artifact_sha256": sha(artifact),
        "provenance": {"kind": "checker", "id": "geometry-checker"},
        "checks": [{"id": "boundary-contract", "status": "pass", "clipped": False,
                    "overflow": False, "clipped_count": 0, "overflow_count": 0,
                    "image_cut_count": 0, "measurement_basis": "rendered_geometry"}],
    }), encoding="utf-8")
    data, errors = common._load_evidence(
        "AQ-BOUNDARY-01", {"path": str(evidence), "sha256": sha(evidence)}, artifact
    )
    assert data is not None and errors == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("sha256", "0" * 64), ("size_bytes", 999), ("media_type", "application/octet-stream")],
)
def test_manifest_entry_field_constraints_are_isolated(tmp_path, field, replacement):
    artifact = tmp_path / "artifact.pdf"
    artifact.write_bytes(b"pdf")
    path = tmp_path / "manifest.json"
    manifest = write_manifest(path, [artifact])
    broken = copy.deepcopy(manifest)
    broken["artifacts"][0][field] = replacement
    broken["artifact_set_id"] = finalization.artifact_set_id(broken["artifacts"])
    write_json(path, broken)
    assert finalization.validate_manifest(path, [str(artifact)])[1] == "finalization manifest artifact hash/size/media-type drift"


def test_hash_drift_after_scan_block_from_passing_base(tmp_path):
    artifact = tmp_path / "artifact.pdf"
    artifact.write_bytes(b"pdf")
    path = tmp_path / "manifest.json"
    write_manifest(path, [artifact])
    assert finalization.validate_manifest(path, [str(artifact)])[1] is None
    artifact.write_bytes(artifact.read_bytes() + b"!")
    assert finalization.validate_manifest(path, [str(artifact)])[1] == "finalization manifest artifact hash/size/media-type drift"


def test_wrong_target_receipt_block_from_passing_base(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    path = tmp_path / "manifest.json"
    write_manifest(path, [a])
    assert finalization.validate_manifest(path, [str(a)])[1] is None
    assert finalization.validate_manifest(path, [str(b)])[1] == "finalization manifest artifact set mismatch"


def test_corrupt_receiver_receipt_blocks_without_fallback(tmp_path):
    artifact = tmp_path / "artifact.pdf"
    copy_path = tmp_path / "copy.pdf"
    artifact.write_bytes(b"pdf")
    copy_path.write_bytes(artifact.read_bytes())
    manifest_path = tmp_path / "manifest.json"
    receipt = tmp_path / "receiver.json"
    manifest = write_manifest(manifest_path, [artifact])
    write_receiver(receipt, manifest, [(artifact, copy_path)])
    assert finalization.validate_receiver_copy(manifest, receipt) is None
    receipt.write_bytes(b'{"status":"accepted"')
    assert finalization.validate_receiver_copy(manifest, receipt) == "receiver receipt is unreadable or malformed"


def test_self_review_block_from_otherwise_valid_review(tmp_path):
    checker = load_checker()
    artifact = tmp_path / "artifact.pdf"
    scan = tmp_path / "scan.json"
    review = tmp_path / "review.json"
    artifact.write_bytes(b"pdf")
    scan.write_text("{}", encoding="utf-8")
    candidate = {"id": "candidate-1", "rule": "manual_break_anomaly_candidate"}
    passing = {
        "schema_version": "docs-layout-visual-review-2", "status": "pass",
        "artifact_path": str(artifact.resolve()), "artifact_sha256": sha(artifact),
        "scan_path": str(scan.resolve()), "scan_sha256": sha(scan),
        "producer_task_id": "t_producer", "producer_run_id": 1,
        "reviewer_task_id": "t_reviewer", "reviewer_run_id": 2, "reviewer_role": "reviewer",
        "candidate_set_digest": checker._candidate_set_digest([candidate]),
        "llm_invoked": False, "vision_invoked": False,
        "reviews": [{
            "issue_id": candidate["id"], "rule": candidate["rule"], "status": "accepted",
            "disposition": "accepted", "rationale_code": "verified",
            "llm_invoked": False, "vision_invoked": False,
        }],
    }
    write_json(review, passing)
    assert checker._layout_review_passes(review, artifact, scan, [candidate])
    for key in ("llm_invoked", "vision_invoked"):
        tool_used = copy.deepcopy(passing)
        tool_used["reviews"][0][key] = True
        write_json(review, tool_used)
        assert not checker._layout_review_passes(review, artifact, scan, [candidate])
    broken = copy.deepcopy(passing)
    broken["reviewer_task_id"] = broken["producer_task_id"]
    broken["reviewer_run_id"] = broken["producer_run_id"]
    write_json(review, broken)
    assert not checker._layout_review_passes(review, artifact, scan, [candidate])


@pytest.mark.parametrize("mode", ["missing", "null", "error"])
def test_review_null_missing_or_error_blocks_one_candidate(tmp_path, mode):
    checker = load_checker()
    artifact = tmp_path / "artifact.pdf"
    scan = tmp_path / "scan.json"
    review = tmp_path / "review.json"
    artifact.write_bytes(b"pdf")
    scan.write_text("{}", encoding="utf-8")
    if mode == "null":
        review.write_text("null", encoding="utf-8")
    elif mode == "error":
        write_json(review, {"status": "error"})
    candidate = {"id": "candidate-1", "rule": "manual_break_anomaly_candidate"}
    assert not checker._layout_review_passes(review, artifact, scan, [candidate])


def test_unit_mismatch_returns_only_geometry_basis_reason(tmp_path):
    pdf = tmp_path / "artifact.pdf"
    pdf.write_bytes(b"pdf")
    contract = {
        "artifacts": [str(pdf.resolve())],
        "artifact_quality": {"required": True, "layout_typography": {
            "required": True, "geometry_contract_required": True,
            "measurement_basis": "render_pixels", "minimum_gap_px": 2,
        }},
    }
    assert common.validate_pdf_layout_contract(contract) == ["AQ-LAYOUT-01: geometry measurement_basis must be pdf_points"]


def test_cjk_wrap_missing_returns_only_wrap_receipt_reason(tmp_path):
    pdf = tmp_path / "artifact.pdf"
    plan = tmp_path / "wrap.jsonl"
    pdf.write_bytes(b"pdf")
    plan.write_text("{}\n", encoding="utf-8")
    passing = strict_layout(pdf, plan)
    assert common.validate_pdf_layout_contract(passing) == []
    broken = copy.deepcopy(passing)
    del broken["artifact_quality"]["layout_typography"]["wrap_plan_receipt"]
    assert common.validate_pdf_layout_contract(broken) == ["AQ-LAYOUT-01: strict PDF contract requires an absolute wrap_plan_receipt"]


def test_cjk_bad_break_returns_only_planned_parity_rule():
    block = {
        "text": "確認する", "bbox": (10.0, 10.0, 80.0, 20.0), "origin_y": 20.0,
        "font_size": 10.0, "chars": [], "block": "p1-b0", "line": 0,
    }
    record = {
        "page": 1, "frame_id": "slide-1-body", "role": "body",
        "geometry": {"frame_bbox_pt": [0, 0, 200, 100], "safe_width_pt": 180},
        "plan": {"line_hashes": ["0" * 64], "line_paragraph_indices": [0]},
    }
    issues = layout_typography._plan_parity_issues(1, [block], [record], {})
    assert [item["rule"] for item in issues] == ["planned_break_parity"]


def test_dead_space_profile_threshold_blocks_only_dead_space_rule(tmp_path, monkeypatch):
    pdf = tmp_path / "artifact.pdf"
    pdf.write_bytes(b"pdf")
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: fake_document(bbox=(10, 10, 80, 20), height=100))
    scan = layout_typography.scan_pdf(pdf, {
        "required": True, "measurement_basis": "pdf_points", "minimum_gap_pt": 2,
        "dead_space": {"bottom_blank_ratio_max": 0.5},
    })
    assert scan["status"] == "block"
    assert scan["measurement_basis"] == "pdf_points"
    assert [item["rule"] for item in scan["issues"]] == ["dead_space_bottom_blank"]
    assert scan["pages"][0]["bottom_blank_ratio"] == 0.8


def test_receiver_copy_hash_drift_blocks_from_passing_base(tmp_path):
    artifact = tmp_path / "artifact.pdf"
    copy_path = tmp_path / "copy.pdf"
    artifact.write_bytes(b"pdf")
    copy_path.write_bytes(artifact.read_bytes())
    manifest = write_manifest(tmp_path / "manifest.json", [artifact])
    receipt = tmp_path / "receiver.json"
    write_receiver(receipt, manifest, [(artifact, copy_path)])
    assert finalization.validate_receiver_copy(manifest, receipt) is None
    copy_path.write_bytes(b"drift")
    assert finalization.validate_receiver_copy(manifest, receipt) == "receiver-copy-hash-drift"


def test_normal_pdf_manifest_receiver_and_layout_pass(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.pdf"
    copy_path = tmp_path / "copy.pdf"
    artifact.write_bytes(b"pdf")
    copy_path.write_bytes(artifact.read_bytes())
    manifest_path = tmp_path / "manifest.json"
    receipt_path = tmp_path / "receiver.json"
    manifest = write_manifest(manifest_path, [artifact])
    write_receiver(receipt_path, manifest, [(artifact, copy_path)])
    assert finalization.validate_manifest(manifest_path, [str(artifact)])[1] is None
    assert finalization.validate_receiver_copy(manifest, receipt_path) is None
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: fake_document(bbox=(10, 80, 80, 95), height=100))
    scan = layout_typography.scan_pdf(artifact, {
        "required": True, "measurement_basis": "pdf_points", "minimum_gap_pt": 2,
        "dead_space": {"bottom_blank_ratio_max": 0.2},
    })
    assert scan["status"] == "pass" and scan["issues"] == []
    assert scan["hard_issue_count"] == 0 and scan["review_candidate_count"] == 0


def test_legacy_non_docs_route_is_unaffected(tmp_path, monkeypatch):
    text = tmp_path / "notes.txt"
    text.write_text("legacy", encoding="utf-8")
    assert common.target_artifacts({"artifacts": [str(text)], "artifact_quality": {"required": True}}) == []
    guard = load_guard()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK_ID", raising=False)
    assert guard._pre_tool_call(tool_name="kanban_complete", args={"artifacts": [str(text)]}) is None
