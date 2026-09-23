"""Completion consumer for parallel-once plus bounded serial finishing receipts."""
from __future__ import annotations
import copy, hashlib, json
from pathlib import Path
from parallel_production import allowed, apply_change, changes, conflict, list_shape_changes, validate_role_name


def sha256(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _live_file(record, label, errors):
    if not isinstance(record, dict): errors.append(f"{label} evidence missing"); return None
    path = Path(str(record.get("path", "")))
    if not path.is_file(): errors.append(f"{label} stale or missing"); return None
    try: current = sha256(path)
    except OSError: errors.append(f"{label} stale or missing"); return None
    if record.get("sha256") != current: errors.append(f"{label} stale or missing"); return None
    return path

def _json_file(record, label, errors):
    path = _live_file(record, label, errors)
    if not path: return None, None
    try: return path, json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc: errors.append(f"{label} is invalid JSON: {exc}"); return path, None

def _verify_process(proc, label, errors, *, allowed_returncodes=(0,)):
    if not isinstance(proc, dict): errors.append(f"{label} process evidence missing"); return
    if proc.get("returncode") not in allowed_returncodes: errors.append(f"{label} process did not pass")
    _live_file(proc.get("stdout"), f"{label} stdout", errors); _live_file(proc.get("stderr"), f"{label} stderr", errors)

def _verify_llm_evidence(summary, *, task_id, role, input_record, scope_record, output_record, errors, label):
    if not isinstance(summary, dict):
        errors.append(f"{label} LLM evidence missing"); return
    _, evidence = _json_file(summary.get("file"), f"{label} LLM evidence", errors)
    if not evidence: return
    if evidence.get("schema_version") != "docs-real-llm-adapter-evidence-1" or evidence.get("status") != "pass": errors.append(f"{label} LLM evidence did not pass")
    if evidence.get("task_id") != task_id or evidence.get("role") != role: errors.append(f"{label} LLM evidence identity mismatch")
    for key, expected in (("input", input_record), ("scope", scope_record), ("output", output_record)):
        if evidence.get(key) != expected: errors.append(f"{label} LLM evidence {key} binding mismatch")
    for key in ("prompt", "raw_response", "stderr", "usage"):
        _live_file(evidence.get(key), f"{label} LLM {key}", errors)

def _verify_evaluation(evaluation, *, task_id, expected_source, require_pass, errors, label):
    if not isinstance(evaluation, dict): errors.append(f"{label} evaluation missing"); return None
    source_path = _live_file(evaluation.get("source"), f"{label} source", errors)
    if source_path and expected_source and (source_path.resolve() != expected_source.resolve()): errors.append(f"{label} source does not match chain input")
    layout = evaluation.get("layout_check")
    if not isinstance(layout, dict): errors.append(f"{label} layout checker evidence missing")
    else:
        _verify_process(layout.get("process"), f"{label} layout checker", errors, allowed_returncodes=(0, 2))
        lp, ld = _json_file(layout.get("receipt"), f"{label} layout receipt", errors)
        if ld:
            if ld.get("task_id") != task_id: errors.append(f"{label} layout receipt task mismatch")
            if source_path and ((ld.get("source") or {}).get("path") != str(source_path.resolve()) or (ld.get("source") or {}).get("sha256") != sha256(source_path)): errors.append(f"{label} layout receipt does not bind current source")
            if require_pass and (ld.get("status") != "pass" or layout.get("status") != "pass"): errors.append(f"{label} layout checker did not pass")
    checker = evaluation.get("final_checker")
    if not isinstance(checker, dict): errors.append(f"{label} final checker evidence missing"); return None
    _verify_process(checker.get("process"), f"{label} final checker", errors)
    artifact = _live_file(checker.get("artifact"), f"{label} checked artifact", errors)
    cp, canonical = _json_file(checker.get("receipt"), f"{label} checker receipt", errors)
    if require_pass and (checker.get("returncode") != 0 or checker.get("status") != "pass"): errors.append(f"{label} final checker did not pass")
    if canonical and artifact:
        entries = canonical.get("entries") if isinstance(canonical, dict) else None
        if canonical.get("status") != "pass" or not isinstance(entries, list) or not any(e.get("artifact_path") == str(artifact.resolve()) and e.get("artifact_sha256") == sha256(artifact) and e.get("status") == "pass" for e in entries): errors.append(f"{label} canonical checker receipt does not bind current artifact")
    quality = evaluation.get("artifact_quality")
    if quality is not None and not isinstance(quality, dict): errors.append(f"{label} artifact-quality evidence is invalid")
    elif isinstance(quality, dict):
        _verify_process(quality.get("process"), f"{label} artifact-quality checker", errors)
        contract_path, contract = _json_file(quality.get("contract"), f"{label} artifact-quality contract", errors)
        receipt_path, receipt = _json_file(quality.get("receipt"), f"{label} artifact-quality receipt", errors)
        if require_pass and quality.get("status") != "pass": errors.append(f"{label} artifact-quality checker did not pass")
        if contract and artifact:
            if contract.get("task_id") != task_id: errors.append(f"{label} artifact-quality contract task mismatch")
            if contract.get("artifacts") != [str(artifact.resolve())]: errors.append(f"{label} artifact-quality contract does not select current artifact")
        if receipt and artifact:
            entries = receipt.get("entries") if isinstance(receipt, dict) else None
            if (receipt.get("status") != "pass" or not isinstance(entries, list)
                    or not any((entry.get("artifact") or {}).get("path") == str(artifact.resolve())
                               and (entry.get("artifact") or {}).get("sha256") == sha256(artifact)
                               and entry.get("status") == "pass" for entry in entries)):
                errors.append(f"{label} artifact-quality receipt does not bind current artifact")
    return artifact

def verify_integration_receipt(path, *, task_id, artifacts):
    p = Path(path); errors = []
    try: r = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc: return [f"integration receipt unreadable: {exc}"]
    if r.get("schema_version") != "docs-parallel-integration-receipt-2": errors.append("wrong schema")
    if r.get("status") != "pass" or r.get("integration", {}).get("status") != "pass": errors.append("integration did not pass")
    if r.get("task_id") != task_id or r.get("parent_task_id") != task_id: errors.append("explicit task isolation failed")
    bp, base_data = _json_file(r.get("common_base"), "common base", errors)
    roles = r.get("roles")
    if not isinstance(roles, list) or not roles: errors.append("roles must be a non-empty list"); roles = []
    if not isinstance(r.get("role_count"), int) or r.get("role_count") != len(roles): errors.append("role_count does not match roles")
    role_changes, path_owners, names = {}, {}, set()
    for role in roles:
        if not isinstance(role, dict): errors.append("role record must be an object"); continue
        name = role.get("name")
        try: validate_role_name(name)
        except ValueError: errors.append("receipt contains unsafe role name"); continue
        if name in names: errors.append("receipt contains duplicate role name")
        names.add(name)
        if role.get("status") != "pass" or role.get("returncode") != 0 or role.get("errors"): errors.append(f"{name}: role did not pass")
        _verify_process(role, f"{name}: role", errors)
        ip, _ = _json_file(role.get("input"), f"{name}: role input", errors)
        if ip and r.get("common_base", {}).get("sha256") and role.get("input", {}).get("sha256") != r["common_base"]["sha256"]: errors.append(f"{name}: role input is not the current common base")
        scope = role.get("scope"); sp, scope_live = _json_file(role.get("scope_file"), f"{name}: role scope", errors)
        if not isinstance(scope, dict) or scope.get("schema_version") != "docs-role-scope-1" or scope.get("task_id") != task_id or scope.get("role") != name or not isinstance(scope.get("allowed_json_pointers"), list): errors.append(f"{name}: role scope declaration is invalid")
        elif scope_live != scope: errors.append(f"{name}: role scope changed after integration")
        op, output_data = _json_file(role.get("output"), f"{name}: role output", errors)
        if role.get("llm_evidence") is not None:
            _verify_llm_evidence(role.get("llm_evidence"), task_id=task_id, role=name, input_record=role.get("input"), scope_record=role.get("scope_file"), output_record=role.get("output"), errors=errors, label=f"{name}: role")
        if base_data is None or output_data is None or not isinstance(scope, dict): continue
        shape = list_shape_changes(base_data, output_data)
        if shape: errors.append(f"{name}: unsupported list shape addition/deletion at: {shape}")
        diff = changes(base_data, output_data); current_paths = sorted(diff)
        if role.get("changed_paths") != current_paths: errors.append(f"{name}: changed_paths does not match current role output")
        bad = [pointer for pointer in current_paths if not allowed(pointer, scope.get("allowed_json_pointers", []))]
        if bad: errors.append(f"{name}: out-of-scope changes: {bad}")
        role_changes[name] = diff
        for pointer in current_paths:
            for existing, owner in path_owners.items():
                if conflict(existing, pointer): errors.append(f"conflict: {owner}:{existing} vs {name}:{pointer}")
            path_owners[pointer] = name
    integration = r.get("integration") or {}; merged_path, merged_data = _json_file(integration.get("original_merged_source"), "original merged source", errors)
    if base_data is not None and merged_data is not None and len(role_changes) == len(roles):
        expected = copy.deepcopy(base_data)
        try:
            for name in sorted(role_changes):
                for pointer, (_, value) in sorted(role_changes[name].items()): apply_change(expected, pointer, value)
        except Exception as exc: errors.append(f"original merge cannot be recomputed safely: {exc}")
        else:
            if expected != merged_data: errors.append("original merged source does not match recomputed role changes")
    initial = r.get("initial_evaluation")
    _verify_evaluation(initial, task_id=task_id, expected_source=merged_path, require_pass=False, errors=errors, label="initial")
    serial = r.get("serial_finishing") or {}; attempts = serial.get("attempts") or []
    if serial.get("invoked") != bool(attempts): errors.append("serial invoked flag does not match attempts")
    max_attempts = serial.get("max_attempts")
    if not isinstance(max_attempts, int) or max_attempts < 0 or max_attempts > 5 or len(attempts) > max_attempts: errors.append("serial finishing bound is invalid")
    patterns = serial.get("allowed_json_pointers") or []
    previous_path, previous_data = merged_path, merged_data
    seen = {r.get("integration", {}).get("original_merged_source", {}).get("sha256")}
    last_eval = initial
    for index, attempt in enumerate(attempts, 1):
        label = f"finisher attempt {index}"
        if attempt.get("attempt") != index or attempt.get("task_id") != task_id: errors.append(f"{label} identity mismatch")
        ip, input_data = _json_file(attempt.get("input"), f"{label} input", errors)
        if previous_path and ip and (ip.read_bytes() != previous_path.read_bytes()): errors.append(f"{label} input does not match previous chain source")
        scope = attempt.get("scope"); sp, scope_live = _json_file(attempt.get("scope_file"), f"{label} scope", errors)
        if not isinstance(scope, dict) or scope.get("task_id") != task_id or scope.get("attempt") != index or scope.get("allowed_json_pointers") != patterns or scope_live != scope: errors.append(f"{label} scope is invalid")
        _live_file(attempt.get("diagnostics_input"), f"{label} diagnostics", errors)
        _verify_process(attempt.get("finisher"), f"{label} finisher", errors)
        op, output_data = _json_file(attempt.get("output"), f"{label} output", errors)
        if attempt.get("llm_evidence") is not None:
            _verify_llm_evidence(attempt.get("llm_evidence"), task_id=task_id, role="finisher", input_record=attempt.get("input"), scope_record=attempt.get("scope_file"), output_record=attempt.get("output"), errors=errors, label=label)
        if input_data is not None and output_data is not None:
            shape = list_shape_changes(input_data, output_data); diff = changes(input_data, output_data); paths = sorted(diff)
            if shape: errors.append(f"{label} changed list shape")
            if attempt.get("changed_paths") != paths: errors.append(f"{label} changed_paths mismatch")
            bad = [pointer for pointer in paths if not allowed(pointer, patterns)]
            if bad: errors.append(f"{label} out-of-scope changes: {bad}")
            if any(pointer.endswith("/text") or pointer == "/text" for pointer in paths): errors.append(f"{label} changed text")
            if not paths: errors.append(f"{label} made no progress")
        if attempt.get("output", {}).get("sha256") in seen: errors.append(f"{label} repeats a prior source hash")
        seen.add(attempt.get("output", {}).get("sha256"))
        evaluation = attempt.get("evaluation")
        if evaluation is not None: _verify_evaluation(evaluation, task_id=task_id, expected_source=op, require_pass=attempt.get("status") == "pass", errors=errors, label=label)
        previous_path, previous_data, last_eval = op, output_data, evaluation
    selected = r.get("selected_final") or {}
    sp = _live_file(selected.get("source"), "selected final source", errors)
    artifact = _live_file(selected.get("artifact"), "selected final artifact", errors)
    _live_file(selected.get("checker_receipt"), "selected checker receipt", errors)
    _live_file(selected.get("layout_receipt"), "selected layout receipt", errors)
    quality_selected = any(selected.get(key) is not None for key in
                           ("producer_result", "artifact_quality_contract", "artifact_quality_receipt"))
    if quality_selected:
        _live_file(selected.get("producer_result"), "selected producer result", errors)
        _live_file(selected.get("artifact_quality_contract"), "selected artifact-quality contract", errors)
        _live_file(selected.get("artifact_quality_receipt"), "selected artifact-quality receipt", errors)
    expected_final = previous_path if attempts else merged_path
    if sp and expected_final and sp.read_bytes() != expected_final.read_bytes(): errors.append("selected final source does not match reconstructed finishing chain")
    terminal = serial.get("termination_reason")
    if terminal not in {"initial_pass_no_finisher", "finisher_pass"}: errors.append("receipt terminal reason is not successful")
    if terminal == "initial_pass_no_finisher" and attempts: errors.append("initial pass must not invoke finisher")
    if terminal == "finisher_pass" and not attempts: errors.append("finisher pass requires at least one attempt")
    if not isinstance(last_eval, dict) or last_eval.get("status") != "pass": errors.append("selected terminal evaluation did not pass")
    if last_eval:
        expected_artifact = (last_eval.get("final_checker") or {}).get("artifact") or last_eval.get("artifact")
        if artifact and expected_artifact and expected_artifact.get("sha256") != sha256(artifact): errors.append("selected artifact hash differs from terminal evaluation")
        if quality_selected:
            quality = last_eval.get("artifact_quality") or {}
            if selected.get("artifact_quality_contract") != quality.get("contract"): errors.append("selected artifact-quality contract differs from terminal evaluation")
            if selected.get("artifact_quality_receipt") != quality.get("receipt"): errors.append("selected artifact-quality receipt differs from terminal evaluation")
            if selected.get("producer_result") != last_eval.get("producer_result"): errors.append("selected producer result differs from terminal evaluation")
    declared = {str(Path(x).resolve()) for x in artifacts}
    if not artifact or str(artifact.resolve()) not in declared: errors.append("selected final artifact not declared for completion")
    for evidence in r.get("test_evidence") or []: _live_file(evidence, "test evidence", errors)
    execution = r.get("execution_report")
    if isinstance(execution, dict):
        declared_roles = execution.get("real_llm_roles_exercised")
        actual_roles = sorted(role.get("name") for role in roles if role.get("llm_evidence", {}).get("status") == "pass")
        if sorted(declared_roles or []) != actual_roles: errors.append("execution report real LLM roles mismatch")
        actual_finisher = any(attempt.get("llm_evidence", {}).get("status") == "pass" for attempt in attempts)
        if execution.get("finisher_exercised") is not actual_finisher: errors.append("execution report finisher status mismatch")
        expected_disposition = "exercised" if actual_finisher else ("not_needed" if terminal == "initial_pass_no_finisher" else "not_exercised")
        if execution.get("finisher_disposition") != expected_disposition: errors.append("execution report finisher disposition mismatch")
        if execution.get("normal_docs_route_activated") is not False or execution.get("core_gate_applied") is not False: errors.append("isolated candidate route boundary changed")
    return errors
