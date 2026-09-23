#!/usr/bin/env python3
"""Run one local-only producer readback with an explicit host manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from portable.producer_core import ProducerRequest, produce


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the portable docs producer without provider calls.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--host-manifest", type=Path)
    args = parser.parse_args()
    result = produce(ProducerRequest(
        source_path=args.source,
        output_root=args.output_root,
        host_manifest=args.host_manifest,
    ))
    print(json.dumps({
        "status": result["status"],
        "quality_status": result["quality"]["status"],
        "renderer_status": result["renderer"]["status"],
        "provider_called": result["provider"]["called"],
        "image_transport": result["provider"]["image_transport"],
        "receipt_path": result.get("receipt_path"),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "pass" and result["provider"]["called"] is False else 2


if __name__ == "__main__":
    raise SystemExit(main())
