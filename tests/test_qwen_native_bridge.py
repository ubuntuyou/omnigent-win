"""Unit tests for qwen-native MCP bridge config wiring."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import omnigent.claude_native_bridge as cnb
from omnigent import qwen_native_bridge


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bridge dir under a production-shaped qwen root (passes secure validation).

    ``write_mcp_bridge_config`` now hardens the bridge tree via
    ``_ensure_secure_dir``, which requires the dir to live below a known bridge
    root. Mirror the real layout (``<uid-scoped temp>/qwen-native/<digest>``) so
    the owner-only ancestor walk anchors at ``tmp_path``.
    """
    root = tmp_path / "omnigent-test" / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", root)
    return qwen_native_bridge.bridge_dir_for_session_id("sess")


def test_write_mcp_config_writes_into_bridge_dir_not_workspace(bridge_dir: Path) -> None:
    """``write_mcp_config`` writes the ``--mcp-config`` file inside the bridge dir."""
    path = qwen_native_bridge.write_mcp_config(bridge_dir)

    # The config lives in the bridge dir — never the workspace (no repo pollution).
    assert path == bridge_dir / "mcp_config.json"
    assert path.parent == bridge_dir

    data = json.loads(path.read_text(encoding="utf-8"))
    server = data["mcpServers"]["omnigent"]
    # Points at the shared stdio relay implemented in claude_native_bridge.
    assert server["args"][:4] == ["-I", "-m", "omnigent.claude_native_bridge", "serve-mcp"]
    assert str(bridge_dir) in server["args"]
    # trust:true auto-approves qwen's own MCP gate (Omnigent gates separately).
    assert server["trust"] is True
    # The relay's bearer token was written for ``serve-mcp`` to read at startup.
    assert (bridge_dir / "bridge.json").is_file()
    token = json.loads((bridge_dir / "bridge.json").read_text())["token"]
    assert isinstance(token, str) and token


def test_write_mcp_config_is_valid_for_qwen_mcp_config_flag(bridge_dir: Path) -> None:
    """The payload is the ``{"mcpServers": {...}}`` shape qwen's --mcp-config expects."""
    path = qwen_native_bridge.write_mcp_config(bridge_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"mcpServers"}
    assert set(data["mcpServers"]) == {"omnigent"}


def test_write_mcp_config_path_is_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions get independent config files carrying their own bridge dir."""
    root = tmp_path / "omnigent-test" / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", root)
    bridge_a = qwen_native_bridge.bridge_dir_for_session_id("a")
    bridge_b = qwen_native_bridge.bridge_dir_for_session_id("b")
    path_a = qwen_native_bridge.write_mcp_config(bridge_a)
    path_b = qwen_native_bridge.write_mcp_config(bridge_b)

    assert path_a != path_b
    args_a = json.loads(path_a.read_text())["mcpServers"]["omnigent"]["args"]
    args_b = json.loads(path_b.read_text())["mcpServers"]["omnigent"]["args"]
    assert str(bridge_a) in args_a
    assert str(bridge_b) in args_b
    # No cross-contamination: A's config never points at B's bridge dir.
    assert str(bridge_b) not in args_a


def test_mcp_config_path_matches_written_path(bridge_dir: Path) -> None:
    """``mcp_config_path`` reports the same path ``write_mcp_config`` writes."""
    assert qwen_native_bridge.write_mcp_config(bridge_dir) == (
        qwen_native_bridge.mcp_config_path(bridge_dir)
    )


def test_write_mcp_bridge_config_is_idempotent(bridge_dir: Path) -> None:
    """The relay token is generated once and preserved across re-launches."""
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    first = (bridge_dir / "bridge.json").read_text()
    qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    assert (bridge_dir / "bridge.json").read_text() == first


def test_write_mcp_bridge_config_rejects_symlinked_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked bridge-tree ancestor is refused — the token is never written.

    bridge.json holds a bearer token, so the dir must pass owner-only ancestor
    validation. If an attacker pre-creates an ancestor as a symlink, writing the
    token must fail loudly rather than land it in attacker-redirectable storage.
    """
    real_root = tmp_path / "omnigent-test"
    qwen_root = real_root / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", qwen_root)
    bridge_dir = qwen_native_bridge.bridge_dir_for_session_id("sess")

    # Redirect an ancestor (the uid-scoped dir) through a symlink.
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    real_root.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(RuntimeError):
        qwen_native_bridge.write_mcp_bridge_config(bridge_dir)
    # No token leaked into the redirected location.
    assert not (elsewhere / "qwen-native").exists()


def test_bridge_root_uses_stable_user_id_under_system_temp() -> None:
    """``bridge_root`` is Windows-safe: no ``os.getuid``, no ``TMPDIR``-only lookup."""
    root = qwen_native_bridge.bridge_root()
    assert root.name == "qwen-native"
    assert root.parent.name.startswith("omnigent-")
    assert root.parent.parent == Path(tempfile.gettempdir())


def test_qwen_project_slug_lowercases_like_real_qwen(tmp_path: Path) -> None:
    """qwen lowercases the whole realpath before slugging, not just replacing separators.

    Verified against a live qwen v0.19.4 session on Windows: workspace ``C:\\`` produced
    an on-disk directory named ``c--`` (lowercase), not ``C--``; a mixed-case repo path
    lowercased the whole slug, not just the drive letter. NTFS is case-insensitive so a
    missing ``.lower()`` doesn't break ``--resume`` lookups on Windows in practice, but
    it's a real deviation from qwen's own scheme that would break on a case-sensitive fs.
    Uses a tmp_path fixture (mixed-case components) rather than a literal Windows path so
    this test stays portable to the WSL2-run full suite.
    """
    ws = tmp_path / "MixedCase" / "RepoDir"
    ws.mkdir(parents=True)
    slug = qwen_native_bridge._qwen_project_slug(ws)
    assert slug == slug.lower()
    assert "mixedcase" in slug
    assert "repodir" in slug


def _forbid_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any tmux-path call fail loudly, proving the Windows branch returned."""

    def boom(*_a, **_k):
        raise AssertionError("tmux path used on Windows")

    monkeypatch.setattr(qwen_native_bridge, "_wait_for_tmux_info", boom)
    monkeypatch.setattr(qwen_native_bridge, "_run_tmux", boom)


def test_inject_interrupt_windows_uses_injection_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qwen_native_bridge, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(
        cnb,
        "_inject_via_injection_server",
        lambda bridge_dir, *, kind, content, timeout_s: calls.append((kind, content)),
    )

    qwen_native_bridge.inject_interrupt(tmp_path, timeout_s=1.0)

    assert calls == [("interrupt", None)]


def test_kill_session_windows_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No tmux session exists on Windows; the ConPTY teardown happens elsewhere
    (``_teardown_session_terminals`` in ``runner/app.py``, called right after)."""
    monkeypatch.setattr(qwen_native_bridge, "IS_WINDOWS", True)
    _forbid_tmux(monkeypatch)

    qwen_native_bridge.kill_session(tmp_path, timeout_s=1.0)


def test_write_tmux_target_advertises_injection_endpoint(tmp_path: Path) -> None:
    qwen_native_bridge.write_tmux_target(
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
    qwen_native_bridge.write_tmux_target(tmp_path, socket_path=Path("/sock"), tmux_target="t")
    info = json.loads((tmp_path / "tmux.json").read_text(encoding="utf-8"))
    assert "input_host" not in info
    assert "input_token" not in info
