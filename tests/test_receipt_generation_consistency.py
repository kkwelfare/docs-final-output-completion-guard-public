from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_guard():
    spec = importlib.util.spec_from_file_location("docs_receipt_generation_guard", ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _handle(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha(path)}


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _fixture(tmp_path: Path):
    artifacts = []
    receipts = []
    produced = []
    for index in range(2):
        artifact = tmp_path / f"artifact-{index}.pdf"
        artifact.write_bytes(f"artifact-{index}".encode())
        receipt = _write(tmp_path / f"receipt-{index}.json", {
            "entries": [{"artifact_path": str(artifact.resolve()), "artifact_sha256": _sha(artifact)}]
        })
        artifacts.append(artifact)
        receipts.append(receipt)
        produced.append({"artifact": _handle(artifact), "final_output_receipt": _handle(receipt)})
    final = _write(tmp_path / "final-verification.json", {
        "status": "PASS", "artifacts": [_handle(path) for path in artifacts]
    })
    manifest = _write(tmp_path / "handoff-manifest.json", {
        "status": "PASS", "produced_artifacts": produced, "final_verification": _handle(final)
    })
    args = {"metadata": {"verification": {"final_output_filter": {
        "manifest": str(manifest.resolve()), "receipts": [str(path.resolve()) for path in receipts]
    }}}}
    return artifacts, receipts, final, manifest, args


def test_declared_receipt_generation_matches_handoff_final_verification_receipts_and_actual_files(tmp_path):
    guard = _load_guard()
    _, receipts, _, _, args = _fixture(tmp_path)
    assert guard._validate_receipt_generation(args, receipts) is None


def test_stale_final_verification_fails(tmp_path):
    guard = _load_guard()
    _, receipts, final, _, args = _fixture(tmp_path)
    final.write_text(final.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert "final-verification handle hash drift" in guard._validate_receipt_generation(args, receipts)


def test_missing_receipt_fails_only_when_generation_declares_receipts(tmp_path):
    guard = _load_guard()
    _, receipts, _, manifest, args = _fixture(tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["produced_artifacts"][0].pop("final_output_receipt")
    _write(manifest, value)
    assert "missing a final-output receipt" in guard._validate_receipt_generation(args, receipts)

    for item in value["produced_artifacts"]:
        item.pop("final_output_receipt", None)
    value.pop("final_verification", None)
    _write(manifest, value)
    assert guard._validate_receipt_generation(args, receipts) is None


def test_duplicate_receipt_or_path_outside_canonical_set_fails(tmp_path):
    guard = _load_guard()
    _, receipts, _, _, args = _fixture(tmp_path)
    assert "duplicate final-output receipt path" in guard._validate_receipt_generation(args, [receipts[0], receipts[0]])

    extra_artifact = tmp_path / "outside.pdf"
    extra_artifact.write_bytes(b"outside")
    extra_receipt = _write(tmp_path / "outside-receipt.json", {
        "entries": [{"artifact_path": str(extra_artifact.resolve()), "artifact_sha256": _sha(extra_artifact)}]
    })
    assert "differ from the canonical handoff receipt set" in guard._validate_receipt_generation(
        args, [*receipts, extra_receipt]
    )


def test_differing_canonical_artifact_set_fails(tmp_path):
    guard = _load_guard()
    _, receipts, final, manifest, args = _fixture(tmp_path)
    final_value = json.loads(final.read_text(encoding="utf-8"))
    final_value["artifacts"].pop()
    _write(final, final_value)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["final_verification"] = _handle(final)
    _write(manifest, manifest_value)
    assert "canonical artifact path/sha256 maps differ" in guard._validate_receipt_generation(args, receipts)
