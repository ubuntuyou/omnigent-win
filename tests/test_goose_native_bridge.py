"""Windows ConPTY-injection branch of the goose-native bridge.

On Windows there is no tmux: ``inject_user_message`` / ``inject_interrupt`` must
route through the runner's loopback injection server (the single Windows
injection client lives in :mod:`omnigent.claude_native_bridge`) instead of
shelling out to ``tmux send-keys``, and ``write_tmux_target`` must advertise the
server's ``host/port/token`` into ``tmux.json`` so the out-of-process executor
can reach it. These tests pin that contract without a live ConPTY.
"""

from __future__ import annotations

import json
from pathlib import Path

import omnigent.claude_native_bridge as cnb
from omnigent import goose_native_bridge as gb


def _forbid_tmux(monkeypatch) -> None:
    """Make any tmux-path call fail loudly, proving the Windows branch returned."""

    def boom(*_a, **_k):
        raise AssertionError("tmux path used on Windows")

    monkeypatch.setattr(gb, "_wait_for_tmux_info", boom)
    monkeypatch.setattr(gb, "_run_tmux", boom)


def test_inject_user_message_windows_uses_injection_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gb, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        cnb,
        "_inject_via_injection_server",
        lambda bridge_dir, *, kind, content, timeout_s: calls.append(
            {"bridge_dir": bridge_dir, "kind": kind, "content": content}
        ),
    )

    gb.inject_user_message(tmp_path, content="hello", timeout_s=1.0)

    assert calls == [{"bridge_dir": tmp_path, "kind": "message", "content": "hello"}]


def test_inject_interrupt_windows_uses_injection_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gb, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(
        cnb,
        "_inject_via_injection_server",
        lambda bridge_dir, *, kind, content, timeout_s: calls.append((kind, content)),
    )

    gb.inject_interrupt(tmp_path, timeout_s=1.0)

    assert calls == [("interrupt", None)]


def test_write_tmux_target_advertises_injection_endpoint(tmp_path: Path) -> None:
    gb.write_tmux_target(
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
    gb.write_tmux_target(tmp_path, socket_path=Path("/sock"), tmux_target="t")
    info = json.loads((tmp_path / "tmux.json").read_text(encoding="utf-8"))
    assert "input_host" not in info
    assert "input_token" not in info
