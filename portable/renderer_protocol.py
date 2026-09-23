"""Renderer contract and a deterministic offline fake renderer."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol

from .evidence import canonical_hash, sha256_path

RENDERER_SCHEMA_VERSION = "docs-renderer-contract-1"


@dataclass(frozen=True)
class RenderPlan:
    format_name: str
    source_sha256: str
    line_count: int | None
    style_preset_id: str
    ruleset_id: str
    rule_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RENDERER_SCHEMA_VERSION,
            "format": self.format_name,
            "source_sha256": self.source_sha256,
            "line_count": self.line_count,
            "style_preset_id": self.style_preset_id,
            "ruleset_id": self.ruleset_id,
            "rule_ids": list(self.rule_ids),
        }


@dataclass(frozen=True)
class RenderedArtifact:
    name: str
    path: Path
    sha256: str
    size_bytes: int
    image_transport: str = "forbidden"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "image_transport": self.image_transport,
        }


class Renderer(Protocol):
    name: str
    contract_version: str

    def render(self, plan: RenderPlan, output_root: Path) -> RenderedArtifact:
        ...


class FakeRenderer:
    """Write only deterministic metadata; it never creates or sends an image."""

    name = "fake-local-renderer"
    contract_version = RENDERER_SCHEMA_VERSION

    def render(self, plan: RenderPlan, output_root: Path) -> RenderedArtifact:
        payload = {
            "schema_version": RENDERER_SCHEMA_VERSION,
            "renderer": self.name,
            "plan": plan.as_dict(),
            "plan_sha256": canonical_hash(plan.as_dict()),
            "network": "disabled",
            "image_transport": "forbidden",
        }
        target_root = Path(output_root).resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / "fake-render.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        return RenderedArtifact(
            name=self.name,
            path=target,
            sha256=sha256_path(target),
            size_bytes=target.stat().st_size,
        )
