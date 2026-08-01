"""Windows ConPTY-injection branch of the kimi-native bridge.

On Windows there is no tmux: ``inject_user_message`` / ``inject_interrupt`` /
``inject_approval_keystroke`` must route through the runner's loopback
injection server (the single Windows injection client lives in
:mod:`omnigent.claude_native_bridge`) instead of shelling out to
``tmux send-keys``, ``kill_session`` is an intentional no-op (the ConPTY
teardown happens separately in ``runner/app.py``), and ``write_tmux_target``
must advertise the server's ``host/port/token`` into ``tmux.json`` so the
out-of-process executor can reach it. These tests pin that contract without a
live ConPTY.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import omnigent.claude_native_bridge as cnb
from omnigent import kimi_native_bridge as kb


def _forbid_tmux(monkeypatch) -> None:
    """Make any tmux-path call fail loudly, proving the Windows branch returned."""

    def boom(*_a, **_k):
        raise AssertionError("tmux path used on Windows")

    monkeypatch.setattr(kb, "_wait_for_tmux_info", boom)
    monkeypatch.setattr(kb, "_run_tmux", boom)


def test_bridge_root_uses_stable_user_id_under_system_temp() -> None:
    """``bridge_root`` is Windows-safe: no ``os.getuid``, no ``TMPDIR``-only lookup."""
    root = kb.bridge_root()
    assert root.name == "kimi-native"
    assert root.parent.name.startswith("omnigent-")
    assert root.parent.parent == Path(tempfile.gettempdir())


def test_inject_user_message_windows_uses_injection_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(kb, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        cnb,
        "_inject_via_injection_server",
        lambda bridge_dir, *, kind, content, timeout_s: calls.append(
            {"bridge_dir": bridge_dir, "kind": kind, "content": content}
        ),
    )

    kb.inject_user_message(tmp_path, content="hello", timeout_s=1.0)

    assert calls == [{"bridge_dir": tmp_path, "kind": "message", "content": "hello"}]


def test_inject_interrupt_windows_uses_injection_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(kb, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(
        cnb,
        "_inject_via_injection_server",
        lambda bridge_dir, *, kind, content, timeout_s: calls.append((kind, content)),
    )

    kb.inject_interrupt(tmp_path, timeout_s=1.0)

    assert calls == [("interrupt", None)]


def test_inject_approval_keystroke_windows_uses_keys_kind(monkeypatch, tmp_path: Path) -> None:
    """The Windows branch has no pane to gate on, so it always sends the digit +
    a confirming Enter via the generic literal-keys path, and always returns True."""
    monkeypatch.setattr(kb, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        cnb,
        "_inject_via_injection_server",
        lambda bridge_dir, *, kind, content, timeout_s: calls.append(
            {"bridge_dir": bridge_dir, "kind": kind, "content": content}
        ),
    )

    result = kb.inject_approval_keystroke(tmp_path, key=kb.APPROVE_KEY, timeout_s=1.0)

    assert result is True
    assert calls == [{"bridge_dir": tmp_path, "kind": "keys", "content": kb.APPROVE_KEY + "\r"}]


def test_kill_session_windows_is_a_noop(monkeypatch, tmp_path: Path) -> None:
    """No tmux session exists on Windows; the ConPTY teardown happens elsewhere
    (``_teardown_session_terminals`` in ``runner/app.py``, called right after)."""
    monkeypatch.setattr(kb, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)

    kb.kill_session(tmp_path, timeout_s=1.0)


def test_write_tmux_target_advertises_injection_endpoint(tmp_path: Path) -> None:
    kb.write_tmux_target(
        tmp_path,
        socket_path=Path("/placeholder"),
        tmux_target="placeholder",
        input_host="127.0.0.1",
        input_port=5050,
        input_token="tok",
    )
    info = json.loads((tmp_path / "tmux.json").read_text(encoding="utf-8"))
    assert info["input_host"] == "127.0.0.1"
    assert info["input_port"] == 5050
    assert info["input_token"] == "tok"


def test_write_tmux_target_omits_injection_fields_without_endpoint(tmp_path: Path) -> None:
    """The POSIX call passes no injection endpoint; the fields stay absent."""
    kb.write_tmux_target(tmp_path, socket_path=Path("/sock"), tmux_target="t")
    info = json.loads((tmp_path / "tmux.json").read_text(encoding="utf-8"))
    assert "input_host" not in info
    assert "input_token" not in info
