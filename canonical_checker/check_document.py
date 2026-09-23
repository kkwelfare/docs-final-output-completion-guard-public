"""Reusable document checker. It never mutates input unless --apply is explicit."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

try:  # Importable package entrypoint used by a document-checking profile.
    from .filter_core import filter_text
except ImportError:  # Direct CLI execution from this hyphenated plugin directory.
    _spec = importlib.util.spec_from_file_location(
        "_final_output_local_filter_core", Path(__file__).with_name("filter_core.py")
    )
    if _spec is None or _spec.loader is None:
        raise
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    filter_text = _module.filter_text


def check_path(path: Path, *, apply: bool = False) -> dict[str, object]:
    original = path.read_text(encoding="utf-8")
    result = filter_text(original, mode="document")
    report: dict[str, object] = {
        "path": str(path),
        "changed": result.text != original,
        "rewrite_count": result.rewrite_count,
        "rules": list(result.rules),
        "protected": result.protected,
        "applied": False,
        "text": result.text,
    }
    if apply and result.text != original:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(result.text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            report["applied"] = True
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local-filter candidates without network or LLM calls.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="Atomically rewrite only the supplied path.")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path instead of stdout.")
    args = parser.parse_args()
    report = check_path(args.path, apply=args.apply)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
