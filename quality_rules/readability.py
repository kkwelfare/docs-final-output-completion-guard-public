"""Generic, hash-bound rendered readability and visual-quality evidence.

The module deliberately separates deterministic hard failures from review candidates.
It never calls a model: hard-fail and candidate paths are fully local and body-free.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any

import fitz
from PIL import Image, ImageChops, ImageFilter, ImageStat

try:
    from . import color_contrast
except ImportError:  # standalone plugin import path
    import importlib.util
    import sys
    _color_spec = importlib.util.spec_from_file_location(
        "docs_readability_color_contrast", Path(__file__).with_name("color_contrast.py")
    )
    if _color_spec is None or _color_spec.loader is None:
        raise
    color_contrast = importlib.util.module_from_spec(_color_spec)
    sys.modules[_color_spec.name] = color_contrast
    _color_spec.loader.exec_module(color_contrast)

POLICY_SCHEMA_VERSION = "docs-readability-policy-1"
RECEIPT_SCHEMA_VERSION = "docs-rendered-readability-receipt-1"
DISPOSITION_SCHEMA_VERSION = "docs-readability-review-disposition-1"
RULE_IDS = ("AQ-CONTRAST-01", "AQ-LEGIBILITY-01", "AQ-PRINT-01", "AQ-HIERARCHY-01", "AQ-VISUAL-01")
STATUSES = frozenset({"pass", "HARD_FAIL", "REVIEW_CANDIDATE", "NOT_VERIFIED", "NOT_APPLICABLE"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a trimmed non-empty string")
    return value


def _finite(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} is below minimum")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} exceeds maximum")
    return number


def canonicalize_policy(value: Any) -> dict[str, Any]:
    fields = {"schema_version", "rollout_mode", "required_evidence", "regions", "print_variants", "review_thresholds"}
    if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("readability policy declaration is malformed")
    mode = value.get("rollout_mode")
    if mode not in {"shadow", "enforced"} or not isinstance(value.get("required_evidence"), bool):
        raise ValueError("readability rollout_mode/required_evidence is invalid")
    raw_regions = value.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("readability policy requires regions")
    regions: list[dict[str, Any]] = []
    seen: set[str] = set()
    region_fields = {"region_id", "role", "render_index", "bbox_norm", "foreground_hex", "foreground_tolerance", "min_contrast_ratio", "min_font_size_pt"}
    for raw in raw_regions:
        if not isinstance(raw, dict) or set(raw) != region_fields:
            raise ValueError("readability region is malformed")
        region_id = _nonempty(raw.get("region_id"), "readability region_id")
        role = _nonempty(raw.get("role"), "readability role")
        if region_id in seen:
            raise ValueError("readability region IDs must be unique")
        index = raw.get("render_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise ValueError("readability render_index is invalid")
        bbox = raw.get("bbox_norm")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("readability bbox_norm requires four coordinates")
        normalized = [_finite(item, "readability bbox coordinate", minimum=0, maximum=1) for item in bbox]
        if normalized[2] <= normalized[0] or normalized[3] <= normalized[1]:
            raise ValueError("readability bbox_norm is empty")
        foreground = color_contrast.normalize_hex(raw.get("foreground_hex"))
        tolerance = _finite(raw.get("foreground_tolerance"), "foreground_tolerance", minimum=0, maximum=255)
        minimum = _finite(raw.get("min_contrast_ratio"), "min_contrast_ratio", minimum=1, maximum=21)
        min_font = _finite(raw.get("min_font_size_pt"), "min_font_size_pt", minimum=0.1)
        seen.add(region_id)
        regions.append({"region_id": region_id, "role": role, "render_index": index, "bbox_norm": normalized,
                        "foreground_hex": foreground, "foreground_tolerance": tolerance,
                        "min_contrast_ratio": minimum, "min_font_size_pt": min_font})
    variants = value.get("print_variants")
    if not isinstance(variants, dict) or set(variants) != {"grayscale", "scales"} or variants.get("grayscale") is not True:
        raise ValueError("readability print_variants must require grayscale")
    scales = variants.get("scales")
    if not isinstance(scales, list) or not scales:
        raise ValueError("readability print scales are required")
    normalized_scales = [_finite(item, "print scale", minimum=0.1, maximum=1.0) for item in scales]
    if normalized_scales != sorted(set(normalized_scales), reverse=True) or 1.0 not in normalized_scales:
        raise ValueError("print scales must be unique descending values including 1.0")
    thresholds = value.get("review_thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != {"dominant_visual_max_ratio", "glow_competition_max_ratio"}:
        raise ValueError("readability review_thresholds are malformed")
    normalized_thresholds = {key: _finite(thresholds[key], key, minimum=0, maximum=1) for key in thresholds}
    return {"schema_version": POLICY_SCHEMA_VERSION, "rollout_mode": mode,
            "required_evidence": value["required_evidence"],
            "regions": sorted(regions, key=lambda item: item["region_id"]),
            "print_variants": {"grayscale": True, "scales": normalized_scales},
            "review_thresholds": normalized_thresholds}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("empty percentile input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _weighted_percentile(values: list[tuple[float, int]], fraction: float) -> float:
    """Return an exact percentile without expanding a rendered-color histogram."""
    if not values or any(count < 1 for _, count in values):
        raise ValueError("empty weighted percentile input")
    ordered = sorted(values, key=lambda item: item[0])
    total = sum(count for _, count in ordered)
    position = (total - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)

    def at_rank(rank: int) -> float:
        covered = 0
        for value, count in ordered:
            covered += count
            if rank < covered:
                return value
        return ordered[-1][0]

    lower_value, upper_value = at_rank(lower), at_rank(upper)
    return lower_value + (upper_value - lower_value) * (position - lower)


def _crop(image: Image.Image, bbox: list[float]) -> Image.Image:
    x1 = max(0, min(image.width - 1, int(math.floor(bbox[0] * image.width))))
    y1 = max(0, min(image.height - 1, int(math.floor(bbox[1] * image.height))))
    x2 = max(x1 + 1, min(image.width, int(math.ceil(bbox[2] * image.width))))
    y2 = max(y1 + 1, min(image.height, int(math.ceil(bbox[3] * image.height))))
    return image.convert("RGB").crop((x1, y1, x2, y2))


def _contrast_metrics(crop: Image.Image, foreground_hex: str, tolerance: float) -> dict[str, Any] | None:
    expected = color_contrast.hex_to_rgb(foreground_hex)
    pixels = list(crop.get_flattened_data())
    foreground = [pixel for pixel in pixels if max(abs(pixel[i] - expected[i]) for i in range(3)) <= tolerance]
    background = [pixel for pixel in pixels if max(abs(pixel[i] - expected[i]) for i in range(3)) > tolerance]
    if not foreground or not background:
        return None
    # Preserve the complete weighted histogram for colors with enough spatial
    # support to represent a surface.  This excludes isolated glyph-antialiasing
    # fringes without a per-color cap or dominant-coverage cutoff that could hide
    # a smaller glow/gradient region behind a high-frequency surface color.
    counts: dict[tuple[int, int, int], int] = {}
    for pixel in background:
        counts[pixel] = counts.get(pixel, 0) + 1
    minimum_surface_support = max(2, math.ceil(len(background) * 0.001))
    surface_counts = {pixel: count for pixel, count in counts.items() if count >= minimum_surface_support}
    if not surface_counts:
        surface_counts = counts
    weighted_ratios = [(color_contrast.contrast_ratio(expected, pixel), count)
                       for pixel, count in surface_counts.items()]
    represented_background = sum(count for _, count in surface_counts.items())
    return {"foreground_pixel_count": len(foreground), "background_sample_count": represented_background,
            "min": round(min(value for value, _ in weighted_ratios), 6),
            "p05": round(_weighted_percentile(weighted_ratios, 0.05), 6),
            "median": round(_weighted_percentile(weighted_ratios, 0.5), 6)}


def _physical_text_metrics(artifact: Path, region: dict[str, Any]) -> dict[str, Any] | None:
    if artifact.suffix.lower() != ".pdf":
        return None
    try:
        with fitz.open(artifact) as document:
            page_index = region["render_index"] - 1
            if page_index >= len(document):
                return None
            page = document[page_index]
            bbox = region["bbox_norm"]
            target = fitz.Rect(bbox[0] * page.rect.width, bbox[1] * page.rect.height,
                               bbox[2] * page.rect.width, bbox[3] * page.rect.height)
            sizes: list[float] = []
            cjk_span_count = 0
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        span_box = fitz.Rect(span.get("bbox", (0, 0, 0, 0)))
                        if span_box.intersects(target) and isinstance(span.get("size"), (int, float)):
                            sizes.append(float(span["size"]))
                            text = str(span.get("text", ""))
                            if any("\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" for char in text):
                                cjk_span_count += 1
            if not sizes:
                return None
            return {"basis": "pdf_points", "span_count": len(sizes), "cjk_span_count": cjk_span_count,
                    "font_size_min_pt": round(min(sizes), 4), "font_size_median_pt": round(median(sizes), 4)}
    except Exception:
        return None


def _variant_record(path: Path, image: Image.Image, *, kind: str, scale: float) -> dict[str, Any]:
    normalized = image.convert("RGBA")
    return {"kind": kind, "scale": scale, "path": str(path.resolve()), "sha256": _sha256(path),
            "width": image.width, "height": image.height,
            "pixel_sha256": hashlib.sha256(normalized.tobytes()).hexdigest()}


def _variant_foreground_hex(foreground_hex: str, kind: str) -> str:
    if kind != "grayscale":
        return foreground_hex
    expected = color_contrast.hex_to_rgb(foreground_hex)
    gray = Image.new("RGB", (1, 1), expected).convert("L").getpixel((0, 0))
    return color_contrast.rgb_to_hex((gray, gray, gray))


def _measure_variant_regions(image: Image.Image, *, kind: str, scale: float, render_index: int,
                             policy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    measurements: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    not_verified: list[dict[str, Any]] = []
    for region in policy["regions"]:
        if region["render_index"] != render_index:
            continue
        crop = _crop(image, region["bbox_norm"])
        contrast = _contrast_metrics(
            crop, _variant_foreground_hex(region["foreground_hex"], kind), region["foreground_tolerance"],
        )
        if contrast is None:
            status, reason = "NOT_VERIFIED", "variant_foreground_or_background_not_detected"
            not_verified.append({"render_index": render_index, "region_id": region["region_id"],
                                 "kind": kind, "scale": scale, "reason": reason})
        elif contrast["min"] + 1e-9 < region["min_contrast_ratio"]:
            status, reason = "HARD_FAIL", "variant_minimum_rendered_contrast_below_threshold"
            hard_failures.append({"render_index": render_index, "region_id": region["region_id"],
                                  "kind": kind, "scale": scale, "reason": reason})
        else:
            status, reason = "pass", None
        measurements.append({"region_id": region["region_id"], "status": status,
                             "reason": reason, "contrast": contrast})
    return measurements, hard_failures, not_verified


def _make_variants(renders: list[dict[str, Any]], policy: dict[str, Any], evidence_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    not_verified: list[dict[str, Any]] = []
    for index, render in enumerate(renders, start=1):
        path = Path(str(render.get("path", "")))
        try:
            with Image.open(path) as opened:
                native = opened.convert("RGB"); native.load()
            native_measurements, native_failures, native_not_verified = _measure_variant_regions(
                native, kind="native", scale=1.0, render_index=index, policy=policy,
            )
            records.append({"kind": "native", "scale": 1.0, "path": str(path.resolve()),
                            "sha256": _sha256(path), "width": native.width, "height": native.height,
                            "pixel_sha256": hashlib.sha256(native.convert("RGBA").tobytes()).hexdigest(),
                            "render_index": index, "measurements": native_measurements})
            failures.extend(native_failures); not_verified.extend(native_not_verified)
            gray = native.convert("L")
            gray_path = evidence_dir / f"readability-render-{index:04d}-grayscale.png"
            gray.save(gray_path)
            gray_measurements, gray_failures, gray_not_verified = _measure_variant_regions(
                gray, kind="grayscale", scale=1.0, render_index=index, policy=policy,
            )
            records.append({**_variant_record(gray_path, gray, kind="grayscale", scale=1.0),
                            "render_index": index, "measurements": gray_measurements})
            failures.extend(gray_failures); not_verified.extend(gray_not_verified)
            for scale in policy["print_variants"]["scales"]:
                if scale == 1.0:
                    continue
                resized = native.resize((max(1, round(native.width * scale)), max(1, round(native.height * scale))), Image.Resampling.LANCZOS)
                scale_path = evidence_dir / f"readability-render-{index:04d}-scale-{scale:.4f}.png"
                resized.save(scale_path)
                scale_measurements, scale_failures, scale_not_verified = _measure_variant_regions(
                    resized, kind="scale", scale=scale, render_index=index, policy=policy,
                )
                records.append({**_variant_record(scale_path, resized, kind="scale", scale=scale),
                                "render_index": index, "measurements": scale_measurements})
                failures.extend(scale_failures); not_verified.extend(scale_not_verified)
        except Exception:
            failures.append({"render_index": index, "reason": "variant_generation_failed"})
    for region in policy["regions"]:
        if region["render_index"] > len(renders):
            not_verified.append({"render_index": region["render_index"], "region_id": region["region_id"],
                                 "reason": "variant_render_mapping_missing"})
    return records, failures, not_verified


def _visual_metrics(crop: Image.Image) -> dict[str, float]:
    rgb = crop.convert("RGB")
    pixels = list(rgb.get_flattened_data())
    saturation_dominant = 0
    for red, green, blue in pixels:
        maximum, minimum = max(red, green, blue), min(red, green, blue)
        saturation = 0 if maximum == 0 else (maximum - minimum) / maximum
        if saturation >= 0.55 or maximum <= 35:
            saturation_dominant += 1
    blurred = rgb.convert("L").filter(ImageFilter.GaussianBlur(radius=max(1, min(rgb.size) / 40)))
    residual = ImageStat.Stat(ImageChops.difference(rgb.convert("L"), blurred)).mean[0]
    # Edge/highlight competition is expressed as a stable normalized residual.
    glow = min(1.0, float(residual) / 255.0)
    return {"dominant_visual_ratio": round(saturation_dominant / max(1, len(pixels)), 6),
            "glow_competition_ratio": round(glow, 6)}


def _load_disposition(path: Path | None, candidates: list[dict[str, Any]], identity: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if path is None:
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["review_disposition_unreadable"]
    candidate_ids = sorted(item["candidate_id"] for item in candidates)
    if (not isinstance(data, dict) or data.get("schema_version") != DISPOSITION_SCHEMA_VERSION
            or data.get("execution_identity") != identity or data.get("candidate_set_hash") != _digest(candidate_ids)):
        return None, ["review_disposition_identity_mismatch"]
    rows = data.get("dispositions")
    if not isinstance(rows, list) or sorted(item.get("candidate_id") for item in rows if isinstance(item, dict)) != candidate_ids:
        return None, ["review_disposition_coverage_mismatch"]
    for row in rows:
        if row.get("disposition") not in {"accepted", "rejected"} or not isinstance(row.get("rationale_code"), str) or not row["rationale_code"]:
            return None, ["review_disposition_invalid"]
    return {"path": str(path.resolve()), "sha256": _sha256(path)}, []


def measure(contract: dict[str, Any], renders: list[dict[str, Any]], identity: dict[str, Any], *, artifact: Path,
            evidence_dir: Path, review_disposition_path: Path | None = None) -> dict[str, Any] | None:
    declaration = contract.get("declaration", {}).get("policy", {}).get("readability_policy")
    if declaration is None:
        return None
    policy = canonicalize_policy(declaration)
    regions: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    not_verified: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for region in policy["regions"]:
        if region["render_index"] > len(renders):
            not_verified.append({"rule_id": "AQ-CONTRAST-01", "region_id": region["region_id"], "reason": "render_mapping_missing"})
            not_verified.append({"rule_id": "AQ-LEGIBILITY-01", "region_id": region["region_id"], "reason": "render_mapping_missing"})
            continue
        render = renders[region["render_index"] - 1]
        try:
            with Image.open(render["path"]) as image:
                image.load(); crop = _crop(image, region["bbox_norm"])
        except Exception:
            not_verified.append({"rule_id": "AQ-CONTRAST-01", "region_id": region["region_id"], "reason": "render_unreadable"})
            continue
        contrast = _contrast_metrics(crop, region["foreground_hex"], region["foreground_tolerance"])
        physical = _physical_text_metrics(artifact, region)
        visual = _visual_metrics(crop)
        row = {"region_id": region["region_id"], "role": region["role"], "render_index": region["render_index"],
               "bbox_norm": region["bbox_norm"], "contrast": contrast, "physical_text": physical, "visual": visual}
        regions.append(row)
        if contrast is None:
            not_verified.append({"rule_id": "AQ-CONTRAST-01", "region_id": region["region_id"], "reason": "foreground_or_background_not_detected"})
        elif contrast["min"] + 1e-9 < region["min_contrast_ratio"]:
            hard_failures.append({"rule_id": "AQ-CONTRAST-01", "region_id": region["region_id"], "reason": "minimum_rendered_contrast_below_threshold"})
        if physical is None:
            not_verified.append({"rule_id": "AQ-LEGIBILITY-01", "region_id": region["region_id"], "reason": "physical_text_mapping_missing"})
        elif physical["font_size_min_pt"] + 1e-9 < region["min_font_size_pt"]:
            hard_failures.append({"rule_id": "AQ-LEGIBILITY-01", "region_id": region["region_id"], "reason": "physical_font_size_below_threshold"})
        if visual["dominant_visual_ratio"] > policy["review_thresholds"]["dominant_visual_max_ratio"]:
            candidates.append({"candidate_id": _digest(["AQ-HIERARCHY-01", region["region_id"]])[:24], "rule_id": "AQ-HIERARCHY-01",
                               "region_id": region["region_id"], "reason": "dominant_visual_competition"})
        if visual["glow_competition_ratio"] > policy["review_thresholds"]["glow_competition_max_ratio"]:
            candidates.append({"candidate_id": _digest(["AQ-VISUAL-01", region["region_id"]])[:24], "rule_id": "AQ-VISUAL-01",
                               "region_id": region["region_id"], "reason": "glow_competition"})
    variants, variant_failures, variant_not_verified = _make_variants(renders, policy, evidence_dir)
    hard_failures.extend({"rule_id": "AQ-PRINT-01", **item} for item in variant_failures)
    not_verified.extend({"rule_id": "AQ-PRINT-01", **item} for item in variant_not_verified)
    disposition, disposition_errors = _load_disposition(review_disposition_path, candidates, identity)
    not_verified.extend({"rule_id": "AQ-HIERARCHY-01", "reason": reason} for reason in disposition_errors)

    def status(rule_id: str) -> str:
        if any(item.get("rule_id") == rule_id for item in hard_failures):
            return "HARD_FAIL"
        if rule_id == "AQ-PRINT-01":
            return "NOT_VERIFIED" if any(item.get("rule_id") == rule_id for item in not_verified) else ("pass" if variants else "NOT_VERIFIED")
        if rule_id in {"AQ-HIERARCHY-01", "AQ-VISUAL-01"}:
            relevant = [item for item in candidates if item["rule_id"] == rule_id]
            if not relevant:
                return "pass"
            return "pass" if disposition is not None else "REVIEW_CANDIDATE"
        if any(item.get("rule_id") == rule_id for item in not_verified):
            return "NOT_VERIFIED"
        return "pass"

    outcomes = [{"rule_id": rule_id, "status": status(rule_id)} for rule_id in RULE_IDS]
    return {"schema_version": RECEIPT_SCHEMA_VERSION, "execution_identity": identity,
            "policy_hash": _digest(policy), "rollout_mode": policy["rollout_mode"],
            "required_evidence": policy["required_evidence"], "artifact_path": str(artifact.resolve()),
            "artifact_sha256": _sha256(artifact), "outcomes": outcomes, "regions": regions,
            "variants": variants, "HARD_FAIL": hard_failures, "NOT_VERIFIED": not_verified,
            "REVIEW_CANDIDATE": candidates, "review_disposition": disposition,
            "model_invoked_on_hard_fail_path": False}


def validate_receipt(value: Any, contract: dict[str, Any], identity: dict[str, Any], *, artifact: Path) -> list[str]:
    declaration = contract.get("declaration", {}).get("policy", {}).get("readability_policy")
    if declaration is None:
        return [] if value is None else ["readability receipt present without readability policy"]
    policy = canonicalize_policy(declaration)
    if not isinstance(value, dict) or value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return ["readability receipt missing or schema mismatch"]
    errors: list[str] = []
    if value.get("execution_identity") != identity or value.get("policy_hash") != _digest(policy):
        errors.append("readability receipt execution/policy identity mismatch")
    if value.get("artifact_path") != str(artifact.resolve()) or value.get("artifact_sha256") != _sha256(artifact):
        errors.append("readability receipt artifact/hash mismatch")
    if value.get("rollout_mode") != policy["rollout_mode"] or value.get("required_evidence") is not policy["required_evidence"]:
        errors.append("readability receipt rollout mismatch")
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list) or {item.get("rule_id") for item in outcomes if isinstance(item, dict)} != set(RULE_IDS):
        errors.append("readability outcome coverage mismatch")
        return errors
    by_rule = {item["rule_id"]: item.get("status") for item in outcomes}
    if any(status not in STATUSES for status in by_rule.values()):
        errors.append("readability outcome status invalid")
    if value.get("model_invoked_on_hard_fail_path") is not False:
        errors.append("readability hard-fail path must not invoke a model")
    if policy["rollout_mode"] == "enforced":
        for rule_id, outcome in by_rule.items():
            if outcome not in {"pass", "NOT_APPLICABLE"}:
                errors.append(f"{rule_id}: enforced readability outcome {outcome}")
    elif any(outcome == "HARD_FAIL" for outcome in by_rule.values()):
        errors.append("readability HARD_FAIL")
    if policy["required_evidence"] and any(outcome == "NOT_VERIFIED" for outcome in by_rule.values()):
        errors.append("readability required evidence NOT_VERIFIED")
    for variant in value.get("variants", []):
        path = Path(str(variant.get("path", ""))) if isinstance(variant, dict) else Path()
        if not path.is_absolute() or not path.is_file() or variant.get("sha256") != _sha256(path):
            errors.append("readability variant path/hash drift")
            break
        try:
            with Image.open(path) as image:
                image.load()
                expected_measurements, _, _ = _measure_variant_regions(
                    image, kind=variant.get("kind"), scale=variant.get("scale"),
                    render_index=variant.get("render_index"), policy=policy,
                )
            if variant.get("measurements") != expected_measurements:
                errors.append("readability variant measurement drift")
                break
        except Exception:
            errors.append("readability variant measurement unreadable")
            break
    return list(dict.fromkeys(errors))
