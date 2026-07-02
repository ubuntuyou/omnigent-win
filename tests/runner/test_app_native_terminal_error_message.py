"""Tests for the Windows native-terminal start-error message.

``_native_terminal_start_error_payload`` returns the client-facing message
for a failed native-terminal auto-create. On Windows the message must
distinguish:

- harnesses ported to the ConPTY backend (claude / codex / pi / opencode /
  goose / qwen / kimi): a start failure is a *real* error → "see runner logs"
  (the ConPTY itself works, so "not supported on Windows" would send the user
  chasing a non-existent platform limitation);
- still-tmux-only harnesses (cursor / kiro / ...): genuinely unsupported →
  keep the actionable "not supported on Windows" message.

The raw cause is always logged for operators, never surfaced to the client.
"""

from __future__ import annotations

import omnigent.runner.app as app_mod
from omnigent.runner.app import (
    _NATIVE_TERMINAL_START_FAILED_CODE,
    _native_terminal_start_error_payload,
)


def test_windows_ported_harness_points_at_runner_logs(monkeypatch):
    monkeypatch.setattr(app_mod, "IS_WINDOWS", True)
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Codex")
    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "not supported on Windows" not in payload["message"]
    assert "runner logs" in payload["message"]


def test_windows_goose_points_at_runner_logs(monkeypatch):
    monkeypatch.setattr(app_mod, "IS_WINDOWS", True)
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Goose")
    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "not supported on Windows" not in payload["message"]
    assert "runner logs" in payload["message"]


def test_windows_qwen_points_at_runner_logs(monkeypatch):
    monkeypatch.setattr(app_mod, "IS_WINDOWS", True)
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Qwen")
    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "not supported on Windows" not in payload["message"]
    assert "runner logs" in payload["message"]


def test_windows_kimi_points_at_runner_logs(monkeypatch):
    monkeypatch.setattr(app_mod, "IS_WINDOWS", True)
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Kimi")
    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "not supported on Windows" not in payload["message"]
    assert "runner logs" in payload["message"]


def test_windows_tmux_only_harness_keeps_unsupported_message(monkeypatch):
    monkeypatch.setattr(app_mod, "IS_WINDOWS", True)
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Cursor")
    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "not supported on Windows" in payload["message"]


def test_posix_always_points_at_runner_logs(monkeypatch):
    monkeypatch.setattr(app_mod, "IS_WINDOWS", False)
    # Even a tmux-only harness gets the generic message on POSIX.
    payload = _native_terminal_start_error_payload(RuntimeError("boom"), "Cursor")
    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "not supported on Windows" not in payload["message"]
    assert "runner logs" in payload["message"]
