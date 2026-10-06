#!/usr/bin/env python3
"""Prepare and validate the ordinary-DOCX baseline contract after real readbacks."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import ordinary_docx_baseline
from prepare_finalization import prepare as prepare_finalization


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    if not path.is_absolute() or path.exists():
        raise ValueError(f"output must be a new absolute path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists():
        raise ValueError(f"temporary output already exists: {temporary}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    paths = [args.artifact, args.receiver_copy, args.source, args.docx_readback,
             args.render_readback, args.rendered_pdf, *args.representative_page,
             args.final_output_receipt, args.manifest, args.receiver_receipt, args.contract]
    if any(not path.is_absolute() for path in paths):
        raise ValueError("all input and output paths must be absolute")
    if args.contract.exists():
        raise ValueError(f"contract output already exists: {args.contract}")
    if not args.final_output_receipt.is_file():
        raise ValueError("canonical final-output receipt is missing")
    if not args.representative_page:
        raise ValueError("at least one representative rendered page is required")
    prepared = prepare_finalization(
        artifacts=[str(args.artifact)], copies=[f"{args.artifact}={args.receiver_copy}"],
        source=str(args.source), checker=str(args.checker),
        manifest_path=args.manifest, receiver_receipt_path=args.receiver_receipt,
        producer_task_id=args.producer_task_id, producer_run_id=args.producer_run_id,
        receiver_task_id=args.receiver_task_id, receiver_run_id=args.receiver_run_id,
    )
    value = {
        "schema_version": ordinary_docx_baseline.SCHEMA_VERSION,
        "artifact": ordinary_docx_baseline.handle(args.artifact),
        "docx_readback": ordinary_docx_baseline.handle(args.docx_readback),
        "render_readback": ordinary_docx_baseline.handle(args.render_readback),
        "rendered_pdf": ordinary_docx_baseline.handle(args.rendered_pdf),
        "representative_pages": [ordinary_docx_baseline.handle(path) for path in args.representative_page],
        "final_output_receipts": [ordinary_docx_baseline.handle(args.final_output_receipt)],
        "finalization_manifest": {"path": str(args.manifest.resolve()), "receiver_receipt": str(args.receiver_receipt.resolve())},
    }
    _atomic_json(args.contract, value)
    error = ordinary_docx_baseline.validate(args.contract, [str(args.artifact)])
    if error:
        raise ValueError(error)
    return {"status": "prepared", "baseline_contract": str(args.contract),
            "finalization_manifest": prepared["manifest_path"],
            "receiver_receipt": prepared["receiver_receipt_path"],
            "quality_receipt": "not_created; ordinary canonical receipt reused",
            "completion": "not_submitted"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifact", "receiver-copy", "source", "docx-readback", "render-readback",
                 "rendered-pdf", "final-output-receipt", "manifest", "receiver-receipt", "contract"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--representative-page", action="append", required=True, type=Path)
    parser.add_argument("--checker", default=ROOT / "check_docs_artifact.py", type=Path)
    parser.add_argument("--producer-task-id")
    parser.add_argument("--producer-run-id", type=int)
    parser.add_argument("--receiver-task-id", required=True)
    parser.add_argument("--receiver-run-id", required=True, type=int)
    args = parser.parse_args()
    if bool(args.producer_task_id) != (args.producer_run_id is not None):
        parser.error("pass both --producer-task-id and --producer-run-id, or neither when the original identity is unavailable")
    try:
        result = prepare(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
