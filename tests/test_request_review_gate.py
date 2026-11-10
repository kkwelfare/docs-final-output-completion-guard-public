"""The review handoff (critic route) must pass the same completion gate."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "finalization_gate_tests_for_review", Path(__file__).resolve().parent / "test_finalization_gate.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
load_guard = _mod.load_guard


def test_request_review_is_gated_like_completion(monkeypatch):
    guard = load_guard()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture")
    done = guard._pre_tool_call(tool_name="kanban_complete", args={}, task_id="t_fixture")
    review = guard._pre_tool_call(tool_name="kanban_request_review", args={}, task_id="t_fixture")
    assert done is not None and done["action"] == "block"
    assert review == done


def test_other_kanban_tools_stay_unaffected():
    guard = load_guard()
    assert guard._pre_tool_call(tool_name="kanban_comment", args={}, task_id="t_fixture") is None
