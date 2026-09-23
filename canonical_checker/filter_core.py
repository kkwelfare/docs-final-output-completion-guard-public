"""Pure, deterministic final-output cleanup shared by plugin and document checks.

The parser preserves Markdown structure and protected syntax. Only isolated prose
segments are considered, and ambiguity fails open at the smallest segment.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

MAX_REWRITES = 2
MAX_DOCUMENT_REWRITES = 8

SENTENCE_PROTECTED = re.compile(
    r"\d|https?://|(?:^|\s)/[\w./~:-]+|(?:^|\s)[A-Za-z]:\\[\\\w. -]+|MEDIA:|`[^`]+`|\b[A-Z][A-Za-z0-9_-]*\b"
)
UNSAFE_CONDITIONAL = re.compile(
    r"承認|許可|権限|認可|停止|安全|再起動|restart|gateway|送信|Slack|Discord|"
    r"外部|費用|課金|支払|payment|cost|時刻|時間|手順|依存|authorization|approval",
    re.IGNORECASE,
)
OFFER_PREFIX = re.compile(r"^(必要なら|必要であれば)[、,\s]*")
UNCERTAINTY = re.compile(r"^(?P<subject>.+?)(だけでは|のみでは)(結論|判断|断定)(?:でき|し)ません[。．]?$")
CONFIRMED = re.compile(r"(?:確認済み|確認できた|確認した)(?:は|.{0,60}(?:は|:|：))")
UNRESOLVED = re.compile(r"(?:未確認|不明|未解決|確認できていない)")
CONTRAST_C1 = re.compile(r"^これは単なる(?P<a>[^。．!?！？\n]{1,80})ではなく[、,\s]*(?P<b>[^。．!?！？\n]{1,120}(?:です|ます|でした|ました))[。．]?$")
CONTRAST_C2 = re.compile(r"^(?P<a>[^。．!?！？\n]{1,80})というより[、,\s]*(?P<b>[^。．!?！？\n]{1,120}(?:です|ます|でした|ました))[。．]?$")
PROPER_NOUN_MARKER = re.compile(r"(?:株式会社|有限会社|合同会社|[都道府県]$|[市区町村]$|大学|病院|銀行)")

# Markdown and technical spans are opaque. The outer scanner retains every byte.
INLINE_PROTECTED = re.compile(
    r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|https?://[^\s<>()]+|"
    r"(?:^|(?<=\s))(?:/[\w.~:-]+(?:/[\w.~:-]+)*|[A-Za-z]:\\[^\s<>|]*)|MEDIA:\S*"
)
LINE_PREFIX = re.compile(r"^(?P<prefix>\s*(?:#{1,6}\s+|(?:[-*+] |\d+[.)] )))")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
BLOCKQUOTE = re.compile(r"^\s*>")
PIPE_TABLE = re.compile(r"^\s*\|.*\|\s*$")
MEDIA_LINE = re.compile(r"^\s*MEDIA:")


@dataclass(frozen=True)
class FilterResult:
    text: str
    rewrite_count: int
    rules: tuple[str, ...]
    protected: bool = False


@dataclass(frozen=True)
class _SegmentResult:
    text: str
    rules: tuple[str, ...]
    candidates: int


def _split_sentences(text: str) -> list[str]:
    """Keep delimiters and newlines exactly; no inferred boundaries are allowed."""
    parts = re.findall(r"[^。．!?！？]+[。．!?！？]?", text)
    return [part for part in parts if part]


def _is_protected(sentence: str) -> bool:
    return bool(SENTENCE_PROTECTED.search(sentence))


def _safe_offer(sentence: str) -> str | None:
    if not OFFER_PREFIX.match(sentence) or _is_protected(sentence):
        return None
    if UNSAFE_CONDITIONAL.search(sentence):
        return None
    candidate = OFFER_PREFIX.sub("", sentence, count=1)
    if not re.search(r"(?:できます|可能です|選択肢は.+です)[。．]?$", candidate):
        return None
    return candidate


def _safe_contrast(sentence: str) -> tuple[str, str] | None:
    if _is_protected(sentence):
        return None
    match = CONTRAST_C1.match(sentence)
    rule = "C1"
    if match is None:
        match = CONTRAST_C2.match(sentence)
        rule = "C2"
    if match is None:
        return None
    left, right = match.group("a").strip(), match.group("b").strip()
    if not left or not right or PROPER_NOUN_MARKER.search(left) or PROPER_NOUN_MARKER.search(right):
        return None
    ending = sentence[-1] if sentence[-1:] in "。．" else ""
    return right + ending, rule


def _redundant_uncertainty(sentences: list[str], index: int) -> bool:
    sentence = sentences[index]
    match = UNCERTAINTY.match(sentence.strip())
    if not match or _is_protected(sentence):
        return False
    neighbours: Iterable[str] = sentences[max(0, index - 2):index] + sentences[index + 1:index + 3]
    context = "".join(neighbours)
    subject = match.group("subject").strip()
    return bool(subject and subject in context and CONFIRMED.search(context) and UNRESOLVED.search(context))


def _filter_prose(text: str, limit: int) -> _SegmentResult:
    """Filter one prose segment, rolling back that segment on its third candidate."""
    sentences = _split_sentences(text)
    if not sentences or "".join(sentences) != text:
        return _SegmentResult(text, (), 0)
    rewritten = list(sentences)
    rules: list[str] = []
    candidates = 0
    for index, sentence in enumerate(sentences):
        result: str | None = _safe_offer(sentence)
        rule: str | None = "F1" if result is not None and "必要なら" in sentence else ("F2" if result is not None else None)
        if result is None:
            contrast = _safe_contrast(sentence)
            if contrast is not None:
                result, rule = contrast
        if result is None and _redundant_uncertainty(sentences, index):
            result, rule = "", "E1"
        if result is None:
            continue
        candidates += 1
        if candidates > limit:
            return _SegmentResult(text, (), candidates)
        rewritten[index] = result
        rules.append(rule or "")
    return _SegmentResult("".join(rewritten) if rules else text, tuple(rules), candidates)


def _line_parts(line: str) -> list[tuple[bool, str]]:
    """Split a normal Markdown line into opaque spans and prose spans."""
    result: list[tuple[bool, str]] = []
    cursor = 0
    prefix = LINE_PREFIX.match(line)
    if prefix:
        result.append((True, prefix.group("prefix")))
        cursor = prefix.end()
    for match in INLINE_PROTECTED.finditer(line, cursor):
        if match.start() > cursor:
            result.append((False, line[cursor:match.start()]))
        result.append((True, match.group(0)))
        cursor = match.end()
    if cursor < len(line):
        result.append((False, line[cursor:]))
    return result or [(False, line)]


def _segments(text: str) -> list[tuple[bool, str]]:
    """Return protected/prose segments without changing newline or Markdown bytes."""
    parts: list[tuple[bool, str]] = []
    fenced = False
    marker = ""
    for line in text.splitlines(keepends=True) or [text]:
        fence = FENCE.match(line)
        if fenced:
            parts.append((True, line))
            if fence and fence.group(1).startswith(marker):
                fenced = False
            continue
        if fence:
            fenced = True
            marker = fence.group(1)[0]
            parts.append((True, line))
        elif BLOCKQUOTE.match(line) or PIPE_TABLE.match(line) or MEDIA_LINE.match(line):
            parts.append((True, line))
        else:
            parts.extend(_line_parts(line))
    return parts


def filter_text(
    text: str,
    *,
    mode: str = "response",
    max_rewrites: int | None = None,
    document_global_cap: int | None = None,
) -> FilterResult:
    """Safely clean multiline Markdown while preserving the historical API.

    ``response`` keeps the historical whole-response two-candidate fail-open.
    ``document`` applies the two-candidate cap per prose segment and a visible
    global cap (default eight); exceeding either preserves only that segment.
    """
    try:
        if not isinstance(text, str) or not text:
            return FilterResult(text=text, rewrite_count=0, rules=(), protected=True)
        if mode not in {"response", "document"}:
            return FilterResult(text=text, rewrite_count=0, rules=())
        local_limit = max_rewrites if isinstance(max_rewrites, int) and max_rewrites >= 0 else MAX_REWRITES
        global_limit = local_limit if mode == "response" else (
            document_global_cap if isinstance(document_global_cap, int) and document_global_cap >= 0 else MAX_DOCUMENT_REWRITES
        )
        source = _segments(text)
        output: list[str] = []
        rules: list[str] = []
        protected = any(flag for flag, _ in source)
        for is_protected, segment in source:
            if is_protected or not segment:
                output.append(segment)
                continue
            filtered = _filter_prose(segment, local_limit)
            if mode == "response" and len(rules) + filtered.candidates > global_limit:
                return FilterResult(text=text, rewrite_count=0, rules=(), protected=protected)
            if mode == "document" and (len(rules) >= global_limit or filtered.candidates > local_limit or len(rules) + len(filtered.rules) > global_limit):
                output.append(segment)
                continue
            output.append(filtered.text)
            rules.extend(filtered.rules)
        result = "".join(output)
        return FilterResult(result if rules else text, len(rules), tuple(rules), protected=protected)
    except Exception:
        return FilterResult(text=text, rewrite_count=0, rules=())
