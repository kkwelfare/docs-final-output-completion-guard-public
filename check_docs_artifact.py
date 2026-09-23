"""Create body-free receipts using the canonical final-output document checker.

Dry-run is the default. A rewrite candidate blocks receipt acceptance until
--apply is explicitly supplied; after apply, the canonical checker is run again.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

_FILTER_ROOT_OVERRIDE = os.environ.get("FINAL_OUTPUT_LOCAL_FILTER_ROOT")
CANONICAL_ROOT = (
    Path(_FILTER_ROOT_OVERRIDE).expanduser().resolve()
    if _FILTER_ROOT_OVERRIDE
    else Path(__file__).resolve().parent / "canonical_checker"
)
CANONICAL_CHECKER = CANONICAL_ROOT / "check_document.py"
CANONICAL_CORE = CANONICAL_ROOT / "filter_core.py"
SCHEMA_VERSION = "docs-final-output-receipt-1"
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".svg", ".tex", ".csv"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_checker():
    spec = importlib.util.spec_from_file_location("_canonical_final_output_document_checker", CANONICAL_CHECKER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load canonical checker: {CANONICAL_CHECKER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    """Return only non-body fields; canonical report['text'] is never persisted."""
    return {
        "changed": bool(report.get("changed")),
        "rewrite_count": int(report.get("rewrite_count", 0)),
        "rules": list(report.get("rules") or []),
        "protected": bool(report.get("protected")),
        "applied": bool(report.get("applied")),
    }


def build_receipt(
    artifact: Path,
    *,
    source_text: Path | None = None,
    no_applicable_reason: str = "",
    apply: bool = False,
) -> tuple[dict[str, Any], int]:
    artifact = artifact.resolve(strict=True)
    target = source_text.resolve(strict=True) if source_text else artifact
    is_text = bool(source_text) or artifact.suffix.lower() in TEXT_SUFFIXES
    entry: dict[str, Any] = {
        "artifact_path": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "kind": "text" if is_text else "non-text",
    }
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "canonical_checker_path": str(CANONICAL_CHECKER),
        "canonical_checker_sha256": _sha256(CANONICAL_CHECKER),
        "canonical_core_path": str(CANONICAL_CORE),
        "canonical_core_sha256": _sha256(CANONICAL_CORE),
        "entries": [entry],
        "status": "block",
    }

    if not is_text:
        reason = no_applicable_reason.strip()
        if len(reason) < 12:
            entry["reason"] = "non-text artifact requires --source-text or a specific --no-applicable-reason"
            return receipt, 2
        entry["no_applicable_reason"] = reason
        entry["status"] = "pass"
        receipt["status"] = "pass"
        return receipt, 0

    checker = _load_checker()
    before = checker.check_path(target, apply=False)
    entry["source_text_path"] = str(target)
    entry["source_text_sha256_before"] = _sha256(target)
    entry["dry_run"] = _summary(before)
    if before.get("changed") and not apply:
        entry["reason"] = "safe rewrite candidate exists; rerun with explicit --apply"
        return receipt, 2

    if before.get("changed") and apply:
        applied = checker.check_path(target, apply=True)
        entry["apply"] = _summary(applied)

    final = checker.check_path(target, apply=False)
    entry["final_readback"] = _summary(final)
    entry["source_text_sha256"] = _sha256(target)
    if final.get("changed"):
        entry["reason"] = "canonical checker still reports a rewrite candidate after readback"
        return receipt, 2

    if target == artifact:
        entry["artifact_sha256"] = entry["source_text_sha256"]
    entry["status"] = "pass"
    receipt["status"] = "pass"
    return receipt, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a privacy-preserving docs completion receipt.")
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--source-text", type=Path)
    parser.add_argument("--no-applicable-reason", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    receipt, code = build_receipt(
        args.artifact,
        source_text=args.source_text,
        no_applicable_reason=args.no_applicable_reason,
        apply=args.apply,
    )
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": receipt["status"],
        "receipt": str(args.receipt.resolve()),
        "entries": len(receipt["entries"]),
    }, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
