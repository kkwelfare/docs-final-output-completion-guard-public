"""Small, explicit source-format adapters with body-free inspection output."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol
from zipfile import is_zipfile

from .evidence import sha256_path


@dataclass(frozen=True)
class FormatInspection:
    path: Path
    format_name: str
    source_sha256: str
    size_bytes: int
    line_count: int | None
    structural_facts: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": self.format_name,
            "sha256": self.source_sha256,
            "size_bytes": self.size_bytes,
            "line_count": self.line_count,
            "structural_facts": dict(self.structural_facts),
        }


class FormatAdapter(Protocol):
    name: str
    suffixes: frozenset[str]

    def inspect(self, path: Path) -> FormatInspection:
        ...


class TextFormatAdapter:
    name = "text"
    suffixes = frozenset({".txt", ".md", ".markdown", ".rst", ".html", ".htm", ".svg", ".tex", ".csv"})

    def inspect(self, path: Path) -> FormatInspection:
        target = path.resolve(strict=True)
        text = target.read_text(encoding="utf-8")
        return FormatInspection(
            path=target,
            format_name=self.name if target.suffix.lower() == ".txt" else target.suffix.lower().lstrip("."),
            source_sha256=sha256_path(target),
            size_bytes=target.stat().st_size,
            line_count=len(text.splitlines()),
            structural_facts={"encoding": "utf-8", "empty": not bool(text), "checker_eligible": True},
        )


class JsonFormatAdapter:
    name = "json"
    suffixes = frozenset({".json"})

    def inspect(self, path: Path) -> FormatInspection:
        target = path.resolve(strict=True)
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            kind = "object"
            facts = {"top_level": kind, "key_count": len(value), "checker_eligible": False}
        elif isinstance(value, list):
            kind = "array"
            facts = {"top_level": kind, "item_count": len(value), "checker_eligible": False}
        else:
            kind = type(value).__name__
            facts = {"top_level": kind, "checker_eligible": False}
        return FormatInspection(
            path=target,
            format_name=self.name,
            source_sha256=sha256_path(target),
            size_bytes=target.stat().st_size,
            line_count=len(target.read_text(encoding="utf-8").splitlines()),
            structural_facts=facts,
        )


class ContainerFormatAdapter:
    """Inspect Office/PDF containers without rendering or reading document body."""

    _formats = {
        ".docx": "docx",
        ".pptx": "pptx",
        ".pdf": "pdf",
    }
    name = "container"
    suffixes = frozenset(_formats)

    def inspect(self, path: Path) -> FormatInspection:
        target = path.resolve(strict=True)
        suffix = target.suffix.lower()
        header = target.read_bytes()[:8]
        if suffix in {".docx", ".pptx"}:
            valid_container = is_zipfile(target)
            container = "zip" if valid_container else "invalid-zip"
        else:
            valid_container = header.startswith(b"%PDF-")
            container = "pdf" if valid_container else "invalid-pdf"
        return FormatInspection(
            path=target,
            format_name=self._formats[suffix],
            source_sha256=sha256_path(target),
            size_bytes=target.stat().st_size,
            line_count=None,
            structural_facts={
                "container": container,
                "valid_container": valid_container,
                "checker_eligible": False,
                "renderer_required": True,
            },
        )


_ADAPTERS: tuple[FormatAdapter, ...] = (TextFormatAdapter(), JsonFormatAdapter(), ContainerFormatAdapter())


def adapter_for(path: Path | str) -> FormatAdapter:
    suffix = Path(path).suffix.lower()
    for adapter in _ADAPTERS:
        if suffix in adapter.suffixes:
            return adapter
    raise ValueError(f"unsupported portable source format: {suffix or '<none>'}")


def inspect_source(path: Path | str) -> FormatInspection:
    target = Path(path)
    return adapter_for(target).inspect(target)
