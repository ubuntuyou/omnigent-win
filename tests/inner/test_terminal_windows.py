"""Unit tests for the Windows ConPTY terminal backend.

These cover the platform-agnostic logic of
:class:`omnigent.inner.terminal_windows.WindowsTerminalInstance` — literal
slash-command injection, smallest-wins multi-client sizing, and reconnect
replay — none of which need a real ConPTY (``pywinpty``). The module imports a
real ``winpty`` only inside ``launch``; everything exercised here works with
``_pty`` left as ``None``, so the file runs on Linux/macOS CI as well as
Windows.
"""

from __future__ import annotations

import asyncio

import pytest_asyncio

from omnigent.inner import terminal_windows as tw
from omnigent.inner.terminal_windows import WindowsTerminalInstance, _build_paste_payload

# Bracketed-paste delimiters Claude Code's TUI uses to mark pasted *data*.
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"


def _make_instance(tmp_path) -> WindowsTerminalInstance:
    """Build an unlaunched instance (no ConPTY/winpty needed)."""
    return WindowsTerminalInstance(
        name="t",
        session_key="s",
        private_dir=tmp_path,
        command="claude",
        args=[],
        env={},
        env_unset=[],
    )


async def _flush_and_drain(inst: WindowsTerminalInstance) -> list[str]:
    """Run pending ``call_soon_threadsafe`` callbacks, then drain the write queue.

    ``inject_payload`` hops onto the loop via ``call_soon_threadsafe``, so a few
    event-loop turns are needed before every enqueued payload is visible.
    """
    for _ in range(5):
        await asyncio.sleep(0)
    q = inst._write_queue
    assert q is not None
    out: list[str] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


@pytest_asyncio.fixture
async def injecting_instance(tmp_path, monkeypatch):
    """An instance wired for ``inject_payload``, with the slash settle sleeps zeroed.

    Async so it binds the test's running loop — ``inject_payload`` schedules its
    writes on ``inst._loop`` via ``call_soon_threadsafe``, so that loop must be
    the one the test awaits on.
    """
    inst = _make_instance(tmp_path)
    inst._loop = asyncio.get_running_loop()
    inst._write_queue = asyncio.Queue()
    monkeypatch.setattr(tw, "_SUBMIT_SETTLE_S", 0.0)
    monkeypatch.setattr(tw, "_SLASH_CONFIRM_S", 0.0)
    return inst


# --------------------------------------------------------------------------
# Slash commands are typed literally, NOT bracket-pasted
# --------------------------------------------------------------------------


def test_build_paste_payload_wraps_content_in_bracketed_paste() -> None:
    # The message path deliberately wraps content in paste markers (the contrast
    # to the slash path below).
    payload = _build_paste_payload("hello")
    assert payload == f"{_PASTE_START}hello\n{_PASTE_END}"
    assert payload.startswith(_PASTE_START)
    assert payload.endswith(_PASTE_END)


async def test_slash_command_typed_as_literal_keystrokes(injecting_instance) -> None:
    """A slash command goes in as C-u, the literal command, then Enter.

    This is the regression guard for the bug where slash commands were sent
    through the bracketed-paste path: the TUI then treated ``/compact`` as data
    and submitted it as a normal turn instead of executing the command.
    """
    await injecting_instance.inject_slash_command("/compact")
    payloads = await _flush_and_drain(injecting_instance)

    assert payloads == ["\x15", "/compact", "\r"]
    # The crux: nothing in the slash path is wrapped in bracketed-paste markers.
    joined = "".join(payloads)
    assert _PASTE_START not in joined
    assert _PASTE_END not in joined
    # The command is typed verbatim as its own payload.
    assert "/compact" in payloads


async def test_message_clears_input_box_before_pasting(injecting_instance) -> None:
    """A web-chat message clears the input box with backspaces before pasting.

    After an Escape-cancel (the web Stop button), Claude re-populates the input
    box with the previous prompt; without a clear the new message pastes onto it
    ("old promptnew prompt"). The clear is backspace (\x7f) repeated, NOT C-k/C-u:
    those are visual-row-scoped (they leave every wrapped row but one) and, once
    the box is empty, insert literally — rendering as box glyphs. Backspace is
    char-scoped (crosses wrapped rows) and a no-op on an empty box, so it fully
    clears a multi-row draft and can never insert anything. Transcript-verified
    against a live Claude TUI.
    """
    injecting_instance.running = True
    injecting_instance._bracketed_paste_seen = True
    injecting_instance._submitted_once = True  # submit_injected fast path: one CR
    server = tw._InjectionServer(injecting_instance)
    ok, error = await server._dispatch(
        {"token": server.token, "kind": "message", "content": "hello"}
    )
    payloads = await _flush_and_drain(injecting_instance)

    assert (ok, error) == (True, None)
    # The clear is only backspaces (never \x0b/C-k — the box-glyph regression),
    # and precedes the bracketed paste.
    assert payloads[0] == "\x7f" * tw._DRAFT_CLEAR_BACKSPACES
    assert tw._DRAFT_CLEAR_BACKSPACES > 1
    assert "\x0b" not in payloads[0]
    assert payloads[1] == _build_paste_payload("hello")
    assert "".join(payloads).index("\x7f") < "".join(payloads).index(_PASTE_START)


async def test_slash_command_auto_confirm_sends_second_enter(injecting_instance) -> None:
    # /effort and /model pop a confirmation dialog; auto_confirm accepts the
    # default with a second Enter after the dialog renders.
    await injecting_instance.inject_slash_command("/effort", auto_confirm=True)
    payloads = await _flush_and_drain(injecting_instance)

    assert payloads == ["\x15", "/effort", "\r", "\r"]


async def test_slash_command_without_auto_confirm_sends_single_enter(injecting_instance) -> None:
    await injecting_instance.inject_slash_command("/compact", auto_confirm=False)
    payloads = await _flush_and_drain(injecting_instance)

    assert payloads.count("\r") == 1


# --------------------------------------------------------------------------
# Smallest-wins multi-client sizing
# --------------------------------------------------------------------------


async def test_effective_size_is_per_axis_min_across_clients(tmp_path) -> None:
    inst = _make_instance(tmp_path)  # _pty is None -> setwinsize is skipped
    q1 = inst.subscribe()
    q2 = inst.subscribe()

    inst.set_client_size(q1, 50, 200)
    assert inst._effective_size == (50, 200)

    # The shared size is the min of each axis independently, so no client's
    # viewport overflows the pane.
    inst.set_client_size(q2, 60, 90)
    assert inst._effective_size == (50, 90)


async def test_pane_grows_back_when_constraining_client_detaches(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    q1 = inst.subscribe()
    q2 = inst.subscribe()
    inst.set_client_size(q1, 50, 200)
    inst.set_client_size(q2, 30, 100)
    assert inst._effective_size == (30, 100)

    # When the client holding the pane small leaves, the size recomputes over
    # the remaining sized clients and the pane grows back.
    inst.unsubscribe(q2)
    assert inst._effective_size == (50, 200)


async def test_unsized_client_does_not_constrain_or_recompute(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    q1 = inst.subscribe()
    q2 = inst.subscribe()  # never reports a size
    inst.set_client_size(q1, 40, 80)
    assert inst._effective_size == (40, 80)

    # Detaching a client that never sized the pane leaves the effective size
    # untouched (no spurious recompute).
    inst.unsubscribe(q2)
    assert inst._effective_size == (40, 80)


# --------------------------------------------------------------------------
# Reconnect / second-browser replay + size seeding
# --------------------------------------------------------------------------


async def test_subscribe_replays_output_tail_to_new_client(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    inst._output_tail = "previous screen output"

    q = inst.subscribe(replay=True)
    # A reconnecting client immediately gets the accumulated tail instead of a
    # blank pane.
    assert q.get_nowait() == b"previous screen output"


async def test_subscribe_without_replay_does_not_seed_tail(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    inst._output_tail = "previous screen output"

    q = inst.subscribe(replay=False)
    assert q.empty()


async def test_late_joiner_is_seeded_with_shared_size(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    q1 = inst.subscribe()
    inst.set_client_size(q1, 30, 100)  # pane now constrained

    # A second browser joining a constrained session pins to the shared size
    # right away (no reflow-from-default flash).
    q2 = inst.subscribe()
    size_q = inst.size_channel(q2)
    assert size_q is not None
    assert size_q.get_nowait() == (30, 100)


async def test_solo_first_client_is_not_size_seeded(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    q1 = inst.subscribe()

    # A solo first client renders at its own fit; seeding it would force a
    # needless reflow to the launch default first.
    size_q = inst.size_channel(q1)
    assert size_q is not None
    assert size_q.empty()
