"""Editable source companions: ``artifact_quality.source_companions``.

A deck delivered as ``deck.pptx`` + ``deck.pdf`` is measured on the PDF only.
The declared PPTX stays a delivered artifact pinned by hash (exact
declared==delivered match, finalization manifest, receiver copy, final-output
receipt) but is not an artifact-quality target, so the checker and the
completion guard share the same reduced target set.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from jsonschema import Draft202012Validator
from pptx import Presentation
from pptx.util import Pt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import common  # noqa: E402

PRODUCER = ("t_fixture-producer", 7)
RECEIVER = ("t_fixture-owner", 8)
NO_TEXT_REASON = "Synthetic deck fixture has no applicable source text rewrite."
COMPANION_LABEL = "AQ-SCOPE-01: artifact_quality.source_companions"
ALLOWED_SUFFIXES = ".docx, .htm, .html, .odp, .ods, .odt, .pptx, .xlsx"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _companion(pptx: Path, pdf: Path, **overrides) -> dict:
    value = {"path": str(pptx), "source_of": str(pdf), **overrides}
    if "sha256" not in value:
        value["sha256"] = _sha(pptx)
    return value


def _bundle(tmp_path: Path, *, declare_companion: bool = True) -> dict:
    """Strict pptx+pdf delivery: real files, manifest, receiver copies, contract."""
    checker = _load(ROOT / "check_docs_artifact_quality.py", "source_companion_checker")
    baseline = _load(ROOT / "tests/test_composition_receipt_e2e.py", "source_companion_baseline")
    tmp_path = tmp_path.resolve()
    pptx = tmp_path / "deck.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Pt(30), Pt(30), Pt(200), Pt(24)).text_frame.text = "Structure fixture"
    deck.save(pptx)
    pdf = tmp_path / "deck.pdf"
    document = pymupdf.open()
    document.new_page(width=300, height=180).insert_text((35, 70), "Structure fixture", fontsize=14)
    document.save(pdf)
    document.close()
    source = tmp_path / "source.txt"
    source.write_text("fixture source", encoding="utf-8")
    plan = tmp_path / "wrap-plan.jsonl"
    plan.write_text("{}\n", encoding="utf-8")

    manifest_path = tmp_path / "finalization.json"
    manifest = checker.finalization.build_manifest(
        [str(pptx), str(pdf)], source=str(source),
        checker=str(ROOT / "check_docs_artifact_quality.py"),
        created_by_task_id=PRODUCER[0], created_by_run_id=PRODUCER[1], state="accepted",
    )
    _write_json(manifest_path, manifest)
    received = tmp_path / "received"
    received.mkdir()
    copies = []
    for item in (pptx, pdf):
        target = received / item.name
        shutil.copyfile(item, target)
        copies.append({
            "canonical_path": str(item), "copy_path": str(target), "sha256": _sha(target),
            "size_bytes": target.stat().st_size, "media_type": checker.finalization.media_type(target),
        })
    receiver_path = _write_json(tmp_path / "receiver.json", {
        "status": "accepted", "artifact_set_id": manifest["artifact_set_id"],
        "receiver_task_id": RECEIVER[0], "receiver_run_id": RECEIVER[1], "copies": copies,
    })

    # Reuse the complete baseline rule declarations/evidence of the existing
    # end-to-end fixture so unrelated rules cannot mask the companion behavior.
    base = baseline._contract(tmp_path, source, pdf)
    contract = {
        "schema_version": "docs-artifact-quality-contract-1",
        "artifact_kind": "pdf",
        "artifacts": [str(pptx), str(pdf)],
        "authoritative_source": {**base["authoritative_source"], "readback_mode": "text"},
        "visible_criteria": base["visible_criteria"],
        "rule_contracts": base["rule_contracts"],
        "finalization_manifest": {"path": str(manifest_path), "receiver_receipt": str(receiver_path)},
        "artifact_quality": {
            "required": True,
            "layout_typography": {
                "required": True, "measurement_basis": "pdf_points", "minimum_gap_pt": 0,
                "dead_space": {"bottom_blank_ratio_max": 1}, "narrow_card_exemption_ratio": 0,
                # Explicit provider permission; the transport itself is mocked.
                "allow_live_provider": True,
                "cjk_contract": {"required": True},
                "wrap_plan_receipt": str(plan), "wrap_plan_receipt_sha256": _sha(plan),
                # The strict contract needs a non-empty region list. This one-page
                # fixture is English-only, so the region names a page it does not
                # have: no CJK glyph candidate is produced and the PDF goes through
                # the zero-candidate live-Jev route (transport mocked below). The
                # strict CJK production path itself is outside this feature.
                "cjk_appearance_regions": [{"page": 2, "bbox_pdf_points": [0, 0, 20, 20]}],
            },
        },
    }
    if declare_companion:
        contract["artifact_quality"]["source_companions"] = [_companion(pptx, pdf)]
    return {"checker": checker, "baseline": baseline, "tmp": tmp_path,
            "pptx": pptx, "pdf": pdf, "contract": contract}


def _run_checker(bundle: dict, monkeypatch, contract: dict | None = None) -> tuple[dict, int, Path, Path]:
    checker, tmp, pdf = bundle["checker"], bundle["tmp"], bundle["pdf"]
    contract = bundle["contract"] if contract is None else contract
    contract_path = _write_json(tmp / "contract.json", contract)
    evidence = tmp / "evidence"
    evidence.mkdir(exist_ok=True)
    for rule in common.CONTRACT_RULES:
        _write_json(evidence / f"{pdf.name}.{rule}.json", bundle["baseline"]._check(rule, contract, pdf))
    calls: list[dict] = []

    def transport(endpoint, key_env, model, payload, timeout):  # no network
        calls.append(payload)
        return {"answers": {"artifact_q0": {"choice": "pass", "confidence": 0.95}}}

    monkeypatch.setattr(checker.jev_post_render.jev_overlap, "_post_provider", transport)
    monkeypatch.setenv(checker.jev_post_render.jev_overlap.PRIMARY_API_KEY_ENV, "test-only-no-network")
    receipt, code = checker.build_receipt(contract_path, evidence_dir=evidence)
    receipt_path = tmp / "aq-receipt.json"
    checker._write_receipt(receipt_path, receipt)
    return receipt, code, contract_path, receipt_path


def _final_output_receipt(artifact: Path) -> str:
    receipt = artifact.parent / f"final-output-{artifact.name}.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "check_docs_artifact.py"), "--artifact", str(artifact),
         "--no-applicable-reason", NO_TEXT_REASON, "--receipt", str(receipt)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return str(receipt)


def _complete(bundle: dict, contract_path: Path, aq_receipt: Path, *, final_receipts: list[str] | None = None):
    guard = _load(ROOT / "__init__.py", "source_companion_guard")
    pptx, pdf = bundle["pptx"], bundle["pdf"]
    args = {
        "artifacts": [str(pptx), str(pdf)],
        "metadata": {"verification": {
            "final_output_filter": {"receipts": final_receipts or [
                _final_output_receipt(pptx), _final_output_receipt(pdf),
            ]},
            "artifact_quality": {"contract": str(contract_path), "receipts": [str(aq_receipt)]},
        }},
    }
    return guard._pre_tool_call(tool_name="kanban_complete", args=args, task_id=RECEIVER[0])


def _entry(receipt: dict, artifact: Path) -> dict | None:
    return next((item for item in receipt["entries"] if item["artifact"]["path"] == str(artifact)), None)


def _rule_status(entry: dict, rule_id: str) -> str | None:
    return next((rule.get("status") for rule in entry["rules"] if rule.get("id") == rule_id), None)


# --- (a) valid companion: real checker + real completion hook --------------------------------


def test_pptx_pdf_with_valid_companion_passes_real_completion_hook(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    assert common.validate_contract(bundle["contract"]) == []
    receipt, code, contract_path, receipt_path = _run_checker(bundle, monkeypatch)
    assert code == 0 and receipt["status"] == "pass"
    # Only the PDF is measured; the companion gets no quality entry.
    assert [item["artifact"]["path"] for item in receipt["entries"]] == [str(bundle["pdf"])]
    assert _rule_status(receipt["entries"][0], "AQ-LAYOUT-01") == "pass"
    assert _complete(bundle, contract_path, receipt_path) is None


def test_companion_stays_hash_pinned_after_valid_completion(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    _, code, contract_path, receipt_path = _run_checker(bundle, monkeypatch)
    assert code == 0
    pptx, pdf = bundle["pptx"], bundle["pdf"]
    pdf_receipt = _final_output_receipt(pdf)
    stale_pptx_receipt = _final_output_receipt(pptx)
    pptx.write_bytes(pptx.read_bytes() + b"drift")
    # Final-output receipt pins the companion bytes ...
    result = _complete(bundle, contract_path, receipt_path, final_receipts=[stale_pptx_receipt, pdf_receipt])
    assert result["action"] == "block" and "artifact changed after receipt" in result["message"]
    # ... and so does the finalization manifest behind the artifact-quality receipt.
    result = _complete(bundle, contract_path, receipt_path,
                       final_receipts=[_final_output_receipt(pptx), pdf_receipt])
    assert result == {"action": "block", "message": (
        "docs artifact-quality guard: docs artifact-quality guard: "
        "finalization manifest artifact hash/size/media-type drift")}


# --- (b) without a companion declaration the pair still blocks --------------------------------


def test_companion_omitted_from_contract_artifacts_blocks_exact_match(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, declare_companion=False)
    contract = bundle["contract"]
    contract["artifacts"] = [str(bundle["pdf"])]
    contract_path = _write_json(bundle["tmp"] / "contract.json", contract)
    result = _complete(bundle, contract_path, bundle["tmp"] / "unused-aq-receipt.json")
    assert result == {"action": "block", "message": (
        "docs artifact-quality guard: contract artifacts must exactly match delivered artifacts")}


def test_declared_pptx_without_companion_stays_a_target_and_blocks(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, declare_companion=False)
    contract = bundle["contract"]
    assert [str(path) for path in common.target_artifacts(contract)] == [str(bundle["pptx"]), str(bundle["pdf"])]
    receipt, code, contract_path, receipt_path = _run_checker(bundle, monkeypatch)
    assert code == 2 and receipt["status"] == "block"
    pptx_entry = _entry(receipt, bundle["pptx"])
    assert pptx_entry is not None and pptx_entry["status"] == "block"
    assert pptx_entry["actual_delivery_readback"] == {}
    assert _rule_status(pptx_entry, "AQ-DELIVERY-01") == "fail"
    result = _complete(bundle, contract_path, receipt_path)
    assert result == {"action": "block", "message": (
        "docs artifact-quality guard: artifact-quality receipt schema/status did not pass")}


# --- (c)-(e) invalid declarations make the contract invalid and fail closed -------------------


def _invalid_cases(pptx: Path, pdf: Path, tmp: Path) -> dict[str, tuple[object, str]]:
    stray_pdf = tmp / "stray.pdf"
    stray_pdf.write_bytes(pdf.read_bytes())
    return {
        "sha_mismatch": ([_companion(pptx, pdf, sha256=_sha(pdf))],
                         f"{COMPANION_LABEL}[0].sha256 does not match the current companion file"),
        "sha_malformed": ([_companion(pptx, pdf, sha256="A" * 64)],
                          f"{COMPANION_LABEL}[0].sha256 must be a lowercase hex SHA-256"),
        "source_of_self": ([_companion(pptx, pptx)],
                           f"{COMPANION_LABEL}[0].source_of must name a different declared .pdf artifact"),
        "source_of_undeclared_pdf": ([_companion(pptx, stray_pdf)],
                                     f"{COMPANION_LABEL}[0].source_of must name a different declared .pdf artifact"),
        "source_of_relative": ([_companion(pptx, pdf, source_of="deck.pdf")],
                               f"{COMPANION_LABEL}[0].source_of must name a different declared .pdf artifact"),
        "path_undeclared": ([_companion(tmp / "other.pptx", pdf, sha256="0" * 64)],
                            f"{COMPANION_LABEL}[0].path is not a declared artifact"),
        "path_relative": ([_companion(pptx, pdf, path="deck.pptx")],
                          f"{COMPANION_LABEL}[0].path must be an absolute path"),
        "path_is_pdf": ([_companion(pdf, pdf)],
                        f"{COMPANION_LABEL}[0].path must be an editable source type ({ALLOWED_SUFFIXES})"),
        "duplicate": ([_companion(pptx, pdf), _companion(pptx, pdf)],
                      f"{COMPANION_LABEL}[1].path duplicates another source companion"),
        "missing_key": ([{"path": str(pptx), "sha256": _sha(pptx)}],
                        f"{COMPANION_LABEL}[0] must contain exactly path, sha256, and source_of"),
        "extra_key": ([{**_companion(pptx, pdf), "role": "source"}],
                      f"{COMPANION_LABEL}[0] must contain exactly path, sha256, and source_of"),
        "not_a_list": (_companion(pptx, pdf), f"{COMPANION_LABEL} must be a list of objects"),
        "list_of_strings": ([str(pptx)], f"{COMPANION_LABEL} must be a list of objects"),
    }


@pytest.mark.parametrize("case", [
    "sha_mismatch", "sha_malformed", "source_of_self", "source_of_undeclared_pdf", "source_of_relative",
    "path_undeclared", "path_relative", "path_is_pdf", "duplicate", "missing_key", "extra_key",
    "not_a_list", "list_of_strings",
])
def test_invalid_companion_declaration_is_contract_invalid_and_excludes_nothing(tmp_path, case):
    bundle = _bundle(tmp_path)
    declaration, message = _invalid_cases(bundle["pptx"], bundle["pdf"], bundle["tmp"])[case]
    contract = bundle["contract"]
    contract["artifact_quality"]["source_companions"] = declaration
    errors = common.validate_contract(contract)
    assert message in errors
    assert all(error.startswith(COMPANION_LABEL) for error in errors), errors
    assert common.source_companion_paths(contract) == set()
    assert str(bundle["pptx"]) in {str(path) for path in common.target_artifacts(contract)}


@pytest.mark.parametrize("suffix", [".txt", ".png", ".svg", ".json"])
def test_companion_with_non_editable_suffix_blocks(tmp_path, suffix):
    bundle = _bundle(tmp_path)
    other = bundle["tmp"] / f"notes{suffix}"
    other.write_bytes(b"companion fixture")
    contract = bundle["contract"]
    contract["artifacts"].append(str(other))
    contract["artifact_quality"]["source_companions"] = [_companion(other, bundle["pdf"])]
    assert common.validate_contract(contract) == [
        f"{COMPANION_LABEL}[0].path must be an editable source type ({ALLOWED_SUFFIXES})"]
    assert common.source_companion_paths(contract) == set()


def test_source_of_must_be_a_declared_pdf_not_another_type(tmp_path):
    bundle = _bundle(tmp_path)
    png = bundle["tmp"] / "deck.png"
    png.write_bytes(b"png fixture")
    contract = bundle["contract"]
    contract["artifacts"].append(str(png))
    contract["artifact_quality"]["source_companions"] = [_companion(bundle["pptx"], png)]
    assert common.validate_contract(contract) == [
        f"{COMPANION_LABEL}[0].source_of must name a different declared .pdf artifact"]


def test_companion_also_listed_as_explicit_rule_target_blocks(tmp_path):
    bundle = _bundle(tmp_path)
    contract = bundle["contract"]
    contract["artifact_quality"]["rule_targets"] = [str(bundle["pptx"])]
    assert common.validate_contract(contract) == [
        f"{COMPANION_LABEL}[0].path is also listed in artifact_quality.rule_targets"]


@pytest.mark.parametrize("case", ["sha_mismatch", "source_of_undeclared_pdf"])
def test_invalid_companion_blocks_checker_and_real_completion_hook(tmp_path, monkeypatch, case):
    bundle = _bundle(tmp_path)
    declaration, _ = _invalid_cases(bundle["pptx"], bundle["pdf"], bundle["tmp"])[case]
    bundle["contract"]["artifact_quality"]["source_companions"] = declaration
    receipt, code, contract_path, receipt_path = _run_checker(bundle, monkeypatch)
    assert code == 2 and receipt["status"] == "block"
    # Fail closed: the companion is measured again and the contract error is surfaced.
    assert _entry(receipt, bundle["pptx"]) is not None
    assert _rule_status(_entry(receipt, bundle["pdf"]), "AQ-SCOPE-01") == "fail"
    result = _complete(bundle, contract_path, receipt_path)
    assert result == {"action": "block", "message": (
        "docs artifact-quality guard: artifact-quality receipt schema/status did not pass")}


# --- (f) target selection ---------------------------------------------------------------------


def test_target_artifacts_excludes_only_a_valid_companion(tmp_path):
    bundle = _bundle(tmp_path)
    contract = bundle["contract"]
    assert common.target_artifacts(contract) == [bundle["pdf"]]
    assert common.source_companion_paths(contract) == {str(bundle["pptx"])}
    absent = copy.deepcopy(contract)
    del absent["artifact_quality"]["source_companions"]
    assert common.validate_source_companions(absent) == []
    assert common.target_artifacts(absent) == [bundle["pptx"], bundle["pdf"]]
    empty = copy.deepcopy(contract)
    empty["artifact_quality"]["source_companions"] = []
    assert common.validate_contract(empty) == []
    assert common.target_artifacts(empty) == [bundle["pptx"], bundle["pdf"]]


def test_html_companion_matches_checker_html_pdf_pair_filter(tmp_path):
    html = tmp_path / "deck.html"
    pdf = tmp_path / "deck.pdf"
    html.write_text("<p>fixture</p>", encoding="utf-8")
    pdf.write_bytes(b"%PDF-fixture")
    contract = {"artifacts": [str(html), str(pdf)], "artifact_quality": {"required": True}}
    assert common.target_artifacts(contract) == [html, pdf]  # unchanged without a declaration
    contract["artifact_quality"]["source_companions"] = [_companion(html, pdf)]
    assert common.validate_source_companions(contract) == []
    assert common.target_artifacts(contract) == [pdf]


def test_contract_schema_accepts_companion_shape_and_rejects_extra_fields(tmp_path):
    schema = json.loads((ROOT / "schemas/docs-artifact-quality-contract-1.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    field = {"$defs": schema["$defs"], **schema["properties"]["artifact_quality"]["properties"]["source_companions"]}
    validator = Draft202012Validator(field)
    good = {"path": "/abs/deck.pptx", "sha256": "a" * 64, "source_of": "/abs/deck.pdf"}
    assert list(validator.iter_errors([good])) == []
    assert list(validator.iter_errors([{**good, "role": "source"}]))
    assert list(validator.iter_errors([{**good, "sha256": "short"}]))
    assert list(validator.iter_errors([{k: v for k, v in good.items() if k != "source_of"}]))
