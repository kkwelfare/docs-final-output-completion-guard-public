from __future__ import annotations

import json
from pathlib import Path

import pytest

from portable.evidence import FORBIDDEN_KEYS, assert_body_free
from portable.format_adapters import inspect_source
from portable.host_adapter import HostAdapter
from portable.producer_core import ProducerRequest, produce
from portable.renderer_protocol import FakeRenderer, RenderPlan


@pytest.fixture()
def clean_source(tmp_path: Path) -> Path:
    source = tmp_path / "source.md"
    source.write_text("設定を確認できます。\n", encoding="utf-8")
    return source


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_default_host_adapter_resolves_bundled_checker_without_machine_paths():
    host = HostAdapter.from_manifest()
    checker, core = host.checker_paths()
    assert checker.name == "check_document.py"
    assert core.name == "filter_core.py"
    assert checker.is_file() and core.is_file()


def test_fake_renderer_is_deterministic_and_image_free(tmp_path: Path):
    plan = RenderPlan("md", "a" * 64, 1, "docs-style-default-v1", "docs-quality-ruleset-v3", ("R1",))
    first = FakeRenderer().render(plan, tmp_path / "one")
    second = FakeRenderer().render(plan, tmp_path / "two")
    assert first.sha256 == second.sha256
    payload = json.loads(first.path.read_text(encoding="utf-8"))
    assert payload["network"] == "disabled"
    assert payload["image_transport"] == "forbidden"
    assert not any(key.lower() in {"image", "images", "png", "jpeg", "payload", "content"} for key in _walk_keys(payload))


def test_producer_pass_contains_versioned_hashes_and_no_document_body(clean_source: Path, tmp_path: Path):
    result = produce(ProducerRequest(clean_source, tmp_path / "run"))
    assert result["status"] == "pass"
    assert result["quality"]["authoritative"] is True
    assert result["provider"] == {
        "mode": "advisory-disabled",
        "called": False,
        "authoritative": False,
        "payloads_persisted": False,
        "image_transport": "forbidden",
    }
    assert len(result["style_preset"]["sha256"]) == 64
    assert len(result["ruleset"]["sha256"]) == 64
    assert result["evidence_boundary"]["image_transport"] == "forbidden"
    assert not (set(_walk_keys(result)) & FORBIDDEN_KEYS)
    assert Path(result["receipt_path"]).is_file()


def test_checker_block_is_authoritative_and_fake_renderer_is_not_called(tmp_path: Path):
    source = tmp_path / "needs-review.md"
    source.write_text("必要なら設定を確認できます。\n", encoding="utf-8")
    result = produce(ProducerRequest(source, tmp_path / "run"))
    assert result["status"] == "block"
    assert result["quality"]["status"] == "block"
    assert result["renderer"]["status"] == "skipped"
    assert not (tmp_path / "run" / "render" / "fake-render.json").exists()


def test_format_adapter_is_explicit_and_structural(tmp_path: Path):
    source = tmp_path / "record.json"
    source.write_text('{"alpha": 1, "beta": [2]}\n', encoding="utf-8")
    inspection = inspect_source(source)
    assert inspection.format_name == "json"
    assert inspection.structural_facts == {"top_level": "object", "key_count": 2, "checker_eligible": False}
    assert inspection.source_sha256 and len(inspection.source_sha256) == 64


def test_versioned_compact_preset_can_be_selected(clean_source: Path, tmp_path: Path):
    preset = Path(__file__).resolve().parents[1] / "presets" / "style-compact-v1.json"
    result = produce(ProducerRequest(clean_source, tmp_path / "compact", style_preset_path=preset))
    assert result["status"] == "pass"
    assert result["style_preset"]["id"] == "docs-style-compact-v1"
    assert len(result["style_preset"]["sha256"]) == 64


def test_malformed_host_manifest_fails_closed(tmp_path: Path):
    manifest = tmp_path / "host.json"
    manifest.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        HostAdapter.from_manifest(manifest)


def test_evidence_rejects_raw_body_keys():
    with pytest.raises(ValueError, match="body-bearing"):
        assert_body_free({"source": {"text": "secret"}})
