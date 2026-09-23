"""Typed, body-free evidence records for the portable producer route."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

FORBIDDEN_KEYS = frozenset({
    "text", "body", "content", "payload", "raw", "value", "original", "rewritten",
    "response_text", "actual_value", "expected_value", "image", "images", "png", "jpeg",
    "media_bytes", "image_bytes",
})


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_body_free(value: Any) -> None:
    """Reject raw document/provider/image payloads from receipts."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"body-bearing evidence key is forbidden: {key}")
            assert_body_free(item)
    elif isinstance(value, list):
        for item in value:
            assert_body_free(item)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("binary evidence payloads are forbidden")


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


@dataclass(frozen=True)
class EvidenceBoundary:
    schema_version: str
    source_sha256: str
    checker_sha256: str
    ruleset_id: str
    ruleset_sha256: str
    style_preset_id: str
    style_preset_sha256: str
    renderer_sha256: str | None
    provider_mode: str
    image_transport: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        assert_body_free(result)
        return result
