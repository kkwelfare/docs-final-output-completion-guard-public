from __future__ import annotations

import json
from pathlib import Path

import pytest

from portable.evidence import FORBIDDEN_KEYS, assert_body_free
from portable.format_adapters import inspect_source
from portable.host_adapter import HostAdapter
from portable.producer_core import ProducerRequest, produce
from portable.renderer_protocol import FakeRenderer, RenderPlan, RenderedArtifact
from portable.evidence import sha256_path


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


def test_checker_cache_isolated_by_checker_and_core_identity(tmp_path: Path):
    import shutil

    source_root = Path(__file__).resolve().parents[1] / "canonical_checker"
    roots = []
    for label, marker in (("one", "cache_one"), ("two", "cache_two")):
        root = tmp_path / label
        shutil.copytree(source_root, root)
        core = root / "filter_core.py"
        core.write_text(core.read_text(encoding="utf-8") + f"\nCACHE_ROOT_MARKER = {marker!r}\n", encoding="utf-8")
        roots.append(root)

    def host(root):
        return HostAdapter.from_manifest(candidate_root=Path(__file__).resolve().parents[1]).__class__(
            candidate_root=Path(__file__).resolve().parents[1], canonical_checker_root=root,
            renderer={"kind": "fake", "executable": None, "output_root": None, "network": "disabled"},
            font_roots=(), task_state_root=None, runtime_root=None, provider_mode="advisory-disabled",
        )

    first_host, second_host = host(roots[0]), host(roots[1])
    first = first_host.load_checker()
    second = second_host.load_checker()
    assert first is first_host.load_checker()
    assert second is second_host.load_checker()
    assert first is not second
    first_core = __import__("sys").modules[first.filter_text.__module__]
    second_core = __import__("sys").modules[second.filter_text.__module__]
    assert first_core.CACHE_ROOT_MARKER == "cache_one"
    assert second_core.CACHE_ROOT_MARKER == "cache_two"

    core = roots[0] / "filter_core.py"
    core.write_text(core.read_text(encoding="utf-8") + "\nCACHE_CORE_REVISION = 'changed-core-revision'\n", encoding="utf-8")
    refreshed = first_host.load_checker()
    refreshed_core = __import__("sys").modules[refreshed.filter_text.__module__]
    assert refreshed is not first
    assert refreshed_core.CACHE_ROOT_MARKER == "cache_one"
    assert refreshed_core.CACHE_CORE_REVISION == "changed-core-revision"


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


@pytest.mark.parametrize("suffix,contents", [(".docx", b"not a zip"), (".pdf", b"not a pdf")])
def test_invalid_document_containers_block_and_skip_fake_renderer(tmp_path: Path, suffix: str, contents: bytes):
    source = tmp_path / f"broken{suffix}"
    source.write_bytes(contents)
    result = produce(ProducerRequest(source, tmp_path / "broken-run"))
    assert result["status"] == "block"
    assert result["source"]["structural_facts"]["valid_container"] is False
    assert result["quality"]["status"] == "block"
    assert result["renderer"]["status"] == "skipped"
    assert not (tmp_path / "broken-run" / "render" / "fake-render.json").exists()
    saved = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert saved["status"] == "block"


@pytest.mark.parametrize("suffix,contents", [(".docx", b"PK\x03\x04fixture"), (".pdf", b"%PDF-1.7\nfixture")])
def test_valid_container_with_fake_renderer_is_explicitly_not_verified(tmp_path: Path, suffix: str, contents: bytes):
    source = tmp_path / f"fixture{suffix}"
    if suffix == ".docx":
        import zipfile
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
    else:
        source.write_bytes(contents)
    result = produce(ProducerRequest(source, tmp_path / "run"))
    assert result["source"]["structural_facts"]["valid_container"] is True
    assert result["renderer"]["status"] == "not_verified"
    assert result["status"] == "not_verified"
    assert result["provider"]["called"] is False
    saved = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert saved["status"] == "not_verified"


def test_not_verified_receipt_matches_public_schema(tmp_path: Path):
    import jsonschema

    source = tmp_path / "schema-fixture.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture")
    result = produce(ProducerRequest(source, tmp_path / "schema-run"))
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/docs-producer-receipt-2.json").read_text(encoding="utf-8"))
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)
    assert receipt["status"] == "not_verified"
    assert receipt["renderer"]["status"] == "not_verified"
    assert receipt["provider"]["called"] is False


@pytest.mark.parametrize("suffix,contents", [(".docx", b"PK\x03\x04fixture"), (".pdf", b"%PDF-1.7\nfixture")])
def test_valid_container_with_protocol_fixture_renderer_passes_as_mock(tmp_path: Path, suffix: str, contents: bytes):
    class ProtocolFixtureRenderer:
        name = "protocol-fixture-renderer"
        contract_version = "docs-renderer-contract-1"

        def render(self, plan: RenderPlan, output_root: Path) -> RenderedArtifact:
            output_root.mkdir(parents=True, exist_ok=True)
            target = output_root / "renderer-fixture.json"
            target.write_text(json.dumps({"renderer": self.name, "plan": plan.as_dict()}), encoding="utf-8")
            return RenderedArtifact(self.name, target, sha256_path(target), target.stat().st_size)

    source = tmp_path / f"fixture{suffix}"
    if suffix == ".docx":
        import zipfile
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
    else:
        source.write_bytes(contents)
    result = produce(ProducerRequest(source, tmp_path / "fixture-run"), renderer=ProtocolFixtureRenderer())
    assert result["status"] == "pass"
    assert result["renderer"]["status"] == "pass"
    assert result["renderer"]["name"] == "protocol-fixture-renderer"
    assert result["provider"]["called"] is False


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
