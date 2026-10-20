"""Optional, advisory Jev 1.14 image judgment for generated PDF layout review.

Role split (master decision): this route only judges coarse visual breakage and
whether margins/placement look natural on the whole page.  Exact fixes remain
with the deterministic numeric checks.  Results are recorded as evidence/labels
next to the existing AQ-LAYOUT-01 review candidates; they never block
completion, never change hard failures, and never satisfy the Plan C live-Jev
requirement (that stays with generated_artifact_jev).

Two layers, both off by default:
  1. Whole-page judgment: one request per page (capped by ``max_pages``; skipped
     pages are recorded, never silently dropped).  An optional ``pages`` list
     (1-based, e.g. ``[1, 3, 5]``) limits this to the chosen pages; every other
     page is recorded as ``not_selected``.  Without ``pages`` all pages are judged.
  2. Candidate judgment: one request per review-candidate group with a
     downscaled whole-page image plus a context crop and measured values as text
     (capped by ``max_candidate_calls``).  With ``pages`` set, only candidates
     on the chosen pages are judged; others are recorded as ``page_not_selected``.
Box (location) is asked in a separate request only after a Choice/Noul verdict
says a page/candidate is likely off, because Box always returns a box.

Labels are two-tier.  ``repair_required`` (Box asked) needs a confident
``repair_required`` choice (confidence >= ``repair_confidence_threshold``) or
``visible_breakage`` noul >= ``likely_threshold``.  A low-confidence
``repair_required`` choice, or ``placement_unnatural`` noul >=
``intent_threshold``, is labeled ``differs_from_surroundings`` (the page differs
from its surroundings; check whether that is intended) and Box is not asked.

Transport: TypeSafe only, literal model ``jev-1.14.0``, images header, one
attempt per request, no fallback, no retry.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pymupdf
from PIL import Image

ROUTE = "jev-visual-image-review"
SCHEMA_VERSION = "docs-jev-visual-review-1"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
MODEL = "jev-1.14.0"  # literal for this image route only; never an alias
IMAGES_HEADER = ("TypeSafe-Danger-Zone-Images", "true")
MIN_SIDE_PX = 280
MAX_SIDE_PX = 1568
MAX_REQUEST_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_PAGES = 40
DEFAULT_MAX_CANDIDATE_CALLS = 8
DEFAULT_MAX_BOX_CALLS = 8
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_LIKELY_THRESHOLD = 0.5
DEFAULT_REPAIR_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_INTENT_THRESHOLD = 0.5
DIFFERS_LABEL = "differs_from_surroundings"
VERDICTS = ("repair_required", "acceptable", "insufficient_information")
CONFIG_KEYS = {"enabled", "allow_live_provider", "max_pages", "max_candidate_calls",
               "max_box_calls", "timeout_seconds", "likely_threshold",
               "repair_confidence_threshold", "intent_threshold", "pages"}
MAX_SELECTED_PAGES = 256
CONTEXT_MARGIN_RATIO = 0.25
CONTEXT_MARGIN_MIN_PT = 36.0
MAX_CROP_ZOOM = 4.0

Transport = Callable[[dict, dict, float], Any]


class VisualReviewError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# --------------------------------------------------------------------------- config

def normalize_config(config: Any) -> tuple[dict | None, str | None]:
    """Return (config, error). ``None`` config means the route is off."""
    if config is None:
        return None, None
    if not isinstance(config, dict) or set(config) - CONFIG_KEYS or not isinstance(config.get("enabled"), bool):
        return None, "invalid_config"
    out = {"enabled": config["enabled"],
           "allow_live_provider": config.get("allow_live_provider", False),
           "max_pages": config.get("max_pages", DEFAULT_MAX_PAGES),
           "max_candidate_calls": config.get("max_candidate_calls", DEFAULT_MAX_CANDIDATE_CALLS),
           "max_box_calls": config.get("max_box_calls", DEFAULT_MAX_BOX_CALLS),
           "timeout_seconds": config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
           "likely_threshold": config.get("likely_threshold", DEFAULT_LIKELY_THRESHOLD),
           "repair_confidence_threshold": config.get("repair_confidence_threshold",
                                                     DEFAULT_REPAIR_CONFIDENCE_THRESHOLD),
           "intent_threshold": config.get("intent_threshold", None),
           "pages": config.get("pages", None)}
    if not isinstance(out["allow_live_provider"], bool):
        return None, "invalid_config"
    for key, low, high in (("max_pages", 0, 256), ("max_candidate_calls", 0, 64), ("max_box_calls", 0, 64)):
        if isinstance(out[key], bool) or not isinstance(out[key], int) or not low <= out[key] <= high:
            return None, "invalid_config"
    for key, low, high in (("timeout_seconds", 0.0, 120.0), ("likely_threshold", 0.0, 1.0),
                           ("repair_confidence_threshold", 0.0, 1.0)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < value <= high:
            return None, "invalid_config"
        out[key] = float(value)
    if out["intent_threshold"] is None:  # lower tier never sits above the repair tier
        out["intent_threshold"] = min(DEFAULT_INTENT_THRESHOLD, out["likely_threshold"])
    value = out["intent_threshold"]
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0.0 < value <= out["likely_threshold"]):
        return None, "invalid_config"
    out["intent_threshold"] = float(value)
    pages = out["pages"]
    if pages is not None:  # 1-based page selection; out-of-range is checked against the PDF in run()
        if (not isinstance(pages, list) or not pages or len(pages) > MAX_SELECTED_PAGES
                or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in pages)
                or len(set(pages)) != len(pages)):
            return None, "invalid_config"
        out["pages"] = sorted(pages)
    return out, None


# --------------------------------------------------------------------------- images

def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def prepare_image(image: Image.Image) -> tuple[bytes, dict]:
    """Fit an upright raster to API limits: longest side <= 1568, each side >= 280.

    Downscale preserves aspect ratio; a too-small short side is padded with white
    (not stretched) so geometry stays faithful.  Output is baseline JPEG.
    Returns (jpeg_bytes, geometry) where geometry maps prepared pixels back to the
    source raster (content box inside the padded canvas).
    """
    img = image.convert("RGB")
    w, h = img.size
    if w < 1 or h < 1:
        raise VisualReviewError("invalid_image")
    scale = min(1.0, MAX_SIDE_PX / max(w, h))
    if min(w, h) * scale < 1:
        raise VisualReviewError("invalid_image")
    # Upscale tiny rasters only as far as both the long-side cap and short-side floor allow.
    if max(w, h) * scale < MIN_SIDE_PX:
        scale = MIN_SIDE_PX / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.LANCZOS)
    cw, ch = max(nw, MIN_SIDE_PX), max(nh, MIN_SIDE_PX)
    ox, oy = (cw - nw) // 2, (ch - nh) // 2
    if (cw, ch) != (nw, nh):
        canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
        canvas.paste(img, (ox, oy))
        img = canvas
    if not (MIN_SIDE_PX <= cw <= MAX_SIDE_PX and MIN_SIDE_PX <= ch <= MAX_SIDE_PX):
        raise VisualReviewError("invalid_image")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88, progressive=False, optimize=False)
    data = buf.getvalue()
    return data, {"width": cw, "height": ch, "content_box_px": [ox, oy, ox + nw, oy + nh],
                  "scale_from_source": round(scale, 6), "format": "jpg", "sha256": _sha(data)}


def _render(page: pymupdf.Page, clip: pymupdf.Rect | None, zoom: float) -> Image.Image:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def render_whole_page(page: pymupdf.Page) -> tuple[bytes, dict]:
    rect = page.rect  # displayed (rotation-applied) rectangle -> upright render
    zoom = MAX_SIDE_PX / max(rect.width, rect.height)
    data, geo = prepare_image(_render(page, None, zoom))
    geo.update(kind="whole_page", page_rect_pt=[round(v, 3) for v in rect], render_zoom=round(zoom, 6))
    return data, geo


def context_rect(page: pymupdf.Page, bbox: list) -> pymupdf.Rect:
    rect = pymupdf.Rect(*[float(v) for v in bbox])
    if rect.is_empty or rect.is_infinite:
        raise VisualReviewError("invalid_candidate_bbox")
    mx = max(CONTEXT_MARGIN_MIN_PT, rect.width * CONTEXT_MARGIN_RATIO)
    my = max(CONTEXT_MARGIN_MIN_PT, rect.height * CONTEXT_MARGIN_RATIO)
    clip = pymupdf.Rect(rect.x0 - mx, rect.y0 - my, rect.x1 + mx, rect.y1 + my) & page.rect
    if clip.is_empty:
        raise VisualReviewError("invalid_candidate_bbox")
    return clip


def render_crop(page: pymupdf.Page, bbox: list) -> tuple[bytes, dict]:
    clip = context_rect(page, bbox)
    zoom = min(MAX_CROP_ZOOM, MAX_SIDE_PX / max(clip.width, clip.height))
    data, geo = prepare_image(_render(page, clip, zoom))
    geo.update(kind="candidate_crop", clip_pt=[round(v, 3) for v in clip], render_zoom=round(zoom, 6),
               candidate_bbox_pt=[round(float(v), 3) for v in bbox])
    return data, geo


def box_to_pdf_points(box: dict, geo: dict) -> list[float] | None:
    """Map a normalized Box answer on a prepared image back to PDF points."""
    try:
        left, top, right, bottom = (float(box[k]) for k in ("left", "top", "right", "bottom"))
    except (KeyError, TypeError, ValueError):
        return None
    W, H = geo["width"], geo["height"]
    cx0, cy0, cx1, cy1 = geo["content_box_px"]
    if geo["kind"] == "whole_page":
        ox, oy, ox1, oy1 = geo["page_rect_pt"]
    else:
        ox, oy, ox1, oy1 = geo["clip_pt"]
    sx = (ox1 - ox) / max(cx1 - cx0, 1)
    sy = (oy1 - oy) / max(cy1 - cy0, 1)

    def px(vx: float, vy: float) -> tuple[float, float]:
        x = min(max(vx * W, cx0), cx1)
        y = min(max(vy * H, cy0), cy1)
        return ox + (x - cx0) * sx, oy + (y - cy0) * sy

    x0, y0 = px(left, top)
    x1, y1 = px(right, bottom)
    return [round(min(x0, x1), 2), round(min(y0, y1), 2), round(max(x0, x1), 2), round(max(y0, y1), 2)]


# --------------------------------------------------------------------------- questions

def _judgment_questions(subject: str) -> dict:
    return {
        "layout_verdict": {
            "type": "choice",
            "instructions": (f"Judge the overall visual layout of {subject} as a finished printed/slide page. "
                             "Consider overall balance, whether margins and placement look natural on the whole page, "
                             "and coarse breakage (overlap, overflow, clipping, collisions). Ignore wording and content."),
            "choices": list(VERDICTS),
            "criteria": {
                "repair_required": "Visibly broken or clearly unnatural placement/margins that a reader would notice; must be fixed.",
                "acceptable": "Balanced, natural margins and placement, no visible breakage.",
                "insufficient_information": "The image does not allow a judgment.",
            },
        },
        "placement_unnatural": {
            "type": "noul",
            "instructions": (f"In {subject}, do the margins or the placement of elements look unnatural or unbalanced "
                             "for the whole page, even if they might be numerically centered or aligned?"),
        },
        "visible_breakage": {
            "type": "noul",
            "instructions": f"In {subject}, is any text or element visibly overlapping, overflowing its container, or clipped?",
        },
    }


def _box_question(subject: str) -> dict:
    return {"problem_region": {"type": "box", "instructions":
            f"the region of {subject} where the layout looks broken, or where margins/placement look most unnatural"}}


def _asset(data: bytes) -> dict:
    return {"type": "image", "format": "jpg", "data_base64": base64.b64encode(data).decode("ascii")}


def build_body(images: dict[str, bytes], state: dict, questions: dict) -> tuple[dict, dict]:
    """Return (wire body, body-free digest record). ``state`` references asset uuids."""
    assets, refs = {}, {}
    for name, data in images.items():
        key = str(uuid.uuid4())
        assets[key] = _asset(data)
        refs[name] = key
    wire_state = {k: (refs[v[1:]] if isinstance(v, str) and v.startswith("@") and v[1:] in refs else v)
                  for k, v in state.items()}
    body = {"model": MODEL, "state": wire_state, "assets": assets, "questions": questions}
    # Digest binds questions, state and image hashes (uuid-independent, no base64 retained).
    record = {"model": MODEL, "questions": questions,
              "state": {k: (f"image:{_sha(images[v[1:]])}" if isinstance(v, str) and v.startswith("@") and v[1:] in refs else v)
                        for k, v in state.items()},
              "images": {name: _sha(data) for name, data in images.items()}}
    return body, {"request_sha256": _sha(_canonical(record)), "image_sha256": record["images"]}


# --------------------------------------------------------------------------- transport

def live_transport(body: dict, headers: dict, timeout: float) -> Any:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise VisualReviewError("provider_unavailable")
    data = json.dumps(body, separators=(",", ":")).encode()
    req = Request(ENDPOINT, data=data, method="POST",
                  headers={**headers, "Authorization": f"Bearer {key}"})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(2_000_000)
    except HTTPError as exc:
        raise VisualReviewError("provider_error", exc.code) from exc
    except TimeoutError as exc:
        raise VisualReviewError("timeout") from exc
    except URLError as exc:
        raise VisualReviewError("timeout" if isinstance(getattr(exc, "reason", None), TimeoutError) else "provider_error") from exc
    except OSError as exc:
        raise VisualReviewError("provider_error") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualReviewError("malformed_response") from exc


def request_headers() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json", IMAGES_HEADER[0]: IMAGES_HEADER[1]}


def _prob(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _parse_answers(raw: Any, questions: dict) -> tuple[dict, str | None, dict | None]:
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict) or set(raw["answers"]) != set(questions):
        raise VisualReviewError("malformed_response")
    model = raw.get("model")
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    usage = {k: v for k, v in (usage or {}).items() if isinstance(v, int) and not isinstance(v, bool)} or None
    parsed: dict[str, Any] = {}
    for key, q in questions.items():
        a = raw["answers"][key]
        if not isinstance(a, dict):
            raise VisualReviewError("malformed_response")
        if q["type"] == "choice":
            conf = _prob(a.get("confidence"))
            if a.get("choice") not in q["choices"] or conf is None:
                raise VisualReviewError("malformed_response")
            parsed[key] = {"choice": a["choice"], "confidence": conf}
        elif q["type"] == "noul":
            p = _prob(a.get("noul"))
            if p is None:
                raise VisualReviewError("malformed_response")
            parsed[key] = {"noul": p}
        elif q["type"] == "box":
            box, conf = a.get("box"), _prob(a.get("confidence"))
            if not isinstance(box, dict) or any(_prob(box.get(k)) is None for k in ("left", "top", "right", "bottom")) or conf is None:
                raise VisualReviewError("malformed_response")
            parsed[key] = {"box": {k: float(box[k]) for k in ("left", "top", "right", "bottom")}, "confidence": conf}
    return parsed, (model if isinstance(model, str) and len(model) <= 128 else None), usage


def call_once(transport: Transport, images: dict[str, bytes], state: dict, questions: dict,
              timeout: float) -> dict:
    """Exactly one provider attempt. Failure states are recorded, never raised."""
    record: dict[str, Any] = {"requested_model": MODEL, "provider": "typesafe-system-one", "attempts": 1}
    try:
        body, digest = build_body(images, state, questions)
        record.update(digest)
        if len(json.dumps(body, separators=(",", ":")).encode()) > MAX_REQUEST_BYTES:
            raise VisualReviewError("invalid_request")
        started = time.monotonic()
        raw = transport(body, request_headers(), timeout)
        record["seconds"] = round(time.monotonic() - started, 3)
        answers, model, usage = _parse_answers(raw, questions)
        record.update(answers=answers, response_model=model, usage=usage, images_accepted=True)
        record["status"] = "ok" if model == MODEL else "model_mismatch"
    except VisualReviewError as exc:
        record.update(status=exc.reason, images_accepted=False)
        if exc.status is not None:
            record["http_status"] = exc.status
    except TimeoutError:
        record.update(status="timeout", images_accepted=False)
    except Exception:
        record.update(status="provider_error", images_accepted=False)
    return record


def likely_off(answers: dict | None, threshold: float,
               repair_confidence: float = DEFAULT_REPAIR_CONFIDENCE_THRESHOLD) -> bool:
    """Repair tier: a confident repair_required choice or visible breakage (Box is asked)."""
    if not isinstance(answers, dict):
        return False
    verdict = answers.get("layout_verdict", {})
    return ((verdict.get("choice") == "repair_required" and verdict.get("confidence", 0.0) >= repair_confidence)
            or answers.get("visible_breakage", {}).get("noul", 0.0) >= threshold)


def differs_from_surroundings(answers: dict | None, intent_threshold: float = DEFAULT_INTENT_THRESHOLD) -> bool:
    """Intent tier: a repair_required choice (any confidence) or unnatural-looking placement.

    Only meaningful when ``likely_off`` is False; no Box is asked for this tier.
    """
    if not isinstance(answers, dict):
        return False
    return (answers.get("layout_verdict", {}).get("choice") == "repair_required"
            or answers.get("placement_unnatural", {}).get("noul", 0.0) >= intent_threshold)


def advisory_label(answers: dict | None, threshold: float,
                   repair_confidence: float = DEFAULT_REPAIR_CONFIDENCE_THRESHOLD,
                   intent_threshold: float | None = None) -> str:
    if not isinstance(answers, dict):
        return "not_judged"
    if likely_off(answers, threshold, repair_confidence):
        return "repair_required"
    if intent_threshold is None:
        intent_threshold = min(DEFAULT_INTENT_THRESHOLD, threshold)
    if differs_from_surroundings(answers, intent_threshold):
        return DIFFERS_LABEL
    choice = answers.get("layout_verdict", {}).get("choice")
    return choice if choice in VERDICTS else "insufficient_information"


# --------------------------------------------------------------------------- numeric cross-reference

def _intersects(a: list, b: list) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def region_measurements(page: pymupdf.Page, region: list, page_candidates: list[dict]) -> dict:
    """Body-free numeric readback inside a Box region plus intersecting numeric candidates."""
    rect = pymupdf.Rect(*region)
    lines = []
    for block in page.get_text("dict", clip=rect).get("blocks", []):
        for line in block.get("lines", []):
            bb = line.get("bbox")
            if bb and any(s.get("text", "").strip() for s in line.get("spans", [])):
                lines.append(bb)
    out: dict[str, Any] = {"region_pt": [round(v, 2) for v in region], "text_line_count": len(lines)}
    if lines:
        ink = [min(b[0] for b in lines), min(b[1] for b in lines), max(b[2] for b in lines), max(b[3] for b in lines)]
        pr = page.rect
        out["text_ink_bbox_pt"] = [round(v, 2) for v in ink]
        out["page_margins_pt"] = {"left": round(ink[0] - pr.x0, 2), "top": round(ink[1] - pr.y0, 2),
                                  "right": round(pr.x1 - ink[2], 2), "bottom": round(pr.y1 - ink[3], 2)}
        tops = sorted(b[1] for b in lines)
        gaps = [round(b - a, 2) for a, b in zip(tops, tops[1:]) if b - a > 0.5]
        if gaps:
            out["line_pitch_pt"] = {"min": min(gaps), "max": max(gaps)}
    out["intersecting_candidates"] = [
        {"id": c.get("id"), "rule": c.get("rule")} for c in page_candidates
        if isinstance(c.get("bbox_pdf_points"), list) and len(c["bbox_pdf_points"]) == 4
        and _intersects(c["bbox_pdf_points"], region)]
    return out


def _measured_text(candidate: dict) -> dict:
    keep = ("rule", "page", "bbox_pdf_points", "overlap_pt", "gutter_pt", "spacing_kind", "measurement",
            "affected_count", "affected_pages", "units", "measurement_basis")
    return {k: candidate[k] for k in keep if candidate.get(k) is not None}


# --------------------------------------------------------------------------- run

def run(*, artifact: Path, candidates: list[dict], config: Any,
        transport: Transport | None = None) -> dict:
    """Run the advisory image review. Never raises; never blocks."""
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "route": ROUTE, "advisory_only": True,
                              "blocks_completion": False, "satisfies_plan_c": False,
                              "model": MODEL, "provider": "typesafe-system-one",
                              "status": "not_run", "reason_code": "disabled", "provider_calls": 0,
                              "pages": [], "skipped_pages": [], "candidates": [], "skipped_candidates": []}
    cfg, err = normalize_config(config)
    if err:
        result["reason_code"] = err
        return result
    if cfg is None or not cfg["enabled"]:
        return result
    result["config"] = cfg
    fn = transport
    if fn is None:
        if not cfg["allow_live_provider"]:
            result["reason_code"] = "live_provider_not_allowed"
            return result
        if not os.environ.get(API_KEY_ENV, "").strip():
            result["reason_code"] = "provider_unavailable"
            return result
        fn = live_transport
    result["execution_mode"] = "fixture" if transport is not None else "live"
    try:
        artifact = Path(artifact)
        result["artifact"] = {"path": str(artifact.resolve()), "sha256": file_sha256(artifact)}
        doc = pymupdf.open(artifact)
    except Exception:
        result["reason_code"] = "artifact_unreadable"
        return result
    timeout, threshold = cfg["timeout_seconds"], cfg["likely_threshold"]
    repair_conf, intent = cfg["repair_confidence_threshold"], cfg["intent_threshold"]
    box_budget = cfg["max_box_calls"]
    review = [c for c in candidates if isinstance(c, dict) and c.get("severity") == "review"]
    by_page: dict[int, list[dict]] = {}
    for c in review:
        if isinstance(c.get("page"), int):
            by_page.setdefault(c["page"], []).append(c)

    def box_call(page: pymupdf.Page, images: dict, state: dict, subject: str, geo: dict,
                 page_candidates: list[dict]) -> dict | None:
        nonlocal box_budget
        if box_budget <= 0:
            return {"status": "skipped_box_cap"}
        box_budget -= 1
        result["provider_calls"] += 1
        rec = call_once(fn, images, state, _box_question(subject), timeout)
        if rec.get("answers"):
            region = box_to_pdf_points(rec["answers"]["problem_region"]["box"], geo)
            if region:
                rec["region_pt"] = region
                rec["numeric_cross_reference"] = region_measurements(page, region, page_candidates)
        return rec

    try:
        page_count = doc.page_count
        selected = set(cfg["pages"]) if cfg["pages"] is not None else None
        if selected is not None and max(selected) > page_count:
            result.update(reason_code="invalid_config", config_error="pages_out_of_range", page_count=page_count)
            return result
        # Layer 1: whole-page judgment for every (selected) page, explicitly capped.
        judged = 0
        for index in range(page_count):
            number = index + 1
            if selected is not None and number not in selected:
                result["skipped_pages"].append({"page": number, "reason": "not_selected"})
                continue
            if judged >= cfg["max_pages"]:
                result["skipped_pages"].append({"page": number, "reason": "max_pages"})
                continue
            judged += 1
            entry: dict[str, Any] = {"page": number}
            try:
                page = doc[index]
                data, geo = render_whole_page(page)
                entry["image"] = geo
                state = {"page_image": "@page",
                         "context": f"Rendered page {number} of {page_count} of a generated PDF document."}
                result["provider_calls"] += 1
                rec = call_once(fn, {"page": data}, state, _judgment_questions("`page_image`"), timeout)
                entry["judgment"] = rec
                entry["label"] = advisory_label(rec.get("answers"), threshold, repair_conf, intent) if rec["status"] in {"ok", "model_mismatch"} else "not_judged"
                if entry["label"] == "repair_required":
                    entry["box"] = box_call(page, {"page": data}, {"page_image": "@page"}, "`page_image`", geo,
                                            by_page.get(number, []))
            except VisualReviewError as exc:
                entry.update(label="not_judged", judgment={"status": exc.reason, "attempts": 0})
            result["pages"].append(entry)
        # Layer 2: candidate crop judgment, explicitly capped.
        calls = 0
        for c in review:
            cid = c.get("id")
            if selected is not None and c.get("page") not in selected:
                result["skipped_candidates"].append({"id": cid, "reason": "page_not_selected"})
                continue
            if calls >= cfg["max_candidate_calls"]:
                result["skipped_candidates"].append({"id": cid, "reason": "max_candidate_calls"})
                continue
            page_no, bbox = c.get("page"), c.get("bbox_pdf_points")
            if not isinstance(page_no, int) or not 1 <= page_no <= page_count or not isinstance(bbox, list) or len(bbox) != 4:
                result["skipped_candidates"].append({"id": cid, "reason": "no_page_bbox"})
                continue
            entry = {"id": cid, "rule": c.get("rule"), "page": page_no}
            try:
                page = doc[page_no - 1]
                whole, _ = render_whole_page(page)
                crop, geo = render_crop(page, bbox)
                entry["image"] = geo
                state = {"page_image": "@page", "crop_image": "@crop",
                         "context": ("`crop_image` is an enlarged region of `page_image` around a layout review candidate "
                                     "found by numeric checks. Measured values (PDF points): "
                                     + json.dumps(_measured_text(c), ensure_ascii=False, sort_keys=True))}
                calls += 1
                result["provider_calls"] += 1
                rec = call_once(fn, {"page": whole, "crop": crop}, state,
                                _judgment_questions("`crop_image` (judged within the whole `page_image`)"), timeout)
                entry["judgment"] = rec
                entry["label"] = advisory_label(rec.get("answers"), threshold, repair_conf, intent) if rec["status"] in {"ok", "model_mismatch"} else "not_judged"
                if entry["label"] == "repair_required":
                    entry["box"] = box_call(page, {"crop": crop}, {"crop_image": "@crop"}, "`crop_image`", geo,
                                            by_page.get(page_no, []))
            except VisualReviewError as exc:
                entry.update(label="not_judged", judgment={"status": exc.reason, "attempts": 0})
            result["candidates"].append(entry)
    finally:
        doc.close()
    statuses = [e.get("judgment", {}).get("status") for e in result["pages"] + result["candidates"]]
    ok = [s for s in statuses if s in {"ok", "model_mismatch"}]
    result["status"] = "evaluated" if ok and len(ok) == len(statuses) else "partial" if ok else "not_run"
    failures = sorted({s for s in statuses if s not in {"ok"}} - {None})
    result["reason_code"] = "ok" if not failures else failures[0]
    result["labels"] = {"pages_repair_required": [e["page"] for e in result["pages"] if e.get("label") == "repair_required"],
                        "candidates_repair_required": [e["id"] for e in result["candidates"] if e.get("label") == "repair_required"],
                        "pages_differs_from_surroundings": [e["page"] for e in result["pages"] if e.get("label") == DIFFERS_LABEL],
                        "candidates_differs_from_surroundings": [e["id"] for e in result["candidates"]
                                                                 if e.get("label") == DIFFERS_LABEL]}
    return result


def write_evidence(result: dict, path: Path) -> dict:
    path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return {"path": str(path.resolve()), "sha256": file_sha256(path)}


def summary(result: dict, handle: dict | None) -> dict:
    """Small, advisory scan annotation (no answers duplicated into the scan)."""
    return {"route": ROUTE, "advisory_only": True, "blocks_completion": False, "satisfies_plan_c": False,
            "model": MODEL, "status": result.get("status"), "reason_code": result.get("reason_code"),
            "provider_calls": result.get("provider_calls", 0), "evidence": handle,
            "labels": copy.deepcopy(result.get("labels", {})),
            "selected_pages": copy.deepcopy((result.get("config") or {}).get("pages")),
            "skipped_pages": len(result.get("skipped_pages", [])),
            "skipped_candidates": len(result.get("skipped_candidates", []))}


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Advisory Jev 1.14 image review of a PDF (live, one attempt per request).")
    p.add_argument("--pdf", required=True)
    p.add_argument("--scan", help="AQ-LAYOUT-01 scan JSON whose review candidates are judged")
    p.add_argument("--candidate-id", action="append", default=[], help="limit candidate layer to these ids")
    p.add_argument("--out", required=True)
    p.add_argument("--page", type=int, action="append", default=None,
                   help="judge only these 1-based pages (repeatable); default all pages")
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    p.add_argument("--max-candidate-calls", type=int, default=DEFAULT_MAX_CANDIDATE_CALLS)
    p.add_argument("--max-box-calls", type=int, default=DEFAULT_MAX_BOX_CALLS)
    p.add_argument("--live", action="store_true", help="required to allow provider calls")
    args = p.parse_args(argv)
    candidates: list[dict] = []
    if args.scan:
        scan = json.loads(Path(args.scan).read_text(encoding="utf-8"))
        candidates = [i for i in scan.get("issues", []) if isinstance(i, dict) and i.get("severity") == "review"]
        if args.candidate_id:
            wanted = set(args.candidate_id)
            candidates = [c for c in candidates if c.get("id") in wanted]
    result = run(artifact=Path(args.pdf), candidates=candidates,
                 config={"enabled": True, "allow_live_provider": args.live, "max_pages": args.max_pages,
                         "max_candidate_calls": args.max_candidate_calls, "max_box_calls": args.max_box_calls,
                         **({"pages": args.page} if args.page else {})})
    handle = write_evidence(result, Path(args.out))
    print(json.dumps(summary(result, handle), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
