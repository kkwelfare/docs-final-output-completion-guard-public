from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_checker_calls_existing_provider_and_completion_accepts_bound_owner_readback(tmp_path, monkeypatch, *, zero_candidates=False):
    checker = _load(ROOT / "check_docs_artifact_quality.py", "jev_normal_checker_integration")
    guard = _load(ROOT / "__init__.py", "jev_normal_completion_guard")

    artifact = tmp_path / "badge.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=180)
    if zero_candidates:
        page.insert_text((35, 70), "Structure fixture", fontsize=14)
    else:
        page.draw_circle((50, 50), 12, color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        page.insert_text((50, 53), "1", fontsize=8)
        page.insert_text((62, 53), "A", fontsize=9)
    doc.save(artifact)
    doc.close()

    source = tmp_path / "source.txt"
    source.write_text("fixture source", encoding="utf-8")
    producer_id, producer_run = "t_fixture-producer", 7
    receiver_id, receiver_run = "t_fixture-owner", 8
    manifest_path = tmp_path / "finalization.json"
    manifest = checker.finalization.build_manifest(
        [str(artifact)], source=str(source),
        checker=str(ROOT / "check_docs_artifact_quality.py"),
        created_by_task_id=producer_id, created_by_run_id=producer_run,
        state="accepted",
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    receiver_copy = tmp_path / "received-badge.pdf"
    shutil.copyfile(artifact, receiver_copy)
    receiver_path = tmp_path / "receiver.json"
    receiver = {
        "status": "accepted", "artifact_set_id": manifest["artifact_set_id"],
        "receiver_task_id": receiver_id, "receiver_run_id": receiver_run,
        "copies": [{
            "canonical_path": str(artifact.resolve()),
            "copy_path": str(receiver_copy.resolve()), "sha256": _sha(receiver_copy),
            "size_bytes": receiver_copy.stat().st_size,
            "media_type": "application/pdf",
        }],
    }
    receiver_path.write_text(json.dumps(receiver, sort_keys=True), encoding="utf-8")

    contract = {
        "artifact_kind": "pdf", "artifacts": [str(artifact.resolve())],
        "finalization_manifest": {
            "path": str(manifest_path.resolve()),
            "receiver_receipt": str(receiver_path.resolve()),
        },
        "artifact_quality": {"required": True, "layout_typography": {
            "required": True, "measurement_basis": "pdf_points", "minimum_gap_pt": 0,
            "dead_space": {"bottom_blank_ratio_max": 1},
            "narrow_card_exemption_ratio": 0,
            # Explicit permission for an external provider is deliberate.
            "allow_live_provider": True,
        }},
    }
    # Reuse the existing complete baseline fixture contract/evidence so
    # unrelated rules cannot mask the normal-provider discriminator.
    baseline = _load(ROOT / "tests/test_composition_receipt_e2e.py", "jev_baseline_fixtures")
    baseline_contract = baseline._contract(tmp_path, source, artifact)
    for key in ("schema_version", "authoritative_source", "visible_criteria", "rule_contracts"):
        contract[key] = baseline_contract[key]
    contract["authoritative_source"]["readback_mode"] = "text"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for rule in checker.common.CONTRACT_RULES:
        (evidence / f"{artifact.name}.{rule}.json").write_text(
            json.dumps(baseline._check(rule, contract, artifact)), encoding="utf-8"
        )

    calls = []

    def mocked_existing_provider(wire, timeout):
        calls.append((wire, timeout))
        reviews = []
        for candidate in wire["candidates"]:
            reviews.append({
                "candidate_id": candidate["id"], "disposition": "accepted",
                "criteria_results": [
                    {"criterion": criterion, "status": "pass"}
                    for criterion in candidate["criteria"]
                ],
            })
        return {
            "schema_version": "docs-jev-post-render-response-1",
            "request_sha256": wire["request_sha256"], "reviews": reviews,
        }

    if zero_candidates:
        def mocked_transport(endpoint, key_env, model, payload, timeout):
            calls.append((payload, timeout))
            assert payload["questions"] and set(payload["questions"]) == {"artifact_q0"}
            wire = json.loads(payload["state"])
            assert wire["candidates"] == [] and wire["review_scope"] == "whole_artifact"
            assert wire["whole_artifact"]["metrics"]["page_count"] == 1
            assert "Structure fixture" not in payload["state"]
            return {"answers": {"artifact_q0": {"choice": "pass", "confidence": 0.95}}}
        monkeypatch.setattr(checker.jev_post_render.jev_overlap, "_post_provider", mocked_transport)
    else:
        monkeypatch.setattr(checker.jev_post_render, "text_provider", mocked_existing_provider)
    monkeypatch.setenv(checker.jev_post_render.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")

    first_receipt, first_code = checker.build_receipt(contract_path, evidence_dir=evidence)
    first_layout = next(rule for rule in first_receipt["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    first_scan_path = Path(first_layout["evidence"]["path"])
    first_scan = json.loads(first_scan_path.read_text(encoding="utf-8"))
    route = first_scan["generated_artifact_jev"]
    assert first_code == 2  # Owner disposition has not yet been recorded.
    assert route["status"] == "evaluated"
    assert route["provider_invoked"] is True and route["formal_provider_evidence"] is True
    assert route["execution_mode"] == "live"
    request = json.loads(Path(route["request"]["path"]).read_text(encoding="utf-8"))
    assert request["reviewer"] == {"task_id": receiver_id, "run_id": receiver_run}
    assert request["allow_live_provider"] is True and len(calls) == 1
    if zero_candidates:
        assert route["candidate_count"] == 0 and request["candidates"] == []
        assert request["binding"]["review_scope"] == "whole_artifact"
        assert route["evaluation_state"] == "executed"
    else:
        assert calls[0][0]["candidates"] and "body" not in calls[0][0]
    assert checker.generated_artifact_jev.validate_readback(first_scan, route) is None

    candidates = first_scan["REVIEW_CANDIDATE"]
    review_path = evidence / f"{checker._key(artifact)}.AQ-LAYOUT-01-REVIEW.json"
    reviews = []
    for index, candidate in enumerate(candidates):
        render = candidate["render_evidence"]
        reviews.append({
            "issue_id": candidate["id"], "rule": candidate["rule"],
            "status": "accepted", "disposition": "accepted",
            "rationale_code": "owner-reviewed", "llm_invoked": False,
            "vision_invoked": False, "page": candidate["page"],
            "bbox_pdf_points": candidate["bbox_pdf_points"],
            "render_path": render["path"], "render_sha256": render["sha256"],
            "affected_pages": candidate.get("affected_pages"),
        })
    owner_review = {
        "schema_version": "docs-layout-visual-review-2", "status": "pass",
        "artifact_path": str(artifact.resolve()), "artifact_sha256": _sha(artifact),
        "scan_path": str(first_scan_path.resolve()),
        "scan_sha256": first_scan["reviewed_scan_sha256"],
        "producer_task_id": producer_id, "producer_run_id": producer_run,
        "reviewer_task_id": receiver_id, "reviewer_run_id": receiver_run,
        "reviewer_role": "artifact-owner", "artifact_set_id": manifest["artifact_set_id"],
        "candidate_set_digest": checker._candidate_set_digest(candidates),
        "llm_invoked": False, "vision_invoked": False, "reviews": reviews,
    }
    review_path.write_text(json.dumps(owner_review, sort_keys=True), encoding="utf-8")

    final_receipt, final_code = checker.build_receipt(contract_path, evidence_dir=evidence)
    # This English-only integration fixture deliberately does not provide
    # the pre-existing strict CJK production contract. Assert the changed
    # checker -> owner -> completion scan seam, without weakening that
    # independent contract or calling its overall receipt a production pass.
    assert final_code == 2
    assert len(calls) == 1  # Unchanged evidence is reused for owner readback.
    final_scan_path = Path(next(
        rule["evidence"]["path"] for rule in final_receipt["entries"][0]["rules"]
        if rule["id"] == "AQ-LAYOUT-01"
    ))
    final_scan = json.loads(final_scan_path.read_text(encoding="utf-8"))
    assert checker.generated_artifact_jev.validate_readback(
        final_scan, final_scan["generated_artifact_jev"]
    ) is None
    assert final_scan["status"] == "pass"
    assert guard._validate_scan_readback(
        final_scan, artifact=artifact, generated_jev_required=True,
        review_path=review_path, artifact_set_id=manifest["artifact_set_id"],
    ) is None
    assert final_scan["reviewed_scan_sha256"] == first_scan["reviewed_scan_sha256"]
    observation = json.loads((evidence / "badge.AQ-LAYOUT-01.JEV.execution.json").read_text())
    assert observation["evaluation_state"] == "evidence_reused"
    if zero_candidates:
        # Full normal checker path with changed bytes and refreshed bindings.
        replacement = pymupdf.open()
        replacement.new_page(width=300, height=180).insert_text((35, 70), "Changed fixture", fontsize=14)
        replacement.save(artifact)
        replacement.close()
        manifest = checker.finalization.build_manifest(
            [str(artifact)], source=str(source), checker=str(ROOT / "check_docs_artifact_quality.py"),
            created_by_task_id=producer_id, created_by_run_id=producer_run, state="accepted",
        )
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        shutil.copyfile(artifact, receiver_copy)
        receiver["artifact_set_id"] = manifest["artifact_set_id"]
        receiver["copies"][0].update(sha256=_sha(receiver_copy), size_bytes=receiver_copy.stat().st_size)
        receiver_path.write_text(json.dumps(receiver, sort_keys=True))
        checker.build_receipt(contract_path, evidence_dir=evidence)
        assert len(calls) == 2


def test_zero_candidates_normal_checker_calls_transport_once_and_reuses(tmp_path, monkeypatch):
    test_checker_calls_existing_provider_and_completion_accepts_bound_owner_readback(
        tmp_path, monkeypatch, zero_candidates=True,
    )
