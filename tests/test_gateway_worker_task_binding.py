"""Completion checks bind to the owned Kanban card in gateway-managed workers.

There the dispatcher binds the owned card in ``HERMES_KANBAN_TASK`` while a tool hook's ``task_id``
is the agent's sandbox key (a session id). Reading the hook value first made the card-bound checks
look up the wrong id; outside a worker the hook value is still used as before.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_KEY = "local-7666f4e85281055f"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_guard():
    return _load("gateway_worker_task_binding_guard", "__init__.py")


def load_retry_guard():
    return _load("gateway_worker_task_binding_retry_guard", "production_retry_guard.py")


def _clear_binding(monkeypatch):
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID", "HERMES_ARTIFACT_QUALITY_CONTRACT"):
        monkeypatch.delenv(key, raising=False)


def _record_validated_tasks(monkeypatch, guard):
    seen = []
    monkeypatch.setattr(guard, "_validate_delivery_workflow_completion",
                        lambda args, task: seen.append(task) or "bound")
    return seen


def test_completion_checks_bind_to_owned_card(monkeypatch):
    guard = load_guard()
    _clear_binding(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    seen = _record_validated_tasks(monkeypatch, guard)
    for tool in ("kanban_complete", "kanban_request_review"):
        result = guard._pre_tool_call(tool_name=tool, args={}, task_id=SESSION_KEY)
        assert result == {"action": "block", "message": "docs delivery-workflow guard: bound"}
    assert seen == ["t_owned", "t_owned"]


def test_outside_worker_keeps_hook_value(monkeypatch):
    guard = load_guard()
    _clear_binding(monkeypatch)
    seen = _record_validated_tasks(monkeypatch, guard)
    guard._pre_tool_call(tool_name="kanban_complete", args={}, task_id="t_hook")
    assert guard._pre_tool_call(tool_name="kanban_complete", args={}, task_id=None) is None
    assert seen == ["t_hook"]


def test_card_declared_contract_is_found_in_gateway_worker(monkeypatch, tmp_path):
    guard = load_guard()
    _clear_binding(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    contract = tmp_path / "contract.json"
    fake_db = types.SimpleNamespace(
        connect=lambda: types.SimpleNamespace(close=lambda: None),
        get_task=lambda connection, task_id: (
            types.SimpleNamespace(body=f"artifact_quality_contract: {contract}\n") if task_id == "t_owned" else None
        ),
    )
    fake_pkg = types.SimpleNamespace(kanban_db=fake_db)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_db)
    monkeypatch.setattr(guard, "_validate_delivery_workflow_completion", lambda args, task: None)
    monkeypatch.setattr(guard, "_receipt_paths", lambda args: [tmp_path / "receipt.json"])
    monkeypatch.setattr(guard, "_validate_receipt_generation", lambda args, receipts: None)
    monkeypatch.setattr(guard, "_receipt_reuse_decision",
                        lambda args, receipts: {"decision": "reuse", "covered": []})
    monkeypatch.setattr(guard, "_validate_parallel_completion", lambda args, task: None)
    monkeypatch.setattr(guard, "_artifact_quality_section", lambda args: None)
    result = guard._pre_tool_call(tool_name="kanban_complete", args={"artifacts": []}, task_id=SESSION_KEY)
    assert result is not None and result["action"] == "block"
    assert "artifact_quality contract" in result["message"]


def test_retry_guard_task_prefers_dispatcher_binding(monkeypatch):
    retry = load_retry_guard()
    _clear_binding(monkeypatch)
    assert retry._task_id(SESSION_KEY) == ""
    assert retry._task_id("t_hook") == "t_hook"
    assert retry._task_id(None) == ""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    assert retry._task_id(SESSION_KEY) == "t_owned"
