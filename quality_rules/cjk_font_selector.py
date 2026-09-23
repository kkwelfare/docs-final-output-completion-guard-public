"""Shared installed UD-family selector for future docs-profile artifacts.

The selector uses fontconfig discovery plus real font-file readback. It never
returns a fabricated family and never silently substitutes a non-UD font.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from PIL import ImageFont

PRIMARY_ALIASES = (
    "BIZ UDPGothic",
    "BIZ UDPゴシック",
    "BIZ UD P Gothic",
    "UD P Gothic",
    "UDPGothic",
)
SECONDARY_ALIASES = (
    "BIZ UDGothic",
    "BIZ UDゴシック",
    "BIZ UD Gothic",
    "UD Gothic",
    "UDGothic",
)
# Japanese text is rendered only with an explicit family from this ordered
# allowlist. Generic Noto Sans CJK and SC/TC/KR faces are deliberately absent.
JAPANESE_FONT_GROUPS = (
    ("ud-p-gothic-primary", "primary_ud_p_gothic_selected; no_fallback_used", PRIMARY_ALIASES),
    ("ud-gothic-secondary", "primary_ud_p_gothic_unavailable; secondary_ud_gothic_selected", SECONDARY_ALIASES),
    ("noto-sans-jp-tertiary", "ud_gothic_families_unavailable; noto_sans_jp_selected", ("Noto Sans JP",)),
    ("noto-serif-jp-tertiary", "ud_gothic_families_unavailable; noto_serif_jp_selected", ("Noto Serif JP",)),
    ("ipa-gothic-tertiary", "ud_and_noto_jp_families_unavailable; ipa_gothic_selected", ("IPA Gothic", "IPAGothic")),
    ("ipa-mincho-tertiary", "ud_and_noto_jp_families_unavailable; ipa_mincho_selected", ("IPA Mincho", "IPAMincho")),
)


@dataclass(frozen=True)
class CjkFontSelection:
    family: str
    path: str
    sha256: str
    status: str
    priority: str
    fallback_decision: str
    fc_list_sources: tuple[str, ...]
    file_readback_family: str = ""
    file_readback_style: str = ""

    def receipt_fields(self) -> dict[str, object]:
        """Body-free fields required in producer receipts/logs."""
        return asdict(self)


def _normalized(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def docs_font_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment that exposes the OS user's installed fonts to docs renderers."""
    environment = dict(os.environ if base is None else base)
    user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    environment["XDG_DATA_HOME"] = str(user_home / ".local" / "share")
    return environment


def _fc_list_runs() -> list[tuple[str, dict[str, str]]]:
    current = dict(os.environ)
    runs = [("profile-environment", current)]
    os_user = docs_font_environment(current)
    user_data = os_user["XDG_DATA_HOME"]
    if current.get("XDG_DATA_HOME") != user_data:
        runs.append((f"os-user-xdg:{user_data}", os_user))
    return runs


def _fc_list_records() -> tuple[list[tuple[tuple[str, ...], str, str, str]], tuple[str, ...]]:
    records: list[tuple[tuple[str, ...], str, str, str]] = []
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()
    command = ["fc-list", "--format=%{family}\t%{style}\t%{file}\n"]
    for source, environment in _fc_list_runs():
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
        except (OSError, subprocess.CalledProcessError):
            sources.append(f"{source}:fc-list-failed")
            continue
        sources.append(f"{source}:fc-list-ok")
        for line in completed.stdout.splitlines():
            family_value, separator, remainder = line.partition("\t")
            if not separator:
                continue
            style, separator, file_value = remainder.partition("\t")
            path = Path(file_value).expanduser()
            key = (family_value, str(path))
            if not separator or key in seen or not path.is_file():
                continue
            seen.add(key)
            families = tuple(item.strip() for item in family_value.split(",") if item.strip())
            records.append((families, style.strip(), str(path.resolve()), source))
    return records, tuple(sources)


def _real_file_readback(path: Path) -> tuple[str, str] | None:
    try:
        family, style = ImageFont.truetype(str(path), 16).getname()
    except (OSError, TypeError, ValueError):
        return None
    if not family:
        return None
    return str(family), str(style)


def _matching_records(
    records: Iterable[tuple[tuple[str, ...], str, str, str]], aliases: tuple[str, ...]
) -> list[tuple[int, int, str, tuple[str, ...], str, str, str]]:
    alias_index = {_normalized(alias): index for index, alias in enumerate(aliases)}
    matches = []
    for families, style, path, source in records:
        indexes = [alias_index[_normalized(family)] for family in families if _normalized(family) in alias_index]
        if not indexes:
            continue
        readback = _real_file_readback(Path(path))
        if readback is None or _normalized(readback[0]) not in alias_index:
            continue
        regular_rank = 0 if "regular" in style.casefold() or "標準" in style else 1
        matches.append((min(indexes), regular_rank, path, families, style, source, readback[0] + "\t" + readback[1]))
    return sorted(matches, key=lambda item: (item[0], item[1], item[2]))


def select_cjk_font() -> CjkFontSelection:
    """Select an installed explicit Japanese allowlist family or fail closed."""
    records, sources = _fc_list_records()
    for priority, fallback_decision, aliases in JAPANESE_FONT_GROUPS:
        matches = _matching_records(records, aliases)
        if not matches:
            continue
        _, _, path_value, _, _, _, readback_value = matches[0]
        family, style = readback_value.split("\t", 1)
        path = Path(path_value)
        return CjkFontSelection(
            family=family,
            path=str(path),
            sha256=_sha256(path),
            status="ud-installed",
            priority=priority,
            fallback_decision=fallback_decision,
            fc_list_sources=sources,
            file_readback_family=family,
            file_readback_style=style,
        )
    return CjkFontSelection(
        family="",
        path="",
        sha256="",
        status="ud-unavailable",
        priority="none",
        fallback_decision="no_explicit_japanese_allowlist_font_available; generation_must_stop",
        fc_list_sources=sources,
    )


def expected_cjk_fonts(selection: CjkFontSelection | None = None) -> list[str]:
    """Return the contract default from the same selection, never a silent fallback."""
    selected = selection or select_cjk_font()
    if selected.status != "ud-installed" or not selected.family or not Path(selected.path).is_file():
        raise RuntimeError(selected.fallback_decision)
    return [selected.family]


def validate_artifact_font_evidence(handle: object, artifact: Path, expected_fonts: list[str]) -> list[str]:
    """Validate body-free selector/file evidence against one delivered artifact."""
    prefix = "cjk_font_evidence"
    if not isinstance(handle, dict):
        return [f"{prefix}_required"]
    raw_path, digest = handle.get("path"), handle.get("sha256")
    evidence_path = Path(raw_path) if isinstance(raw_path, str) else Path()
    if not evidence_path.is_absolute() or not evidence_path.is_file() or digest != _sha256(evidence_path):
        return [f"{prefix}_handle_mismatch"]
    try:
        data = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [f"{prefix}_malformed"]
    artifact = artifact.resolve()
    required = {
        "artifact_path": str(artifact), "artifact_sha256": _sha256(artifact),
        "selector_path": str(Path(__file__).resolve()), "selector_sha256": _sha256(Path(__file__).resolve()),
    }
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in required.items()):
        return [f"{prefix}_artifact_or_selector_mismatch"]
    selection = data.get("selection")
    if not isinstance(selection, dict):
        return [f"{prefix}_selection_missing"]
    font_path = Path(selection.get("path", ""))
    fields_valid = (
        selection.get("status") == "ud-installed"
        and isinstance(selection.get("family"), str) and selection["family"] in expected_fonts
        and font_path.is_absolute() and font_path.is_file()
        and selection.get("sha256") == _sha256(font_path)
        and isinstance(selection.get("priority"), str) and bool(selection["priority"])
        and isinstance(selection.get("fallback_decision"), str) and bool(selection["fallback_decision"])
        and selection.get("file_readback_family") == selection.get("family")
    )
    return [] if fields_valid else [f"{prefix}_selection_mismatch"]
