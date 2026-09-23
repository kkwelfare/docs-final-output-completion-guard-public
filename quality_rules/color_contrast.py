"""Generic contract-bound color selection and rendered contrast measurement."""
from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import Image

COLOR_POLICY_SCHEMA_VERSION = "docs-color-contrast-policy-1"
COLOR_APPLIED_SCHEMA_VERSION = "docs-color-contrast-policy-applied-1"
COLOR_EFFECTIVE_SCHEMA_VERSION = "docs-color-contrast-policy-effective-1"
COLOR_RECEIPT_SCHEMA_VERSION = "docs-rendered-color-receipt-1"
TOKEN_KINDS = frozenset({"foreground", "surface", "border"})
FOREGROUND_MODES = frozenset({"body-near-black", "contrast-select"})


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def normalize_hex(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        raise ValueError("color HEX must use #RRGGBB")
    try:
        int(value[1:], 16)
    except ValueError as exc:
        raise ValueError("color HEX must use #RRGGBB") from exc
    return value.upper()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    normalized = normalize_hex(value)
    return tuple(int(normalized[index:index + 2], 16) for index in (1, 3, 5))


def rgb_to_hex(value: Any) -> str:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 255 for item in value)):
        raise ValueError("color RGB must contain three integers from 0 to 255")
    return "#" + "".join(f"{item:02X}" for item in value)


def relative_luminance(rgb: tuple[int, int, int] | list[int]) -> float:
    channels = []
    for raw in rgb:
        value = float(raw) / 255.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(foreground: tuple[int, int, int] | list[int], surface: tuple[int, int, int] | list[int]) -> float:
    first, second = relative_luminance(foreground), relative_luminance(surface)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def foreground_mode(rgb: tuple[int, int, int] | list[int]) -> str:
    luminance = relative_luminance(rgb)
    if luminance <= 0.10:
        return "near-black"
    if luminance >= 0.90:
        return "near-white"
    return "other"


def canonicalize_color_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "palette", "tokens", "roles"}:
        raise ValueError("color policy declaration is malformed")
    if value.get("schema_version") != COLOR_POLICY_SCHEMA_VERSION:
        raise ValueError("color policy schema mismatch")
    palette = value.get("palette")
    if not isinstance(palette, dict) or not set(palette).issubset({"palette_id", "palette_seed", "selection_metadata"}):
        raise ValueError("color palette metadata is malformed")
    palette_id = _nonempty(palette.get("palette_id"), "palette_id")
    if "palette_seed" not in palette and "selection_metadata" not in palette:
        raise ValueError("color palette requires palette_seed or selection_metadata")
    normalized_palette: dict[str, Any] = {"palette_id": palette_id}
    if "palette_seed" in palette:
        seed = palette["palette_seed"]
        if isinstance(seed, bool) or not isinstance(seed, (str, int)) or (isinstance(seed, str) and not seed.strip()):
            raise ValueError("palette_seed must be a non-empty string or integer")
        normalized_palette["palette_seed"] = seed
    if "selection_metadata" in palette:
        metadata = palette["selection_metadata"]
        if not isinstance(metadata, dict) or not metadata:
            raise ValueError("selection_metadata must be a non-empty object")
        normalized_palette["selection_metadata"] = deepcopy(metadata)

    raw_tokens = value.get("tokens")
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise ValueError("color policy requires tokens")
    tokens: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for raw in raw_tokens:
        if not isinstance(raw, dict) or set(raw) != {"id", "kind", "hex", "rgb"}:
            raise ValueError("color token is malformed")
        token_id = _nonempty(raw.get("id"), "color token id")
        if token_id in seen_tokens:
            raise ValueError("color token IDs must be unique")
        kind = raw.get("kind")
        if kind not in TOKEN_KINDS:
            raise ValueError("color token kind is invalid")
        hex_value = normalize_hex(raw.get("hex"))
        rgb_value = list(hex_to_rgb(hex_value))
        if rgb_to_hex(raw.get("rgb")) != hex_value:
            raise ValueError("color token HEX/RGB mismatch")
        seen_tokens.add(token_id)
        tokens.append({"id": token_id, "kind": kind, "hex": hex_value, "rgb": rgb_value})
    by_id = {item["id"]: item for item in tokens}

    raw_roles = value.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ValueError("color policy requires roles")
    roles: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    role_fields = {
        "role", "foreground_mode", "foreground_token_ids", "surface_token_id", "border_token_id",
        "min_contrast_ratio", "preferred_contrast_ratio", "render_samples",
    }
    for raw in raw_roles:
        if not isinstance(raw, dict) or set(raw) != role_fields:
            raise ValueError("color role policy is malformed")
        role = _nonempty(raw.get("role"), "color role")
        if role in seen_roles:
            raise ValueError("color roles must be unique")
        mode = raw.get("foreground_mode")
        if mode not in FOREGROUND_MODES:
            raise ValueError("color foreground_mode is invalid")
        foreground_ids = raw.get("foreground_token_ids")
        if (not isinstance(foreground_ids, list) or not foreground_ids
                or any(not isinstance(item, str) or item not in by_id for item in foreground_ids)
                or len(set(foreground_ids)) != len(foreground_ids)):
            raise ValueError("foreground_token_ids are invalid")
        foreground_tokens = [by_id[item] for item in foreground_ids]
        if any(item["kind"] != "foreground" for item in foreground_tokens):
            raise ValueError("foreground role references a non-foreground token")
        surface_id = raw.get("surface_token_id")
        if surface_id not in by_id or by_id[surface_id]["kind"] != "surface":
            raise ValueError("surface_token_id must reference a surface token")
        border_id = raw.get("border_token_id")
        if border_id is not None and (border_id not in by_id or by_id[border_id]["kind"] != "border"):
            raise ValueError("border_token_id must reference a border token or null")
        minimum, preferred = raw.get("min_contrast_ratio"), raw.get("preferred_contrast_ratio")
        if (isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(float(minimum))
                or isinstance(preferred, bool) or not isinstance(preferred, (int, float)) or not math.isfinite(float(preferred))
                or float(minimum) <= 1 or float(preferred) < float(minimum) or float(preferred) > 21):
            raise ValueError("color contrast thresholds are invalid")
        modes = {foreground_mode(item["rgb"]) for item in foreground_tokens}
        if mode == "body-near-black":
            if len(foreground_tokens) != 1 or modes != {"near-black"}:
                raise ValueError("body color role requires exactly one near-black foreground")
        elif modes != {"near-black", "near-white"}:
            raise ValueError("contrast-select role requires near-black and near-white candidates")
        samples = raw.get("render_samples")
        if not isinstance(samples, list) or not samples:
            raise ValueError("color role requires rendered samples")
        normalized_samples: list[dict[str, Any]] = []
        seen_samples: set[str] = set()
        for sample in samples:
            if not isinstance(sample, dict) or set(sample) != {"sample_id", "render_index", "foreground_xy", "surface_xy"}:
                raise ValueError("rendered color sample is malformed")
            sample_id = _nonempty(sample.get("sample_id"), "rendered color sample id")
            render_index = sample.get("render_index")
            if sample_id in seen_samples or isinstance(render_index, bool) or not isinstance(render_index, int) or render_index < 1:
                raise ValueError("rendered color sample identity is invalid")
            coordinates: dict[str, list[float]] = {}
            for field in ("foreground_xy", "surface_xy"):
                xy = sample.get(field)
                if (not isinstance(xy, list) or len(xy) != 2
                        or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or not 0 <= float(item) <= 1 for item in xy)):
                    raise ValueError("rendered color sample coordinates must be normalized")
                coordinates[field] = [float(item) for item in xy]
            seen_samples.add(sample_id)
            normalized_samples.append({"sample_id": sample_id, "render_index": render_index, **coordinates})
        seen_roles.add(role)
        roles.append({
            "role": role, "foreground_mode": mode, "foreground_token_ids": sorted(foreground_ids),
            "surface_token_id": surface_id, "border_token_id": border_id,
            "min_contrast_ratio": float(minimum), "preferred_contrast_ratio": float(preferred),
            "render_samples": sorted(normalized_samples, key=lambda item: item["sample_id"]),
        })
    return {
        "schema_version": COLOR_POLICY_SCHEMA_VERSION,
        "palette": normalized_palette,
        "tokens": sorted(tokens, key=lambda item: item["id"]),
        "roles": sorted(roles, key=lambda item: item["role"]),
    }


def resolve_color_role(policy: Any, *, role: str, direct_values: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical = canonicalize_color_policy(policy)
    role = _nonempty(role, "color role")
    role_policy = next((item for item in canonical["roles"] if item["role"] == role), None)
    if role_policy is None:
        raise ValueError(f"unknown color role: {role}")
    by_id = {item["id"]: item for item in canonical["tokens"]}
    surface = by_id[role_policy["surface_token_id"]]
    candidates = [by_id[item] for item in role_policy["foreground_token_ids"]]
    ranked = sorted(
        ((contrast_ratio(item["rgb"], surface["rgb"]), item["id"], item) for item in candidates),
        key=lambda row: (-row[0], row[1]),
    )
    ratio, _token_id, selected = ranked[0]
    if ratio + 1e-9 < role_policy["min_contrast_ratio"]:
        if role_policy["foreground_mode"] == "body-near-black":
            raise ValueError("body near-black foreground does not meet surface contrast; change the surface or reject the color")
        raise ValueError("contrast-select role has no candidate meeting minimum contrast")
    concrete = {
        "foreground_hex": selected["hex"],
        "surface_hex": surface["hex"],
        "border_hex": by_id[role_policy["border_token_id"]]["hex"] if role_policy["border_token_id"] else None,
    }
    supplied = {} if direct_values is None else direct_values
    if not isinstance(supplied, dict) or any(key not in concrete for key in supplied):
        raise ValueError("direct color values are malformed")
    for key, expected in concrete.items():
        if key in supplied and supplied[key] is not None and normalize_hex(supplied[key]) != expected:
            raise ValueError(f"direct color drift: {key}")
    state = {
        "schema_version": COLOR_APPLIED_SCHEMA_VERSION,
        "role": role,
        "palette": canonical["palette"],
        "foreground_mode": role_policy["foreground_mode"],
        "selected_token_ids": {
            "foreground": selected["id"], "surface": surface["id"], "border": role_policy["border_token_id"],
        },
        "colors": concrete,
        "declared_contrast_ratio": round(ratio, 6),
        "min_contrast_ratio": role_policy["min_contrast_ratio"],
        "preferred_contrast_ratio": role_policy["preferred_contrast_ratio"],
    }
    return concrete, state


def effective_color_state(applied: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(applied)
    if value.get("schema_version") != COLOR_APPLIED_SCHEMA_VERSION:
        raise ValueError("applied color state schema mismatch")
    value["schema_version"] = COLOR_EFFECTIVE_SCHEMA_VERSION
    return value


def _sample_rgb(render: dict[str, Any], xy: list[float]) -> list[int]:
    path = Path(render.get("path", ""))
    with Image.open(path) as image:
        image.load()
        rgb = image.convert("RGB")
        x = min(rgb.width - 1, max(0, round(xy[0] * (rgb.width - 1))))
        y = min(rgb.height - 1, max(0, round(xy[1] * (rgb.height - 1))))
        return list(rgb.getpixel((x, y)))


def measure_rendered_colors(contract: dict[str, Any], renders: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any] | None:
    declaration = contract.get("declaration", {}).get("policy", {}).get("color_policy")
    if declaration is None:
        return None
    policy = canonicalize_color_policy(declaration)
    by_id = {item["id"]: item for item in policy["tokens"]}
    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for role_policy in policy["roles"]:
        _concrete, applied = resolve_color_role(policy, role=role_policy["role"])
        effective = effective_color_state(applied)
        expected_foreground = hex_to_rgb(effective["colors"]["foreground_hex"])
        expected_surface = hex_to_rgb(effective["colors"]["surface_hex"])
        for sample in role_policy["render_samples"]:
            if sample["render_index"] > len(renders):
                issues.append({"role": role_policy["role"], "sample_id": sample["sample_id"], "reason": "render_index_missing"})
                continue
            render = renders[sample["render_index"] - 1]
            measured_foreground = _sample_rgb(render, sample["foreground_xy"])
            measured_surface = _sample_rgb(render, sample["surface_xy"])
            ratio = contrast_ratio(measured_foreground, measured_surface)
            mode = foreground_mode(measured_foreground)
            foreground_delta = max(abs(a - b) for a, b in zip(measured_foreground, expected_foreground))
            surface_delta = max(abs(a - b) for a, b in zip(measured_surface, expected_surface))
            body_pass = mode == "near-black" if role_policy["foreground_mode"] == "body-near-black" else None
            minimum_pass = ratio + 1e-9 >= role_policy["min_contrast_ratio"]
            preferred_pass = ratio + 1e-9 >= role_policy["preferred_contrast_ratio"]
            drift_pass = foreground_delta <= 12 and surface_delta <= 12
            row = {
                "role": role_policy["role"], "sample_id": sample["sample_id"],
                "render_index": sample["render_index"], "requested": policy["palette"],
                "applied": applied, "effective": effective,
                "measured_foreground": {"rgb": measured_foreground, "hex": rgb_to_hex(measured_foreground)},
                "measured_surface": {"rgb": measured_surface, "hex": rgb_to_hex(measured_surface)},
                "contrast_ratio": round(ratio, 6), "foreground_mode": mode,
                "body_near_black_pass": body_pass,
                "minimum": {"threshold": role_policy["min_contrast_ratio"], "pass": minimum_pass},
                "preferred": {"threshold": role_policy["preferred_contrast_ratio"], "pass": preferred_pass},
                "identity_consistency": {"foreground_max_channel_delta": foreground_delta, "surface_max_channel_delta": surface_delta, "pass": drift_pass},
                "grayscale_readability": {
                    "foreground_luminance": round(relative_luminance(measured_foreground), 8),
                    "surface_luminance": round(relative_luminance(measured_surface), 8),
                    "contrast_ratio": round(ratio, 6), "pass": minimum_pass,
                },
            }
            results.append(row)
            if not minimum_pass:
                issues.append({"role": role_policy["role"], "sample_id": sample["sample_id"], "reason": "insufficient_rendered_contrast"})
            if body_pass is False:
                issues.append({"role": role_policy["role"], "sample_id": sample["sample_id"], "reason": "body_foreground_not_near_black"})
            if not drift_pass:
                issues.append({"role": role_policy["role"], "sample_id": sample["sample_id"], "reason": "rendered_color_drift"})
    return {
        "schema_version": COLOR_RECEIPT_SCHEMA_VERSION,
        "execution_identity": deepcopy(identity),
        "status": "HARD_FAIL" if issues else "pass",
        "palette": policy["palette"],
        "samples": results,
        "HARD_FAIL": issues,
    }


def validate_rendered_color_receipt(value: Any, contract: dict[str, Any], identity: dict[str, Any]) -> list[str]:
    has_policy = "color_policy" in contract.get("declaration", {}).get("policy", {})
    if not has_policy:
        return [] if value is None else ["rendered color receipt present without color policy"]
    if not isinstance(value, dict) or value.get("schema_version") != COLOR_RECEIPT_SCHEMA_VERSION:
        return ["rendered color receipt missing or schema mismatch"]
    if value.get("execution_identity") != identity:
        return ["rendered color receipt execution identity mismatch"]
    if value.get("status") != "pass" or value.get("HARD_FAIL") != []:
        return ["rendered color contrast HARD_FAIL"]
    samples = value.get("samples")
    policy = canonicalize_color_policy(contract["declaration"]["policy"]["color_policy"])
    expected = sum(len(item["render_samples"]) for item in policy["roles"])
    if not isinstance(samples, list) or len(samples) != expected:
        return ["rendered color sample coverage mismatch"]
    if any(not isinstance(item, dict) or not item.get("minimum", {}).get("pass")
           or item.get("body_near_black_pass") is False
           or not item.get("identity_consistency", {}).get("pass") for item in samples):
        return ["rendered color evidence failed"]
    return []
