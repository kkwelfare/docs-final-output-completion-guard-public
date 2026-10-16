"""Master decision: producer/receiver task/run IDs are optional records and
AQ-LAYOUT-01 REVIEW_CANDIDATE items alone do not stop completion.

Kept: artifact sha256 / artifact_set_id identity, receiver-copy actual-hash readback,
and HARD_FAIL blocking.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import content_blocks, finalization, rendered_readback  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _guard():
    spec = importlib.util.spec_from_file_location("identity_optional_guard", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _setup(tmp_path: Path):
    artifact = tmp_path / "out.pdf"
    artifact.write_bytes(b"%PDF-1.4 fixture\n")
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    checker = ROOT / "check_docs_artifact.py"
    copy_dir = tmp_path / "received"
    copy_dir.mkdir()
    received = copy_dir / artifact.name
    shutil.copyfile(artifact, received)
    return artifact, source, checker, received


def _receiver(path: Path, manifest: dict, artifact: Path, received: Path, **identity) -> None:
    value = {
        "status": "accepted", "artifact_set_id": manifest["artifact_set_id"],
        "copies": [{"canonical_path": str(artifact.resolve()), "copy_path": str(received.resolve()),
                    "sha256": _sha(received), "size_bytes": received.stat().st_size,
                    "media_type": finalization.media_type(received)}],
        **identity,
    }
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("task_id,run_id", [(None, None), (None, 0), ("t_x", None)])
def test_manifest_without_or_with_partial_producer_ids_passes(tmp_path, task_id, run_id):
    artifact, source, checker, _ = _setup(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = finalization.build_manifest(
        [str(artifact)], source=str(source), checker=str(checker),
        created_by_task_id=task_id, created_by_run_id=run_id,
    )
    if task_id is None and run_id is None:
        manifest["created_by_task_id"] = None  # explicit null is also accepted
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data, error = finalization.validate_manifest(manifest_path, [str(artifact)], require_identity=True)
    assert error is None and data is not None


def test_receiver_without_ids_passes_and_actual_copy_hash_is_still_checked(tmp_path):
    artifact, source, checker, received = _setup(tmp_path)
    manifest = finalization.build_manifest([str(artifact)], source=str(source), checker=str(checker))
    receipt = tmp_path / "receiver.json"
    _receiver(receipt, manifest, artifact, received, receiver_task_id=None, receiver_run_id=0)
    assert finalization.validate_receiver_copy(manifest, receipt, require_identity=True) is None
    _receiver(receipt, manifest, artifact, received)
    assert finalization.validate_receiver_copy(manifest, receipt, require_identity=True) is None
    received.write_bytes(b"%PDF-1.4 tampered copy\n")
    _receiver(receipt, manifest, artifact, received)
    receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_data["copies"][0]["sha256"] = manifest["artifacts"][0]["sha256"]
    receipt.write_text(json.dumps(receipt_data), encoding="utf-8")
    assert finalization.validate_receiver_copy(manifest, receipt, require_identity=True) == "receiver-copy-hash-drift"


def test_artifact_sha_mismatch_and_artifact_set_mismatch_still_fail(tmp_path):
    artifact, source, checker, received = _setup(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = finalization.build_manifest([str(artifact)], source=str(source), checker=str(checker))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifact.write_bytes(b"%PDF-1.4 changed after check\n")
    _, error = finalization.validate_manifest(manifest_path, [str(artifact)], require_identity=True)
    assert error == "finalization manifest artifact hash/size/media-type drift"
    artifact.write_bytes(b"%PDF-1.4 fixture\n")
    bad = dict(manifest, artifact_set_id="0" * 64)
    manifest_path.write_text(json.dumps(bad), encoding="utf-8")
    _, error = finalization.validate_manifest(manifest_path, [str(artifact)], require_identity=True)
    assert error == "finalization manifest artifact set mismatch"
    receipt = tmp_path / "receiver.json"
    _receiver(receipt, manifest, artifact, received)
    receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_data["artifact_set_id"] = "1" * 64
    receipt.write_text(json.dumps(receipt_data), encoding="utf-8")
    assert finalization.validate_receiver_copy(manifest, receipt, require_identity=True) == (
        "receiver receipt is missing or does not accept the manifest")


def test_malformed_present_ids_still_fail(tmp_path):
    artifact, source, checker, received = _setup(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = finalization.build_manifest([str(artifact)], source=str(source), checker=str(checker))
    manifest_path.write_text(json.dumps(dict(manifest, created_by_run_id="674")), encoding="utf-8")
    _, error = finalization.validate_manifest(manifest_path, [str(artifact)], require_identity=True)
    assert error == "finalization manifest producer run identity is malformed"
    receipt = tmp_path / "receiver.json"
    _receiver(receipt, manifest, artifact, received, receiver_task_id="")
    assert finalization.validate_receiver_copy(manifest, receipt, require_identity=True) == (
        "receiver receipt task identity is malformed")


def test_source_and_checker_handles_remain_required_under_strict_identity(tmp_path):
    artifact, _, _, _ = _setup(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    finalization.write_manifest(manifest_path.resolve(), [str(artifact)])
    _, error = finalization.validate_manifest(manifest_path, [str(artifact)], require_identity=True)
    assert error == "finalization source handle is missing"


def test_content_block_run_id_drift_alone_does_not_stop_but_artifact_set_drift_does(tmp_path, monkeypatch):
    evidence = tmp_path / "content.json"
    evidence.write_text(json.dumps({"producer_run_id": 0, "artifact_set_id": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(content_blocks, "validate_manifest", lambda data, artifact=None: [])
    policy = {"source_evidence": {"path": str(evidence), "sha256": _sha(evidence)},
              "strict_identity": {"producer_run_id": 674, "artifact_set_id": "a" * 64}}
    data, errors = content_blocks.load_manifest(policy, tmp_path / "out.pdf")
    assert errors == [] and data is not None
    policy["strict_identity"] = {"producer_run_id": None, "artifact_set_id": "a" * 64}
    assert content_blocks.load_manifest(policy, tmp_path / "out.pdf")[1] == []
    policy["strict_identity"] = {"producer_run_id": 0, "artifact_set_id": "b" * 64}
    assert content_blocks.load_manifest(policy, tmp_path / "out.pdf")[1] == [
        "content_block_evidence: run/artifact-set identity mismatch"]


def test_rendered_readback_without_run_id_keeps_artifact_set_binding(tmp_path):
    import pymupdf
    pdf = tmp_path / "page.pdf"
    with pymupdf.open() as document:
        document.new_page(width=200, height=200).insert_text((20, 40), "readback")
        document.save(pdf)
    evidence = tmp_path / "evidence"
    manifest = rendered_readback.build_manifest(pdf, evidence, producer_run_id=None, artifact_set_id="c" * 64)
    handle = {"path": str(manifest), "sha256": _sha(manifest)}
    assert rendered_readback.validate_handle(handle, pdf, producer_run_id=None, artifact_set_id="c" * 64) == []
    assert rendered_readback.validate_handle(handle, pdf, producer_run_id=None, artifact_set_id="d" * 64) == [
        "AQ-DELIVERY-01: rendered readback run/artifact-set identity mismatch"]


def _scan(review_mode: str, *, hard: bool = False, candidate_ids=None, status=None) -> dict:
    review = [{"id": "pdf-r1", "rule": "cjk_glyph_or_region_appearance_anomaly_candidate", "severity": "review",
               "bucket": "REVIEW_CANDIDATE", "page": 1}]
    hard_items = [{"id": "pdf-h1", "rule": "text_overlap", "severity": "hard", "bucket": "HARD_FAIL", "page": 1}] if hard else []
    return {
        "issues": hard_items + review, "HARD_FAIL": hard_items, "REVIEW_CANDIDATE": review,
        "hard_issue_count": len(hard_items), "review_candidate_count": 1,
        "measurement_basis": "pdf_points",
        "status": status or ("block" if hard else "pass"),
        "limited_visual_review": {
            "required": False, "candidate_count": 1, "candidate_pages": [1],
            "candidate_ids": ["pdf-r1"] if candidate_ids is None else candidate_ids,
            "review_mode": review_mode, "vision_model_invoked": False, "evidence": None,
            "status": "review-required" if hard else "pass",
        },
    }


def test_guard_accepts_recorded_review_candidates_without_review_record():
    guard = _guard()
    assert guard._validate_scan_readback(_scan("candidates-recorded-no-stop")) is None


def test_guard_still_rejects_hard_fail_and_legacy_or_forged_review_states():
    guard = _guard()
    assert guard._validate_scan_readback(_scan("candidates-recorded-no-stop", hard=True)) is not None
    assert guard._validate_scan_readback(_scan("manual-receipt-only")) == "AQ-LAYOUT-01: review attachment is missing"
    assert guard._validate_scan_readback(_scan("candidates-recorded-no-stop", candidate_ids=["other"])) == (
        "AQ-LAYOUT-01: review attachment is missing")
    assert guard._validate_scan_readback(
        _scan("candidates-recorded-no-stop"), review_route_opt_in=True) == "AQ-LAYOUT-01: review attachment is missing"
