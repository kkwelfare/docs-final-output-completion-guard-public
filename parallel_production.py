#!/usr/bin/env python3
"""Parallel-once document coordinator with bounded serial finishing."""
from __future__ import annotations
import argparse, copy, fnmatch, hashlib, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCHEMA = "docs-parallel-integration-receipt-2"
MISSING = object()
SAFE_ROLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_FINISHING_ATTEMPTS_CAP = 5

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()

def dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def esc(token: str) -> str: return token.replace("~", "~0").replace("/", "~1")
def leaves(value: Any, pointer: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        if not value: return {pointer: {}}
        out = {}
        for k, v in value.items(): out.update(leaves(v, pointer + "/" + esc(str(k))))
        return out
    if isinstance(value, list):
        if not value: return {pointer: []}
        out = {}
        for i, v in enumerate(value): out.update(leaves(v, pointer + "/" + str(i)))
        return out
    return {pointer: value}

def changes(base: Any, other: Any) -> dict[str, tuple[Any, Any]]:
    a, b, out = leaves(base), leaves(other), {}
    for p in sorted(set(a) | set(b)):
        av, bv = a.get(p, MISSING), b.get(p, MISSING)
        if av is MISSING or bv is MISSING or av != bv: out[p] = (av, bv)
    return out

def list_shape_changes(base: Any, other: Any, pointer: str = "") -> list[str]:
    out = []
    if isinstance(base, list) and isinstance(other, list):
        if len(base) != len(other): out.append(pointer or "/")
        for i, (a, b) in enumerate(zip(base, other)): out.extend(list_shape_changes(a, b, pointer + "/" + str(i)))
    elif isinstance(base, dict) and isinstance(other, dict):
        for key in sorted(set(base) & set(other), key=str): out.extend(list_shape_changes(base[key], other[key], pointer + "/" + esc(str(key))))
    return out

def validate_role_name(name: Any) -> str:
    if not isinstance(name, str) or not SAFE_ROLE_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("role name must be one safe path component using letters, digits, dot, underscore, or hyphen")
    return name

def normalize_stream(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, bytes): return value.decode("utf-8", errors="replace")
    return str(value)

def allowed(pointer: str, patterns: list[str]) -> bool: return any(fnmatch.fnmatchcase(pointer, p) for p in patterns)
def conflict(a: str, b: str) -> bool: return a == b or a.startswith(b + "/") or b.startswith(a + "/")
def tokens(pointer: str) -> list[str]: return [] if pointer == "" else [x.replace("~1", "/").replace("~0", "~") for x in pointer.split("/")[1:]]
def apply_change(target: Any, pointer: str, value: Any) -> None:
    ts = tokens(pointer)
    if not ts: raise ValueError("root replacement is not supported")
    cur = target
    for t in ts[:-1]: cur = cur[int(t)] if isinstance(cur, list) else cur[t]
    last = ts[-1]
    if value is MISSING:
        if isinstance(cur, list): cur.pop(int(last))
        else: del cur[last]
    elif isinstance(cur, list): cur[int(last)] = copy.deepcopy(value)
    else: cur[last] = copy.deepcopy(value)

def expand(argv: list[str], values: dict[str, str]) -> list[str]: return [part.format(**values) for part in argv]
def within(path: Path, root: Path) -> bool:
    try: path.resolve().relative_to(root.resolve()); return True
    except ValueError: return False

def file_record(path: Path) -> dict[str, Any]: return {"path": str(path.resolve()), "sha256": sha256(path)}
def run_process(argv: list[str], *, timeout: float, evidence_dir: Path, label: str, cwd: Path | None = None) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    start, mono_start = time.time_ns(), time.monotonic_ns()
    try:
        proc = subprocess.run(argv, cwd=cwd, env=os.environ.copy(), text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(argv, 124, normalize_stream(exc.stdout), normalize_stream(exc.stderr) + f"\n{label} timed out after {timeout}s")
    mono_end, end = time.monotonic_ns(), time.time_ns()
    stdout_path, stderr_path = evidence_dir / f"{label}-stdout.txt", evidence_dir / f"{label}-stderr.txt"
    stdout_path.write_text(normalize_stream(proc.stdout), encoding="utf-8")
    stderr_path.write_text(normalize_stream(proc.stderr), encoding="utf-8")
    return {"argv": argv, "returncode": proc.returncode, "timeout_seconds": timeout, "timing": {"started_unix_ns": start, "ended_unix_ns": end, "monotonic_start_ns": mono_start, "monotonic_end_ns": mono_end, "duration_ms": round((mono_end - mono_start) / 1e6, 3)}, "stdout": file_record(stdout_path), "stderr": file_record(stderr_path)}

def run_role(role: dict[str, Any], *, base: Path, base_hash: str, task_id: str, work_root: Path) -> dict[str, Any]:
    name = validate_role_name(role["name"]); role_dir = work_root / name; role_dir.mkdir(parents=True, exist_ok=False)
    input_path, output_path, scope_path = role_dir / "input.json", role_dir / "output.json", role_dir / "scope.json"
    input_path.write_bytes(base.read_bytes())
    scope = {"schema_version": "docs-role-scope-1", "task_id": task_id, "role": name, "allowed_json_pointers": role["allowed_json_pointers"]}; dump(scope_path, scope)
    values = {"input": str(input_path), "output": str(output_path), "scope": str(scope_path), "task_id": task_id, "role": name, "workcopy": str(role_dir)}
    rec = run_process(expand(role["command_argv"], values), timeout=float(role.get("timeout_seconds", 60)), evidence_dir=role_dir, label="worker", cwd=role_dir)
    rec.update({"name": name, "scope": scope, "scope_file": file_record(scope_path), "workcopy": str(role_dir), "input": file_record(input_path), "command_argv": rec["argv"]})
    errors = []
    if rec["input"]["sha256"] != base_hash: errors.append("role input hash differs from common base after worker execution")
    if rec["returncode"] != 0: errors.append(f"worker returned {rec['returncode']}")
    if not output_path.is_file(): errors.append("worker did not create output.json")
    if errors: rec.update({"status": "fail", "errors": errors}); return rec
    try: data = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc: rec.update({"status": "fail", "errors": [f"invalid output JSON: {exc}"]}); return rec
    rec["output"], rec["status"], rec["errors"], rec["_data"] = file_record(output_path), "pass", [], data
    llm_evidence = role_dir / "llm-evidence.json"
    if llm_evidence.is_file():
        try: llm_data = json.loads(llm_evidence.read_text(encoding="utf-8"))
        except Exception as exc:
            rec.update({"status": "fail", "errors": [f"invalid LLM evidence JSON: {exc}"]}); return rec
        rec["llm_evidence"] = {"file": file_record(llm_evidence), "schema_version": llm_data.get("schema_version"), "status": llm_data.get("status"), "usage_summary": llm_data.get("usage_summary")}
    return rec

def _template_path(config: dict, key: str, values: dict[str, str], default: str) -> Path:
    return Path(str(config.get(key, default)).format(**values)).resolve()

def evaluate_source(config: dict[str, Any], *, source: Path, stage_dir: Path, task_id: str) -> dict[str, Any]:
    stage_dir.mkdir(parents=True, exist_ok=False)
    values = {"task_id": task_id, "source": str(source), "current": str(source), "integrated": str(source), "stage_dir": str(stage_dir), "render_dir": str(stage_dir / "rendered"), "render_result": str(stage_dir / "render-result.json"), "source_text": str(stage_dir / "visible-source.md"), "verification_report": str(stage_dir / "rendered" / "verification-report.json"), "layout_receipt": str(stage_dir / "layout-check-receipt.json"), "checker_receipt": str(stage_dir / "final-output-receipt.json")}
    artifact = _template_path(config, "artifact_path_template", values, config.get("artifact_path", str(source)))
    values["artifact"] = str(artifact)
    rec: dict[str, Any] = {"source": file_record(source), "stage_dir": str(stage_dir), "status": "pass", "diagnostics": []}
    render_argv = config.get("render_argv")
    if render_argv:
        proc = run_process(expand(render_argv, values), timeout=float(config.get("render_timeout_seconds", 120)), evidence_dir=stage_dir, label="render")
        rec["render"] = proc
        if proc["returncode"] != 0:
            rec.update({"status": "fail", "diagnostics": ["renderer process failed"]}); return rec
        selection = config.get("artifact_selection")
        if selection is not None:
            try:
                producer_result_path = Path(values["render_result"]).resolve(strict=True)
                producer_result = json.loads(producer_result_path.read_text(encoding="utf-8"))
            except Exception as exc:
                rec.update({"status": "fail", "diagnostics": [f"producer result is missing or invalid: {exc}"]}); return rec
            if not isinstance(selection, dict) or selection.get("mode") != "producer_primary":
                rec.update({"status": "fail", "diagnostics": ["artifact_selection mode must be producer_primary"]}); return rec
            if producer_result.get("task_id") != task_id:
                rec.update({"status": "fail", "diagnostics": ["producer result task identity mismatch"]}); return rec
            primary = producer_result.get("primary_artifact")
            if not isinstance(primary, dict) or not isinstance(primary.get("path"), str):
                rec.update({"status": "fail", "diagnostics": ["producer primary artifact evidence missing"]}); return rec
            try:
                selected_path = Path(primary["path"]).resolve(strict=True)
            except OSError:
                rec.update({"status": "fail", "diagnostics": ["producer primary artifact is missing"]}); return rec
            required_suffix = str(selection.get("required_suffix", "")).lower()
            if required_suffix and selected_path.suffix.lower() != required_suffix:
                rec.update({"status": "fail", "diagnostics": ["producer primary artifact suffix mismatch"]}); return rec
            if not within(selected_path, stage_dir) or primary.get("sha256") != sha256(selected_path):
                rec.update({"status": "fail", "diagnostics": ["producer primary artifact binding mismatch"]}); return rec
            artifact = selected_path
            values["artifact"] = str(artifact)
            rec["producer_result"] = file_record(producer_result_path)
            rec["producer_primary_artifact"] = file_record(artifact)
    layout_argv = config.get("layout_checker_argv")
    layout_receipt = Path(values["layout_receipt"])
    if layout_argv:
        proc = run_process(expand(layout_argv, values), timeout=float(config.get("layout_checker_timeout_seconds", 60)), evidence_dir=stage_dir, label="layout-check")
        layout_data = None
        if layout_receipt.is_file():
            try: layout_data = json.loads(layout_receipt.read_text(encoding="utf-8"))
            except Exception: layout_data = None
        rec["layout_check"] = {"process": proc, "receipt": file_record(layout_receipt) if layout_receipt.is_file() else None, "status": (layout_data or {}).get("status"), "actionable": bool((layout_data or {}).get("actionable")), "diagnostics": (layout_data or {}).get("diagnostics") or []}
    checker_argv = config.get("checker_argv")
    checker_receipt = Path(values["checker_receipt"])
    if checker_argv:
        proc = run_process(expand(checker_argv, values), timeout=float(config.get("checker_timeout_seconds", 60)), evidence_dir=stage_dir, label="final-check")
        checker_data = None
        if checker_receipt.is_file():
            try: checker_data = json.loads(checker_receipt.read_text(encoding="utf-8"))
            except Exception: checker_data = None
        rec["final_checker"] = {"process": proc, "returncode": proc["returncode"], "status": (checker_data or {}).get("status"), "artifact": file_record(artifact) if artifact.is_file() else {"path": str(artifact), "sha256": None}, "receipt": file_record(checker_receipt) if checker_receipt.is_file() else None}
    quality_argv = config.get("artifact_quality_checker_argv")
    if quality_argv:
        producer_result = json.loads(Path(values["render_result"]).read_text(encoding="utf-8"))
        quality_contract = producer_result.get("artifact_quality_contract")
        if not isinstance(quality_contract, dict) or not isinstance(quality_contract.get("path"), str):
            rec.update({"status": "fail", "diagnostics": ["producer artifact-quality contract evidence missing"]}); return rec
        contract_path = Path(quality_contract["path"])
        if (not contract_path.is_file() or quality_contract.get("sha256") != sha256(contract_path)
                or not within(contract_path, stage_dir)):
            rec.update({"status": "fail", "diagnostics": ["producer artifact-quality contract binding mismatch"]}); return rec
        values["artifact_quality_contract"] = str(contract_path.resolve())
        values["artifact_quality_receipt"] = str(stage_dir / "artifact-quality-receipt.json")
        quality_evidence_dir = producer_result.get("artifact_quality_evidence_dir")
        if not isinstance(quality_evidence_dir, str) or not within(Path(quality_evidence_dir), stage_dir):
            rec.update({"status": "fail", "diagnostics": ["producer artifact-quality evidence directory binding mismatch"]}); return rec
        values["artifact_quality_evidence_dir"] = str(Path(quality_evidence_dir).resolve())
        values["artifact_quality_scan"] = str(Path(quality_evidence_dir) / f"{artifact.name}.AQ-LAYOUT-01.json")
        values["artifact_quality_review"] = str(Path(quality_evidence_dir) / f"{artifact.name}.AQ-LAYOUT-01-REVIEW.json")
        proc = run_process(expand(quality_argv, values), timeout=float(config.get("artifact_quality_checker_timeout_seconds", 120)), evidence_dir=stage_dir, label="artifact-quality-check")
        initial_quality_process = proc
        review_process = None
        review_argv = config.get("artifact_quality_review_argv")
        if proc["returncode"] == 2 and review_argv:
            review_process = run_process(expand(review_argv, values), timeout=float(config.get("artifact_quality_review_timeout_seconds", 60)), evidence_dir=stage_dir, label="artifact-quality-review")
            if review_process["returncode"] == 0:
                proc = run_process(expand(quality_argv, values), timeout=float(config.get("artifact_quality_checker_timeout_seconds", 120)), evidence_dir=stage_dir, label="artifact-quality-recheck")
        receipt_path = Path(values["artifact_quality_receipt"])
        try: quality_data = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception: quality_data = None
        rec["artifact_quality"] = {"process": proc, "status": (quality_data or {}).get("status"),
                                   "contract": file_record(contract_path),
                                   "receipt": file_record(receipt_path) if receipt_path.is_file() else None,
                                   "initial_process": initial_quality_process,
                                   "review_process": review_process}
    layout = rec.get("layout_check")
    checker = rec.get("final_checker")
    if checker and (checker["returncode"] != 0 or checker.get("status") != "pass" or checker.get("receipt") is None):
        rec.update({"status": "fail", "diagnostics": ["canonical final checker failed"]}); return rec
    quality = rec.get("artifact_quality")
    if quality and (quality["process"]["returncode"] != 0 or quality.get("status") != "pass" or quality.get("receipt") is None):
        rec.update({"status": "fail", "diagnostics": ["canonical artifact-quality checker failed"]}); return rec
    if layout:
        lp = layout["process"]
        if lp["returncode"] == 0 and layout.get("status") == "pass": pass
        elif lp["returncode"] == 2 and layout.get("status") == "fail" and layout.get("actionable"):
            rec.update({"status": "actionable_fail", "diagnostics": layout.get("diagnostics") or []}); return rec
        else:
            rec.update({"status": "fail", "diagnostics": ["layout checker failed without actionable diagnostics"]}); return rec
    if artifact.is_file(): rec["artifact"] = file_record(artifact)
    return rec

def run(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8")); task_id = config.get("task_id", "")
    if not isinstance(task_id, str) or not task_id.startswith("t_"): raise ValueError("explicit task_id is required")
    isolated_root = Path(config["isolated_root"]).resolve(strict=True); base = Path(config["base"]).resolve(strict=True); output = Path(config["integrated_output"]).resolve(); receipt = Path(config["receipt"]).resolve(); work_root = Path(config["work_root"]).resolve()
    checked_paths = [base, output, receipt, work_root]
    if not all(within(p, isolated_root) for p in checked_paths): raise ValueError("all source, workcopy, output, artifact, and receipt paths must remain in isolated_root")
    if output == base: raise ValueError("integrated output cannot alias source")
    roles, max_workers = config.get("roles", []), int(config.get("max_workers", 2))
    if not roles: raise ValueError("roles must be a non-empty list")
    role_names = [validate_role_name(r.get("name")) for r in roles]
    if len(set(role_names)) != len(roles): raise ValueError("roles must have unique names")
    if max_workers < 1 or max_workers > 2: raise ValueError("bounded concurrency must be 1 or 2")
    finishing = config.get("serial_finishing") or {}
    max_attempts = int(finishing.get("max_attempts", 2)) if finishing else 0
    if finishing and (max_attempts < 1 or max_attempts > MAX_FINISHING_ATTEMPTS_CAP): raise ValueError(f"serial finishing max_attempts must be 1..{MAX_FINISHING_ATTEMPTS_CAP}")
    if work_root.exists(): raise ValueError("work_root must be new for role isolation")
    work_root.mkdir(parents=True); base_hash = sha256(base); base_data = json.loads(base.read_text(encoding="utf-8"))
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run_role, r, base=base, base_hash=base_hash, task_id=task_id, work_root=work_root): r["name"] for r in roles}
        for future in as_completed(futures): results.append(future.result())
    results.sort(key=lambda r: r["name"]); errors = []
    for rec in results:
        if rec.get("status") == "fail": errors += [f"{rec['name']}: {e}" for e in rec["errors"]]
    path_owners, role_changes, roles_by_name = {}, {}, {r["name"]: r for r in roles}
    if not errors:
        for rec in results:
            role = roles_by_name[rec["name"]]; shape = list_shape_changes(base_data, rec["_data"])
            if shape: errors.append(f"{role['name']}: unsupported list shape addition/deletion at: {shape}")
            diff = changes(base_data, rec["_data"]); paths = sorted(diff); rec["changed_paths"] = paths
            bad = [p for p in paths if not allowed(p, role["allowed_json_pointers"])]
            if bad: errors.append(f"{role['name']}: out-of-scope changes: {bad}")
            role_changes[role["name"]] = diff
            for p in paths:
                for existing, owner in path_owners.items():
                    if conflict(existing, p): errors.append(f"conflict: {owner}:{existing} vs {role['name']}:{p}")
                path_owners[p] = role["name"]
    if not errors:
        integrated = copy.deepcopy(base_data)
        for name in sorted(role_changes):
            for pointer, (_, value) in sorted(role_changes[name].items()): apply_change(integrated, pointer, value)
        dump(output, integrated)
    for rec in results: rec.pop("_data", None)
    overlap = len(results) > 1 and max(r["timing"]["monotonic_start_ns"] for r in results) < min(r["timing"]["monotonic_end_ns"] for r in results)
    initial_eval, attempts, termination, selected = None, [], "parallel_or_merge_failure", None
    if not errors:
        if config.get("render_argv") or config.get("layout_checker_argv") or config.get("checker_argv"):
            initial_stage = Path(config.get("initial_stage_root", str(output.parent / "initial-check"))).resolve()
            if not within(initial_stage, isolated_root): raise ValueError("initial_stage_root must remain in isolated_root")
            initial_eval = evaluate_source(config, source=output, stage_dir=initial_stage, task_id=task_id)
        else: initial_eval = {"source": file_record(output), "status": "pass", "diagnostics": []}
        if initial_eval["status"] == "pass":
            termination, selected = "initial_pass_no_finisher", initial_eval
        elif initial_eval["status"] != "actionable_fail" or not finishing:
            termination, errors = "initial_nonactionable_failure", ["initial render/check did not pass"]
        else:
            attempts_root = Path(finishing["attempts_root"]).resolve()
            if not within(attempts_root, isolated_root): raise ValueError("attempts_root must remain in isolated_root")
            attempts_root.mkdir(parents=True, exist_ok=False)
            current_path, current_data, current_eval = output, json.loads(output.read_text(encoding="utf-8")), initial_eval
            seen = {sha256(current_path)}; allowed_patterns = finishing.get("allowed_json_pointers") or []
            for number in range(1, max_attempts + 1):
                attempt_dir = attempts_root / f"attempt-{number:02d}"; attempt_dir.mkdir()
                input_path, output_path, scope_path = attempt_dir / "input.json", attempt_dir / "output.json", attempt_dir / "scope.json"
                input_path.write_bytes(current_path.read_bytes())
                scope = {"schema_version": "docs-serial-finisher-scope-1", "task_id": task_id, "attempt": number, "allowed_json_pointers": allowed_patterns}; dump(scope_path, scope)
                layout_receipt = Path(current_eval["layout_check"]["receipt"]["path"])
                values = {"task_id": task_id, "attempt": str(number), "input": str(input_path), "current": str(input_path), "output": str(output_path), "diagnostics": str(layout_receipt), "scope": str(scope_path), "attempt_dir": str(attempt_dir)}
                proc = run_process(expand(finishing["command_argv"], values), timeout=float(finishing.get("timeout_seconds", 60)), evidence_dir=attempt_dir, label="finisher", cwd=attempt_dir)
                attempt = {"attempt": number, "task_id": task_id, "input": file_record(input_path), "scope": scope, "scope_file": file_record(scope_path), "diagnostics_input": file_record(layout_receipt), "finisher": proc, "status": "fail"}
                llm_evidence = attempt_dir / "llm-evidence.json"
                if llm_evidence.is_file():
                    try: llm_data = json.loads(llm_evidence.read_text(encoding="utf-8"))
                    except Exception: llm_data = {}
                    attempt["llm_evidence"] = {"file": file_record(llm_evidence), "schema_version": llm_data.get("schema_version"), "status": llm_data.get("status"), "usage_summary": llm_data.get("usage_summary")}
                attempts.append(attempt)
                if proc["returncode"] != 0 or not output_path.is_file():
                    termination, errors = "finisher_process_failure", [f"finisher attempt {number} failed"]
                    break
                try: next_data = json.loads(output_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    termination, errors = "finisher_invalid_output", [f"finisher attempt {number} invalid JSON: {exc}"]; break
                shape = list_shape_changes(current_data, next_data); diff = changes(current_data, next_data); paths = sorted(diff)
                text_paths = [p for p in paths if p.endswith("/text") or p == "/text"]
                bad = [p for p in paths if not allowed(p, allowed_patterns)]
                attempt.update({"output": file_record(output_path), "changed_paths": paths, "status": "candidate"})
                if shape or bad or text_paths:
                    attempt["violations"] = {"list_shape_changes": shape, "out_of_scope": bad, "text_changes": text_paths}
                    termination, errors = "finisher_scope_or_text_violation", [f"finisher attempt {number} violated scope or text preservation"]
                    break
                out_hash = attempt["output"]["sha256"]
                if not paths or out_hash == attempt["input"]["sha256"]:
                    termination, errors = "finisher_no_progress", [f"finisher attempt {number} made no progress"]
                    break
                if out_hash in seen:
                    termination, errors = "finisher_cycle", [f"finisher attempt {number} repeated a prior source hash"]
                    break
                seen.add(out_hash)
                evaluation = evaluate_source(config, source=output_path, stage_dir=attempt_dir / "check", task_id=task_id)
                attempt["evaluation"], attempt["status"] = evaluation, "pass" if evaluation["status"] == "pass" else "nonpass"
                if evaluation["status"] == "pass":
                    termination, selected = "finisher_pass", evaluation
                    current_path, current_data, current_eval = output_path, next_data, evaluation
                    break
                if evaluation["status"] != "actionable_fail":
                    termination, errors = "finisher_nonactionable_failure", [f"finisher attempt {number} render/check failed without actionable diagnostics"]
                    break
                current_path, current_data, current_eval = output_path, next_data, evaluation
            else:
                termination, errors = "finisher_bound_exhausted", [f"serial finishing exhausted {max_attempts} attempts"]
    status = "pass" if selected is not None and not errors else "fail"
    test_evidence = []
    for value in config.get("test_evidence", []):
        p = Path(value).resolve(strict=True); test_evidence.append(file_record(p))
    real_roles = [rec["name"] for rec in results if rec.get("llm_evidence", {}).get("status") == "pass"]
    finisher_llm = any(attempt.get("llm_evidence", {}).get("status") == "pass" for attempt in attempts)
    data = {"schema_version": SCHEMA, "status": status, "task_id": task_id, "parent_task_id": task_id, "common_base": file_record(base), "role_count": len(roles), "max_concurrency": max_workers, "roles": results, "concurrency_evidence": {"overlap_observed": overlap}, "integration": {"status": "pass" if output.is_file() and not any(e.startswith("conflict:") or "role" in e for e in errors) else "rejected", "conflict_status": "rejected" if any(e.startswith("conflict:") for e in errors) else "clear", "original_merged_source": file_record(output) if output.is_file() else None}, "initial_evaluation": initial_eval, "serial_finishing": {"invoked": bool(attempts), "max_attempts": max_attempts, "cap": MAX_FINISHING_ATTEMPTS_CAP, "allowed_json_pointers": finishing.get("allowed_json_pointers", []) if finishing else [], "attempts": attempts, "termination_reason": termination}, "selected_final": None if selected is None else {"source": selected["source"], "artifact": selected.get("artifact") or (selected.get("final_checker") or {}).get("artifact"), "checker_receipt": (selected.get("final_checker") or {}).get("receipt"), "layout_receipt": (selected.get("layout_check") or {}).get("receipt"), "artifact_quality_contract": (selected.get("artifact_quality") or {}).get("contract"), "artifact_quality_receipt": (selected.get("artifact_quality") or {}).get("receipt"), "producer_result": selected.get("producer_result"), "stage_dir": selected.get("stage_dir")}, "execution_report": {"real_llm_roles_exercised": real_roles, "finisher_exercised": finisher_llm, "finisher_disposition": "exercised" if finisher_llm else ("not_needed" if termination == "initial_pass_no_finisher" else "not_exercised"), "normal_docs_route_activated": False, "core_gate_applied": False}, "test_evidence": test_evidence, "errors": errors}
    dump(receipt, data)
    if errors or status != "pass": raise RuntimeError("; ".join(errors or [termination]))
    return data

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("--config", required=True, type=Path); a = p.parse_args()
    try:
        result = run(a.config); print(json.dumps({"status": result["status"], "receipt": str(Path(a.config).resolve())}, ensure_ascii=False)); return 0
    except Exception as exc: print(str(exc), file=sys.stderr); return 1
if __name__ == "__main__": raise SystemExit(main())
