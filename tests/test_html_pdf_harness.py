from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import fitz
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import common, html_pdf_harness, layout_typography, safe_wrap


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chrome() -> str:
    binary = shutil.which("google-chrome") or shutil.which("chromium")
    assert binary, "headless Chrome is required for the HTML-to-PDF E2E fixture"
    return binary


def _load_checker():
    spec = importlib.util.spec_from_file_location("html_pdf_contract_checker", ROOT / "check_docs_artifact_quality.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    return module


def _handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha(path)}


def _rules(suite: Path) -> dict:
    return {
        "AQ-CHANGE-01": {"approved_scope_ids": ["scope"], "preserved_field_ids": ["preserved"]},
        "AQ-IDENTITY-01": {"identities": []}, "AQ-ASSET-01": {"assets": []}, "AQ-AUTHORITY-01": {"owner_id": "owner"},
        "AQ-BOUNDARY-01": {"clipped": False, "overflow": False}, "AQ-RELATION-01": {"relations": []},
        "AQ-SCOPE-01": {"protected_region_ids": ["protected"], "baseline_sha256": "a" * 64},
        "AQ-VISIBLE-01": {"criterion_ids": ["v1", "v2", "v3", "v4", "v5"], "image_evidence_sha256": "b" * 64},
        "AQ-FIXTURE-01": {"suite": _handle(suite), "expected_rule_failures": ["none"]}, "AQ-LOCAL-GLOBAL-01": {"required": False},
    }


def _evidence(rule: str, contract: dict, artifact: Path) -> dict:
    spec = contract["rule_contracts"][rule]
    if rule == "AQ-CHANGE-01":
        checks = {"id": "change-contract", "observed_scope_ids": spec["approved_scope_ids"], "changed_preserved_field_ids": []}
    elif rule == "AQ-IDENTITY-01": checks = {"id": "identity-contract", "identities": []}
    elif rule == "AQ-ASSET-01": checks = {"id": "asset-contract", "assets": []}
    elif rule == "AQ-AUTHORITY-01": checks = {"id": "authority-contract", "observed_owner_ids": ["owner"], "duplicate_owner_ids": []}
    elif rule == "AQ-BOUNDARY-01": checks = {"id": "boundary-contract", "clipped": False, "overflow": False, "clipped_count": 0, "overflow_count": 0, "image_cut_count": 0}
    elif rule == "AQ-RELATION-01": checks = {"id": "relation-contract", "relations": []}
    elif rule == "AQ-SCOPE-01": checks = {"id": "scope-contract", "protected_region_ids": ["protected"], "baseline_sha256": "a" * 64}
    elif rule == "AQ-VISIBLE-01": checks = {"id": "visible-contract", "criterion_ids": ["v1", "v2", "v3", "v4", "v5"], "image_evidence_sha256": "b" * 64}
    elif rule == "AQ-FIXTURE-01": checks = {"id": "fixture-contract", "suite": spec["suite"], "observed_rule_failures": ["none"]}
    else: checks = {"id": "local-global-contract", "required": False}
    checks["status"] = "pass"
    return {"schema_version": "docs-artifact-quality-evidence-1", "provenance": {"kind": "checker", "id": "fixture"}, "rule_id": rule, "status": "pass", "review_status": "pass", "artifact_path": str(artifact.resolve()), "artifact_sha256": _sha(artifact), "checks": [checks]}


def _current_shape_contract(tmp_path: Path, source_pdf: Path, html_path: Path, plan_path: Path, final_pdf: Path) -> dict:
    return {"schema_version": "docs-artifact-quality-contract-1", "artifact_kind": "pdf_html", "artifacts": [str(final_pdf.resolve()), str(html_path.resolve())],
            "authoritative_source": {**_handle(source_pdf), "readback_mode": "original-pdf"}, "visible_criteria": [{"id": f"v{i}"} for i in range(1, 6)], "rule_contracts": _rules(Path(__file__)),
            "artifact_quality": {"required": True, "layout_typography": {"required": True, "expected_cjk_fonts": [html_pdf_harness.safe_wrap.resolve_cjk_font()["family"]], "wrap_plan_receipt": str(plan_path.resolve()), "wrap_plan_receipt_sha256": _sha(plan_path)},
                                 "html_pdf": {"required": True, "source_path": str(html_path.resolve()), "receipt_path": str(plan_path.resolve())}}}


def test_controlled_html_pdf_uses_canonical_plan_and_pdf_parity(tmp_path):
    html_path, plan_path, pdf_path = tmp_path / "controlled.html", tmp_path / "plan.json", tmp_path / "controlled.pdf"
    receipt = html_pdf_harness.write_controlled_html(
        "ご紹介していく。次のご案内です。", html_path, plan_path,
        frame_id="html-frame", page=1, frame_bbox_pt=(36, 36, 180, 120),
        margins_pt=(6, 6), font_size_pt=16, max_lines=4,
    )
    assert "generic" not in html_path.read_text(encoding="utf-8").lower()
    assert html_pdf_harness.validate_html_receipt(plan_path, html_path) == []
    completed = subprocess.run([
        _chrome(), "--headless", "--no-sandbox", "--disable-gpu", "--print-to-pdf=" + str(pdf_path), html_path.as_uri(),
    ], check=False, capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    bound = html_pdf_harness.bind_rendered_pdf(plan_path, html_path, pdf_path)
    assert bound["status"] == "pass" and html_pdf_harness.validate_html_receipt(plan_path, html_path, pdf_path) == []
    scan = layout_typography.scan_pdf(pdf_path, {
        "required": True, "expected_cjk_fonts": [receipt["font"]["family"]],
        "wrap_plan_receipt": str(plan_path), "wrap_plan_receipt_sha256": _sha(plan_path),
    })
    assert scan["status"] == "pass", scan["issues"]


def test_top_level_contract_binds_editable_html_while_original_pdf_remains_authoritative(tmp_path):
    checker = _load_checker()
    source_pdf, html_path, plan_path, final_pdf = tmp_path / "original.pdf", tmp_path / "editable.html", tmp_path / "plan.json", tmp_path / "final.pdf"
    source = fitz.open(); source.new_page().insert_text((36, 36), "Original authority"); source.save(source_pdf); source.close()
    html_pdf_harness.write_controlled_html(
        "ご紹介していく。次のご案内です。", html_path, plan_path,
        frame_id="html-frame", page=1, frame_bbox_pt=(36, 36, 180, 120), margins_pt=(6, 6), font_size_pt=16, max_lines=4,
    )
    completed = subprocess.run([_chrome(), "--headless", "--no-sandbox", "--disable-gpu", "--print-to-pdf=" + str(final_pdf), html_path.as_uri()], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    html_pdf_harness.bind_rendered_pdf(plan_path, html_path, final_pdf)
    contract = _current_shape_contract(tmp_path, source_pdf, html_path, plan_path, final_pdf)
    evidence = tmp_path / "evidence"; evidence.mkdir()
    for rule in common.CONTRACT_RULES:
        (evidence / f"{final_pdf.name}.{rule}.json").write_text(json.dumps(_evidence(rule, contract, final_pdf)), encoding="utf-8")
    contract_path = tmp_path / "contract.json"; contract_path.write_text(json.dumps(contract), encoding="utf-8")
    receipt, code = checker.build_receipt(contract_path, evidence_dir=evidence)
    html_rule = next(rule for rule in receipt["entries"][0]["rules"] if rule["id"] == "AQ-HTML-PDF-01")
    assert code == 0 and receipt["status"] == "pass" and html_rule["status"] == "pass", json.dumps(receipt, ensure_ascii=False, indent=2)

    for label, mutate in (
        ("missing-config", lambda value: value["artifact_quality"].pop("html_pdf")),
        ("missing-source", lambda value: value["artifact_quality"]["html_pdf"].pop("source_path")),
        ("missing-receipt", lambda value: value["artifact_quality"]["html_pdf"].__setitem__("receipt_path", str(tmp_path / "none.json"))),
    ):
        broken = json.loads(json.dumps(contract)); mutate(broken)
        broken_path = tmp_path / f"{label}.json"; broken_path.write_text(json.dumps(broken), encoding="utf-8")
        blocked, blocked_code = checker.build_receipt(broken_path, evidence_dir=evidence)
        assert blocked_code == 2 and blocked["status"] == "block"
        assert next(rule for rule in blocked["entries"][0]["rules"] if rule["id"] == "AQ-HTML-PDF-01")["status"] == "fail"

    html_path.write_text(html_path.read_text(encoding="utf-8") + "<!-- stale -->", encoding="utf-8")
    stale_path = tmp_path / "stale.json"; stale_path.write_text(json.dumps(contract), encoding="utf-8")
    blocked, blocked_code = checker.build_receipt(stale_path, evidence_dir=evidence)
    assert blocked_code == 2 and blocked["status"] == "block"
    assert next(rule for rule in blocked["entries"][0]["rules"] if rule["id"] == "AQ-HTML-PDF-01")["status"] == "fail"


def test_html_receipt_missing_mismatch_overflow_and_font_substitution_block(tmp_path):
    html_path, plan_path = tmp_path / "controlled.html", tmp_path / "plan.json"
    html_pdf_harness.write_controlled_html(
        "ご紹介していく。次のご案内です。", html_path, plan_path,
        frame_id="html-frame", page=1, frame_bbox_pt=(36, 36, 180, 120), margins_pt=(6, 6), font_size_pt=16, max_lines=4,
    )
    assert html_pdf_harness.validate_html_receipt(tmp_path / "missing.json", html_path) == ["HTML-PDF-RECEIPT-01"]
    broken = json.loads(plan_path.read_text(encoding="utf-8"))
    broken["plan"]["widths_pt"][0] = broken["geometry"]["safe_width_pt"] + 1
    plan_path.write_text(json.dumps(broken), encoding="utf-8")
    assert html_pdf_harness.validate_html_receipt(plan_path, html_path) == ["SAFE-WRAP-IMPLICIT-OVERFLOW-01"]
    broken["plan"]["widths_pt"][0] = 1
    broken["font"]["family"] = "Noto Sans CJK SC"
    plan_path.write_text(json.dumps(broken), encoding="utf-8")
    assert html_pdf_harness.validate_html_receipt(plan_path, html_path) == ["CJK-FONT-UNAVAILABLE-01"]


def test_pdf_parity_blocks_intended_japanese_line_mismatch(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"status": "pass", "page": 1, "frame_id": "html-frame", "geometry": {"frame_bbox_pt": [0, 0, 200, 100], "safe_width_pt": 180}, "plan": {"line_hashes": [hashlib.sha256("ご紹介して".encode()).hexdigest(), hashlib.sha256("いく".encode()).hexdigest()]}}), encoding="utf-8")
    pdf = tmp_path / "fixture.pdf"; pdf.write_bytes(b"%PDF-fake")
    class Page:
        rect = type("Rect", (), {"width": 300, "height": 180})()
        def get_text(self, kind):
            return {"blocks": [{"type": 0, "lines": [{"bbox": [0, 0, 100, 10], "spans": [{"chars": [{"c": c, "bbox": [i, 0, i + 1, 10]} for i, c in enumerate("ご紹介していく")], "font": "BIZUDPGothic", "size": 10, "origin": [0, 10], "flags": 0}]}]}]}
    class Doc:
        def __enter__(self): return [Page()]
        def __exit__(self, *args): return False
    monkeypatch.setattr(layout_typography.fitz, "open", lambda _: Doc())
    scan = layout_typography.scan_pdf(pdf, {"required": True, "expected_cjk_fonts": ["BIZUDPGothic"], "wrap_plan_receipt": str(plan), "wrap_plan_receipt_sha256": _sha(plan)})
    rules = {issue["rule"] for issue in scan["issues"]}
    assert "planned_break_parity" in rules


def test_real_html_pdf_glyph_boundaries_hard_fail_known_bad_and_pass_positive(tmp_path):
    negative_pairs = [
        ("形にす", "る"), ("引き継", "ぎ"), ("進めら", "れる"),
        ("よく使", "う"), ("Web操作ま", "で"),
    ]
    positive_pairs = [("確認項目：", "内容です。"), ("この資料は", "明日の会議で確認します")]
    selected = safe_wrap.resolve_cjk_font()
    font_path = Path(selected["path"])
    rows, frames = [], []
    for index, (left, right) in enumerate(negative_pairs + positive_pairs):
        top = 24 + index * 72
        rows.append('<div class="frame" style="top:%spt"><div>%s</div><div>%s</div></div>' %
                    (top, left, right))
        frames.append({
            "status": "pass", "frame_id": f"fixture-{index}", "page": 1, "role": "body",
            "geometry": {"frame_bbox_pt": [30, top, 280, 60], "safe_width_pt": 252,
                         "font_size_pt": 16, "margins_pt": [0, 0]},
            "plan": {"line_hashes": [hashlib.sha256(left.encode()).hexdigest(), hashlib.sha256(right.encode()).hexdigest()],
                     "line_paragraph_indices": [0, 0]},
            "font": selected,
        })
    html_path, pdf_path = tmp_path / "rendered-boundaries.html", tmp_path / "rendered-boundaries.pdf"
    html_path.write_text(
        "<!doctype html><meta charset=utf-8><style>@page{size:612pt 792pt;margin:0}"
        "@font-face{font-family:'FixtureJP';src:url('file://%s')}body{margin:0;font-family:'FixtureJP';font-size:16pt}"
        ".frame{position:absolute;left:30pt;width:280pt;height:60pt;line-height:22pt}.frame div{white-space:nowrap}</style>%s" %
        (font_path, "".join(rows)), encoding="utf-8")
    completed = subprocess.run([_chrome(), "--headless", "--no-sandbox", "--disable-gpu",
                                "--print-to-pdf=" + str(pdf_path), html_path.as_uri()],
                               capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    rendered = html_pdf_harness.scan_rendered_pdf_boundaries(pdf_path, frames)
    assert rendered["status"] == "block"
    assert all(item["status"] == "block" for item in rendered["frames"][:5])
    assert all(item["status"] == "pass" for item in rendered["frames"][5:])
    kinds = {item["boundary_check"]["boundaries"][0]["kind"] for item in rendered["frames"][:5]}
    reason_codes = {reason for item in rendered["frames"][:5] for reason in item["reason_codes"]}
    assert {"kanji-okurigana", "contiguous-hiragana", "stranded-particle"} <= kinds
    assert "SAFE-WRAP-VERB-AUX-SPLIT-01" in reason_codes


def test_multiframe_html_plan_binds_each_frame_to_source_and_rendered_pdf(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "frames.html", tmp_path / "frames.json", tmp_path / "frames.pdf"
    frames = [
        {"text": "確認項目：内容です。", "frame_id": "card-a", "page": 1,
         "frame_bbox_pt": (36, 36, 118, 70), "margins_pt": (6, 6), "font_size_pt": 16, "max_lines": 3},
        {"text": "この資料は明日の会議で確認します。", "frame_id": "card-b", "page": 1,
         "frame_bbox_pt": (36, 150, 168, 90), "margins_pt": (6, 6), "font_size_pt": 16, "max_lines": 3},
    ]
    planned = html_pdf_harness.write_controlled_html_frames(frames, html_path, receipt_path)
    assert planned["status"] == "planned" and len(planned["frames"]) == 2
    source = html_path.read_text(encoding="utf-8")
    assert source.count('class="safe-wrap-line"') >= 4 and "safe-wrap-frame-1" in source and "safe-wrap-frame-2" in source
    completed = subprocess.run([_chrome(), "--headless", "--no-sandbox", "--disable-gpu",
                                "--print-to-pdf=" + str(pdf_path), html_path.as_uri()],
                               capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    assert bound["status"] == "pass"
    assert bound["source_sha256"] == _sha(html_path) and bound["rendered_pdf_sha256"] == _sha(pdf_path)
    assert len(bound["rendered_boundary_check"]["frames"]) == 2
    schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-2.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(bound)) == []
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == []


def _render_html_pdf(html_path: Path, pdf_path: Path) -> None:
    completed = subprocess.run([
        _chrome(), "--headless", "--no-sandbox", "--disable-gpu",
        "--print-to-pdf=" + str(pdf_path), html_path.as_uri(),
    ], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    with fitz.open(pdf_path) as reopened:
        assert len(reopened) == 1


def _spacing_frames() -> list[dict]:
    return [
        {"text": "項目一です。", "frame_id": "list-a", "page": 1,
         "frame_bbox_pt": (36, 36, 220, 28), "margins_pt": (0, 0), "font_size_pt": 14, "max_lines": 2,
         "role": "bullet-list", "intra_frame_line_step_pt": 18,
         "spacing_group_id": "items", "spacing_index": 0, "inter_frame_item_step_pt": 20, "spacing_tolerance_pt": 1},
        {"text": "項目二です。", "frame_id": "list-b", "page": 1,
         "frame_bbox_pt": (36, 56, 220, 28), "margins_pt": (0, 0), "font_size_pt": 14, "max_lines": 2,
         "role": "bullet-list", "intra_frame_line_step_pt": 18,
         "spacing_group_id": "items", "spacing_index": 1, "inter_frame_item_step_pt": 20, "spacing_tolerance_pt": 1},
    ]


def test_spacing_v3_real_pdf_records_body_free_intra_inter_measurements(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "spacing.html", tmp_path / "spacing.json", tmp_path / "spacing.pdf"
    planned = html_pdf_harness.write_controlled_html_frames(_spacing_frames(), html_path, receipt_path)
    assert planned["schema_version"] == "docs-html-pdf-wrap-plan-3"
    assert planned["spacing_contract"] == {"mode": "enforced", "spacing_pass": None}
    _render_html_pdf(html_path, pdf_path)
    bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    assert bound["status"] == "pass" and bound["spacing_contract"]["spacing_pass"] is True
    checks = [check for frame in bound["rendered_boundary_check"]["frames"] for check in frame.get("spacing_checks", [])]
    assert {check["kind"] for check in checks} == {"intra_frame_line_step", "inter_frame_item_step"}
    assert all(check["spacing_pass"] is True for check in checks)
    inter = next(check for check in checks if check["kind"] == "inter_frame_item_step")
    assert inter["requested_pt"] == 20 and inter["applied_pt"] == 20 and 19 <= inter["effective_pt"] <= 21 and inter["tolerance_pt"] == 1
    serialized = json.dumps(bound, ensure_ascii=False)
    assert "項目一" not in serialized and "項目二" not in serialized and "explicit_lines" not in serialized and '"text"' not in serialized
    schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-3.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(bound)) == []
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == []


def test_spacing_list_item_requested_19_applied_21_passes_with_tolerance(tmp_path):
    frames = _spacing_frames()
    for frame in frames:
        frame["inter_frame_item_step_pt"] = 19
        frame["inter_frame_item_step_applied_pt"] = 21
        frame["spacing_tolerance_pt"] = 2
    frames[1]["frame_bbox_pt"] = (36, 57, 220, 28)
    html_path, receipt_path, pdf_path = tmp_path / "19-to-21.html", tmp_path / "19-to-21.json", tmp_path / "19-to-21.pdf"
    html_pdf_harness.write_controlled_html_frames(frames, html_path, receipt_path)
    _render_html_pdf(html_path, pdf_path)
    bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    inter = next(check for item in bound["rendered_boundary_check"]["frames"] for check in item.get("spacing_checks", [])
                 if check["kind"] == "inter_frame_item_step")
    assert bound["status"] == "pass" and inter["requested_pt"] == 19 and inter["applied_pt"] == 21
    assert 19 <= inter["effective_pt"] <= 21.01 and inter["tolerance_pt"] == 2 and inter["spacing_pass"] is True


def test_spacing_body_below_12pt_and_planned_inter_frame_drift_fail(tmp_path):
    frames = _spacing_frames()
    frames[0]["role"] = "body"; frames[0]["intra_frame_line_step_pt"] = 11.9
    with pytest.raises(ValueError, match="HTML-PDF-SPACING-MINIMUM-01"):
        html_pdf_harness.write_controlled_html_frames(frames, tmp_path / "low.html", tmp_path / "low.json")
    frames = _spacing_frames(); frames[1]["frame_bbox_pt"] = (36, 65, 220, 28)
    with pytest.raises(ValueError, match="HTML-PDF-SPACING-PLANNED-DRIFT-01"):
        html_pdf_harness.write_controlled_html_frames(frames, tmp_path / "drift.html", tmp_path / "drift.json")


def test_spacing_rendered_intra_frame_positive_and_nine_point_drift(tmp_path):
    frame = [{"text": "確認項目の内容を明日の会議で説明します。", "frame_id": "body-a", "page": 1,
              "frame_bbox_pt": (36, 36, 90, 120), "margins_pt": (0, 0), "font_size_pt": 14, "max_lines": 8,
              "role": "body", "intra_frame_line_step_pt": 18, "spacing_tolerance_pt": 1}]
    good_html, good_receipt, good_pdf = tmp_path / "intra-good.html", tmp_path / "intra-good.json", tmp_path / "intra-good.pdf"
    html_pdf_harness.write_controlled_html_frames(frame, good_html, good_receipt)
    _render_html_pdf(good_html, good_pdf)
    good = html_pdf_harness.bind_rendered_pdf(good_receipt, good_html, good_pdf)
    check = good["rendered_boundary_check"]["frames"][0]["spacing_checks"][0]
    assert check["kind"] == "intra_frame_line_step" and check["sample_count"] > 0 and check["spacing_pass"] is True
    assert 17 <= check["effective_pt"] <= 19

    bad_html, bad_receipt, bad_pdf = tmp_path / "intra-bad.html", tmp_path / "intra-bad.json", tmp_path / "intra-bad.pdf"
    html_pdf_harness.write_controlled_html_frames(frame, bad_html, bad_receipt)
    bad_html.write_text(bad_html.read_text(encoding="utf-8").replace("line-height:18.0pt", "line-height:27.0pt"), encoding="utf-8")
    data = json.loads(bad_receipt.read_text(encoding="utf-8")); data["source_sha256"] = _sha(bad_html)
    bad_receipt.write_text(json.dumps(data), encoding="utf-8")
    _render_html_pdf(bad_html, bad_pdf)
    bad = html_pdf_harness.bind_rendered_pdf(bad_receipt, bad_html, bad_pdf)
    assert bad["status"] == "block" and "HTML-PDF-SPACING-DRIFT-01" in bad["reason_codes"]


def test_spacing_rendered_nine_point_drift_and_missing_pass_hard_fail(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "drift.html", tmp_path / "drift.json", tmp_path / "drift.pdf"
    html_pdf_harness.write_controlled_html_frames(_spacing_frames(), html_path, receipt_path)
    source = html_path.read_text(encoding="utf-8").replace("top:56pt", "top:65pt")
    html_path.write_text(source, encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")); receipt["source_sha256"] = _sha(html_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    _render_html_pdf(html_path, pdf_path)
    blocked = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    assert blocked["status"] == "block" and blocked["spacing_contract"]["spacing_pass"] is False
    assert "HTML-PDF-SPACING-DRIFT-01" in blocked["reason_codes"]
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == ["HTML-PDF-SPACING-DRIFT-01"]

    good_html, good_receipt, good_pdf = tmp_path / "good.html", tmp_path / "good.json", tmp_path / "good.pdf"
    html_pdf_harness.write_controlled_html_frames(_spacing_frames(), good_html, good_receipt)
    _render_html_pdf(good_html, good_pdf); html_pdf_harness.bind_rendered_pdf(good_receipt, good_html, good_pdf)
    missing = json.loads(good_receipt.read_text(encoding="utf-8")); missing["spacing_contract"].pop("spacing_pass")
    good_receipt.write_text(json.dumps(missing), encoding="utf-8")
    assert html_pdf_harness.validate_html_receipt(good_receipt, good_html, good_pdf) == ["HTML-PDF-SPACING-EVIDENCE-01"]


def test_spacing_v3_source_and_pdf_identity_and_legacy_v2_compatibility(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "v3.html", tmp_path / "v3.json", tmp_path / "v3.pdf"
    html_pdf_harness.write_controlled_html_frames(_spacing_frames(), html_path, receipt_path)
    _render_html_pdf(html_path, pdf_path); html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    other = tmp_path / "other.html"; other.write_text(html_path.read_text(encoding="utf-8") + "<!--other-run-->", encoding="utf-8")
    assert html_pdf_harness.validate_html_receipt(receipt_path, other, pdf_path) == ["HTML-PDF-SOURCE-01"]
    other_pdf = tmp_path / "other.pdf"; other_pdf.write_bytes(pdf_path.read_bytes() + b"\n")
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, other_pdf) == ["HTML-PDF-RENDERED-PDF-01"]

    legacy_html, legacy_receipt = tmp_path / "v2.html", tmp_path / "v2.json"
    legacy = html_pdf_harness.write_controlled_html_frames([
        {"text": "従来形式です。", "frame_id": "legacy", "page": 1, "frame_bbox_pt": (36, 36, 180, 60),
         "margins_pt": (6, 6), "font_size_pt": 14, "max_lines": 2}
    ], legacy_html, legacy_receipt)
    assert legacy["schema_version"] == "docs-html-pdf-wrap-plan-2"
    assert "spacing_contract" not in legacy and html_pdf_harness.validate_html_receipt(legacy_receipt, legacy_html) == []


def test_spacing_shadow_mode_is_explicit_and_does_not_enforce_drift(tmp_path):
    frames = _spacing_frames()
    for frame in frames:
        frame["spacing_mode"] = "shadow"
    html_path, receipt_path, pdf_path = tmp_path / "shadow.html", tmp_path / "shadow.json", tmp_path / "shadow.pdf"
    planned = html_pdf_harness.write_controlled_html_frames(frames, html_path, receipt_path)
    assert planned["spacing_contract"]["mode"] == "shadow"
    html_path.write_text(html_path.read_text(encoding="utf-8").replace("top:56pt", "top:65pt"), encoding="utf-8")
    data = json.loads(receipt_path.read_text(encoding="utf-8")); data["source_sha256"] = _sha(html_path)
    receipt_path.write_text(json.dumps(data), encoding="utf-8")
    _render_html_pdf(html_path, pdf_path)
    bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    assert bound["status"] == "pass" and bound["spacing_contract"] == {"mode": "shadow", "spacing_pass": False}
    schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-3.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(bound)) == []
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == []


def test_layout_typography_hard_fails_v3_spacing_pass_drift(tmp_path):
    html_path, receipt_path, pdf_path = tmp_path / "layout.html", tmp_path / "layout.json", tmp_path / "layout.pdf"
    bound = html_pdf_harness.write_controlled_html_frames(_spacing_frames(), html_path, receipt_path)
    _render_html_pdf(html_path, pdf_path); bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    bound["spacing_contract"]["spacing_pass"] = False
    receipt_path.write_text(json.dumps(bound), encoding="utf-8")
    scan = layout_typography.scan_pdf(pdf_path, {
        "required": True, "expected_cjk_fonts": [bound["frames"][0]["font"]["family"]],
        "wrap_plan_receipt": str(receipt_path), "wrap_plan_receipt_sha256": _sha(receipt_path),
    })
    assert scan["status"] == "block"
    assert any(issue["rule"] == "html_pdf_spacing_contract" and issue["bucket"] == "HARD_FAIL" for issue in scan["issues"])


def _layout_relation_fixture() -> dict:
    return json.loads((ROOT / "tests/fixtures/html-pdf-layout-relations.json").read_text(encoding="utf-8"))


def test_v4_centered_single_line_and_separated_surface_pass_real_pdf_and_schema(tmp_path):
    fixture = _layout_relation_fixture()["positive"]
    centered, separated = fixture["centered"], fixture["separated"]
    html_path, receipt_path, pdf_path = tmp_path / "layout.html", tmp_path / "layout.json", tmp_path / "layout.pdf"
    planned = html_pdf_harness.write_controlled_html_frames([{
        "text": "状態", "frame_id": "generic-badge", "page": 1,
        "frame_bbox_pt": tuple(centered["frame_bbox_pt"]), "margins_pt": (0, 0),
        "font_size_pt": 14, "max_lines": 1, "role": centered["role"],
        "single_line": centered["single_line"], "padding_pt": centered["padding_pt"],
        "alignment_tolerance_pt": centered["alignment_tolerance_pt"],
        "relations": [{"target_id": "generic-surface", **separated}],
    }], html_path, receipt_path)
    assert planned["schema_version"] == "docs-html-pdf-wrap-plan-4"
    assert planned["layout_contract"] == {"mode": "enforced", "relation_sink": "AQ-RELATION-01",
                                           "alignment_pass": None, "relation_pass": None}
    assert planned["frames"][0]["alignment"]["vertical_align"] == "center"
    assert planned["frames"][0]["alignment"]["horizontal_align"] == "center"
    _render_html_pdf(html_path, pdf_path)
    bound = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    assert bound["status"] == "pass" and bound["layout_contract"]["alignment_pass"] is True
    assert bound["layout_contract"]["relation_pass"] is True
    rendered_frame = bound["rendered_boundary_check"]["frames"][0]
    alignment = rendered_frame["alignment_check"]
    relation = rendered_frame["relation_checks"][0]
    assert abs(alignment["horizontal_center_delta_pt"]) <= centered["alignment_tolerance_pt"]
    assert abs(alignment["vertical_center_delta_pt"]) <= centered["alignment_tolerance_pt"]
    assert relation["effective_gap_pt"] >= separated["min_gap_pt"] and relation["overlap_area_pt2"] == 0
    serialized = json.dumps(bound, ensure_ascii=False)
    assert "状態" not in serialized and "explicit_lines" not in serialized and '"text"' not in serialized
    schema = json.loads((ROOT / "schemas/docs-html-pdf-wrap-plan-4.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(bound)) == []
    assert html_pdf_harness.validate_html_receipt(receipt_path, html_path, pdf_path) == []
    scan = layout_typography.scan_pdf(pdf_path, {
        "required": True, "expected_cjk_fonts": [bound["frames"][0]["font"]["family"]],
        "wrap_plan_receipt": str(receipt_path), "wrap_plan_receipt_sha256": _sha(receipt_path),
    })
    assert scan["status"] == "pass" and scan["html_pdf_alignment"][0]["status"] == "pass"
    assert scan["content_block_relations"][0]["sink"] == "AQ-RELATION-01"


def test_v4_real_pdf_one_point_overlap_blocks_relation(tmp_path):
    fixture = _layout_relation_fixture()
    centered = fixture["positive"]["centered"]
    probe_html, probe_receipt, probe_pdf = tmp_path / "probe.html", tmp_path / "probe.json", tmp_path / "probe.pdf"
    base = {"text": "指標", "frame_id": "generic-metric", "page": 1,
            "frame_bbox_pt": tuple(centered["frame_bbox_pt"]), "margins_pt": (0, 0),
            "font_size_pt": 14, "max_lines": 1, "role": "metric", "single_line": True,
            "padding_pt": centered["padding_pt"], "alignment_tolerance_pt": centered["alignment_tolerance_pt"]}
    html_pdf_harness.write_controlled_html_frames([base], probe_html, probe_receipt)
    _render_html_pdf(probe_html, probe_pdf)
    probe = html_pdf_harness.bind_rendered_pdf(probe_receipt, probe_html, probe_pdf)
    glyph = probe["rendered_boundary_check"]["frames"][0]["alignment_check"]["glyph_bbox_pdf_points"]
    overlap_pt = fixture["negative"]["overlap"]["overlap_pt"]
    target = [glyph[0], glyph[3] - overlap_pt, max(12, glyph[2] - glyph[0]), 20]
    html_path, receipt_path, pdf_path = tmp_path / "overlap.html", tmp_path / "overlap.json", tmp_path / "overlap.pdf"
    relation = {key: value for key, value in fixture["negative"]["overlap"].items() if key != "overlap_pt"}
    html_pdf_harness.write_controlled_html_frames([{**base, "relations": [{
        "target_id": "overlap-surface", "target_bbox_pt": target, **relation,
    }]}], html_path, receipt_path)
    _render_html_pdf(html_path, pdf_path)
    blocked = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    check = blocked["rendered_boundary_check"]["frames"][0]["relation_checks"][0]
    assert blocked["status"] == "block" and "HTML-PDF-RELATION-DRIFT-01" in blocked["reason_codes"]
    assert check["relation_pass"] is False and check["overlap_area_pt2"] > 0 and check["effective_gap_pt"] < 0


def test_v4_real_pdf_top_aligned_padding_blocks_center_contract(tmp_path):
    fixture = _layout_relation_fixture()
    centered, mutation = fixture["positive"]["centered"], fixture["negative"]["top_aligned"]
    html_path, receipt_path, pdf_path = tmp_path / "top.html", tmp_path / "top.json", tmp_path / "top.pdf"
    html_pdf_harness.write_controlled_html_frames([{
        "text": "操作", "frame_id": "generic-button", "page": 1,
        "frame_bbox_pt": tuple(centered["frame_bbox_pt"]), "margins_pt": (0, 0),
        "font_size_pt": 14, "max_lines": 1, "role": "button", "single_line": True,
        "padding_pt": centered["padding_pt"], "alignment_tolerance_pt": centered["alignment_tolerance_pt"],
    }], html_path, receipt_path)
    html_path.write_text(html_path.read_text(encoding="utf-8").replace(mutation["css_from"], mutation["css_to"]), encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")); receipt["source_sha256"] = _sha(html_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    _render_html_pdf(html_path, pdf_path)
    blocked = html_pdf_harness.bind_rendered_pdf(receipt_path, html_path, pdf_path)
    check = blocked["rendered_boundary_check"]["frames"][0]["alignment_check"]
    assert blocked["status"] == "block" and "HTML-PDF-ALIGNMENT-DRIFT-01" in blocked["reason_codes"]
    assert check["alignment_pass"] is False and abs(check["vertical_center_delta_pt"]) > check["tolerance_pt"]


def test_v4_invalid_layout_declarations_fail_without_changing_legacy_defaults(tmp_path):
    legacy_html, legacy_receipt = tmp_path / "legacy.html", tmp_path / "legacy.json"
    legacy = html_pdf_harness.write_controlled_html_frames([{
        "text": "従来", "frame_id": "legacy", "page": 1, "frame_bbox_pt": (36, 36, 120, 40),
        "margins_pt": (4, 4), "font_size_pt": 14, "max_lines": 1,
    }], legacy_html, legacy_receipt)
    assert legacy["schema_version"] == "docs-html-pdf-wrap-plan-2" and "layout_contract" not in legacy
    with pytest.raises(ValueError, match="HTML-PDF-ALIGNMENT-CONTRACT-01"):
        html_pdf_harness.plan_html_frame(
            "不正", frame_id="bad", page=1, frame_bbox_pt=(0, 0, 100, 30), margins_pt=(0, 0),
            font_size_pt=12, max_lines=1, role="badge", single_line=True, padding_pt=(20, 60, 20, 60),
        )
    with pytest.raises(ValueError, match="HTML-PDF-RELATION-CONTRACT-01"):
        html_pdf_harness.plan_html_frame(
            "不正", frame_id="bad", page=1, frame_bbox_pt=(0, 0, 100, 30), margins_pt=(0, 0),
            font_size_pt=12, max_lines=1, relations=[{"target_id": "x", "target_kind": "shape",
            "target_bbox_pt": (0, 0, 10, 10), "relation": "touching", "min_gap_pt": 0}],
        )
