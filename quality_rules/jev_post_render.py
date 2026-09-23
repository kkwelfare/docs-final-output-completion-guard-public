"""Opt-in post-render structural Jev review. No body, pixels or visual judgment.

Only actual PDF readback metrics are sent; deterministic hard failures dominate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
import fitz
import os
import time
import math
from . import jev_overlap, jev_glyph_locale

ROOT = Path(__file__).resolve().parents[1]
ROUTE = "post-render-jev-structural"
MAX_JSON_BYTES = 256_000
MAX_WIRE_BYTES = 32_000
# No endpoint guessing, automatic fallback, or retries. Locale is a proxy-only
# advisory judgment, not visual perception or semantic proofreading.
RULE_CRITERIA = {
    jev_glyph_locale.RULE: [jev_glyph_locale.CRITERION],
    "line_spacing_candidate": ["line_spacing"],
    "content_block_spacing_candidate": ["line_spacing"],
    "content_block_intra_spacing_candidate": ["line_spacing"],
    "unclassified_source_overlap_or_gutter": ["geometry_consistency"],
}


class PostRenderError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_schema(value: Any, kind: str) -> None:
    try:
        encoded = canonical(value)
    except (TypeError, ValueError) as exc:
        raise PostRenderError("malformed_response" if kind == "response" else "invalid_request") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise PostRenderError("malformed_response" if kind == "response" else "invalid_request")
    schema = json.loads((ROOT / "schemas" / f"docs-jev-post-render-{kind}-1.json").read_text())
    if not Draft202012Validator(schema).is_valid(value):
        raise PostRenderError("malformed_response" if kind == "response" else "invalid_request")


def read_json(path: Path) -> dict:
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise PostRenderError("identity_drift")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise PostRenderError("invalid_request")
    return value


def check_handle(handle: dict) -> Path:
    path = Path(handle["path"])
    if not path.is_absolute() or str(path.resolve()) != str(path) or not path.is_file() or file_hash(path) != handle["sha256"]:
        raise PostRenderError("identity_drift")
    return path


def candidate_digest(candidates: list[dict]) -> str:
    # Bind the entire bounded candidate descriptors, not just IDs.
    return digest(candidates)


def scan_candidates(scan: dict) -> list[dict]:
    """Recount raw issues and reject ambiguous/malformed classifications."""
    if scan.get("schema_version") != "docs-layout-typography-scan-3" or scan.get("measurement_basis") != "pdf_points":
        raise PostRenderError("invalid_request")
    issues = scan.get("issues")
    if not isinstance(issues, list) or any(not isinstance(i, dict) for i in issues):
        raise PostRenderError("invalid_request")
    if (scan.get("status") == "block" or scan.get("hard_issue_count", 0) != 0
            or scan.get("HARD_FAIL") or any(i.get("severity") == "hard" or i.get("bucket") == "HARD_FAIL" for i in issues)):
        raise PostRenderError("hard_fail")
    if any(i.get("severity") != "review" for i in issues):
        raise PostRenderError("invalid_request")
    if "review_candidate_count" in scan and scan["review_candidate_count"] != len(issues):
        raise PostRenderError("identity_drift")
    if "REVIEW_CANDIDATE" in scan and scan["REVIEW_CANDIDATE"] != issues:
        raise PostRenderError("identity_drift")
    if not issues or len({i.get("id") for i in issues}) != len(issues):
        raise PostRenderError("invalid_request")
    return issues


def validate_request(request: dict) -> dict:
    validate_schema(request, "request")
    b = request["binding"]
    if request["reviewer"]["task_id"] == b["producer"]["task_id"]:
        raise PostRenderError("identity_drift")
    check_handle(b["artifact"])
    scan = read_json(check_handle(b["scan"]))
    if scan.get("artifact_sha256") != b["artifact"]["sha256"]:
        raise PostRenderError("identity_drift")
    # Producer/set fields, when emitted by the scanner, must agree as well.
    for key, expected in (("artifact_set_id", b["artifact_set_id"]), ("producer_task_id", b["producer"]["task_id"]), ("producer_run_id", b["producer"]["run_id"])):
        if key in scan and scan[key] != expected:
            raise PostRenderError("identity_drift")
    raw = scan_candidates(scan)
    candidates = request["candidates"]
    if b["candidate_set_digest"] != candidate_digest(candidates):
        raise PostRenderError("identity_drift")
    by_id = {c["id"]: c for c in candidates}
    if len(by_id) != len(candidates) or set(by_id) != {c["id"] for c in raw}:
        raise PostRenderError("identity_drift")
    for row in raw:
        c = by_id[row["id"]]
        if c["rule"] not in RULE_CRITERIA or c["criteria"] != RULE_CRITERIA[c["rule"]]:
            raise PostRenderError("invalid_request")
        if any(c[k] != row.get(k) for k in ("rule", "page", "bbox_pdf_points")):
            raise PostRenderError("identity_drift")
        if c["affected_pages"] != row.get("affected_pages", [row["page"]]):
            raise PostRenderError("identity_drift")
        if c["render"] != row.get("render_evidence"):
            raise PostRenderError("identity_drift")
        if c["semantic_role"] != row.get("semantic_role", "unspecified"):
            raise PostRenderError("identity_drift")
        check_handle(c["render"])
        if c["metrics"] != measure_candidate(Path(b["artifact"]["path"]), c):
            raise PostRenderError("identity_drift")
        if c["rule"] == jev_glyph_locale.RULE and c["affected_pages"] != [c["page"]]:
            raise PostRenderError("insufficient_evidence")
        box = c["bbox_pdf_points"]
        if box[2] <= box[0] or box[3] <= box[1]:
            raise PostRenderError("invalid_request")
        if c["page"] not in c["affected_pages"]:
            raise PostRenderError("invalid_request")
    return scan


def prepare_raw_scan(scan: dict, source_scan: dict, surface_hard: list[dict]) -> dict:
    """Opt-in complete scan snapshot, including source/surface hard failures."""
    result = copy.deepcopy(scan)
    rows = result["issues"] + copy.deepcopy(source_scan.get("issues", [])) + copy.deepcopy(surface_hard)
    for row in rows:
        if row in surface_hard:
            row["severity"] = "hard"
    hard = [r for r in rows if r.get("severity") == "hard" or r.get("bucket") == "HARD_FAIL"]
    review = [r for r in rows if r.get("severity") == "review" and r not in hard]
    for r in hard:
        r.update(severity="hard", bucket="HARD_FAIL")
    for r in review:
        r["bucket"] = "REVIEW_CANDIDATE"
    result.update(issues=rows, HARD_FAIL=hard, REVIEW_CANDIDATE=review,
                  hard_issue_count=max(len(hard), int(scan.get("hard_issue_count", 0))),
                  review_candidate_count=len(review))
    result["status"] = "block" if hard or result["hard_issue_count"] or source_scan.get("status") == "block" else "review" if review else "pass"
    return result


PDF_REGION_EPSILON_PT = 0.005  # Half of the scanner's two-decimal point unit.


def normalize_pdf_region(page_rect: fitz.Rect, bbox: list) -> tuple[fitz.Rect, dict]:
    """Clamp only bounded serialization overflow; never rewrite bound evidence."""
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or v < 0 for v in bbox)):
        raise PostRenderError("invalid_request")
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0:
        raise PostRenderError("invalid_request")
    if (x0 < page_rect.x0 - PDF_REGION_EPSILON_PT
            or y0 < page_rect.y0 - PDF_REGION_EPSILON_PT
            or x1 > page_rect.x1 + PDF_REGION_EPSILON_PT
            or y1 > page_rect.y1 + PDF_REGION_EPSILON_PT):
        raise PostRenderError("invalid_request")
    normalized = [max(x0, page_rect.x0), max(y0, page_rect.y0),
                  min(x1, page_rect.x1), min(y1, page_rect.y1)]
    region = fitz.Rect(normalized)
    if region.is_empty or not page_rect.contains(region):
        raise PostRenderError("invalid_request")
    return region, {"epsilon_pt": PDF_REGION_EPSILON_PT,
                    "normalized_bbox_pdf_points": list(region),
                    "applied": list(region) != list(bbox)}


def measure_pdf(artifact: Path, page_number: int, bbox: list) -> dict:
    """Recompute from PDF bytes; never include body or literal font names."""
    with fitz.open(artifact) as doc:
        if not doc.is_pdf or page_number < 1 or page_number > len(doc):
            raise PostRenderError("invalid_request")
        page = doc[page_number - 1]
        if page.rotation != 0:
            raise PostRenderError("invalid_request")
        region, normalization = normalize_pdf_region(page.rect, bbox)
        lines = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                box = fitz.Rect(line["bbox"])
                if not box.intersects(region):
                    continue
                spans = line.get("spans", [])
                lines.append({"bbox": [round(v, 4) for v in box],
                    "font_sizes": sorted({round(v["size"], 4) for v in spans}),
                    "font_hashes": sorted({digest(v["font"]) for v in spans}),
                    "character_count": sum(len(v["text"]) for v in spans),
                    "colors_rgb": sorted({int(v["color"]) for v in spans}),
                    "flags": sorted({int(v["flags"]) for v in spans})})
        if len(lines) > 64:
            raise PostRenderError("invalid_request")
        gaps = [round(lines[i+1]["bbox"][1]-lines[i]["bbox"][3],4) for i in range(len(lines)-1)]
        # Interval union measures vertical line coverage, not ink or aesthetic quality.
        intervals=sorted((max(region.y0,v["bbox"][1]),min(region.y1,v["bbox"][3])) for v in lines)
        covered=0.0; end=region.y0
        for lo,hi in intervals:
            covered += max(0,hi-max(lo,end)); end=max(end,hi)
        return {"basis":"pdf_points", "page_size":[round(page.rect.width,4),round(page.rect.height,4)],
                "region_normalization":normalization,
                "lines":lines, "line_order_gaps":gaps,
                "vertical_line_coverage":round(covered/region.height,6)}


def measure_candidate(artifact: Path, candidate: dict) -> dict:
    metrics = measure_pdf(artifact, candidate["page"], candidate["bbox_pdf_points"])
    if candidate["rule"] == jev_glyph_locale.RULE:
        try:
            metrics["glyph_locale"] = jev_glyph_locale.measure(
                artifact, candidate["page"], candidate["bbox_pdf_points"], candidate["render"])
        except ValueError as exc:
            raise PostRenderError(str(exc)) from exc
    return metrics


def build_request(*, artifact: Path, raw_scan: Path, artifact_set_id: str,
                  producer: dict, reviewer: dict, allow_live_provider: bool = False,
                  timeout_seconds: float = 15) -> dict:
    scan=read_json(raw_scan)
    candidates=[]
    for row in scan_candidates(scan):
        if row["rule"] not in RULE_CRITERIA:
            raise PostRenderError("invalid_request")
        c={k:copy.deepcopy(row[k]) for k in ("id","rule","page","bbox_pdf_points")}
        c["affected_pages"] = copy.deepcopy(row.get("affected_pages", [row["page"]]))
        c.update(render=copy.deepcopy(row["render_evidence"]),
                 semantic_role=row.get("semantic_role","unspecified"),
                 criteria=list(RULE_CRITERIA[row["rule"]]))
        c["metrics"] = measure_candidate(artifact, c)
        candidates.append(c)
    request={"schema_version":"docs-jev-post-render-request-1", "enabled":True,
        "binding":{"artifact":{"path":str(artifact.resolve()),"sha256":file_hash(artifact)},
                   "scan":{"path":str(raw_scan.resolve()),"sha256":file_hash(raw_scan)},
                   "artifact_set_id":artifact_set_id,"producer":producer,
                   "candidate_set_digest":candidate_digest(candidates)},
        "reviewer":reviewer,"provider":"typesafe-system-one","model":"jev-latest",
        "allow_live_provider":allow_live_provider,"timeout_seconds":timeout_seconds,"candidates":candidates}
    validate_request(request)
    return request


def wire_metrics(metrics: dict) -> dict:
    """Deterministic typed projection of validated, fully recomputed metrics.

    Retain all structural lines and locale proxies. Replace only the glyph
    array with complete aggregates (not sampling), bound to its full digest.
    Full glyph geometry remains in the local request/candidate digest.
    """
    result = copy.deepcopy(metrics)
    if "glyph_locale" not in result:
        return result
    locale = result["glyph_locale"]
    glyphs = locale.pop("glyphs")
    groups = {}
    for glyph in glyphs:
        key = (glyph["font_xref"], glyph["span_flags"], glyph["contained"])
        groups[key] = groups.get(key, 0) + 1
    locale["glyph_summary"] = {
        "schema_version": "docs-jev-glyph-summary-1",
        "aggregation": "all_glyphs_no_sampling",
        "glyphs_sha256": digest(glyphs),
        "glyph_count": len(glyphs),
        "contained_count": sum(g["contained"] for g in glyphs),
        "uncontained_count": sum(not g["contained"] for g in glyphs),
        "unmapped_font_count": sum(g["font_xref"] == 0 for g in glyphs),
        "bbox_union_pdf_points": ([min(g["bbox"][0] for g in glyphs),
                                   min(g["bbox"][1] for g in glyphs),
                                   max(g["bbox"][2] for g in glyphs),
                                   max(g["bbox"][3] for g in glyphs)] if glyphs else None),
        "font_flag_containment_counts": [
            {"font_xref": key[0], "span_flags": key[1], "contained": key[2], "count": count}
            for key, count in sorted(groups.items())],
    }
    return result


def build_wire(request: dict) -> tuple[dict, list[dict]]:
    validate_request(request)
    rows=[]; records=[]
    for c in request["candidates"]:
        rows.append({k:copy.deepcopy(c[k]) for k in ("id","rule","page","bbox_pdf_points","affected_pages","criteria","semantic_role")})
        rows[-1]["metrics"] = wire_metrics(c["metrics"])
        rows[-1]["metrics_sha256"] = digest(c["metrics"])
        rows[-1]["render_sha256"]=c["render"]["sha256"]
        records.append({"candidate_id":c["id"],"render_sha256":c["render"]["sha256"],"metrics_sha256":digest(c["metrics"])})
    b=request["binding"]
    wire={"schema_version":"docs-jev-post-render-wire-1","request_sha256":digest(request),
          "artifact_sha256":b["artifact"]["sha256"],"scan_sha256":b["scan"]["sha256"],
          "artifact_set_id":b["artifact_set_id"],"producer":b["producer"],"reviewer":request["reviewer"],
          "candidate_set_digest":b["candidate_set_digest"],"advisory_only":True,"candidates":rows}
    if len(canonical(wire)) > MAX_WIRE_BYTES:
        raise PostRenderError("invalid_request")
    build_provider_payload(wire)  # Include JSON escaping and questions in the cap.
    return wire, records


def build_provider_payload(wire: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """Pure, bounded payload construction; no provider or credential access."""
    if len(canonical(wire)) > MAX_WIRE_BYTES:
        raise PostRenderError("invalid_request")
    questions={}; mapping=[]
    for i,c in enumerate(wire["candidates"]):
        for j,criterion in enumerate(c["criteria"]):
            key=f"c{i}_q{j}"
            mapping.append((key,c["id"],criterion))
            questions[key]={"type":"choice","instructions":
                "Review only artifact-bound post-render metrics for "+criterion+
                ". Advisory only. No pixels or body are available. Choose uncertain for meaning, glyph appearance, contrast/background or other unavailable evidence.",
                "choices":["pass","fail","uncertain"],
                "criteria":{"pass":"Supplied measurements establish no structural defect for this criterion.",
                            "fail":"Supplied measurements establish a structural defect.",
                            "uncertain":"Measurements do not establish this criterion."}}
            if criterion == jev_glyph_locale.CRITERION:
                questions[key] = {"type":"choice", "instructions":
                    "Assess display glyph locale only from PDF-derived typed font/glyph and local raster proxies. "
                    "No pixels or page body are available. This is advisory inference, NOT visual perception. "
                    "Do not infer Japanese locale from CJK counts or dark-pixel density alone. "
                    "Use embedded font, mapping and explicit Japanese-selector matches; choose uncertain when inconclusive.",
                    "choices":jev_glyph_locale.CHOICES,
                    "criteria":{
                        "japanese_glyph_likely":"Mapped embedded fonts and selector/name proxies support Japanese display glyph locale.",
                        "non_japanese_glyph_likely":"Mapped embedded font proxies support non-Japanese display glyph locale.",
                        "uncertain":"Missing, contradictory or inconclusive locale proxies."}}
    payload={"model":"jev-latest","state":canonical(wire).decode(),"questions":questions}
    if len(canonical(payload)) > MAX_WIRE_BYTES:
        raise PostRenderError("invalid_request")
    return payload, mapping


def text_provider(wire: dict, timeout: float) -> dict:
    """One bounded text-only call; raw response stays transient."""
    payload, mapping = build_provider_payload(wire)
    questions = payload["questions"]
    raw=jev_overlap._post_provider(jev_overlap.PRIMARY_ENDPOINT,jev_overlap.PRIMARY_API_KEY_ENV,
                                  "jev-latest",payload,timeout)
    if not isinstance(raw,dict) or len(canonical(raw)) > MAX_JSON_BYTES or not isinstance(raw.get("answers"),dict):
        raise PostRenderError("malformed_response")
    answers=raw["answers"]
    if set(answers) != set(questions):
        raise PostRenderError("malformed_response")
    if "model" in raw:
        # Providers may resolve the requested alias to a different model name.
        # Validate it transiently; requests and receipts retain jev-latest.
        model = raw["model"]
        if (not isinstance(model, str) or not model.strip() or len(model) > 128
                or any(ord(ch) < 32 or 0x7f <= ord(ch) <= 0x9f
                       or ch in "\u2028\u2029" for ch in model)):
            raise PostRenderError("malformed_response")
    reviews={c["id"]:{"candidate_id":c["id"],"disposition":"accepted","criteria_results":[]} for c in wire["candidates"]}
    for key,cid,criterion in mapping:
        answer=answers[key]
        allowed = set(questions[key]["choices"])
        if not isinstance(answer,dict) or answer.get("choice") not in allowed:
            raise PostRenderError("malformed_response")
        confidence=answer.get("confidence")
        if isinstance(confidence,bool) or not isinstance(confidence,(float,int)) or not math.isfinite(confidence) or not 0<=confidence<=1:
            raise PostRenderError("malformed_response")
        if "probabilities" in answer:
            probs=answer["probabilities"]
            if not isinstance(probs,dict) or set(probs)!=allowed or any(isinstance(p,bool) or not isinstance(p,(int,float)) or not math.isfinite(p) or not 0<=p<=1 for p in probs.values()):
                raise PostRenderError("malformed_response")
        reviews[cid]["criteria_results"].append({"criterion":criterion,"status":answer["choice"]})
    for r in reviews.values():
        values={v["status"] for v in r["criteria_results"]}
        r["disposition"]=disposition(values)
    return {"schema_version":"docs-jev-post-render-response-1","request_sha256":wire["request_sha256"],"reviews":list(reviews.values())}


def _bounded_call(fn: Callable, wire: dict, timeout: float) -> Any:
    result: queue.Queue = queue.Queue(maxsize=1)
    def call():
        try:
            result.put((True, fn(copy.deepcopy(wire), timeout)))
        except Exception as exc:
            result.put((False, exc))
    # Deadline bounds caller latency even for a misbehaving adapter. No retry.
    threading.Thread(target=call, daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError from exc
    if not ok:
        raise value
    return value


def disposition(values: set) -> str:
    return "repair_required" if values & {"fail", "non_japanese_glyph_likely"} else "uncertain" if "uncertain" in values else "accepted"


def _validate_reviews(response: dict, request: dict) -> str:
    validate_schema(response, "response")
    if response["request_sha256"] != digest(request):
        raise PostRenderError("malformed_response")
    rows = response["reviews"]
    by_id = {r["candidate_id"]: r for r in rows}
    if len(by_id) != len(rows) or set(by_id) != {c["id"] for c in request["candidates"]}:
        raise PostRenderError("malformed_response")
    for c in request["candidates"]:
        row = by_id[c["id"]]
        values = row["criteria_results"]
        if len(values) != len(c["criteria"]) or {v["criterion"] for v in values} != set(c["criteria"]):
            raise PostRenderError("malformed_response")
        statuses = {v["status"] for v in values}
        for value in values:
            allowed = set(jev_glyph_locale.CHOICES) if value["criterion"] == jev_glyph_locale.CRITERION else {"pass", "fail", "uncertain"}
            if value["status"] not in allowed:
                raise PostRenderError("malformed_response")
        if c["rule"] == jev_glyph_locale.RULE and not c["metrics"]["glyph_locale"]["evidence_sufficient"]:
            raise PostRenderError("insufficient_evidence")
        expected = disposition(statuses)
        if row["disposition"] != expected:
            raise PostRenderError("malformed_response")
    choices = {r["disposition"] for r in rows}
    return "repair_required" if "repair_required" in choices else "uncertain" if "uncertain" in choices else "accepted"


def evaluate(request: dict, *, fixture_adapter: Callable | None = None) -> dict:
    # Reject invalid requests before constructing a receipt with untrusted fields.
    validate_schema(request, "request")
    receipt = {"schema_version": "docs-jev-post-render-receipt-1", "route": ROUTE,
               "binding": copy.deepcopy(request["binding"]), "reviewer": copy.deepcopy(request["reviewer"]),
               "request_sha256": digest(request), "provider": request["provider"], "model": request["model"],
               "execution_mode": "fixture" if fixture_adapter is not None else "live",
               "provider_invoked": False, "advisory_only": True, "input_kind": "post-render-metrics", "status": "unresolved",
               "reason_code": "provider_unavailable", "measurements": [], "reviews": []}
    try:
        wire, records = build_wire(request)
        receipt["measurements"] = records
        if any(c["rule"] == jev_glyph_locale.RULE and not c["metrics"]["glyph_locale"]["evidence_sufficient"] for c in request["candidates"]):
            raise PostRenderError("insufficient_evidence")
        fn = fixture_adapter
        if fn is None and request["allow_live_provider"] and os.environ.get(jev_overlap.PRIMARY_API_KEY_ENV, "").strip():
            fn = text_provider
        if fn is None:
            return receipt
        receipt["provider_invoked"] = True
        response = _bounded_call(fn, wire, float(request["timeout_seconds"]))
        status = _validate_reviews(response, request)
        validate_request(request)  # detect artifact/scan/image mutation during call
        receipt.update(status=status, reason_code="ok", reviews=copy.deepcopy(response["reviews"]))
    except PostRenderError as exc:
        receipt["reason_code"] = str(exc) if str(exc) in {"identity_drift", "invalid_request", "hard_fail", "malformed_response", "insufficient_evidence"} else "invalid_request"
    except jev_overlap.JevOverlapError as exc:
        receipt["reason_code"] = "timeout" if exc.reason == "timeout" else "provider_unavailable" if exc.reason == "credential_unavailable" else "malformed_response" if exc.reason == "malformed_response" else "provider_error"
    except TimeoutError:
        receipt["reason_code"] = "timeout"
    except Exception:
        receipt["reason_code"] = "provider_error"
    validate_schema(receipt, "receipt")
    return receipt


def validate_config(config: Any) -> list[str]:
    if config is None:
        return []
    try:
        validate_schema(config, "config")
        return []
    except Exception:
        return ["AQ-LAYOUT-01: invalid jev_post_render_review opt-in configuration"]


def consume(config: dict, *, artifact: Path, artifact_set_id: str | None,
            producer: dict | None, raw_scan: Path | None = None, allow_fixture: bool = False) -> tuple[dict | None, str | None]:
    """Pure readback: preserve raw scan and receipt, reject fixture formal close."""
    try:
        validate_schema(config, "config")
        if config["enabled"] is not True or not artifact_set_id or producer is None:
            raise PostRenderError("identity_drift")
        request = read_json(check_handle(config["request"]))
        receipt = read_json(check_handle(config["receipt"]))
        scan = validate_request(request)
        validate_schema(receipt, "receipt")
        b = request["binding"]
        if (b["artifact"] != {"path": str(artifact.resolve()), "sha256": file_hash(artifact)}
                or b["artifact_set_id"] != artifact_set_id or b["producer"] != producer
                or (raw_scan is not None and b["scan"] != {"path": str(raw_scan.resolve()), "sha256": file_hash(raw_scan)})):
            raise PostRenderError("identity_drift")
        if (receipt["binding"] != b or receipt["reviewer"] != request["reviewer"]
                or receipt["request_sha256"] != digest(request)
                or receipt["provider"] != request["provider"] or receipt["model"] != request["model"]
                or receipt["status"] != "accepted" or receipt["reason_code"] != "ok"
                or receipt["provider_invoked"] is not True
                or (receipt["execution_mode"] != "live" and not allow_fixture)):
            raise PostRenderError("identity_drift")
        response = {"schema_version": "docs-jev-post-render-response-1", "request_sha256": receipt["request_sha256"],
                    "reviews": receipt["reviews"]}
        if _validate_reviews(response, request) != "accepted":
            raise PostRenderError("malformed_response")
        _, measurements = build_wire(request)
        if receipt["measurements"] != measurements:
            raise PostRenderError("identity_drift")
        return {"route": ROUTE, "raw_scan": b["scan"], "request": config["request"],
                "receipt": config["receipt"], "provider": receipt["provider"], "model": receipt["model"],
                "provider_invoked": True, "advisory_only": True, "input_kind": "post-render-metrics", "candidate_count": len(scan["issues"])}, None
    except Exception as exc:
        reason = str(exc) if isinstance(exc, PostRenderError) else "invalid_request"
        return None, "AQ-LAYOUT-01: Jev post-render structural review " + reason


def tool_handler(args: dict, **kwargs: Any) -> str:
    """Local registered route. No files written and no implicit provider opt-in."""
    try:
        if set(args) != {"request_path"}:
            raise PostRenderError("invalid_request")
        request = read_json(Path(args["request_path"]))
        return json.dumps(evaluate(request), sort_keys=True)
    except Exception:
        return json.dumps({"route": ROUTE, "status": "unresolved", "reason_code": "invalid_request",
                           "provider_invoked": False, "advisory_only": True})


def register(ctx: Any) -> None:
    ctx.register_tool(name="docs_jev_post_render_review", toolset="docs_jev_post_render_review",
        schema={"type": "object", "properties": {"request_path": {"type": "string"}},
                "required": ["request_path"], "additionalProperties": False}, handler=tool_handler,
        description="Opt-in post-render structural advisory review using typed PDF metrics and current text-only Jev. No body or pixels sent.")
