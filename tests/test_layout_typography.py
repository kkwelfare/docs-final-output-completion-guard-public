from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import common, cjk_font_selector, layout_typography

JP_FONT = "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"


def _load_checker():
    spec = importlib.util.spec_from_file_location("layout_quality_checker", ROOT / "check_docs_artifact_quality.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pdf(path: Path, draw) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=300, height=180)
    draw(page)
    doc.save(path)
    doc.close()
    return path


def _rules(result):
    return {issue["rule"] for issue in result["issues"]}


def _raw_span(text, bbox, flags=0, *, font="IPAGothic", size=10.0, origin_y=None):
    width = (float(bbox[2]) - float(bbox[0])) / max(1, len(text))
    chars = [
        {"c": char, "bbox": [float(bbox[0]) + index * width, float(bbox[1]),
                              float(bbox[0]) + (index + 1) * width, float(bbox[3])]}
        for index, char in enumerate(text)
    ]
    return {"chars": chars, "font": font, "flags": flags, "size": size,
            "origin": [float(bbox[0]), float(bbox[3]) if origin_y is None else float(origin_y)]}


def _fake_scan(tmp_path, monkeypatch, lines, *, render_manifest=None, config=None):
    pdf = tmp_path / "fake-wrap.pdf"
    pdf.write_bytes(b"%PDF-fake-fixture")
    class FakePage:
        rect = fitz.Rect(0, 0, 300, 180)
        def get_text(self, kind):
            assert kind == "rawdict"
            return {"blocks": [{"type": 0, "lines": [
                {"bbox": row[1], "spans": [_raw_span(row[0], row[1], row[2], origin_y=row[3] if len(row) > 3 else None)]}
                for row in lines
            ]}]}
    class FakeDoc:
        def __enter__(self): return [FakePage()]
        def __exit__(self, *args): return False
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: FakeDoc())
    scan_config = {"required": True, "expected_cjk_fonts": ["IPAGothic"]} if config is None else config
    return layout_typography.scan_pdf(pdf, scan_config, render_manifest=render_manifest)


def test_normal_teaching_material_fixture_passes(tmp_path):
    pdf = _pdf(tmp_path / "normal-teaching-material.pdf", lambda page: (page.insert_text((30, 40), "Step 1: prepare"), page.insert_text((30, 75), "Step 2: confirm")))
    result = layout_typography.scan_pdf(pdf, {"minimum_gap_px": 2})
    assert result["status"] == "pass" and result["issues"] == []


def test_overlap_fixture_fails_targeted_rule(tmp_path):
    pdf = _pdf(tmp_path / "overlap.pdf", lambda page: (page.insert_text((30, 40), "ONE"), page.insert_text((30, 40), "TWO")))
    result = layout_typography.scan_pdf(pdf, {"minimum_gap_px": 0})
    assert "overlap" in _rules(result)
    assert any(issue["rule"] == "overlap" and issue["bucket"] == "HARD_FAIL" for issue in result["HARD_FAIL"])


def test_same_block_adjacent_line_ink_boxes_are_line_rhythm_not_overlap(tmp_path, monkeypatch):
    result = _fake_scan(tmp_path, monkeypatch, [
        ("first line", [10, 10, 150, 22], 0, 18),
        ("second line", [10, 20, 160, 32], 0, 30),
    ])
    assert "overlap" not in _rules(result)


def test_same_block_coincident_baselines_remain_hard_overlap(tmp_path, monkeypatch):
    result = _fake_scan(tmp_path, monkeypatch, [
        ("first draw", [10, 10, 150, 22], 0, 18),
        ("second draw", [10, 10, 160, 22], 0, 18),
    ])
    assert "overlap" in _rules(result)


def test_clipping_or_out_of_page_fixture_fails_targeted_rule(tmp_path, monkeypatch):
    result = _fake_scan(tmp_path, monkeypatch, [("outside", [-2, 10, 40, 20], 0)], config={"required": True})
    assert "clipping_or_out_of_page" in _rules(result)


def test_font_fallback_fixture_fails_targeted_rule(tmp_path):
    pdf = _pdf(tmp_path / "fallback.pdf", lambda page: page.insert_text((30, 40), "日本語", fontname="japan", fontfile=JP_FONT, fontsize=16))
    assert "cjk_font_fallback" in _rules(layout_typography.scan_pdf(pdf, {"expected_cjk_fonts": ["never-a-pdf-font"]}))


def test_circled_number_bold_fixture_fails_targeted_rule(tmp_path, monkeypatch):
    # Use a deterministic extraction seam: PDF bytes are real, and the scanner
    # receives the extracted circled-number/bold span as its renderer would.
    pdf = _pdf(tmp_path / "circled.pdf", lambda page: page.insert_text((30, 40), "placeholder"))
    class FakePage:
        rect = fitz.Rect(0, 0, 300, 180)
        def get_text(self, kind):
            assert kind == "rawdict"
            return {"blocks": [{"type": 0, "lines": [{"bbox": [10, 10, 30, 20], "spans": [_raw_span("①", [10, 10, 30, 20], 16)]}]}]}
    class FakeDoc:
        def __enter__(self): return [FakePage()]
        def __exit__(self, *args): return False
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: FakeDoc())
    assert "circled_number_bold" in _rules(layout_typography.scan_pdf(pdf, {}))


def test_unusual_wrap_fixture_fails_targeted_rules(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "wrap.pdf", lambda page: page.insert_text((30, 40), "placeholder"))
    class FakePage:
        rect = fitz.Rect(0, 0, 300, 180)
        def get_text(self, kind):
            return {"blocks": [{"type": 0, "lines": [
                {"bbox": [10, 10, 30, 20], "spans": [_raw_span("（", [10, 10, 30, 20])]},
                {"bbox": [10, 25, 30, 35], "spans": [_raw_span("）", [10, 25, 30, 35])]},
                {"bbox": [10, 40, 30, 50], "spans": [_raw_span("は", [10, 40, 30, 50])]},
                {"bbox": [10, 55, 30, 65], "spans": [_raw_span("①", [10, 55, 30, 65])]},
            ]}]}
    class FakeDoc:
        def __enter__(self): return [FakePage()]
        def __exit__(self, *args): return False
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: FakeDoc())
    rules = _rules(layout_typography.scan_pdf(pdf, {}))
    assert {"opening_bracket_at_line_end", "closing_bracket_at_line_start", "orphan_particle_or_punctuation", "circled_number_separated"} <= rules


def test_short_final_line_and_automatic_wrap_candidates_are_independently_identified(tmp_path, monkeypatch):
    result = _fake_scan(tmp_path, monkeypatch, [
        ("This explanation continues", [10, 10, 200, 20], 0),
        ("短い", [10, 25, 25, 35], 0),
    ])
    assert {"short_final_line_candidate", "automatic_wrap_anomaly_candidate"} <= _rules(result)
    assert "manual_break_anomaly_candidate" not in _rules(result)
    assert result["status"] == "review" and result["hard_issue_count"] == 0
    for candidate in result["REVIEW_CANDIDATE"]:
        assert candidate["bucket"] == "REVIEW_CANDIDATE"
        assert candidate["issue_id"] == candidate["id"]
        assert candidate["scan_hash"] == result["scan_hash"]
        assert candidate["page_index"] == candidate["page"] - 1
        assert candidate["bbox"] == candidate["bbox_pdf_points"]
        assert candidate["candidate_type"] == candidate["rule"]


def test_cjk_glyph_or_region_appearance_anomaly_is_review_candidate(tmp_path, monkeypatch):
    result = _fake_scan(
        tmp_path,
        monkeypatch,
        [("日本語", [10, 10, 80, 20], 0)],
        config={
            "required": True,
            "expected_cjk_fonts": ["IPAGothic"],
            "cjk_appearance_regions": [{"page": 1, "bbox": [9, 9, 81, 21]}],
        },
    )
    candidate = next(issue for issue in result["REVIEW_CANDIDATE"]
                     if issue["candidate_type"] == "cjk_glyph_or_region_appearance_anomaly_candidate")
    assert result["status"] == "review" and result["hard_issue_count"] == 0
    assert candidate["page_index"] == 0 and candidate["bbox"] == [9.0, 9.0, 81.0, 21.0]


def test_manual_break_anomaly_candidate_is_distinct(tmp_path, monkeypatch):
    result = _fake_scan(tmp_path, monkeypatch, [
        ("Intentionally broken clause", [10, 10, 160, 20], 0),
        ("短", [10, 25, 20, 35], 0),
    ])
    assert {"short_final_line_candidate", "manual_break_anomaly_candidate"} <= _rules(result)
    assert "automatic_wrap_anomaly_candidate" not in _rules(result)


def test_split_inside_latin_word_candidate_is_identified_without_using_japanese_spacing(tmp_path, monkeypatch):
    result = _fake_scan(tmp_path, monkeypatch, [
        ("pre", [10, 10, 180, 20], 0),
        ("pare", [10, 25, 45, 35], 0),
    ])
    assert _rules(result) == {"split_inside_word_candidate"}


def test_normal_short_heading_automatic_wrap_manual_break_and_heading_weight_pass(tmp_path, monkeypatch):
    heading = _fake_scan(tmp_path, monkeypatch, [("短い見出し", [10, 10, 80, 20], 16)])
    assert heading["status"] == "pass"
    automatic = _fake_scan(tmp_path, monkeypatch, [
        ("This is a complete line", [10, 10, 200, 20], 0),
        ("continuation text", [10, 25, 145, 35], 0),
    ])
    assert automatic["status"] == "pass"
    manual = _fake_scan(tmp_path, monkeypatch, [
        ("First intended clause.", [10, 10, 120, 20], 0),
        ("Second intended clause.", [10, 25, 135, 35], 0),
    ])
    assert manual["status"] == "pass"
    heading_body_weight = _fake_scan(tmp_path, monkeypatch, [
        ("Intentional heading", [10, 10, 120, 20], 16),
        ("Ordinary body paragraph.", [10, 25, 170, 35], 0),
    ])
    assert "font_weight_inconsistent" not in _rules(heading_body_weight)
    intended_short_sentence = _fake_scan(tmp_path, monkeypatch, [
        ("This statement is complete.", [10, 10, 200, 20], 0),
        ("OK.", [10, 25, 25, 35], 0),
    ])
    assert intended_short_sentence["status"] == "pass"


def test_candidate_links_full_size_render_path_hash_page_and_bbox(tmp_path, monkeypatch):
    render = tmp_path / "full-size-page.png"
    Image.new("RGB", (600, 360), "white").save(render)
    digest = hashlib.sha256(render.read_bytes()).hexdigest()
    manifest = tmp_path / "delivery.json"
    manifest.write_text(json.dumps({"renders": [{"path": str(render), "sha256": digest, "page": 1, "width": 600, "height": 360}]}), encoding="utf-8")
    result = _fake_scan(tmp_path, monkeypatch, [
        ("This explanation continues", [10, 10, 200, 20], 0),
        ("短い", [10, 25, 25, 35], 0),
    ], render_manifest=manifest)
    candidate = next(issue for issue in result["issues"] if issue["rule"] == "short_final_line_candidate")
    assert candidate["render_evidence"] == {
        "path": str(render), "sha256": digest, "page": 1, "width": 600, "height": 360,
        "render_scale": 2.0, "bbox_pdf_points": [10, 10, 200, 35],
        "bbox_render_pixels": [20.0, 20.0, 400.0, 70.0],
    }


def test_receipt_builder_blocks_layout_fixture_through_actual_path(tmp_path):
    checker = _load_checker()
    pdf = _pdf(tmp_path / "overlap.pdf", lambda page: (page.insert_text((30, 40), "ONE"), page.insert_text((30, 40), "TWO")))
    contract = {"artifact_kind": "pdf", "artifacts": [str(pdf)], "artifact_quality": {"required": True, "layout_typography": {"required": True, "minimum_gap_px": 0, "narrow_card_exemption_ratio": 0}}}
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    receipt, code = checker.build_receipt(contract_path, evidence_dir=tmp_path / "evidence")
    assert code == 2 and receipt["status"] == "block"
    layout_rule = next(rule for rule in receipt["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert layout_rule["status"] == "fail"
    scan = json.loads(Path(layout_rule["evidence"]["path"]).read_text(encoding="utf-8"))
    candidate = next(issue for issue in scan["issues"] if issue["rule"] == "split_inside_word_candidate")
    evidence = candidate["render_evidence"]
    assert Path(evidence["path"]).is_file() and evidence["sha256"]
    assert evidence["page"] == 1 and evidence["bbox_pdf_points"] and evidence["bbox_render_pixels"]


def test_pdf_layout_contract_missing_false_and_non_dict_fail_closed(tmp_path):
    pdf = tmp_path / "fixture.pdf"
    base = {"artifacts": [str(pdf)], "artifact_quality": {"required": True}}
    assert common.validate_pdf_layout_contract(base)
    base["artifact_quality"]["layout_typography"] = {"required": False}
    assert common.validate_pdf_layout_contract(base)
    base["artifact_quality"]["layout_typography"] = "enabled"
    assert common.validate_pdf_layout_contract(base)


def test_pdf_layout_contract_required_true_passes(tmp_path):
    contract = {
        "artifacts": [str(tmp_path / "fixture.pdf")],
        "artifact_quality": {"required": True, "layout_typography": {"required": True}},
    }
    assert common.validate_pdf_layout_contract(contract) == []


def test_cjk_pdf_without_expected_fonts_fails_configuration(tmp_path, monkeypatch):
    result = _fake_scan(
        tmp_path,
        monkeypatch,
        [("日本語", [10, 10, 80, 20], 0)],
        config={"required": True},
    )
    assert result["status"] == "block"
    assert "expected_cjk_fonts_required" in _rules(result)


def test_cjk_pdf_with_matching_expected_font_passes(tmp_path, monkeypatch):
    result = _fake_scan(
        tmp_path,
        monkeypatch,
        [("日本語", [10, 10, 80, 20], 0)],
        config={"required": True, "expected_cjk_fonts": ["IPAGothic"]},
    )
    assert result["status"] == "pass" and result["issues"] == []


def _cjk_font_evidence(tmp_path, artifact, *, artifact_sha=None, family="IPAGothic"):
    evidence = tmp_path / "cjk-font-evidence.json"
    selector_path = Path(cjk_font_selector.__file__).resolve()
    evidence.write_text(json.dumps({
        "artifact_path": str(artifact.resolve()),
        "artifact_sha256": artifact_sha or hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "selector_path": str(selector_path),
        "selector_sha256": hashlib.sha256(selector_path.read_bytes()).hexdigest(),
        "selection": {
            "family": family, "status": "ud-installed", "path": JP_FONT,
            "sha256": hashlib.sha256(Path(JP_FONT).read_bytes()).hexdigest(),
            "priority": "fixture-primary", "fallback_decision": "fixture-no-fallback",
            "file_readback_family": family,
        },
    }), encoding="utf-8")
    return {"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}


def test_cjk_font_evidence_binds_selector_file_and_artifact_identity(tmp_path, monkeypatch):
    pdf = tmp_path / "fake-wrap.pdf"
    pdf.write_bytes(b"%PDF-fake-fixture")
    handle = _cjk_font_evidence(tmp_path, pdf)
    result = _fake_scan(tmp_path, monkeypatch, [("日本語", [10, 10, 80, 20], 0)],
        config={"required": True, "expected_cjk_fonts": ["IPAGothic"],
                "cjk_font_evidence_required": True, "cjk_font_evidence": handle})
    assert result["status"] == "pass" and result["issues"] == []


def test_cjk_font_evidence_rejects_stale_artifact_and_family_mismatch(tmp_path, monkeypatch):
    pdf = tmp_path / "fake-wrap.pdf"
    pdf.write_bytes(b"%PDF-fake-fixture")
    stale = _cjk_font_evidence(tmp_path, pdf, artifact_sha="0" * 64)
    stale_result = _fake_scan(tmp_path, monkeypatch, [("日本語", [10, 10, 80, 20], 0)],
        config={"required": True, "expected_cjk_fonts": ["IPAGothic"],
                "cjk_font_evidence_required": True, "cjk_font_evidence": stale})
    assert "cjk_font_evidence_artifact_or_selector_mismatch" in _rules(stale_result)
    mismatch = _cjk_font_evidence(tmp_path, pdf, family="OtherFamily")
    mismatch_result = _fake_scan(tmp_path, monkeypatch, [("日本語", [10, 10, 80, 20], 0)],
        config={"required": True, "expected_cjk_fonts": ["IPAGothic"],
                "cjk_font_evidence_required": True, "cjk_font_evidence": mismatch})
    assert "cjk_font_evidence_selection_mismatch" in _rules(mismatch_result)


def test_english_only_pdf_does_not_require_expected_cjk_fonts(tmp_path, monkeypatch):
    result = _fake_scan(
        tmp_path,
        monkeypatch,
        [("English only", [10, 10, 100, 20], 0)],
        config={"required": True},
    )
    assert result["status"] == "pass" and result["issues"] == []


def test_receipt_builder_emits_failed_layout_rule_when_pdf_config_missing(tmp_path):
    checker = _load_checker()
    pdf = _pdf(tmp_path / "english.pdf", lambda page: page.insert_text((30, 40), "English only"))
    contract_path = tmp_path / "missing-layout.json"
    contract_path.write_text(json.dumps({
        "artifact_kind": "pdf",
        "artifacts": [str(pdf)],
        "artifact_quality": {"required": True},
    }), encoding="utf-8")
    receipt, code = checker.build_receipt(contract_path, evidence_dir=tmp_path / "evidence-missing")
    layout_rule = next(rule for rule in receipt["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert code == 2 and receipt["status"] == "block" and layout_rule["status"] == "fail"


def test_receipt_builder_runs_layout_rule_when_pdf_config_required(tmp_path):
    checker = _load_checker()
    pdf = _pdf(tmp_path / "english.pdf", lambda page: page.insert_text((30, 40), "English only"))
    contract_path = tmp_path / "required-layout.json"
    contract_path.write_text(json.dumps({
        "artifact_kind": "pdf", "artifacts": [str(pdf)],
        "artifact_quality": {"required": True, "layout_typography": {"required": True}},
    }), encoding="utf-8")
    receipt, _ = checker.build_receipt(contract_path, evidence_dir=tmp_path / "evidence-required")
    layout_rule = next(rule for rule in receipt["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert layout_rule["status"] == "pass" and Path(layout_rule["evidence"]["path"]).is_file()


def test_mixed_pptx_pdf_runs_layout_only_for_pdf(tmp_path, monkeypatch):
    checker = _load_checker()
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"not-rendered-in-this-targeting-test")
    pdf = _pdf(tmp_path / "deck.pdf", lambda page: page.insert_text((30, 40), "English only"))
    monkeypatch.setattr(checker.rendered_readback, "build_manifest", lambda *_: None)
    evidence_dir = tmp_path / "mixed-evidence"
    evidence_dir.mkdir()
    contract_path = tmp_path / "mixed-layout.json"
    contract_path.write_text(json.dumps({
        "artifact_kind": "pptx_pdf", "artifacts": [str(pptx), str(pdf)],
        "authoritative_source": {"path": str(pdf), "sha256": common.sha256(pdf), "readback_mode": "fixture"},
        "artifact_quality": {"required": True, "rule_targets": [str(pdf)], "layout_typography": {"required": True}},
    }), encoding="utf-8")
    receipt, _ = checker.build_receipt(contract_path, evidence_dir=tmp_path / "mixed-evidence")
    pptx_entry, pdf_entry = receipt["entries"]
    assert all(rule["id"] != "AQ-LAYOUT-01" for rule in pptx_entry["rules"])
    layout_rule = next(rule for rule in pdf_entry["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert layout_rule["status"] == "pass" and Path(layout_rule["evidence"]["path"]).is_file()



def test_soft_candidate_requires_exact_hash_bound_limited_review(tmp_path, monkeypatch):
    checker = _load_checker()
    pdf = _pdf(tmp_path / "soft.pdf", lambda page: page.insert_text((30, 40), "English only"))
    evidence_dir = tmp_path / "review-evidence"
    contract_path = tmp_path / "review-layout.json"
    contract_path.write_text(json.dumps({
        "artifact_kind": "pdf", "artifacts": [str(pdf)],
        "artifact_quality": {"required": True, "layout_typography": {"required": True}},
    }), encoding="utf-8")

    def fake_scan(artifact, config, *, render_manifest=None):
        manifest = json.loads(Path(render_manifest).read_text(encoding="utf-8"))
        render = manifest["renders"][0]
        return {
            "schema_version": "docs-layout-typography-scan-3", "artifact_sha256": common.sha256(artifact),
            "status": "review", "hard_issue_count": 0, "review_candidate_count": 1, "pages": [{"page": 1, "line_count": 2}],
            "issues": [{
                "id": "pdf-soft-1", "rule": "short_final_line_candidate", "severity": "review", "page": 1,
                "bbox_pdf_points": [10, 10, 200, 35], "items": ["p1-b0-l0", "p1-b0-l1"],
                "render_evidence": {"path": render["path"], "sha256": render["sha256"], "page": 1,
                                    "width": render["width"], "height": render["height"], "render_scale": 2.0,
                                    "bbox_pdf_points": [10, 10, 200, 35], "bbox_render_pixels": [20, 20, 400, 70]},
            }],
        }

    monkeypatch.setattr(checker.layout_typography, "scan_pdf", fake_scan)
    first, _ = checker.build_receipt(contract_path, evidence_dir=evidence_dir)
    first_rule = next(rule for rule in first["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert first_rule["status"] == "fail"
    scan_path = Path(first_rule["evidence"]["path"])
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    candidate = scan["issues"][0]
    render = candidate["render_evidence"]
    review_path = evidence_dir / f"{pdf.name}.AQ-LAYOUT-01-REVIEW.json"
    valid = {
        "schema_version": "docs-layout-visual-review-1", "status": "pass",
        "artifact_path": str(pdf.resolve()), "artifact_sha256": common.sha256(pdf),
        "scan_path": str(scan_path.resolve()), "scan_sha256": scan["reviewed_scan_sha256"],
        "reviews": [{"issue_id": candidate["id"], "rule": candidate["rule"], "status": "accepted",
                     "disposition": "accepted", "rationale_code": "verified",
                     "llm_invoked": False, "vision_invoked": False, "page": 1,
                     "bbox_pdf_points": candidate["bbox_pdf_points"], "render_path": render["path"], "render_sha256": render["sha256"]}],
    }
    review_path.write_text(json.dumps(valid), encoding="utf-8")
    passed, _ = checker.build_receipt(contract_path, evidence_dir=evidence_dir)
    passed_rule = next(rule for rule in passed["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")
    assert passed_rule["status"] == "pass"

    for key, bad in (("issue_id", "unrelated"), ("render_sha256", "0" * 64)):
        broken = json.loads(json.dumps(valid))
        broken["reviews"][0][key] = bad
        review_path.write_text(json.dumps(broken), encoding="utf-8")
        failed, _ = checker.build_receipt(contract_path, evidence_dir=evidence_dir)
        assert next(rule for rule in failed["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")["status"] == "fail"
    stale = json.loads(json.dumps(valid)); stale["scan_sha256"] = "f" * 64
    review_path.write_text(json.dumps(stale), encoding="utf-8")
    failed, _ = checker.build_receipt(contract_path, evidence_dir=evidence_dir)
    assert next(rule for rule in failed["entries"][0]["rules"] if rule["id"] == "AQ-LAYOUT-01")["status"] == "fail"


def test_candidate_zero_skips_review_path_and_records_no_vision_invocation(tmp_path, monkeypatch):
    checker = _load_checker()
    pdf = _pdf(tmp_path / "candidate-zero.pdf", lambda page: page.insert_text((30, 40), "English only"))
    contract_path = tmp_path / "candidate-zero.json"
    evidence_dir = tmp_path / "candidate-zero-evidence"
    contract_path.write_text(json.dumps({
        "artifact_kind": "pdf",
        "artifacts": [str(pdf)],
        "artifact_quality": {"required": True, "layout_typography": {"required": True}},
    }), encoding="utf-8")
    calls = []

    def forbidden_review_path(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("candidate-zero must not enter any visual/manual review path")

    monkeypatch.setattr(checker, "_layout_review_passes", forbidden_review_path)
    receipt, _ = checker.build_receipt(contract_path, evidence_dir=evidence_dir)
    rule = next(item for item in receipt["entries"][0]["rules"] if item["id"] == "AQ-LAYOUT-01")
    scan = json.loads(Path(rule["evidence"]["path"]).read_text(encoding="utf-8"))
    review = scan["limited_visual_review"]
    assert rule["status"] == "pass" and calls == []
    assert review == {
        "required": False,
        "candidate_count": 0,
        "candidate_pages": [],
        "review_mode": "manual-receipt-only",
        "vision_model_invoked": False,
        "evidence": None,
        "status": "pass",
    }


def test_image_and_table_deterministic_coverage_gaps_are_manual_review_candidates_only(tmp_path, monkeypatch):
    result = _fake_scan(
        tmp_path,
        monkeypatch,
        [("English only", [10, 10, 100, 20], 0)],
        config={
            "required": True,
            "deterministic_coverage_gaps": [
                {"kind": "image", "page": 1, "bbox": [20, 30, 120, 100]},
                {"kind": "table", "page": 1, "bbox": [130, 30, 280, 120]},
            ],
        },
    )
    gaps = [item for item in result["issues"] if item["rule"] == "deterministic_coverage_gap_candidate"]
    assert result["status"] == "review" and result["hard_issue_count"] == 0
    assert {item["coverage_kind"] for item in gaps} == {"image", "table"}
    assert all(item["severity"] == "review" and item["bucket"] == "REVIEW_CANDIDATE" for item in gaps)


def test_visual_baseline_groups_join_same_line_fragments_without_joining_paragraph_rows():
    items = [
        {"text": "訪問者役：", "bbox": (10, 10, 70, 22), "origin_y": 20.0, "font_size": 22.0},
        {"text": "用件を伝える", "bbox": (70, 10, 180, 22), "origin_y": 20.2, "font_size": 22.0},
        {"text": "次の項目", "bbox": (10, 40, 100, 52), "origin_y": 50.0, "font_size": 22.0},
    ]
    grouped = layout_typography._visual_baseline_lines(items)
    assert [item["text"] for item in grouped] == ["訪問者役：用件を伝える", "次の項目"]
    assert grouped[0]["bbox"] == (10.0, 10.0, 180.0, 22.0)


def test_declared_zero_source_images_skips_renderer_rasterized_shape_blocks(tmp_path, monkeypatch):
    pdf = tmp_path / "rasterized-shape.pdf"
    pdf.write_bytes(b"%PDF-fake-fixture")
    class FakePage:
        rect = fitz.Rect(0, 0, 300, 180)
        def get_text(self, kind):
            assert kind == "rawdict"
            return {"blocks": [{"type": 1, "bbox": [10, 10, 290, 170]}]}
    class FakeDoc:
        def __enter__(self): return [FakePage()]
        def __exit__(self, *args): return False
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: FakeDoc())
    result = layout_typography.scan_pdf(pdf, {"required": True, "source_image_count": 0})
    assert result["status"] == "pass" and result["issues"] == []


def test_role_list_continuation_x_uses_each_paragraph_hanging_indent():
    texts = ["受付", "・訪問者役：名乗り、", "用件を伝える", "・観察役：必要情報と", "次の行動を見る"]
    origins = [120.0, 150.0, 179.7, 213.0, 242.7]
    starts = [106.0, 106.0, 226.0, 106.0, 196.0]
    body_starts = [None, 226.0, None, 196.0, None]

    def rendered_line(text, start_x, y, body_x=None):
        body_index = layout_typography._role_body_start_index(text)
        chars = []
        for index, char in enumerate(text):
            x = (body_x + (index - body_index) * 18.0) if body_x is not None and body_index is not None and index >= body_index else start_x + index * 18.0
            chars.append({"c": char, "bbox": (x, y - 20.0, x + 17.0, y)})
        return {
            "text": text, "bbox": (start_x, y - 20.0, max(item["bbox"][2] for item in chars), y),
            "origin_y": y, "font_size": 22.0, "chars": chars,
        }

    blocks = [rendered_line(text, x, y, body_x) for text, x, y, body_x in zip(texts, starts, origins, body_starts)]
    record = {
        "page": 1,
        "frame_id": "role-list-fixture",
        "role": "role-list",
        "geometry": {
            "frame_bbox_pt": [100.0, 90.0, 356.0, 170.0],
            "margins_pt": [6.0, 6.0],
            "safe_width_pt": 300.0,
            "font_size_pt": 22.0,
        },
        "plan": {
            "line_hashes": [layout_typography._line_hash(text) for text in texts],
            "line_paragraph_indices": [0, 1, 1, 2, 2],
            "paragraph_hanging_indents_pt": [70.0, 120.0, 90.0],
        },
    }
    issues = layout_typography._plan_parity_issues(
        1, blocks, [record], {"continuation_x_tolerance_pt": 0.5}
    )
    assert issues == []

    # Planner expected x and continuation x still agree at 226pt, but the first
    # rendered body glyph alone drifts to 236pt. The existing parity rule must fail.
    blocks[1] = rendered_line(texts[1], 106.0, origins[1], 236.0)
    issues = layout_typography._plan_parity_issues(
        1, blocks, [record], {"continuation_x_tolerance_pt": 0.5}
    )
    assert any(issue["rule"] == "rendered_continuation_x" for issue in issues)


def test_role_list_paragraph_without_continuation_is_not_applicable():
    text = "・訪問者役：名乗る"
    block = {
        "text": text, "bbox": (106.0, 100.0, 260.0, 122.0), "origin_y": 120.0,
        "font_size": 22.0, "chars": [],
    }
    record = {
        "page": 1, "frame_id": "single-line-role", "role": "role-list",
        "geometry": {"frame_bbox_pt": [100.0, 90.0, 356.0, 80.0], "margins_pt": [6.0, 6.0],
                     "safe_width_pt": 300.0, "font_size_pt": 22.0},
        "plan": {"line_hashes": [layout_typography._line_hash(text)],
                 "line_paragraph_indices": [0], "paragraph_hanging_indents_pt": [90.0]},
    }
    assert layout_typography._plan_parity_issues(1, [block], [record], {}) == []


def test_exact_plan_render_hash_match_excludes_planned_break_boundary():
    texts = ["内容確認", "次工程"]
    blocks = [
        {"text": text, "bbox": (10.0, 10.0 + index * 20, 120.0, 22.0 + index * 20),
         "origin_y": 20.0 + index * 20, "font_size": 12.0, "chars": []}
        for index, text in enumerate(texts)
    ]
    record = {
        "page": 1, "frame_id": "planned-body", "role": "body",
        "geometry": {"frame_bbox_pt": [0.0, 0.0, 200.0, 100.0], "safe_width_pt": 150.0,
                     "font_size_pt": 12.0},
        "plan": {"line_hashes": [layout_typography._line_hash(text) for text in texts],
                 "line_paragraph_indices": [0, 0]},
    }
    assert layout_typography._plan_parity_issues(1, blocks, [record], {}) == []
    record["plan"]["line_hashes"][1] = "0" * 64
    issues = layout_typography._plan_parity_issues(1, blocks, [record], {})
    assert {item["rule"] for item in issues} == {"rendered_japanese_boundary", "planned_break_parity"}


def test_safe_wrap_exempt_role_skips_boundary_orphan_and_parity_only_for_code():
    blocks = [
        {"text": "Alpha", "bbox": (10.0, 10.0, 80.0, 22.0), "origin_y": 20.0, "font_size": 12.0, "chars": []},
        {"text": "x", "bbox": (10.0, 30.0, 20.0, 42.0), "origin_y": 40.0, "font_size": 12.0, "chars": []},
    ]
    base = {
        "page": 1, "frame_id": "code-fixture", "role": "code",
        "geometry": {"frame_bbox_pt": [0.0, 0.0, 200.0, 100.0], "safe_width_pt": 150.0,
                     "font_size_pt": 12.0},
        "plan": {"line_hashes": ["0" * 64], "line_paragraph_indices": [0, 0]},
    }
    assert layout_typography._plan_parity_issues(
        1, blocks, [base], {"safe_wrap_exempt_roles": ["code"]}
    ) == []
    body = dict(base, role="body")
    assert layout_typography._plan_parity_issues(
        1, blocks, [body], {"safe_wrap_exempt_roles": ["code"]}
    )


def _line_policy(mode="enforced", manifest=None):
    policy = {"enabled": True, "mode": mode,
        "measurement_basis": "rendered_baseline_to_baseline_over_font_size",
        "target_roles": ["body"], "target_region_ids": ["region-a"],
        "candidate_ratios": [1.25, 1.35, 1.5, 1.75, 2.0], "requested_max_ratio": 2.0,
        "selection": "manual_accepted_candidate", "preserve": list(common.LINE_SPACING_PRESERVE)}
    if manifest is not None:
        policy["evidence_manifest"] = {"path": str(manifest), "sha256": common.sha256(manifest)}
    return policy


def test_line_spacing_policy_validation_and_legacy_absence(tmp_path):
    pdf = tmp_path / "fixture.pdf"
    contract = {"artifacts": [str(pdf)], "artifact_quality": {"required": True, "layout_typography": {"required": True}}}
    assert common.validate_pdf_layout_contract(contract) == []
    contract["artifact_quality"]["layout_typography"]["line_spacing_policy"] = _line_policy()
    assert common.validate_pdf_layout_contract(contract) == []
    contract["artifact_quality"]["layout_typography"]["line_spacing_policy"]["candidate_ratios"] = [1.25, 2.01]
    assert "maximum" in common.validate_pdf_layout_contract(contract)[0]


def test_line_spacing_shadow_enforced_hash_and_boundary(tmp_path, monkeypatch):
    render = tmp_path / "candidate.png"; Image.new("RGB", (600, 360), "white").save(render)
    pdf = tmp_path / "fake-wrap.pdf"; pdf.write_bytes(b"%PDF-fake-fixture")
    base_candidate = {"region_id": "region-a", "role": "body", "page": 1, "bbox_pdf_points": [10, 10, 200, 40],
        "candidate_kind": "trial", "control_ratio": 1.2, "source_candidate_ratio": 1.5,
        "rendered_ratio_min": 1.49, "rendered_ratio_max": 1.51, "line_count": 3,
        "preservation_diff": {key + "_changed": False for key in common.LINE_SPACING_PRESERVE}, "defects": [],
        "render_evidence": {"path": str(render), "sha256": common.sha256(render), "page": 1, "width": 600, "height": 360,
                            "render_scale": 2.0, "bbox_pdf_points": [10, 10, 200, 40], "bbox_render_pixels": [20, 20, 400, 80]}}
    manifest = tmp_path / "line.json"
    manifest.write_text(json.dumps({"schema_version": "docs-line-spacing-candidates-1", "artifact_sha256": common.sha256(pdf),
                                    "candidates": [base_candidate]}), encoding="utf-8")
    class FakePage:
        rect = fitz.Rect(0, 0, 300, 180)
        def get_text(self, kind): return {"blocks": []}
    class FakeDoc:
        def __enter__(self): return [FakePage()]
        def __exit__(self, *args): return False
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: FakeDoc())
    shadow = layout_typography.scan_pdf(pdf, {"required": True, "line_spacing_policy": _line_policy("shadow", manifest)})
    assert shadow["status"] == "pass" and shadow["issues"] == [] and shadow["line_spacing_evidence"]["candidate_count"] == 1
    enforced = layout_typography.scan_pdf(pdf, {"required": True, "line_spacing_policy": _line_policy("enforced", manifest)})
    assert enforced["status"] == "review" and enforced["REVIEW_CANDIDATE"][0]["rule"] == "line_spacing_candidate"
    over = dict(base_candidate, source_candidate_ratio=2.01)
    manifest.write_text(json.dumps({"schema_version": "docs-line-spacing-candidates-1", "artifact_sha256": common.sha256(pdf), "candidates": [over]}), encoding="utf-8")
    blocked = layout_typography.scan_pdf(pdf, {"required": True, "line_spacing_policy": _line_policy("enforced", manifest)})
    assert "line_spacing_contract_limit_exceeded" in _rules(blocked) and blocked["status"] == "block"
    existing_over = dict(over, candidate_kind="control", control_ratio=2.01)
    manifest.write_text(json.dumps({"schema_version": "docs-line-spacing-candidates-1", "artifact_sha256": common.sha256(pdf), "candidates": [existing_over]}), encoding="utf-8")
    numeric_only = layout_typography.scan_pdf(pdf, {"required": True, "line_spacing_policy": _line_policy("enforced", manifest)})
    assert "line_spacing_contract_limit_exceeded" not in _rules(numeric_only)
    assert numeric_only["status"] == "review"


def test_line_spacing_review_accepts_exactly_one_candidate_per_region(tmp_path):
    checker = _load_checker()
    artifact = tmp_path / "artifact.pdf"; artifact.write_bytes(b"pdf")
    render = tmp_path / "page.png"; Image.new("RGB", (100, 100), "white").save(render)
    scan = tmp_path / "scan.json"; scan.write_text("{}", encoding="utf-8")
    candidates = [{"id": f"c-{i}", "rule": "line_spacing_candidate", "region_id_hash": "region-hash", "page": 1,
                   "bbox_pdf_points": [0, 0, 10, 10], "render_evidence": {"path": str(render), "sha256": common.sha256(render)}} for i in (1, 2)]
    def receipt(statuses):
        return {"schema_version": "docs-layout-visual-review-1", "status": "pass",
            "artifact_path": str(artifact.resolve()), "artifact_sha256": common.sha256(artifact),
            "scan_path": str(scan.resolve()), "scan_sha256": common.sha256(scan),
            "reviews": [{"issue_id": item["id"], "rule": item["rule"], "status": status,
                         "page": 1, "bbox_pdf_points": item["bbox_pdf_points"],
                         "render_path": str(render), "render_sha256": common.sha256(render)}
                        for item, status in zip(candidates, statuses)]}
    review = tmp_path / "review.json"; review.write_text(json.dumps(receipt(["accepted", "rejected"])), encoding="utf-8")
    assert checker._layout_review_passes(review, artifact, scan, candidates)
    review.write_text(json.dumps(receipt(["accepted", "accepted"])), encoding="utf-8")
    assert not checker._layout_review_passes(review, artifact, scan, candidates)
    review.write_text(json.dumps(receipt(["rejected", "rejected"])), encoding="utf-8")
    assert not checker._layout_review_passes(review, artifact, scan, candidates)
