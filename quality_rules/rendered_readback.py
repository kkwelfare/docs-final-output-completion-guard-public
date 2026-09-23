"""Deterministic, hash-bound rendered-image readback for user-facing artifacts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageStat

try:
    from . import color_contrast, execution_contract, readability, relative_anchor_gap
except ImportError:  # standalone fixture/import path
    package_root = str(Path(__file__).resolve().parents[1])
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    from quality_rules import color_contrast, execution_contract, readability, relative_anchor_gap

SCHEMA_VERSION = "docs-rendered-readback-1"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
SCREENSHOT_SUFFIXES = frozenset({".html", ".htm", ".svg"})
OFFICE_SUFFIXES = frozenset({".pptx", ".docx", ".xlsx", ".odt", ".ods"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_sha256(image: Image.Image) -> str:
    normalized = image.convert("RGBA")
    return hashlib.sha256(normalized.tobytes()).hexdigest()


def _image_record(path: Path, *, page: int | None = None) -> dict[str, Any]:
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        if width < 1 or height < 1:
            raise ValueError("empty rendered image")
        record: dict[str, Any] = {
            "path": str(path.resolve()),
            "sha256": _sha256(path),
            "width": width,
            "height": height,
            "pixel_sha256": _pixel_sha256(image),
        }
    if page is not None:
        record["page"] = page
    return record


def _write_manifest(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _surface_candidate_id(artifact_sha256: str, region_id: str, rule: str) -> str:
    seed = json.dumps([artifact_sha256, region_id, rule], separators=(",", ":")).encode()
    return "surface-" + hashlib.sha256(seed).hexdigest()[:16]


def measure_relative_anchor_gap_artifact(
    *, artifact: Path, output_path: Path, execution_identity: dict[str, Any], annotation_id: str,
    source_annotation_map: Path, requested: dict[str, Any], applied: dict[str, Any],
    tail_tip: list[float], avatar_search_bbox: list[float], facing_actual: str,
    points_per_pixel: float | None = None, page: int = 1,
) -> Path:
    """Independently reopen one final PDF/PNG and write body-free relative-gap evidence."""
    policy = relative_anchor_gap.canonicalize_contract(requested)
    if annotation_id != policy["annotation_id"] or relative_anchor_gap.sha256(source_annotation_map) != policy["source_annotation_map_sha256"]:
        raise ValueError("relative anchor-gap readback source identity drift")
    suffix = artifact.suffix.lower()
    common = {
        "artifact": artifact, "direction_vector": policy["direction_vector"],
        "target_gap_ratio": policy["target_gap_ratio"], "tolerance": policy["tolerance"],
        "facing_expected": policy["facing"], "facing_actual": facing_actual,
        "no_contact": policy["no_contact"], "contact_tolerance_pt": policy["contact_tolerance_pt"],
    }
    if suffix == ".png":
        if points_per_pixel is None:
            raise ValueError("PNG relative anchor-gap readback requires points_per_pixel")
        effective = relative_anchor_gap.measure_png(
            **common, avatar_search_bbox_pixels=avatar_search_bbox, tail_tip_pixels=tail_tip,
            points_per_pixel=points_per_pixel,
        )
        format_name = "png"
    elif suffix == ".pdf":
        effective = relative_anchor_gap.measure_pdf(
            **common, page=page, avatar_search_bbox_pdf_points=avatar_search_bbox,
            tail_tip_pdf_points=tail_tip,
        )
        format_name = "pdf"
    else:
        raise ValueError("independent relative anchor-gap readback supports PDF or PNG")
    evidence = relative_anchor_gap.build_evidence(
        execution_identity=execution_identity, annotation_id=annotation_id,
        source_path=source_annotation_map, source_annotation_map_sha256=policy["source_annotation_map_sha256"],
        artifact=artifact, format_name=format_name, requested=policy, applied=applied, effective=effective,
    )
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return _write_manifest(output_path, evidence)


def attach_geometry_relation_readback(
    manifest_path: Path, content_block_evidence_path: Path, *, output_path: Path | None = None,
    relative_evidence_paths: list[Path] | None = None,
) -> Path:
    """Attach hash-bound generic relation mappings to the existing readback manifest."""
    manifest_path = manifest_path.resolve(strict=True)
    evidence_path = content_block_evidence_path.resolve(strict=True)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(evidence, dict) or evidence.get("schema_version") != "docs-content-block-evidence-1"):
        raise ValueError("geometry relation readback input schema mismatch")
    artifact = evidence.get("artifact")
    if (not isinstance(artifact, dict) or artifact.get("path") != data.get("artifact_path")
            or artifact.get("sha256") != data.get("artifact_sha256")):
        raise ValueError("geometry relation evidence artifact identity mismatch")
    identity = evidence.get("execution_identity")
    if not isinstance(identity, dict) or set(identity) != {"contract_id", "policy_hash", "producer_run_id", "artifact_set_id"}:
        raise ValueError("geometry relation evidence execution identity missing")
    if data.get("execution_identity") is not None and data.get("execution_identity") != identity:
        raise ValueError("geometry relation rendered readback identity drift")
    data.setdefault("execution_identity", identity)
    data.setdefault("producer_run_id", identity["producer_run_id"])
    data.setdefault("artifact_set_id", identity["artifact_set_id"])
    if (data.get("producer_run_id") != identity["producer_run_id"]
            or data.get("artifact_set_id") != identity["artifact_set_id"]):
        raise ValueError("geometry relation rendered readback run/artifact-set drift")
    relative_by_annotation: dict[str, dict[str, Any]] = {}
    for raw_path in relative_evidence_paths or []:
        path = raw_path.resolve(strict=True)
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("schema_version") != relative_anchor_gap.EVIDENCE_SCHEMA_VERSION
                or record.get("execution_identity") != identity
                or record.get("artifact") != {"path": data["artifact_path"], "sha256": data["artifact_sha256"]}
                or record.get("evidence_sha256") != hashlib.sha256(json.dumps({key: value for key, value in record.items() if key != "evidence_sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()):
            raise ValueError("relative anchor-gap rendered evidence identity/hash drift")
        annotation_id = record.get("annotation_id")
        if not isinstance(annotation_id, str) or annotation_id in relative_by_annotation:
            raise ValueError("relative anchor-gap rendered evidence annotation mapping is ambiguous")
        relative_by_annotation[annotation_id] = {"path": str(path), "sha256": _sha256(path), "record": record}
    relations: list[dict[str, Any]] = []
    for relation in evidence.get("relations", []):
        if not isinstance(relation, dict) or relation.get("type") not in {"anchor_gap", "connector_route", "layer_relation", "content_inset"}:
            continue
        applied, effective = relation.get("applied"), relation.get("effective")
        if not isinstance(applied, dict) or not isinstance(effective, dict):
            raise ValueError("geometry relation applied/effective mapping is incomplete")
        mappings = effective.get("primitive_mappings")
        if not isinstance(mappings, list) or not mappings:
            raise ValueError("geometry relation rendered primitive mapping is missing")
        item = {
            "relation_id": relation.get("id"), "type": relation.get("type"),
            "format": applied.get("format"), "adapter": applied.get("adapter"),
            "source_order_pass": applied.get("source_order_pass"),
            "source_evidence_sha256": applied.get("source_evidence_sha256"),
            "measurement_basis": effective.get("measurement_basis"),
            "status": effective.get("status"), "evidence_sha256": effective.get("evidence_sha256"),
            "primitive_mappings": mappings,
        }
        declaration = relation.get("constraints", {}).get("anchor_gap") if relation.get("type") == "anchor_gap" else None
        if isinstance(declaration, dict) and declaration.get("measurement_model") == relative_anchor_gap.MEASUREMENT_MODEL:
            attached = relative_by_annotation.get(declaration.get("annotation_id"))
            if attached is None:
                raise ValueError("relative anchor-gap independent rendered evidence is missing")
            record = attached["record"]
            if (record.get("source", {}).get("sha256") != declaration.get("source_annotation_map_sha256")
                    or record.get("requested") != relative_anchor_gap.canonicalize_contract(declaration)
                    or record.get("effective", {}).get("status") != "pass"):
                raise ValueError("relative anchor-gap independent rendered evidence mismatch")
            item["relative_anchor_gap"] = {"evidence": {"path": attached["path"], "sha256": attached["sha256"]},
                                           "annotation_id": record["annotation_id"], "format": record["format"],
                                           "effective": record["effective"]}
        relations.append(item)
    if not relations:
        raise ValueError("geometry relation evidence has no mapped relations")
    data["geometry_relation_readback"] = {
        "schema_version": "docs-rendered-geometry-relation-readback-1",
        "artifact_path": data["artifact_path"], "artifact_sha256": data["artifact_sha256"],
        "execution_identity": identity,
        "content_block_evidence": {"path": str(evidence_path), "sha256": _sha256(evidence_path)},
        "relation_count": len(relations), "relations": sorted(relations, key=lambda item: str(item["relation_id"])),
    }
    return _write_manifest((output_path or manifest_path).resolve(), data)


def attach_surface_readback(manifest_path: Path, composition_evidence_path: Path, *, output_path: Path | None = None) -> Path:
    """Attach optional pixel-derived surface facts to the existing delivery manifest.

    Missing/ambiguous pixel mappings are review candidates. Only identity, hash,
    schema, containment, and impossible geometry become hard failures.
    """
    manifest_path = manifest_path.resolve(strict=True)
    composition_evidence_path = composition_evidence_path.resolve(strict=True)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    composition = json.loads(composition_evidence_path.read_text(encoding="utf-8"))
    surfaces = composition.get("semantic_surfaces") if isinstance(composition, dict) else None
    if not isinstance(surfaces, dict):
        return manifest_path
    if composition.get("artifact") != {"path": data.get("artifact_path"), "sha256": data.get("artifact_sha256")}:
        raise ValueError("SURFACE-HASH-01: composition artifact/hash mismatch")
    if data.get("execution_identity") != surfaces.get("execution_identity"):
        raise ValueError("SURFACE-IDENTITY-01: composition/render identity mismatch")
    if surfaces.get("schema_version") != "docs-semantic-surface-composition-evidence-1" or not isinstance(surfaces.get("regions"), list):
        raise ValueError("SURFACE-SCHEMA-01: composition surface schema mismatch")
    renders = {item.get("page", 1): item for item in data.get("renders", []) if isinstance(item, dict)}
    page_sizes = {page.get("page", 0): page.get("page_size_pt") for page in composition.get("pages", []) if isinstance(page, dict)}
    facts: list[dict[str, Any]] = []
    hard_fail: list[dict[str, Any]] = list(surfaces.get("HARD_FAIL", [])) if isinstance(surfaces.get("HARD_FAIL"), list) else [{"rule_id": "SURFACE-SCHEMA-01"}]
    review: list[dict[str, Any]] = []
    artifact_sha = str(data.get("artifact_sha256", ""))
    for region in surfaces["regions"]:
        if not isinstance(region, dict) or not isinstance(region.get("region_id"), str):
            hard_fail.append({"rule_id": "SURFACE-SCHEMA-01"})
            continue
        region_id, page = region["region_id"], region.get("page_index")
        bbox, shape_id = region.get("bbox_pt"), region.get("shape_id")
        if bbox is None and region.get("effective", {}).get("topology") == "none":
            facts.append({"region_id": region_id, "page_index": page, "shape_id": None, "bbox_pt": None,
                          "surface_present": False, "relative_strength": None, "status": "pass"})
            continue
        render, page_size = renders.get(page + 1 if isinstance(page, int) else -1), page_sizes.get(page + 1 if isinstance(page, int) else -1)
        if bbox is None or render is None or not isinstance(page_size, list) or len(page_size) != 2:
            review.append({"id": _surface_candidate_id(artifact_sha, region_id, "semantic_surface_mapping_unavailable"),
                           "rule": "semantic_surface_mapping_unavailable", "region_id": region_id,
                           "page": page + 1 if isinstance(page, int) else None, "shape_ids": [shape_id] if shape_id else [],
                           "bbox_pdf_points": bbox})
            continue
        if (not isinstance(bbox, list) or len(bbox) != 4 or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in bbox)
                or bbox[2] <= bbox[0] or bbox[3] <= bbox[1] or bbox[0] < 0 or bbox[1] < 0
                or bbox[2] > page_size[0] or bbox[3] > page_size[1]):
            hard_fail.append({"rule_id": "SURFACE-GEOMETRY-01", "region_id": region_id, "shape_id": shape_id, "bbox_pt": bbox})
            continue
        image_path = Path(str(render.get("path", "")))
        try:
            with Image.open(image_path) as image:
                image.load(); rgb = image.convert("RGB")
                sx, sy = rgb.width / page_size[0], rgb.height / page_size[1]
                pixel_box = (max(0, round(bbox[0] * sx)), max(0, round(bbox[1] * sy)),
                             min(rgb.width, round(bbox[2] * sx)), min(rgb.height, round(bbox[3] * sy)))
                if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
                    raise ValueError("empty pixel crop")
                crop = rgb.crop(pixel_box).convert("L")
                crop_stat, page_stat = ImageStat.Stat(crop), ImageStat.Stat(rgb.convert("L"))
                mean = round(crop_stat.mean[0] / 255.0, 6)
                page_mean = round(page_stat.mean[0] / 255.0, 6)
                relative = round(abs(mean - page_mean), 6)
                variance = round(crop_stat.var[0] / (255.0 * 255.0), 6)
        except Exception:
            review.append({"id": _surface_candidate_id(artifact_sha, region_id, "semantic_surface_mapping_unavailable"),
                           "rule": "semantic_surface_mapping_unavailable", "region_id": region_id, "page": page + 1,
                           "shape_ids": [shape_id] if shape_id else [], "bbox_pdf_points": bbox})
            continue
        status = "pass" if relative >= 0.01 or variance >= 0.0005 else "review"
        facts.append({"region_id": region_id, "page_index": page, "shape_id": shape_id, "bbox_pt": bbox,
                      "surface_present": True, "relative_strength": {"mean_luminance": mean,
                      "page_mean_luminance": page_mean, "absolute_luminance_delta": relative,
                      "normalized_pixel_variance": variance}, "status": status})
        if status == "review":
            review.append({"id": _surface_candidate_id(artifact_sha, region_id, "semantic_surface_relative_strength_ambiguous"),
                           "rule": "semantic_surface_relative_strength_ambiguous", "region_id": region_id,
                           "page": page + 1, "shape_ids": [shape_id] if shape_id else [], "bbox_pdf_points": bbox})
    data["surface_readback"] = {
        "schema_version": "docs-rendered-semantic-surface-readback-1",
        "artifact_path": data["artifact_path"], "artifact_sha256": data["artifact_sha256"],
        "execution_identity": surfaces["execution_identity"],
        "source_evidence": {"path": str(composition_evidence_path), "sha256": _sha256(composition_evidence_path)},
        "surface_presence_count": sum(item.get("surface_present") is True for item in facts),
        "facts": facts, "HARD_FAIL": hard_fail, "REVIEW_CANDIDATE": review,
        "status": "HARD_FAIL" if hard_fail else "REVIEW_CANDIDATE" if review else "pass",
    }
    target = output_path.resolve() if output_path is not None else manifest_path.with_name(manifest_path.name.replace(".json", ".surface.json"))
    return _write_manifest(target, data)


def build_manifest(
    artifact: Path, evidence_dir: Path, *, producer_run_id: int | None = None,
    artifact_set_id: str | None = None,
    execution_contract_path: Path | None = None, producer_receipt_path: Path | None = None,
    review_disposition_path: Path | None = None,
) -> Path | None:
    """Generate readback where this checker can render deterministically.

    HTML/SVG and office files intentionally return an existing externally produced
    manifest only; source bytes or copied files are never treated as screenshots.
    """
    artifact = artifact.resolve(strict=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = evidence_dir / f"{artifact.name}.delivery.json"
    suffix = artifact.suffix.lower()
    renders: list[dict[str, Any]] = []
    renderer: dict[str, str]
    if suffix == ".pdf":
        renderer = {"checker_id": "pymupdf-fixed-2x-rgba"}
        with fitz.open(artifact) as document:
            if len(document) < 1:
                raise ValueError("PDF has no pages")
            for index, page in enumerate(document):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, colorspace=fitz.csRGB)
                image_path = evidence_dir / f"{artifact.name}.delivery-page-{index + 1:04d}.png"
                pixmap.save(str(image_path))
                renders.append(_image_record(image_path, page=index + 1))
    elif suffix in IMAGE_SUFFIXES:
        renderer = {"checker_id": "pillow-decoded-image-readback"}
        renders = [_image_record(artifact)]
    elif suffix in SCREENSHOT_SUFFIXES | OFFICE_SUFFIXES:
        return manifest if manifest.is_file() else None
    else:
        return None
    if (producer_run_id is None) != (artifact_set_id is None):
        raise ValueError("producer_run_id and artifact_set_id must be declared together")
    data = {
        "schema_version": SCHEMA_VERSION,
        "artifact_path": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "artifact_type": suffix.lstrip("."),
        "renderer": renderer,
        "renders": renders,
    }
    if producer_run_id is not None:
        if isinstance(producer_run_id, bool) or not isinstance(producer_run_id, int) or producer_run_id < 0:
            raise ValueError("producer_run_id must be a non-negative integer")
        if (not isinstance(artifact_set_id, str) or len(artifact_set_id) != 64
                or any(char not in "0123456789abcdef" for char in artifact_set_id)):
            raise ValueError("artifact_set_id must be a sha256 value")
        data["producer_run_id"] = producer_run_id
        data["artifact_set_id"] = artifact_set_id
    integrated = execution_contract_path is not None or producer_receipt_path is not None
    if integrated:
        if execution_contract_path is None or producer_receipt_path is None:
            raise ValueError("integrated renderer requires execution contract and producer receipt")
        contract, contract_errors = execution_contract.load_contract(execution_contract_path, require_artifacts=True)
        if contract is None:
            raise ValueError("invalid execution contract: " + "; ".join(contract_errors))
        producer, producer_errors = execution_contract.validate_producer_receipt(producer_receipt_path, contract)
        if producer is None:
            raise ValueError("invalid producer receipt: " + "; ".join(producer_errors))
        by_path = {item["path"]: item for item in contract["artifacts"]}
        if str(artifact) not in by_path or by_path[str(artifact)]["sha256"] != _sha256(artifact):
            raise ValueError("renderer artifact differs from execution contract")
        data["execution_identity"] = execution_contract.identity(contract)
        data["execution_contract"] = execution_contract.file_handle(execution_contract_path)
        data["producer_receipt"] = execution_contract.file_handle(producer_receipt_path)
        color_receipt = color_contrast.measure_rendered_colors(contract, renders, data["execution_identity"])
        if color_receipt is not None:
            data["color_receipt"] = color_receipt
        readability_receipt = readability.measure(
            contract, renders, data["execution_identity"], artifact=artifact,
            evidence_dir=evidence_dir, review_disposition_path=review_disposition_path,
        )
        if readability_receipt is not None:
            data["readability_receipt"] = readability_receipt
    return _write_manifest(manifest, data)


def write_external_render_manifest(
    artifact: Path, rendered_images: list[Path], evidence_dir: Path, *, renderer_id: str,
    producer_run_id: int | None = None, artifact_set_id: str | None = None,
) -> Path:
    """Bind real external screenshots to SVG/HTML or other source artifacts.

    This is the non-integrated sibling of ``write_external_manifest``. It keeps
    legacy execution-contract callers unchanged while giving reusable source
    producers a deterministic, hash-bound rendered-readback handle.
    """
    artifact = artifact.resolve(strict=True)
    if not renderer_id or not rendered_images:
        raise ValueError("external renderer id and rendered images are required")
    if (producer_run_id is None) != (artifact_set_id is None):
        raise ValueError("producer_run_id and artifact_set_id must be declared together")
    if producer_run_id is not None and (
            isinstance(producer_run_id, bool) or not isinstance(producer_run_id, int) or producer_run_id < 0
            or not isinstance(artifact_set_id, str) or len(artifact_set_id) != 64
            or any(char not in "0123456789abcdef" for char in artifact_set_id)):
        raise ValueError("invalid producer run/artifact-set identity")
    renders = [
        _image_record(path.resolve(strict=True), page=index + 1 if len(rendered_images) > 1 else None)
        for index, path in enumerate(rendered_images)
    ]
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = evidence_dir / f"{artifact.name}.delivery.json"
    data = {
        "schema_version": SCHEMA_VERSION,
        "artifact_path": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "artifact_type": artifact.suffix.lower().lstrip("."),
        "renderer": {"reviewer_id": renderer_id},
        "renders": renders,
        **({"producer_run_id": producer_run_id, "artifact_set_id": artifact_set_id}
           if producer_run_id is not None else {}),
    }
    return _write_manifest(manifest, data)


def write_external_manifest(
    artifact: Path, rendered_images: list[Path], evidence_dir: Path, *, renderer_id: str,
    execution_contract_path: Path, producer_receipt_path: Path,
    review_disposition_path: Path | None = None,
) -> Path:
    """Bind a real external Office renderer result to the integrated contract."""
    artifact = artifact.resolve(strict=True)
    if not renderer_id or not rendered_images:
        raise ValueError("external renderer id and rendered images are required")
    contract, contract_errors = execution_contract.load_contract(execution_contract_path, require_artifacts=True)
    if contract is None:
        raise ValueError("invalid execution contract: " + "; ".join(contract_errors))
    producer, producer_errors = execution_contract.validate_producer_receipt(producer_receipt_path, contract)
    if producer is None:
        raise ValueError("invalid producer receipt: " + "; ".join(producer_errors))
    by_path = {item["path"]: item for item in contract["artifacts"]}
    if str(artifact) not in by_path or by_path[str(artifact)]["sha256"] != _sha256(artifact):
        raise ValueError("renderer artifact differs from execution contract")
    renders = [
        _image_record(path.resolve(strict=True), page=index + 1 if len(rendered_images) > 1 else None)
        for index, path in enumerate(rendered_images)
    ]
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = evidence_dir / f"{artifact.name}.delivery.json"
    data = {
        "schema_version": SCHEMA_VERSION,
        "artifact_path": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "artifact_type": artifact.suffix.lower().lstrip("."),
        "producer_run_id": contract["producer_run_id"],
        "artifact_set_id": contract["artifact_set_id"],
        "execution_identity": execution_contract.identity(contract),
        "execution_contract": execution_contract.file_handle(execution_contract_path),
        "producer_receipt": execution_contract.file_handle(producer_receipt_path),
        "renderer": {"reviewer_id": renderer_id},
        "renders": renders,
    }
    color_receipt = color_contrast.measure_rendered_colors(contract, renders, data["execution_identity"])
    if color_receipt is not None:
        data["color_receipt"] = color_receipt
    readability_receipt = readability.measure(
        contract, renders, data["execution_identity"], artifact=artifact,
        evidence_dir=evidence_dir, review_disposition_path=review_disposition_path,
    )
    if readability_receipt is not None:
        data["readability_receipt"] = readability_receipt
    return _write_manifest(manifest, data)


def _load_manifest(handle: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(handle, dict):
        return None, ["AQ-DELIVERY-01: rendered readback manifest handle missing"]
    raw_path = handle.get("path")
    digest = handle.get("sha256")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or not isinstance(digest, str):
        return None, ["AQ-DELIVERY-01: rendered readback manifest path/hash required"]
    path = Path(raw_path)
    if not path.is_file() or digest != _sha256(path):
        return None, ["AQ-DELIVERY-01: rendered readback manifest missing/drift"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["AQ-DELIVERY-01: rendered readback manifest is not JSON"]
    return data if isinstance(data, dict) else None, [] if isinstance(data, dict) else ["AQ-DELIVERY-01: rendered readback manifest must be object"]


def validate_handle(
    handle: Any, artifact: Path, *, producer_run_id: int | None = None,
    artifact_set_id: str | None = None,
    execution_contract_path: Path | None = None, producer_receipt_path: Path | None = None,
) -> list[str]:
    data, errors = _load_manifest(handle)
    if data is None:
        return errors
    artifact = artifact.resolve()
    suffix = artifact.suffix.lower()
    if data.get("schema_version") != SCHEMA_VERSION:
        return ["AQ-DELIVERY-01: rendered readback schema mismatch"]
    if data.get("artifact_path") != str(artifact) or data.get("artifact_sha256") != _sha256(artifact):
        return ["AQ-DELIVERY-01: rendered readback artifact/hash mismatch"]
    if producer_run_id is not None or artifact_set_id is not None:
        if data.get("producer_run_id") != producer_run_id or data.get("artifact_set_id") != artifact_set_id:
            return ["AQ-DELIVERY-01: rendered readback run/artifact-set identity mismatch"]
    integrated = execution_contract_path is not None or producer_receipt_path is not None
    if integrated:
        if execution_contract_path is None or producer_receipt_path is None:
            return ["AQ-DELIVERY-01: integrated renderer evidence is incomplete"]
        contract, contract_errors = execution_contract.load_contract(execution_contract_path, require_artifacts=True)
        if contract is None:
            return ["AQ-DELIVERY-01: " + "; ".join(contract_errors)]
        producer, producer_errors = execution_contract.validate_producer_receipt(producer_receipt_path, contract)
        if producer is None:
            return ["AQ-DELIVERY-01: " + "; ".join(producer_errors)]
        identity_errors = execution_contract.validate_identity(data.get("execution_identity"), contract, "renderer receipt")
        if identity_errors:
            return ["AQ-DELIVERY-01: " + identity_errors[0]]
        expected_contract = execution_contract.file_handle(execution_contract_path)
        expected_producer = execution_contract.file_handle(producer_receipt_path)
        if data.get("execution_contract") != expected_contract or data.get("producer_receipt") != expected_producer:
            return ["AQ-DELIVERY-01: renderer contract/producer receipt handle mismatch"]
        color_errors = color_contrast.validate_rendered_color_receipt(
            data.get("color_receipt"), contract, execution_contract.identity(contract),
        )
        if color_errors:
            return ["AQ-DELIVERY-01: " + color_errors[0]]
        readability_errors = readability.validate_receipt(
            data.get("readability_receipt"), contract, execution_contract.identity(contract), artifact=artifact,
        )
        if readability_errors:
            return ["AQ-DELIVERY-01: " + readability_errors[0]]
    surface = data.get("surface_readback")
    if surface is not None:
        if (not isinstance(surface, dict)
                or surface.get("schema_version") != "docs-rendered-semantic-surface-readback-1"
                or surface.get("artifact_path") != str(artifact)
                or surface.get("artifact_sha256") != _sha256(artifact)):
            return ["AQ-DELIVERY-01: SURFACE-SCHEMA-01 rendered surface readback mismatch"]
        if surface.get("execution_identity") != data.get("execution_identity"):
            return ["AQ-DELIVERY-01: SURFACE-IDENTITY-01 rendered surface identity mismatch"]
        source_evidence = surface.get("source_evidence")
        source_path = Path(source_evidence.get("path", "")) if isinstance(source_evidence, dict) else Path()
        if (not source_path.is_absolute() or not source_path.is_file()
                or source_evidence.get("sha256") != _sha256(source_path)):
            return ["AQ-DELIVERY-01: SURFACE-HASH-01 surface source evidence missing/drift"]
        facts, hard_fail, review = surface.get("facts"), surface.get("HARD_FAIL"), surface.get("REVIEW_CANDIDATE")
        if not isinstance(facts, list) or not isinstance(hard_fail, list) or not isinstance(review, list):
            return ["AQ-DELIVERY-01: SURFACE-SCHEMA-01 surface outcome arrays malformed"]
        if surface.get("surface_presence_count") != sum(isinstance(item, dict) and item.get("surface_present") is True for item in facts):
            return ["AQ-DELIVERY-01: SURFACE-SCHEMA-01 surface presence count mismatch"]
        expected_status = "HARD_FAIL" if hard_fail else "REVIEW_CANDIDATE" if review else "pass"
        if surface.get("status") != expected_status:
            return ["AQ-DELIVERY-01: SURFACE-SCHEMA-01 surface status mismatch"]
    geometry = data.get("geometry_relation_readback")
    if geometry is not None:
        if (not isinstance(geometry, dict)
                or geometry.get("schema_version") != "docs-rendered-geometry-relation-readback-1"
                or geometry.get("artifact_path") != str(artifact)
                or geometry.get("artifact_sha256") != _sha256(artifact)):
            return ["AQ-DELIVERY-01: RELATION-SCHEMA-01 rendered geometry relation mismatch"]
        if geometry.get("execution_identity") != data.get("execution_identity"):
            return ["AQ-DELIVERY-01: RELATION-IDENTITY-01 rendered geometry identity mismatch"]
        evidence_handle = geometry.get("content_block_evidence")
        evidence_path = Path(evidence_handle.get("path", "")) if isinstance(evidence_handle, dict) else Path()
        if (not evidence_path.is_absolute() or not evidence_path.is_file()
                or evidence_handle.get("sha256") != _sha256(evidence_path)):
            return ["AQ-DELIVERY-01: RELATION-HASH-01 relation evidence missing/drift"]
        relations = geometry.get("relations")
        if (not isinstance(relations, list) or not relations
                or geometry.get("relation_count") != len(relations)
                or any(not isinstance(item, dict) or not isinstance(item.get("primitive_mappings"), list)
                       or not item["primitive_mappings"] for item in relations)):
            return ["AQ-DELIVERY-01: RELATION-SCHEMA-01 relation mappings malformed"]
        for item in relations:
            relative = item.get("relative_anchor_gap")
            if relative is None:
                continue
            evidence_handle = relative.get("evidence") if isinstance(relative, dict) else None
            evidence_path = Path(evidence_handle.get("path", "")) if isinstance(evidence_handle, dict) else Path()
            if (not evidence_path.is_absolute() or not evidence_path.is_file()
                    or evidence_handle.get("sha256") != _sha256(evidence_path)):
                return ["AQ-DELIVERY-01: RELATION-HASH-01 relative anchor-gap evidence missing/drift"]
            try:
                record = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return ["AQ-DELIVERY-01: RELATION-SCHEMA-01 relative anchor-gap evidence malformed"]
            if (record.get("execution_identity") != data.get("execution_identity")
                    or record.get("artifact") != {"path": str(artifact), "sha256": _sha256(artifact)}
                    or record.get("annotation_id") != relative.get("annotation_id")
                    or record.get("format") != relative.get("format")
                    or record.get("effective") != relative.get("effective")
                    or record.get("effective", {}).get("status") != "pass"):
                return ["AQ-DELIVERY-01: RELATION-IDENTITY-01 relative anchor-gap readback drift"]
    renderer = data.get("renderer")
    if not isinstance(renderer, dict) or not any(isinstance(renderer.get(key), str) and renderer[key] for key in ("checker_id", "reviewer_id")):
        return ["AQ-DELIVERY-01: rendered readback provenance missing"]
    renders = data.get("renders")
    if not isinstance(renders, list) or not renders:
        return ["AQ-DELIVERY-01: rendered image evidence missing"]
    seen_pages: set[int] = set()
    for item in renders:
        if not isinstance(item, dict):
            return ["AQ-DELIVERY-01: rendered image record invalid"]
        raw_path = item.get("path")
        path = Path(raw_path) if isinstance(raw_path, str) else Path()
        if not path.is_absolute() or not path.is_file() or path.resolve() == artifact:
            if suffix not in IMAGE_SUFFIXES or path.resolve() != artifact:
                return ["AQ-DELIVERY-01: rendered image path missing or source-copy claim"]
        try:
            actual = _image_record(path, page=item.get("page") if isinstance(item.get("page"), int) else None)
        except Exception:
            return ["AQ-DELIVERY-01: rendered image is undecodable"]
        for key in ("sha256", "width", "height", "pixel_sha256"):
            if item.get(key) != actual.get(key):
                return [f"AQ-DELIVERY-01: rendered image {key} mismatch"]
        if suffix == ".pdf":
            page = item.get("page")
            if not isinstance(page, int) or page < 1 or page in seen_pages:
                return ["AQ-DELIVERY-01: PDF rendered page coverage invalid"]
            seen_pages.add(page)
    if suffix == ".pdf":
        try:
            with fitz.open(artifact) as document:
                if seen_pages != set(range(1, len(document) + 1)):
                    return ["AQ-DELIVERY-01: PDF rendered page coverage mismatch"]
                if renderer.get("checker_id") != "pymupdf-fixed-2x-rgba":
                    return ["AQ-DELIVERY-01: PDF deterministic renderer provenance mismatch"]
                for index, page in enumerate(document, start=1):
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, colorspace=fitz.csRGB)
                    expected = hashlib.sha256(Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert("RGBA").tobytes()).hexdigest()
                    recorded = next(item for item in renders if item.get("page") == index)
                    if recorded.get("width") != pixmap.width or recorded.get("height") != pixmap.height or recorded.get("pixel_sha256") != expected:
                        return ["AQ-DELIVERY-01: PDF deterministic rendered content mismatch"]
        except Exception:
            return ["AQ-DELIVERY-01: PDF deterministic readback failed"]
    elif suffix in SCREENSHOT_SUFFIXES:
        if renderer.get("checker_id") == "source-copy" or not all(Path(item["path"]).suffix.lower() in IMAGE_SUFFIXES for item in renders):
            return ["AQ-DELIVERY-01: HTML/SVG rendered screenshot evidence required"]
    elif suffix in OFFICE_SUFFIXES:
        if not all(Path(item["path"]).suffix.lower() in IMAGE_SUFFIXES for item in renders):
            return ["AQ-DELIVERY-01: office rendered PDF/image evidence required"]
    return []
