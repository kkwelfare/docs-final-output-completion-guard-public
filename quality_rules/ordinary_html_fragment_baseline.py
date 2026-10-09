"""Fail-closed CMS prose-fragment completion contract (no rendering route)."""
from __future__ import annotations
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ordinary-html-fragment-baseline-1"
FORBIDDEN_TAGS = {"html", "head", "body", "script", "style", "iframe", "object", "embed", "form", "input", "button", "select", "textarea", "video", "audio", "canvas", "svg"}
ALLOWED_TAGS = {"p", "h2", "h3", "h4", "h5", "h6", "figure", "img", "a", "strong", "em", "b", "i", "ul", "ol", "li", "blockquote", "br", "hr", "span", "div", "table", "thead", "tbody", "tr", "th", "td", "caption", "code", "pre", "small", "sup", "sub"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

class _Fragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.error = None
        self.stack = []
    def handle_starttag(self, tag, attrs):
        tag=tag.lower()
        self._tag("start", tag, attrs)
        if tag not in VOID_TAGS: self.stack.append(tag)
    def handle_startendtag(self, tag, attrs): self._tag("void", tag, attrs)
    def handle_endtag(self, tag):
        tag=tag.lower()
        if tag in VOID_TAGS: return
        if not self.stack or self.stack[-1]!=tag:
            self.error="fragment contains unbalanced or mismatched tags"
            return
        self.stack.pop()
        self.tokens.append(("end", tag, ()))
    def handle_data(self, data): self.tokens.append(("text", data))
    def handle_comment(self, data): self.tokens.append(("comment", data))
    def handle_decl(self, decl): self.tokens.append(("decl", decl))
    def unknown_decl(self, data): self.tokens.append(("unknown", data))
    def _tag(self, kind, tag, attrs):
        tag = tag.lower()
        if tag in FORBIDDEN_TAGS:
            self.error = self.error or f"fragment contains disallowed {tag} element"
        elif tag not in ALLOWED_TAGS:
            self.error = self.error or f"fragment contains unsupported {tag} element"
        normalized = tuple(sorted((str(k).lower(), v or "") for k, v in attrs))
        for key, value in normalized:
            if key.startswith("on"): self.error = "fragment contains an event-handler attribute"
            if key in {"href", "src", "srcset", "action", "poster"} and value.strip().lower().startswith("javascript:"):
                self.error = "fragment contains a javascript URL"
        self.tokens.append((kind, tag, normalized))

def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def _handle(value: Any, label: str):
    if not isinstance(value,dict) or set(value)!={"path","sha256"}: return None,f"{label} handle is malformed"
    p=Path(value.get("path")) if isinstance(value.get("path"),str) else Path()
    if not p.is_absolute() or not p.is_file(): return None,f"{label} is missing"
    if value.get("sha256")!=sha256(p): return None,f"{label} hash drift"
    return p.resolve(),None

def _json(path: Path,label: str):
    try: value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,json.JSONDecodeError): return None,f"{label} is malformed"
    if not isinstance(value,dict): return None,f"{label} is malformed"
    return value,None

def _parse(path: Path):
    try: text=path.read_text(encoding="utf-8")
    except (OSError,UnicodeError): return None,"fragment HTML is unreadable"
    parser=_Fragment()
    try: parser.feed(text); parser.close()
    except Exception: return None,"fragment HTML parse failed"
    if parser.error: return None,parser.error
    if parser.stack: return None,"fragment contains unclosed tags"
    if any(t[0]=="decl" for t in parser.tokens): return None,"fragment must not contain a doctype or document declaration"
    return parser.tokens,None

def validate(contract_path: Path, artifacts: list[str], *, trusted_task_id: str | None = None, trusted_run_id: int | None = None) -> str | None:
    if not contract_path.is_absolute() or not contract_path.is_file(): return "ordinary HTML fragment contract is missing"
    c,error=_json(contract_path,"ordinary HTML fragment contract")
    if error: return error
    required={"schema_version","route","artifact_kind","editorial_scope","source","target","change_evidence","source_identity","receiver_readback","final_output_receipts"}
    if set(c)!=required or c.get("schema_version")!=SCHEMA_VERSION: return "ordinary HTML fragment contract schema mismatch"
    if c.get("route")!="cms_prose_fragment" or c.get("artifact_kind")!="cms-prose-fragment" or c.get("editorial_scope")!="text-nodes-only": return "fragment route must explicitly declare CMS prose-only scope"
    if artifacts: return "CMS prose-fragment route does not deliver a standalone HTML artifact"
    source,error=_handle(c.get("source"),"source HTML")
    if error:return error
    target,error=_handle(c.get("target"),"target HTML")
    if error:return error
    if source.suffix.lower() not in {".html",".htm"} or target.suffix.lower() not in {".html",".htm"}: return "fragment source and target must be HTML files"
    before,error=_parse(source)
    if error:return error
    after,error=_parse(target)
    if error:return error
    structural=lambda ts:[t for t in ts if t[0]!="text"]
    if structural(before)!=structural(after): return "source and target markup, attributes, URLs, or media differ"
    identity=c.get("source_identity")
    if not isinstance(identity,dict) or set(identity)!={"task_id","run_id","target_id"}:
        return "source task/run/target identity is malformed"
    changes_path,error=_handle(c.get("change_evidence"),"change evidence")
    if error:return error
    report,error=_json(changes_path,"change evidence")
    if error:return error
    if report.get("task_id")!=c["source_identity"].get("task_id") or report.get("source_sha256")!=sha256(source) or report.get("revised_sha256")!=sha256(target): return "change evidence source/target identity mismatch"
    changes=report.get("changes")
    if not isinstance(changes,list) or not changes:return "fragment change list is missing"
    try:
        reproduced=source.read_text(encoding="utf-8")
        for row in changes:
            if not isinstance(row,dict) or not isinstance(row.get("before"),str) or not isinstance(row.get("after"),str) or not row["before"] or not row["after"]: return "fragment change list is malformed"
            if reproduced.count(row["before"])!=1:return "fragment change does not uniquely identify source prose"
            reproduced=reproduced.replace(row["before"],row["after"],1)
        if reproduced!=target.read_text(encoding="utf-8"):return "target prose differs from the declared text-only change list"
    except (OSError,UnicodeError):return "fragment source/target readback failed"
    identity=c.get("source_identity")
    if not isinstance(identity,dict) or set(identity)!={"task_id","run_id","target_id"}:
        return "source task/run/target identity is malformed"
    if (not isinstance(identity.get("task_id"),str) or not identity["task_id"]
            or type(identity.get("run_id")) is not int or identity["run_id"] < 1
            or type(identity.get("target_id")) is not int or identity["target_id"] < 1):
        return "source task/run/target identity is malformed"
    if trusted_task_id is None or identity["task_id"] != trusted_task_id:
        return "trusted source task identity mismatch"
    if trusted_run_id is None or identity["run_id"] != trusted_run_id:
        return "trusted source run identity mismatch"
    if artifacts:
        return "CMS prose-fragment route keeps its HTML source/target internal; artifacts must be empty"
    if not isinstance(c.get("source_identity"),dict):
        return "source task/run/target identity is malformed"
    receiver_path,error=_handle(c.get("receiver_readback"),"actual CMS receiver readback")
    if error:return error
    receiver,error=_json(receiver_path,"actual CMS receiver readback")
    if error:return error
    if receiver.get("id")!=identity["target_id"] or receiver.get("status")!="draft" or not isinstance(receiver.get("content"),dict) or receiver["content"].get("raw")!=target.read_text(encoding="utf-8"): return "actual receiver readback does not exactly match target draft content"
    receipts=c.get("final_output_receipts")
    if not isinstance(receipts,list) or len(receipts)!=1:return "canonical final-output receipt handle is missing"
    receipt_path,error=_handle(receipts[0],"canonical final-output receipt")
    if error:return error
    receipt,error=_json(receipt_path,"canonical final-output receipt")
    if error:return error
    entries=receipt.get("entries")
    if receipt.get("schema_version")!="docs-final-output-receipt-1" or receipt.get("status")!="pass" or not isinstance(entries,list) or len(entries)!=1:return "canonical final-output receipt did not pass"
    e=entries[0]
    if not isinstance(e,dict) or e.get("status")!="pass" or Path(str(e.get("artifact_path",""))).resolve()!=target or e.get("artifact_sha256")!=sha256(target):return "canonical final-output receipt does not cover exactly the target"
    return None
