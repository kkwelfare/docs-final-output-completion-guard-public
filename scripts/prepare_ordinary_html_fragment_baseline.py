#!/usr/bin/env python3
"""Bind actual prose-fragment source, edit, receiver and checker readbacks."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from quality_rules import ordinary_html_fragment_baseline as fragment

def handle(p: Path): return {"path":str(p.resolve(strict=True)),"sha256":fragment.sha256(p)}
def main():
 p=argparse.ArgumentParser(description=__doc__)
 for name in ("source","target","change-evidence","source-identity","receiver-readback","final-output-receipt","contract"):
  p.add_argument("--"+name,required=True,type=Path)
 args=p.parse_args()
 paths=[args.source,args.target,args.change_evidence,args.source_identity,args.receiver_readback,args.final_output_receipt,args.contract]
 if any(not x.is_absolute() for x in paths): p.error("all paths must be absolute")
 if args.contract.exists(): p.error("contract output must be a new path")
 identity=json.loads(args.source_identity.read_text(encoding="utf-8"))
 contract={"schema_version":fragment.SCHEMA_VERSION,"route":"cms_prose_fragment","artifact_kind":"cms-prose-fragment","editorial_scope":"text-nodes-only","source":handle(args.source),"target":handle(args.target),"change_evidence":handle(args.change_evidence),"source_identity":identity,"receiver_readback":handle(args.receiver_readback),"final_output_receipts":[handle(args.final_output_receipt)]}
 args.contract.parent.mkdir(parents=True,exist_ok=True)
 args.contract.write_text(json.dumps(contract,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
 import subprocess
 observed=subprocess.run(["hermes","kanban","show",identity["task_id"],"--json"],capture_output=True,text=True,check=True,timeout=30).stdout
 state=json.loads(observed[observed.index("{"):])
 run_id=max((r["id"] for r in state.get("runs",[])),default=None)
 error=fragment.validate(args.contract,[],trusted_task_id=identity["task_id"],trusted_run_id=run_id)
 print(json.dumps({"status":"prepared" if not error else "blocked","contract":str(args.contract),"validation":error or "pass","completion":"not_submitted"},ensure_ascii=False,sort_keys=True))
 return 0 if not error else 2
if __name__=="__main__": raise SystemExit(main())
