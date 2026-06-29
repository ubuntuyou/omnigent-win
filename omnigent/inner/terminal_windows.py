"""Windows ConPTY terminal backend (additive; the POSIX tmux path is untouched).

On Windows there is no tmux, so :func:`omnigent.inner.terminal.create_terminal_instance`
cannot build a :class:`~omnigent.inner.terminal.TerminalInstance`. This module
provides a parallel backend, :class:`WindowsTerminalInstance`, built on a ConPTY
(via ``pywinpty``) that implements the subset of the terminal contract the Claude
Code web-UI harness touches.

Three facts drive the design (see ``polished-beaming-nebula-REVISED.md``):

1. **ConPTY is in-process and single-consumer.** Unlike tmux (an external
   multi-client socket server), a ConPTY has no socket and its output can be
   read only once. So the instance OWNS the pseudo-console and runs a single
   daemon reader thread that fans each output chunk out to (a) internal
   accumulation for ``read()`` / ``last_pane_text()`` / idle timing and (b) any
   attached WebSocket subscriber queues.

2. **Windows uses the ProactorEventLoop, where ``loop.add_reader()`` raises.**
   The reader thread hands chunks to the event loop via
   ``loop.call_soon_threadsafe`` rather than registering an fd.

3. **All input funnels through one queue.** Browser keystrokes (from the WS
   bridge) AND injected web messages (from the runner's input server) enqueue
   onto one :class:`asyncio.Queue` drained by a single writer task. This gives
   strict FIFO ordering and makes each payload indivisible — a bracketed-paste
   injection plus its Enter cannot interleave with a browser keystroke. A bare
   lock would not guarantee that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.runner.identity import strip_runner_auth_secrets

from . import _proc
from .terminal import TerminalResult, _strip_ansi

if TYPE_CHECKING:
    # pywinpty is a Windows-only dependency (see pyproject's platform marker).
    # Import it lazily — at module top it would make this module unimportable on
    # Linux/macOS, where the platform-agnostic logic here (smallest-wins sizing,
    # replay, literal slash-command injection) is unit-tested. The only runtime
    # use is ``PtyProcess.spawn`` in ``launch``, which imports it there.
    from winpty import PtyProcess

logger = logging.getLogger(__name__)

# Output-quiet idle defaults. Callers (the runner's claude-native watcher)
# pass explicit fast values; these are only fallbacks.
_IDLE_THRESHOLD_SECONDS = 1.0
_IDLE_POLL_INTERVAL_SECONDS = 0.2

# Cap on the accumulated raw-output tail kept for read()/last_pane_text().
# Best-effort context, not a rendered screen — bounded so a long-lived pane
# does not grow without limit.
_OUTPUT_TAIL_MAX_CHARS = 200_000

# Per-subscriber output queue bound. On overflow (a stuck WS client) the
# oldest chunk is dropped so the producer never blocks the event loop.
_SUBSCRIBER_QUEUE_MAXSIZE = 2048

# Bracketed-paste enable: Claude Code emits this once its input box mounts.
# Used as the capture-pane-free "prompt ready" signal (validated against the
# real CLI; see polished-beaming-nebula-REVISED.md gate results).
_BRACKETED_PASTE_ENABLE = "\x1b[?2004h"
# After the input box mounts (bracketed-paste enable), Claude repaints a
# multi-line welcome/splash that clears and rewrites the input line ~1.3s
# later; a paste injected during that storm is clobbered and dropped (the
# first-message-of-a-fresh-session drop). So gate injection on the box having
# stopped repainting: output quiet for _READY_QUIET_S AND at least
# _READY_MIN_S elapsed since the enable (skips an ~0.9s lull mid-storm that a
# pure quiet check would fire inside). Tuned against a real-CLI ConPTY boot
# trace (paste-enable ~0.9s, splash ~2.2s, box settles ~2.7s).
_READY_QUIET_S = 0.6
_READY_MIN_S = 1.5
# Submitting the first message of a fresh session is a race. Boot-time hooks (the
# SessionStart subprocess, statusLine repaints) keep Claude busy right when the
# readiness gate opens, so a single submit CR is dropped while the pasted text
# still lands in the draft — message in box, never sent (the first-message
# no-submit, bisected to --settings hooks, not MCP). So write the paste (no CR),
# then RESEND a bare CR, but only during an output-quiet lull (Claude idle, draft
# pending): the first CR that lands once the hooks settle submits the draft. If
# output stays busy right after a CR, a turn is streaming (it submitted) — stop,
# which also keeps CRs from landing during the response. A redundant CR on the
# now-empty box is a no-op. Bounded by _SUBMIT_WINDOW_S.
_SUBMIT_SETTLE_S = 0.25
_SUBMIT_WINDOW_S = 12.0
_SUBMIT_QUIET_S = 0.4
_SUBMIT_CONFIRM_S = 1.2
# Delay before the optional auto-confirm CR of a slash command, so the TUI's
# confirmation dialog (/effort, /model) has rendered before the Enter lands.
_SLASH_CONFIRM_S = 0.3
# Max length-framed injection message body (loopback, same-user trust).
_INJECT_MAX_FRAME_BYTES = 4 * 1024 * 1024

# tmux key-name vocabulary the terminal contract uses, mapped to the bytes a
# raw ConPTY expects. Only the keys the claude path and sys_terminal tool emit
# are covered; unknown names are ignored (logged once).
_KEY_TO_BYTES: dict[str, str] = {
    "Enter": "\r",
    "Tab": "\t",
    "Escape": "\x1b",
    "Space": " ",
    "BSpace": "\x7f",
    "Backspace": "\x7f",
    "C-c": "\x03",
    "C-d": "\x04",
    "C-z": "\x1a",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
}


class WindowsTerminalInstance:
    """A single ConPTY-backed terminal for the Claude Code web harness.

    Constructed by ``create_terminal_instance`` on Windows in place of a
    tmux-backed :class:`~omnigent.inner.terminal.TerminalInstance`. Not yet
    launched — the caller invokes :meth:`launch` with the resolved cwd.
    """

    def __init__(
        self,
        *,
        name: str,
        session_key: str,
        private_dir: Path,
        command: str,
        args: list[str],
        env: dict[str, str],
        env_unset: list[str],
        inherit_env: bool = True,
        conversation_link: str | None = None,
        scrollback: int = 10_000,
    ) -> None:
        self.name = name
        self.session_key = session_key
        self.private_dir = Path(private_dir)
        # Placeholder kept only so registry / terminal_attach attribute access
        # (``.socket_path``) does not AttributeError. There is no real socket on
        # Windows; cross-process input uses ``input_pipe`` (set by the runner).
        self.socket_path = self.private_dir / "conpty.placeholder"
        self.command = command
        self.args = list(args)
        self.env = dict(env)
        self.env_unset = list(env_unset)
        self.inherit_env = inherit_env
        self.conversation_link = conversation_link
        self.scrollback = scrollback
        # NEW additive cross-process input channel: the named-pipe path the
        # runner advertises so the out-of-process executor can inject web
        # messages. ``None`` until the runner stands the input server up.
        self.input_pipe: str | None = None

        self.running = False
        self.launch_cwd: str | None = None
        # Distinct OS environment backing this terminal (sandbox/fork). The
        # POSIX TerminalInstance carries this so terminal_resource_view can
        # decide whether the terminal needs its own environment resource.
        # Windows sandbox is always "none" and fork is unsupported here, so
        # the ConPTY terminal always runs in the primary environment → None,
        # which terminal_resource_view maps to DEFAULT_ENVIRONMENT_ID.
        self.os_env = None

        self._pty: PtyProcess | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()

        self._write_queue: asyncio.Queue[str | None] | None = None
        self._writer_task: asyncio.Task[None] | None = None

        # Output fan-out. Maps each subscriber queue to the terminal size that
        # client last requested (``None`` until its first resize). Created and
        # consumed on the event loop; mutated only on the loop, so no lock is
        # needed. The size map drives smallest-wins sizing (see
        # ``_apply_effective_size``); the keys are the broadcast fan-out set.
        self._subscribers: dict[asyncio.Queue[bytes | None], tuple[int, int] | None] = {}
        # Current ConPTY dimensions (rows, cols). Seeded to the launch size so a
        # detach that lifts the last size constraint can restore it.
        self._effective_size: tuple[int, int] = (24, 80)
        # Per-subscriber control channel carrying effective (rows, cols) updates.
        # A ConPTY has ONE size, so with several clients it renders at the
        # smallest (see ``_apply_effective_size``); each client must then pin its
        # xterm grid to that shared size. A larger client left at its own size
        # shows stale cells outside the smaller active region — the multi-client
        # "artifacts". tmux solves this by drawing every client at the shared
        # size (padding the rest with dots); we push the size so the web client
        # letterboxes the same way.
        self._size_queues: dict[asyncio.Queue[bytes | None], asyncio.Queue[tuple[int, int]]] = {}

        self._output_tail = ""
        self._last_output_at = time.monotonic()
        self._last_client_interaction_at = float("-inf")
        # Set once Claude's input box mounts (bracketed-paste enable seen).
        self._bracketed_paste_seen = False
        # Set once the first injected message has completed its submit. The
        # boot-hook race that drops a lone submit CR only affects that first
        # message; afterwards the box is mounted and stable, so later messages
        # skip the output-quiet readiness gate (which never opens while a prior
        # turn is still streaming — the cause of injection timeouts on a
        # mid-turn steer).
        self._submitted_once = False
        # Loopback injection server for cross-process web-chat message injection
        # (stood up by the runner via ensure_injection_server). None until then.
        self._injection_server: _InjectionServer | None = None

        self._idle_thread: threading.Thread | None = None
        self._idle_stop_event: threading.Event | None = None
        self._idle_task: asyncio.Task[None] | None = None

    # -- contract: identity --------------------------------------------------

    @property
    def tmux_target(self) -> str:
        """Logical pane target. Always ``"main"`` (no tmux; kept for parity)."""
        return "main"

    def note_client_interaction(self) -> None:
        """Record that a web client just interacted (keystroke/resize/etc.).

        The idle watcher discounts output that lands within a threshold of this
        stamp so client-driven repaints don't read as agent activity. A single
        float assignment, atomic under the GIL — written on the loop, read on
        the watcher thread.
        """
        self._last_client_interaction_at = time.monotonic()

    def last_pane_text(self) -> str | None:
        """Return the recent visible output, ANSI-stripped, for diagnostics.

        Best-effort: the accumulated raw stream, not a rendered screen.
        """
        text = _strip_ansi(self._output_tail).strip()
        return text or None

    async def set_conversation_link(self, conversation_link: str | None) -> None:
        """Store the conversation link. No-op otherwise — a Windows console has
        no tmux status bar to display it in."""
        self.conversation_link = conversation_link

    # -- contract: lifecycle -------------------------------------------------

    async def launch(self, *, cwd: Path | None = None) -> None:
        """Spawn the command in a ConPTY and start the reader/writer plumbing."""
        if self.running:
            return

        self._loop = asyncio.get_running_loop()
        effective_cwd = str(cwd or self.private_dir)

        # Build env exactly as the POSIX path does: inherit -> per-terminal
        # overrides -> env_unset strip -> runner-auth-secret strip.
        if self.inherit_env:
            env = os.environ.copy()
        else:
            env = {}
        env.pop("OMNIGENT_TMUX_SOCK", None)
        env.update(self.env)
        for key in self.env_unset:
            env.pop(key, None)
        env = strip_runner_auth_secrets(env)
        # Force UTF-8 I/O for Python subprocesses (PEP 540). Without this,
        # Windows defaults to the system code page (cp1252), causing em-dashes
        # and other non-ASCII characters to be mangled in the chat view.
        env.setdefault("PYTHONUTF8", "1")

        # Deferred import: pywinpty is Windows-only, so importing it at module
        # load would break import on other platforms (see the TYPE_CHECKING note).
        from winpty import PtyProcess

        argv = _resolve_windows_argv(self.command, self.args)
        # ConPTY initial size is deliberately small (24x80) — matching
        # _effective_size — so the first browser attach GROWS it losslessly (a
        # shrink would rewrap and garble). With several clients the size is the
        # smallest each requests (set_client_size / smallest-wins).
        self._pty = await asyncio.to_thread(
            PtyProcess.spawn,
            argv,
            cwd=effective_cwd,
            env=env,
            dimensions=(24, 80),
        )
        self.launch_cwd = effective_cwd
        self.running = True

        self._write_queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._writer_loop())

        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"conpty-reader-{self.name}", daemon=True
        )
        self._reader_thread.start()

    async def is_alive(self) -> bool:
        """Return whether the ConPTY child process is alive."""
        pty = self._pty
        if pty is None:
            return False
        alive = bool(pty.isalive())
        if not alive:
            self.running = False
        return alive

    async def close(self) -> None:
        """Tear down: stop watchers, reader, writer; kill the child; rmtree."""
        await self._stop_idle_watcher()
        self._stop_idle_watcher_thread()

        if self._injection_server is not None:
            self._injection_server.close()
            self._injection_server = None

        self.running = False
        self._reader_stop.set()

        # Stop the writer task (sentinel; then cancel as a backstop).
        if self._write_queue is not None:
            with contextlib.suppress(Exception):
                self._write_queue.put_nowait(None)
        if self._writer_task is not None:
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer_task
            self._writer_task = None

        # Join the reader thread BEFORE closing the ConPTY handle (Windows
        # "handle in use" trap). Non-blocking reads let it exit within a poll;
        # the timeout + force-close below covers a read that blocked.
        reader = self._reader_thread
        if reader is not None:
            reader.join(timeout=1.0)

        pty = self._pty
        if pty is not None:
            pid = getattr(pty, "pid", None)
            with contextlib.suppress(Exception):
                pty.close(force=True)
            # Backstop: kill any surviving child tree (claude spawns helpers).
            if pid:
                with contextlib.suppress(Exception):
                    _proc.kill_tree(pid)
            self._pty = None

        # Final reader join in case close() was what unblocked it.
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        self._reader_thread = None

        # Wake any remaining subscribers so attached bridges can exit.
        self._broadcast(None)

        if self.private_dir.exists():
            shutil.rmtree(self.private_dir, ignore_errors=True)

    # -- contract: input -----------------------------------------------------

    async def send(self, text: str | None = None, *, keys: str = "Enter") -> TerminalResult:
        """Type ``text`` then press ``keys`` (space-separated tmux key names).

        Text and keys are enqueued as ONE payload so the whole keystroke is
        written atomically relative to other input sources.
        """
        if not self.running:
            return {"error": "Terminal is not running"}
        payload = text or ""
        if keys:
            for key in keys.split():
                mapped = _KEY_TO_BYTES.get(key)
                if mapped is None:
                    logger.warning("WindowsTerminalInstance: unknown key %r ignored", key)
                    continue
                payload += mapped
        if payload:
            self._enqueue_write(payload)
        return {"status": "sent"}

    async def send_raw(self, data: bytes) -> None:
        """Write raw input bytes (browser keystrokes from the WS bridge)."""
        if not self.running or not data:
            return
        self._enqueue_write(data.decode("utf-8", "replace"))

    def inject_payload(self, payload: str) -> None:
        """Enqueue a fully-formed input payload as one atomic write.

        Used by the runner's cross-process input server for hook-gated message
        injection (a bracketed-paste sequence plus Enter). Thread-safe: may be
        called from any thread; hops to the loop if needed.
        """
        loop = self._loop
        if loop is None:
            return
        # Swallow "event loop is closed" during teardown.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._enqueue_write, payload)

    async def submit_injected(self) -> None:
        """Submit a just-pasted first message, riding out the boot-hook race.

        A single submit CR right after the paste is dropped while Claude is busy
        with boot-time hooks (the SessionStart subprocess, statusLine repaints),
        so the message lands in the draft but never sends. Resend a bare CR, but
        only during an output-quiet lull (Claude idle, draft pending, ready for
        Enter): the first CR that lands once the hooks settle submits the draft.
        When output stays busy right after a CR, a turn is streaming — it
        submitted, so return (this also stops CRs landing during the response).
        Bounded by ``_SUBMIT_WINDOW_S``; a redundant CR on the now-empty box is a
        no-op.
        """
        await asyncio.sleep(_SUBMIT_SETTLE_S)  # let the paste commit to the draft
        try:
            if self._submitted_once:
                # Post-boot: the hook race that swallows a lone CR is over, so a
                # single Enter submits reliably. Don't wait for an output-quiet
                # lull — a steer sent while a prior turn is still streaming would
                # never see one, stalling the whole inject until it times out.
                self.inject_payload("\r")
                return
            deadline = time.monotonic() + _SUBMIT_WINDOW_S
            while time.monotonic() < deadline:
                if not self.running:
                    return
                if (time.monotonic() - self._last_output_at) < _SUBMIT_QUIET_S:
                    await asyncio.sleep(0.1)  # Claude is rendering; wait for a lull
                    continue
                self.inject_payload("\r")
                await asyncio.sleep(_SUBMIT_CONFIRM_S)
                if (time.monotonic() - self._last_output_at) < _SUBMIT_QUIET_S:
                    return  # output still streaming after the CR -> the turn started
        finally:
            # The box is mounted by now; later messages take the fast path above.
            self._submitted_once = True

    async def inject_slash_command(self, command: str, *, auto_confirm: bool = False) -> None:
        """Type a Claude Code slash command literally and submit it.

        The ConPTY analogue of the tmux ``inject_slash_command`` path. A slash
        command MUST be typed as literal keystrokes, NOT delivered as a
        bracketed paste: the paste markers tell Claude Code's TUI to treat the
        content as data, so a pasted ``/compact`` lands in the draft as text and
        submits as a normal turn instead of executing the command. So no
        ``_build_paste_payload`` here — the raw bytes go straight to the pty,
        exactly as ``tmux send-keys -l`` types them.

        Mirrors the tmux sequence: ``C-u`` clears any draft the user is
        mid-typing (otherwise the command concatenates with their text), then
        the literal command, then ``Enter``. A short settle between the command
        and the submit ``\\r`` keeps Claude from coalescing the two into one
        pasted burst (which would defeat slash-command parsing). ``auto_confirm``
        sends a second ``Enter`` after a beat to accept the default option of a
        TUI confirmation dialog (``/effort`` / ``/model`` pop one; ``/compact``
        does not); on an empty box the extra CR is a harmless no-op.
        """
        self.inject_payload("\x15")  # C-u: clear any in-progress draft
        self.inject_payload(command)
        # Let the command commit as typed input before the CR, so the two are
        # not merged into a single coalesced paste the slash parser ignores.
        await asyncio.sleep(_SUBMIT_SETTLE_S)
        self.inject_payload("\r")
        if auto_confirm:
            # Give the TUI time to render its confirmation dialog before the
            # auto-Enter arrives; otherwise the keystroke races the prompt.
            await asyncio.sleep(_SLASH_CONFIRM_S)
            self.inject_payload("\r")

    async def wait_until_ready(self, *, timeout_s: float = 30.0) -> bool:
        """Wait until Claude's input box has mounted AND stopped repainting.

        Capture-pane-free, two-phase readiness gate:

        1. Claude emits the bracketed-paste enable (``\\x1b[?2004h``) when its
           input box first mounts.
        2. But the box mounts mid-boot — Claude then repaints a multi-line
           welcome/splash that clears and rewrites the input line. A paste
           injected during that storm is clobbered and silently dropped (the
           first-message-of-a-fresh-session drop). So also wait until output
           has been quiet for ``_READY_QUIET_S`` (repaints stopped) and at
           least ``_READY_MIN_S`` has elapsed since the enable (skips an early
           mid-storm lull a pure quiet check would fire inside).

        Returns True once ready — or best-effort at the deadline so the caller
        still injects rather than silently dropping — and False only if the
        terminal died or the box never mounted in time.
        """
        deadline = time.monotonic() + timeout_s
        # Phase 1: input box mounts (bracketed-paste enable seen).
        paste_seen_at: float | None = None
        while time.monotonic() < deadline:
            if self._bracketed_paste_seen:
                paste_seen_at = time.monotonic()
                break
            if not self.running:
                return False
            await asyncio.sleep(0.05)
        if paste_seen_at is None:
            return False
        # Post-boot fast path: the splash-repaint drop only threatens the first
        # message. Once one has submitted, the box is stable — skip the
        # output-quiet gate so a steer sent mid-stream (output never quiet) isn't
        # stalled until the deadline, which is what surfaced as an inject timeout.
        if self._submitted_once:
            return True
        # Phase 2: repaints settled (output quiet) and the boot storm has had
        # time to start (min elapsed since the enable).
        while time.monotonic() < deadline:
            if not self.running:
                return False
            now = time.monotonic()
            if (now - self._last_output_at) >= _READY_QUIET_S and (
                now - paste_seen_at
            ) >= _READY_MIN_S:
                return True
            await asyncio.sleep(0.05)
        return True  # deadline: inject best-effort rather than silently skip

    def _enqueue_write(self, payload: str) -> None:
        """Put a payload on the write queue (call on the event loop)."""
        q = self._write_queue
        if q is None:
            return
        with contextlib.suppress(Exception):
            q.put_nowait(payload)

    async def _writer_loop(self) -> None:
        """Drain the write queue, writing each payload fully before the next."""
        q = self._write_queue
        assert q is not None
        while True:
            item = await q.get()
            try:
                if item is None:
                    return
                pty = self._pty
                if pty is None:
                    return
                # pywinpty writes are blocking — offload so a large paste never
                # stalls the event loop.
                await asyncio.to_thread(pty.write, item)
            except Exception:
                logger.exception("WindowsTerminalInstance writer failed (%s)", self.name)
            finally:
                q.task_done()

    def resize(self, rows: int, cols: int) -> None:
        """Force the ConPTY to an exact size, ignoring per-client constraints.

        Escape hatch for callers that want a hard size (none today). The WS
        bridge instead uses :meth:`set_client_size` so multiple attached
        browsers negotiate a shared size (smallest-wins). A later
        :meth:`set_client_size`/:meth:`unsubscribe` recomputes and overrides
        whatever this set.
        """
        self._set_pty_size(rows, cols)

    def set_client_size(self, q: asyncio.Queue[bytes | None], rows: int, cols: int) -> None:
        """Record one client's requested size and re-apply smallest-wins.

        A ConPTY has a single dimension, but several browsers can attach to the
        same session at once. Mirroring tmux's shared-session default, the
        effective size is the smallest rows/cols across all attached clients, so
        no client's viewport overflows the pane. Call on the event loop.
        """
        if q not in self._subscribers:
            return  # unsubscribed concurrently; ignore a late resize
        self._subscribers[q] = (rows, cols)
        self._apply_effective_size()

    def _apply_effective_size(self) -> None:
        """Resize the ConPTY to the min rows/cols across all sized clients.

        Clients that have not reported a size yet (``None``) do not constrain
        the result. When the last sized client detaches there is nothing to
        apply, so the pane keeps its current size until the next client sizes
        it. A no-op when the computed size already matches.
        """
        sizes = [s for s in self._subscribers.values() if s is not None]
        if not sizes:
            return
        rows = min(r for r, _ in sizes)
        cols = min(c for _, c in sizes)
        if (rows, cols) == self._effective_size:
            return
        self._set_pty_size(rows, cols)

    def _set_pty_size(self, rows: int, cols: int) -> None:
        """Apply a size to the ConPTY and remember it. Call on the event loop.

        Broadcasts the new size to every attached client so each pins its grid
        to the shared dimensions (see :meth:`_broadcast_effective_size`)."""
        self._effective_size = (rows, cols)
        self._broadcast_effective_size()
        pty = self._pty
        if pty is None:
            return
        with contextlib.suppress(Exception):
            pty.setwinsize(rows, cols)

    def _broadcast_effective_size(self) -> None:
        """Push the current effective (rows, cols) to every client's size channel.

        Each attached bridge forwards it as a ``resize`` control frame so the
        client renders at the shared smallest-wins size. Drop-oldest on a full
        queue (like :meth:`_broadcast`) so a wedged client never blocks others;
        only the latest size matters, so a dropped intermediate is harmless.
        """
        for size_q in self._size_queues.values():
            try:
                size_q.put_nowait(self._effective_size)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    size_q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    size_q.put_nowait(self._effective_size)

    # -- contract: output ----------------------------------------------------

    async def read(self, scrollback: int = 0) -> TerminalResult:
        """Best-effort screen capture: the recent raw output, ANSI-stripped.

        Not a true rendered screen (no pyte buffer); fine for the claude path,
        whose turn-completion is hook/transcript-driven, not screen-scraped.
        """
        if not self.running:
            return {"error": "Terminal is not running"}
        return {
            "terminal": f"{self.name}:{self.session_key}",
            "screen": _strip_ansi(self._output_tail),
            "scrollback_lines": scrollback,
        }

    def subscribe(self, *, replay: bool = True) -> asyncio.Queue[bytes | None]:
        """Register a WS subscriber. Returns a queue of output byte-chunks
        (and a final ``None`` sentinel on process exit). Call on the loop.

        When ``replay`` is set (the default), the queue is primed with a
        snapshot of the accumulated output tail BEFORE it joins the fan-out, so
        a reconnecting client — or a second browser opening the same session —
        immediately renders the current screen instead of a blank pane that
        only fills on the next output. This snapshot-then-register order is
        atomic: ``subscribe`` and ``_on_output`` both run on the event loop and
        there is no ``await`` between snapshotting the tail and registering, so
        no chunk can slip in between (no gap, no duplicate). The replay is the
        best-effort raw stream (same basis as :meth:`read`), not a re-rendered
        screen — xterm.js replays the ANSI and Claude's frequent repaints
        reconcile any sequence clipped by the tail bound.
        """
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        if replay and self._output_tail:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(self._output_tail.encode("utf-8", "replace"))
        # Seed a late joiner with the current shared size so it pins immediately
        # — but only when another sized client already constrains the pane.
        # A solo first client just renders at its own fit (its first resize sets
        # the size), so seeding it would force a needless reflow to the launch
        # default first.
        size_q: asyncio.Queue[tuple[int, int]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        if any(s is not None for s in self._subscribers.values()):
            with contextlib.suppress(asyncio.QueueFull):
                size_q.put_nowait(self._effective_size)
        self._size_queues[q] = size_q
        self._subscribers[q] = None
        return q

    def size_channel(
        self, q: asyncio.Queue[bytes | None]
    ) -> asyncio.Queue[tuple[int, int]] | None:
        """Return the effective-size control queue for a subscribed output queue.

        The WS bridge drains this and forwards each ``(rows, cols)`` to its
        client as a ``resize`` control frame. ``None`` if ``q`` is not (or no
        longer) subscribed.
        """
        return self._size_queues.get(q)

    def unsubscribe(self, q: asyncio.Queue[bytes | None]) -> None:
        """Deregister a WS subscriber and lift its size constraint.

        Recomputing after removal lets the pane grow back when the client that
        was holding it small detaches (smallest-wins, see
        :meth:`set_client_size`)."""
        self._size_queues.pop(q, None)
        had_size = self._subscribers.pop(q, None) is not None
        if had_size:
            self._apply_effective_size()

    def _reader_loop(self) -> None:
        """Daemon thread: read ConPTY output, hand chunks to the loop."""
        pty = self._pty
        assert pty is not None
        while not self._reader_stop.is_set():
            try:
                data = pty.read(8192)
            except EOFError:
                break
            except Exception:  # noqa: BLE001 - any ConPTY read failure ends the loop
                break
            if not data:
                if not pty.isalive():
                    break
                time.sleep(0.01)
                continue
            loop = self._loop
            if loop is None:
                break
            try:
                loop.call_soon_threadsafe(self._on_output, data)
            except RuntimeError:
                break  # loop closed
        # Signal exit to the loop side.
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._mark_exited)

    def _on_output(self, data: str) -> None:
        """Loop-thread sink for one output chunk: accumulate + fan out."""
        self._last_output_at = time.monotonic()
        tail = self._output_tail + data
        if len(tail) > _OUTPUT_TAIL_MAX_CHARS:
            tail = tail[-_OUTPUT_TAIL_MAX_CHARS:]
        self._output_tail = tail
        if not self._bracketed_paste_seen and _BRACKETED_PASTE_ENABLE in tail:
            self._bracketed_paste_seen = True
        self._broadcast(data.encode("utf-8", "replace"))

    def _broadcast(self, chunk: bytes | None) -> None:
        """Fan a chunk (or the ``None`` EOF sentinel) out to subscribers.

        On a full subscriber queue, drop the oldest chunk so a stuck client
        never blocks the producer.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(chunk)

    def _mark_exited(self) -> None:
        """Loop-thread: the child exited; flip state and notify subscribers."""
        self.running = False
        self._broadcast(None)

    # -- contract: idle watching (output-quiet timing, no capture-pane) ------

    def start_idle_watcher(
        self,
        on_idle: Any,
        *,
        on_exit: Any | None = None,
    ) -> None:
        """Asyncio output-quiet idle watcher (edge-triggered)."""
        if not self.running:
            raise RuntimeError("Cannot start idle watcher before launch")
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(self._idle_watch_loop(on_idle, on_exit))

    async def _idle_watch_loop(self, on_idle: Any, on_exit: Any | None) -> None:
        threshold = _IDLE_THRESHOLD_SECONDS
        last_seen = self._last_output_at
        idle_fired = False
        while self.running:
            await asyncio.sleep(_IDLE_POLL_INTERVAL_SECONDS)
            if not self.running:
                return
            if not await self.is_alive():
                if on_exit is not None:
                    await _fire_async(on_exit)
                return
            cur = self._last_output_at
            now = time.monotonic()
            if cur != last_seen:
                last_seen = cur
                idle_fired = False
            elif not idle_fired and (now - cur) >= threshold:
                if (now - self._last_client_interaction_at) >= threshold:
                    idle_fired = True
                    if not await _fire_async(on_idle):
                        return

    async def _stop_idle_watcher(self) -> None:
        task = self._idle_task
        if task is None:
            return
        self._idle_task = None
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def start_idle_watcher_thread(
        self,
        on_idle: Any | None = None,
        *,
        on_activity: Any | None = None,
        on_exit: Any | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        """Daemon-thread output-quiet idle watcher.

        Thread-based sibling of :meth:`start_idle_watcher` for callers without a
        long-lived event loop. ``on_activity`` fires each poll tick output
        changed; ``on_idle`` fires once per quiet transition (re-arms when
        output resumes); ``on_exit`` fires once when the child dies.
        """
        if self._idle_thread is not None and self._idle_thread.is_alive():
            if not replace:
                return
            self._stop_idle_watcher_thread()

        threshold = idle_threshold_s or _IDLE_THRESHOLD_SECONDS
        interval = poll_interval_s or _IDLE_POLL_INTERVAL_SECONDS
        stop = threading.Event()
        self._idle_stop_event = stop

        def _loop() -> None:
            last_seen = self._last_output_at
            idle_fired = False
            while not stop.is_set():
                stop.wait(interval)
                if stop.is_set():
                    return
                pty = self._pty
                if pty is None or not pty.isalive():
                    self.running = False
                    if on_exit is not None:
                        _fire_sync(on_exit)
                    return
                cur = self._last_output_at
                now = time.monotonic()
                if cur != last_seen:
                    last_seen = cur
                    idle_fired = False
                    if on_activity is not None:
                        _fire_sync(on_activity)
                elif not idle_fired and (now - cur) >= threshold:
                    if (now - self._last_client_interaction_at) >= threshold:
                        idle_fired = True
                        if on_idle is not None:
                            _fire_sync(on_idle)

        t = threading.Thread(target=_loop, name=f"conpty-idle-{self.name}", daemon=True)
        self._idle_thread = t
        t.start()

    def _stop_idle_watcher_thread(self) -> None:
        stop = self._idle_stop_event
        if stop is not None:
            stop.set()
        self._idle_stop_event = None
        self._idle_thread = None


def _build_paste_payload(content: str) -> str:
    """Build the bracketed-paste portion of a Claude Code injection (no submit CR).

    ``ESC[200~`` + content + ``\\n`` + ``ESC[201~``. The interior newline keeps
    multi-line input as data (the paste markers tell the TUI not to submit on
    each newline) and absorbs a trailing backslash so it cannot escape the
    submit CR. The ``ESC[201~`` cleanly ends the paste. The submit CR is written
    SEPARATELY by the caller after ``_SUBMIT_SETTLE_S`` — bundling it here lets
    it race the paste under boot/MCP load and submit an empty draft.
    """
    return "\x1b[200~" + content + "\n" + "\x1b[201~"


class _InjectionServer:
    """Per-instance loopback TCP server for cross-process message injection.

    The web-chat executor runs as a SEPARATE process and cannot touch the
    in-process ConPTY, so the runner (which owns the instance) hosts this
    server. The executor connects to ``127.0.0.1:<port>``, sends one
    length-framed JSON request ``{token, kind, content}``, and reads a framed
    JSON ack. ``token`` (random, advertised in the bridge file) gates the
    same-user-trust boundary so an arbitrary local process can't inject input.

    Loopback TCP (not a named pipe): asyncio ``start_server`` is fully
    supported on the ProactorEventLoop, binding 127.0.0.1 raises no firewall
    prompt, and binding port 0 makes port management trivial.
    """

    def __init__(self, instance: WindowsTerminalInstance) -> None:
        self._instance = instance
        self.host = "127.0.0.1"
        self.token = secrets.token_hex(16)
        self.port = 0
        self._sock: socket.socket | None = None
        self._server: asyncio.AbstractServer | None = None
        self._serve_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Bind synchronously (so ``port`` is known immediately) and schedule
        the async serve loop on the current running loop."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((self.host, 0))
        s.listen(8)
        s.setblocking(False)
        self.port = s.getsockname()[1]
        self._sock = s
        loop = asyncio.get_running_loop()
        self._serve_task = loop.create_task(self._serve())

    async def _serve(self) -> None:
        assert self._sock is not None
        server = await asyncio.start_server(self._handle, sock=self._sock)
        self._server = server
        with contextlib.suppress(asyncio.CancelledError):
            async with server:
                await server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await self._read_frame(reader)
            if req is None:
                return
            ok, error = await self._dispatch(req)
            await self._write_frame(writer, {"ok": ok, "error": error})
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception:
            logger.exception("injection server handler failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _dispatch(self, req: dict[str, Any]) -> tuple[bool, str | None]:
        if req.get("token") != self.token:
            return False, "bad token"
        if not self._instance.running:
            return False, "terminal not running"
        kind = req.get("kind", "message")
        if kind == "interrupt":
            self._instance.inject_payload("\x1b")  # Escape cancels the turn
            return True, None
        content = req.get("content")
        if not isinstance(content, str) or not content:
            return False, "empty content"
        # Gate on prompt readiness so the first message of a fresh session is
        # not typed into a still-booting TUI and dropped.
        await self._instance.wait_until_ready(timeout_s=float(req.get("timeout_s", 30.0)))
        if kind == "slash":
            # Slash commands (/compact, /effort, /model) are typed literally,
            # not bracket-pasted — see WindowsTerminalInstance.inject_slash_command.
            await self._instance.inject_slash_command(
                content, auto_confirm=bool(req.get("auto_confirm"))
            )
            return True, None
        # Write the paste, then submit with a quiet-gated CR resend that rides
        # out the boot-hook race (see WindowsTerminalInstance.submit_injected).
        self._instance.inject_payload(_build_paste_payload(content))
        await self._instance.submit_injected()
        return True, None

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
        header = await reader.readexactly(4)
        n = int.from_bytes(header, "big")
        if n <= 0 or n > _INJECT_MAX_FRAME_BYTES:
            return None
        body = await reader.readexactly(n)
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    async def _write_frame(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        writer.write(len(body).to_bytes(4, "big") + body)
        with contextlib.suppress(Exception):
            await writer.drain()

    def close(self) -> None:
        if self._serve_task is not None:
            self._serve_task.cancel()
            self._serve_task = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


def ensure_injection_server(instance: WindowsTerminalInstance) -> _InjectionServer:
    """Start (once) and return the instance's loopback injection server.

    Idempotent: repeated calls return the same server. Must be called from a
    coroutine/callback running on the instance's event loop (the runner's
    terminal-launch path is).
    """
    existing = instance._injection_server
    if existing is not None:
        return existing
    server = _InjectionServer(instance)
    server.start()
    instance._injection_server = server
    return server


def _resolve_windows_argv(command: str, args: list[str]) -> list[str]:
    """Resolve a command name to a spawnable ConPTY argv on Windows.

    ``CreateProcess`` (used by pywinpty) only finds ``name.exe`` on PATH; it
    does not honor PATHEXT, so a CLI installed as a ``.cmd``/``.bat`` shim
    (npm's ``claude.cmd``) or a ``.ps1`` would fail to spawn directly. Resolve
    via ``shutil.which`` (which honors PATHEXT) and wrap a non-native shim
    through its interpreter — mirroring how the POSIX path runs the command via
    a shell inside tmux. An absolute path to an ``.exe`` passes straight
    through.
    """
    resolved = shutil.which(command)
    if resolved is None:
        # Let pywinpty surface a clear spawn error against the raw name.
        return [command, *args]
    ext = Path(resolved).suffix.lower()
    if ext in (".exe", ".com", ""):
        return [resolved, *args]
    if ext == ".ps1":
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved,
            *args,
        ]
    # .cmd / .bat (and anything else): run through cmd.exe, which fully
    # resolves and chains to the real executable while inheriting the ConPTY.
    return ["cmd.exe", "/d", "/s", "/c", resolved, *args]


def _fire_sync(callback: Any) -> None:
    try:
        callback()
    except Exception:
        logger.exception("idle watcher callback failed")


async def _fire_async(callback: Any) -> bool:
    """Invoke a possibly-async callback. Returns False if it raised."""
    try:
        result = callback()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("idle watcher callback failed")
        return False
    return True
