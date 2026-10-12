from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, RefResolver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules.common import CONTRACT_RULES, validate_contract

TEMPLATE_PATH = ROOT / "templates/artifact-quality-local-revision.json"
SCHEMA_PATH = ROOT / "schemas/docs-artifact-quality-contract-1.json"


def _filled_fixture(tmp_path: Path) -> dict:
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    source_path = tmp_path / "source.txt"
    source_path.write_text("inert source fixture", encoding="utf-8")
    suite_path = tmp_path / "fixture-suite.txt"
    suite_path.write_text("inert suite fixture", encoding="utf-8")
    baseline_path = tmp_path / "baseline.txt"
    baseline_path.write_text("inert baseline fixture", encoding="utf-8")
    visible_evidence_path = tmp_path / "visible-check-evidence.txt"
    visible_evidence_path.write_text("inert visible-check evidence", encoding="utf-8")
    artifact_path = tmp_path / "final.txt"
    artifact_path.write_text("inert final artifact", encoding="utf-8")
    # These inert values exercise structure only. They do not refer to real
    # production artifacts or attest that any measurement/evidence passed.
    template["artifact_kind"] = "inert_test_fixture"
    template["artifacts"] = [str(artifact_path.resolve())]
    template["authoritative_source"] = {
        "path": str(source_path.resolve()),
        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "readback_mode": "inert-structural-test-only",
    }
    ids = [f"criterion-{n}" for n in range(1, 6)]
    template["visible_criteria"] = [{"id": item} for item in ids]
    rules = template["rule_contracts"]
    rules["AQ-CHANGE-01"] = {"approved_scope_ids": ["scope-1"], "preserved_field_ids": ["field-1"]}
    rules["AQ-AUTHORITY-01"] = {"owner_id": "inert-owner"}
    rules["AQ-SCOPE-01"] = {"protected_region_ids": ["region-1"], "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest()}
    rules["AQ-VISIBLE-01"] = {"criterion_ids": ids, "image_evidence_sha256": hashlib.sha256(visible_evidence_path.read_bytes()).hexdigest()}
    rules["AQ-FIXTURE-01"] = {
        "suite": {"path": str(suite_path.resolve()), "sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest()},
        "expected_rule_failures": ["none"],
    }
    return template


def _schema_errors(contract: dict) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Resolve the schema's relative local reference using the canonical sibling
    # schema file; no schema copy or alternate validator is introduced.
    store = {
        "docs-artifact-quality-common-rule-applicability-1.json": json.loads(
            (ROOT / "schemas/docs-artifact-quality-common-rule-applicability-1.json").read_text(encoding="utf-8")
        ),
        str(ROOT / "schemas/docs-artifact-quality-common-rule-applicability-1.json"):
            json.loads((ROOT / "schemas/docs-artifact-quality-common-rule-applicability-1.json").read_text(encoding="utf-8")),
    }
    resolver = RefResolver(base_uri=SCHEMA_PATH.as_uri(), referrer=schema, store=store)
    validator = Draft202012Validator(schema, resolver=resolver)
    return [error.message for error in validator.iter_errors(contract)]


def test_filled_inert_fixture_passes_existing_structural_validators(tmp_path: Path):
    contract = _filled_fixture(tmp_path)
    assert validate_contract(contract) == []
    assert _schema_errors(contract) == []
    assert set(contract["rule_contracts"]) == set(CONTRACT_RULES)
    assert Path(contract["authoritative_source"]["path"]).is_file()
    assert all(Path(path).is_file() for path in contract["artifacts"])
    assert len(contract["rule_contracts"]["AQ-SCOPE-01"]["baseline_sha256"]) == 64


def test_omitting_required_input_fails_existing_validators(tmp_path: Path):
    contract = _filled_fixture(tmp_path)
    del contract["authoritative_source"]["readback_mode"]
    assert any("readback mode required" in error for error in validate_contract(contract))
    assert _schema_errors(contract)


def test_unfilled_template_is_not_a_pass():
    contract = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    errors = validate_contract(contract)
    assert errors
    assert _schema_errors(contract)


def test_visible_criteria_and_rule_contracts_are_required(tmp_path: Path):
    contract = _filled_fixture(tmp_path)
    del contract["visible_criteria"]
    assert any("visible criteria" in error for error in validate_contract(contract))
    assert _schema_errors(contract)

    contract = _filled_fixture(tmp_path)
    del contract["rule_contracts"]["AQ-RELATION-01"]
    assert any("AQ-RELATION-01" in error for error in validate_contract(contract))
    assert _schema_errors(contract)
