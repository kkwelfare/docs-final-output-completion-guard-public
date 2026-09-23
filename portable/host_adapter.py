"""Explicit host seam for paths and optional runtime integrations.

No machine or profile path is compiled into this module.  A caller may provide
an environment manifest through ``DOCS_GUARD_HOST_MANIFEST`` or directly to
``HostAdapter.from_manifest``.  Relative paths are resolved against that
manifest; the no-manifest default is the bundled checker and a disabled fake
renderer contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

SCHEMA_VERSION = "docs-host-adapter-manifest-1"


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path(raw: Any, *, base: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("host path values must be non-empty strings or null")
    value = Path(raw).expanduser()
    return (value if value.is_absolute() else base / value).resolve()


def _path_list(raw: Any, *, base: Path) -> tuple[Path, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise ValueError("host path lists must contain non-empty strings")
    return tuple(path for item in raw if (path := _resolve_path(item, base=base)) is not None)


@dataclass(frozen=True)
class HostAdapter:
    """Resolved host resources; no adapter method performs network I/O."""

    candidate_root: Path
    canonical_checker_root: Path
    renderer: dict[str, Any]
    font_roots: tuple[Path, ...]
    task_state_root: Path | None
    runtime_root: Path | None
    provider_mode: str
    manifest_path: Path | None = None

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path | str | None = None,
        *,
        candidate_root: Path | str | None = None,
    ) -> "HostAdapter":
        candidate = Path(candidate_root).resolve() if candidate_root is not None else Path(__file__).resolve().parents[1]
        requested = manifest_path
        if requested is None:
            env_value = os.environ.get("DOCS_GUARD_HOST_MANIFEST")
            requested = env_value if env_value else None
        manifest = Path(requested).expanduser().resolve(strict=True) if requested is not None else None
        data: dict[str, Any] = {}
        base = candidate
        if manifest is not None:
            if not manifest.is_file():
                raise FileNotFoundError(f"host manifest is not a regular file: {manifest}")
            try:
                loaded = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("host manifest is unreadable or malformed") from exc
            if not isinstance(loaded, dict) or loaded.get("schema_version") != SCHEMA_VERSION:
                raise ValueError(f"host manifest schema must be {SCHEMA_VERSION}")
            data = loaded
            base = manifest.parent

        checker_root = _resolve_path(data.get("canonical_checker_root"), base=base)
        if checker_root is None:
            checker_root = candidate / "canonical_checker"
        renderer_data = data.get("renderer") if isinstance(data.get("renderer"), dict) else {}
        renderer: dict[str, Any] = {
            "kind": str(renderer_data.get("kind", "fake")),
            "executable": _resolve_path(renderer_data.get("executable"), base=base),
            "output_root": _resolve_path(renderer_data.get("output_root"), base=base),
            "network": str(renderer_data.get("network", "disabled")),
        }
        if renderer["network"] != "disabled":
            raise ValueError("renderer network policy must be disabled for the portable route")
        font_data = data.get("font_discovery") if isinstance(data.get("font_discovery"), dict) else {}
        font_roots = _path_list(font_data.get("roots", []), base=base)
        task_data = data.get("task_state") if isinstance(data.get("task_state"), dict) else {}
        runtime_data = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        provider_data = data.get("provider") if isinstance(data.get("provider"), dict) else {}
        provider_mode = str(provider_data.get("mode", "advisory-disabled"))
        if provider_mode not in {"advisory-disabled", "advisory-opt-in"}:
            raise ValueError("provider mode must remain advisory-disabled or advisory-opt-in")
        task_root = _resolve_path(task_data.get("root"), base=base)
        runtime_root = _resolve_path(runtime_data.get("root"), base=base)
        return cls(
            candidate_root=candidate,
            canonical_checker_root=checker_root,
            renderer=renderer,
            font_roots=font_roots,
            task_state_root=task_root,
            runtime_root=runtime_root,
            provider_mode=provider_mode,
            manifest_path=manifest,
        )

    def checker_paths(self) -> tuple[Path, Path]:
        checker = self.canonical_checker_root / "check_document.py"
        core = self.canonical_checker_root / "filter_core.py"
        if not checker.is_file() or not core.is_file():
            raise FileNotFoundError("canonical checker requires check_document.py and filter_core.py")
        return checker, core

    def load_checker(self):
        checker, _ = self.checker_paths()
        module_name = "_docs_guard_checker_" + sha256_path(checker)[:16]
        cached = sys.modules.get(module_name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(module_name, checker)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load canonical checker: {checker}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def describe(self) -> dict[str, Any]:
        checker, core = self.checker_paths()
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_supplied": self.manifest_path is not None,
            "canonical_checker": {
                "path": str(checker),
                "sha256": sha256_path(checker),
                "core_path": str(core),
                "core_sha256": sha256_path(core),
            },
            "renderer": {
                "kind": self.renderer["kind"],
                "executable": str(self.renderer["executable"]) if self.renderer["executable"] else None,
                "network": self.renderer["network"],
            },
            "font_discovery": {
                "roots": [str(path) for path in self.font_roots],
                "policy": "host-supplied-only",
            },
            "task_state": {
                "root": str(self.task_state_root) if self.task_state_root else None,
                "policy": "caller-supplied-metadata-only",
            },
            "runtime": {
                "root": str(self.runtime_root) if self.runtime_root else None,
                "network": "disabled",
            },
            "provider": {
                "mode": self.provider_mode,
                "called": False,
                "payloads_persisted": False,
                "image_transport": "forbidden",
            },
        }
