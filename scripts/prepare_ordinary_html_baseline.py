#!/usr/bin/env python3
"""Bind existing single-HTML browser/render/final-output/receiver readbacks.

This command only creates a contract of hash-bound handles. It never visits a
URL, executes browser checks, renders a file, creates a canonical checker
receipt, or asserts a PASS. Inputs must already exist as real readbacks.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quality_rules import ordinary_html_baseline


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
    all_paths = [args.artifact, args.browser_receipt, args.render,
                 args.final_output_receipt, args.manifest, args.receiver_receipt,
                 args.contract]
    if any(not path.is_absolute() for path in all_paths):
        raise ValueError("all input and output paths must be absolute")
    if args.contract.exists():
        raise ValueError(f"contract output already exists: {args.contract}")
    if len(args.artifact_path) != 1 or args.artifact_path[0].resolve(strict=True) != args.artifact.resolve(strict=True):
        raise ValueError("ordinary HTML route requires exactly the one declared artifact")
    browser_handle = ordinary_html_baseline.handle(args.browser_receipt)
    browser, error = ordinary_html_baseline._json(Path(browser_handle["path"]), "real-browser receipt")
    if error or browser is None:
        raise ValueError(error or "real-browser receipt is malformed")
    if browser.get("artifact_sha256") != ordinary_html_baseline.sha256(args.artifact):
        raise ValueError("real-browser receipt artifact hash does not match current HTML")
    if browser.get("render") != ordinary_html_baseline.handle(args.render):
        raise ValueError("actual render handle differs from real-browser receipt")
    contract = {
        "schema_version": ordinary_html_baseline.SCHEMA_VERSION,
        "artifact": ordinary_html_baseline.handle(args.artifact),
        "browser": browser_handle,
        "render": ordinary_html_baseline.handle(args.render),
        "final_output_receipts": [ordinary_html_baseline.handle(args.final_output_receipt)],
        "finalization_manifest": {
            "path": str(args.manifest.resolve()),
            "receiver_receipt": str(args.receiver_receipt.resolve()),
        },
        "checks": browser.get("checks"),
        "applicability": json.loads(args.applicability.read_text(encoding="utf-8")),
    }
    _atomic_json(args.contract, contract)
    error = ordinary_html_baseline.validate(args.contract, [str(args.artifact)])
    if error:
        raise ValueError(error)
    return {"status": "prepared", "baseline_contract": str(args.contract),
            "artifact": str(args.artifact.resolve()), "artifact_sha256": ordinary_html_baseline.sha256(args.artifact),
            "browser_receipt": browser_handle, "render": contract["render"],
            "final_output_receipt": contract["final_output_receipts"][0],
            "completion": "not_submitted"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--artifact-path", action="append", required=True, type=Path)
    parser.add_argument("--browser-receipt", required=True, type=Path)
    parser.add_argument("--render", required=True, type=Path)
    parser.add_argument("--final-output-receipt", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--receiver-receipt", required=True, type=Path)
    parser.add_argument("--applicability", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
