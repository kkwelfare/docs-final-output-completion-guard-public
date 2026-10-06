#!/usr/bin/env python3
"""Prepare hash-bound finalization records from an actual received artifact copy.

This helper does not copy artifacts, run quality checks, call Jev/providers, or
claim that a completion was accepted. Call it only after the destination copy
has been read back from the authorized delivery/attachment mechanism.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from quality_rules import finalization


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise ValueError(f"output path must be absolute: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _pairs(values: list[str]) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for value in values:
        left, separator, right = value.partition("=")
        if not separator or not left or not right:
            raise ValueError("each --copy must be ARTIFACT=RECEIVED_COPY")
        if not Path(left).is_absolute() or not Path(right).is_absolute():
            raise ValueError("artifact and received-copy paths must be absolute")
        source = Path(left).resolve(strict=True)
        copy = Path(right).resolve(strict=True)
        if not source.is_file() or not copy.is_file():
            raise ValueError("artifact and received copy must both be files")
        if source == copy:
            raise ValueError("received copy must be a distinct file, not the artifact itself")
        if str(source) in seen:
            raise ValueError(f"duplicate artifact copy mapping: {source}")
        seen.add(str(source))
        pairs.append((source, copy))
    if not pairs:
        raise ValueError("at least one --copy ARTIFACT=RECEIVED_COPY is required")
    return pairs


def prepare(
    *, artifacts: list[str], copies: list[str], source: str, checker: str,
    manifest_path: Path, receiver_receipt_path: Path,
    producer_task_id: str, producer_run_id: int,
    receiver_task_id: str, receiver_run_id: int,
) -> dict[str, Any]:
    if (not isinstance(receiver_task_id, str) or not receiver_task_id.strip()
            or isinstance(receiver_run_id, bool) or not isinstance(receiver_run_id, int) or receiver_run_id < 1):
        raise ValueError("actual receiver task identity is required")
    manifest_path = manifest_path.resolve()
    receiver_receipt_path = receiver_receipt_path.resolve()
    if manifest_path == receiver_receipt_path:
        raise ValueError("manifest and receiver receipt paths must be distinct")
    mappings = _pairs(copies)
    artifact_paths = [Path(value).resolve(strict=True) for value in artifacts]
    source_path = Path(source).resolve(strict=True)
    checker_path = Path(checker).resolve(strict=True)
    protected_inputs = {str(source_path), str(checker_path), *(str(path) for path in artifact_paths)}
    protected_inputs.update(str(received) for _, received in mappings)
    if str(manifest_path) in protected_inputs or str(receiver_receipt_path) in protected_inputs:
        raise ValueError("output records must not overwrite source, checker, artifact, or received copy")
    mapped = {str(original): received for original, received in mappings}
    if len(set(map(str, artifact_paths))) != len(artifact_paths):
        raise ValueError("duplicate artifact path")
    if set(mapped) != {str(path) for path in artifact_paths}:
        raise ValueError("received-copy mappings must exactly cover declared artifacts")

    manifest = finalization.build_manifest(
        [str(path) for path in artifact_paths],
        source=str(Path(source).resolve(strict=True)),
        checker=str(Path(checker).resolve(strict=True)),
        created_by_task_id=producer_task_id if producer_task_id else None,
        created_by_run_id=producer_run_id if producer_task_id and producer_run_id is not None else None,
        state="accepted",
    )
    receiver = {
        "status": "accepted",
        "artifact_set_id": manifest["artifact_set_id"],
        "receiver_task_id": receiver_task_id,
        "receiver_run_id": receiver_run_id,
        "copies": [
            {
                "canonical_path": str(original),
                "copy_path": str(received),
                "sha256": finalization.sha256(received),
                "size_bytes": received.stat().st_size,
                "media_type": finalization.media_type(original),
            }
            for original, received in mappings
        ],
    }
    # Reject stale or incorrect destination bytes before writing either record.
    if set(copy["canonical_path"] for copy in receiver["copies"]) != {
        item["canonical_path"] for item in manifest["artifacts"]
    }:
        raise ValueError("received-copy mappings do not match manifest coverage")
    for copy in receiver["copies"]:
        source_item = next(item for item in manifest["artifacts"] if item["canonical_path"] == copy["canonical_path"])
        received_path = Path(copy["copy_path"])
        if (copy["sha256"] != source_item["sha256"]
                or copy["size_bytes"] != source_item["size_bytes"]
                or copy["media_type"] != source_item["media_type"]
                or finalization.sha256(received_path) != source_item["sha256"]
                or received_path.stat().st_size != source_item["size_bytes"]):
            raise ValueError("receiver-copy-hash-drift")
    _atomic_json(manifest_path, manifest)
    _atomic_json(receiver_receipt_path, receiver)
    readback, manifest_error = finalization.validate_manifest(
        manifest_path, [str(path) for path in artifact_paths],
        require_identity=bool(producer_task_id and producer_run_id is not None),
    )
    receiver_error = finalization.validate_receiver_copy(readback or {}, receiver_receipt_path, require_identity=True)
    if manifest_error or receiver_error:
        raise ValueError(manifest_error or receiver_error or "finalization readback failed")
    return {
        "status": "prepared",
        "manifest_path": str(manifest_path.resolve()),
        "receiver_receipt_path": str(receiver_receipt_path.resolve()),
        "artifact_set_id": manifest["artifact_set_id"],
        "artifact_count": len(artifact_paths),
        "receiver_readback": "hash/size/media-type matched actual distinct copy",
        "quality_receipt": "not_created; run the canonical checker separately",
        "completion": "not_submitted",
    }
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", required=True, help="absolute declared artifact; repeat per artifact")
    parser.add_argument("--copy", action="append", required=True, help="ARTIFACT=RECEIVED_COPY from actual delivery readback; repeat per artifact")
    parser.add_argument("--source", required=True, help="absolute authoritative source/readback file")
    parser.add_argument("--checker", default=str(ROOT / "check_docs_artifact.py"), help="canonical final-output checker path")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--receiver-receipt", required=True, type=Path)
    parser.add_argument("--producer-task-id", help="optional actual producer task identity")
    parser.add_argument("--producer-run-id", type=int, help="optional actual producer run ID")
    parser.add_argument("--receiver-task-id", required=True, help="actual receiving task identity")
    parser.add_argument("--receiver-run-id", required=True, type=int)
    args = parser.parse_args()
    try:
        result = prepare(
            artifacts=args.artifact, copies=args.copy, source=args.source,
            checker=args.checker, manifest_path=args.manifest,
            receiver_receipt_path=args.receiver_receipt,
            producer_task_id=args.producer_task_id,
            producer_run_id=args.producer_run_id,
            receiver_task_id=args.receiver_task_id, receiver_run_id=args.receiver_run_id,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
