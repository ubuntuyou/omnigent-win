# ARCHITECTURE — omnigent-win

This document describes the **Windows-specific** architecture of `omnigent-win` and how
it aligns with upstream `omnigent-ai/omnigent`. It assumes familiarity with upstream's
claude-native harness. The companion diagram is `architecture.mmd`.

> **Design rule:** every Windows behavior is additive and `IS_WINDOWS`-guarded. The
> POSIX/tmux path is byte-for-byte upstream. Where upstream branches on tmux, the fork
> adds a parallel ConPTY branch and returns early.

---

## 1. How upstream's claude-native harness works (the part we mirror)

Omnigent's claude-native harness does **not** call the Anthropic API directly. It runs
the real `claude` CLI in a terminal and mirrors its activity into the web UI:

1. The runner launches `claude` inside a **terminal instance** (tmux pane on POSIX).
2. A **transcript forwarder** tails Claude's JSONL transcript file and POSTs each new
   item to the Omnigent server as an `external_conversation_item` event.
3. The server persists those items and republishes them on the per-session **SSE
   stream**; the web **chat view** renders them.
4. Web-chat user messages are **injected** into the same terminal (tmux `send-keys` on
   POSIX) so Claude sees them as if typed.
5. The browser **terminal view** attaches to the terminal over a WebSocket for the live
   TUI.

So there are two independent rendering surfaces fed from the one `claude` process:
the **chat view** (transcript-forwarder → server → SSE) and the **terminal view**
(terminal output → WebSocket).

## 2. What Windows lacks, and the ConPTY substitution

tmux is POSIX-only. Upstream's `create_terminal_instance` historically raised
`RuntimeError` on Windows, so the entire claude-native path was dead there. This fork
substitutes a **ConPTY** (Windows pseudo-console, via `pywinpty`).

The defining differences between tmux and a ConPTY, and how the fork copes:

| tmux (POSIX) | ConPTY (Windows) | Fork's approach |
| --- | --- | --- |
| External multi-client socket server | In-process, single-consumer; output read **once** | The instance OWNS the ConPTY and **fans output out** to subscriber queues |
| `send-keys` over a socket from any process | No socket; only the owning process can write | A **loopback injection server** lets the out-of-process executor inject input |
| Multi-client, per-client sizing | Exactly **one** dimension | **Smallest-wins** sizing + client-side **pin/letterbox** |
| `capture-pane` rendered screen | Raw byte stream only | Best-effort raw tail (turn completion is hook/transcript-driven, so this suffices) |

## 3. Component map (Windows path)

```
claude.exe (ConPTY child)
   │  stdout/stderr (UTF-8, forced via PYTHONUTF8=1)
   ▼
WindowsTerminalInstance              omnigent/inner/terminal_windows.py
   │  - reader thread → _on_output → fan-out to subscriber queues
   │  - single write queue (atomic injection vs. keystrokes)
   │  - _InjectionServer (loopback TCP, token-gated)
   │  - smallest-wins sizing + per-subscriber size channel
   ├─────────────► transcript JSONL on disk
   │                   │
   │                   ▼
   │              transcript forwarder         omnigent/claude_native_forwarder.py
   │                   │  - tails JSONL by byte_offset + seen_source_ids cursor
   │                   │  - _json_safe() scrubs lone surrogates before POST
   │                   ▼
   │              Omnigent server  ── persists ──► SSE stream ──► CHAT VIEW
   │
   └─ bridge_conpty_to_websocket   omnigent/terminals/ws_bridge.py ──► TERMINAL VIEW
                                                                    (TerminalSession.ts)
web chat input ─► executor ─► inject_user_message ─► _InjectionServer ─► write queue
```

## 4. Windows-specific mechanisms in detail

### 4.1 Terminal creation (`omnigent/inner/terminal.py`)
`create_terminal_instance` has an `IS_WINDOWS` branch (~line 1729) that builds a
`WindowsTerminalInstance` instead of a tmux pane. `reap_orphaned_terminals` also gained
a Windows guard so it no-ops cleanly without tmux.

### 4.2 `WindowsTerminalInstance` (`omnigent/inner/terminal_windows.py`)
The tmux replacement. Key responsibilities:

- **Launch** (`launch`): spawns `claude` via `PtyProcess.spawn` with
  `env.setdefault("PYTHONUTF8", "1")` so the child's I/O is UTF-8 (the mojibake fix).
- **Output fan-out** (`subscribe`, `_on_output`, `_broadcast`): a reader thread hands
  chunks to the event loop; each is broadcast to every subscriber queue. `subscribe`
  with `replay=True` primes a new queue with the accumulated output tail **before**
  registering it — atomic snapshot-then-register (no `await` between), giving reconnect
  resilience and instant second-browser paint.
- **Single write queue** (`inject_payload`, `send_raw`): browser keystrokes and injected
  web messages funnel through one queue so a bracketed-paste + Enter can't interleave
  with a keystroke.
- **First-message submit** (`submit_injected`, `wait_until_ready`, `_submitted_once`):
  a boot-hook race drops a lone submit CR on the first message; the readiness gate +
  quiet-gated CR resend handle it. Subsequent messages take a fast path (the
  injection-timeout fix).
- **Slash commands** (`inject_slash_command`): types `C-u` → literal command → `Enter`
  (NOT a bracketed paste, or the TUI treats it as data).
- **Smallest-wins sizing** (`set_client_size`, `_apply_effective_size`,
  `_broadcast_effective_size`, `size_channel`): the pane is sized to the min across all
  attached clients; the new size is broadcast on a per-subscriber control channel.
- **Injection server** (`_InjectionServer`, `ensure_injection_server`): a loopback TCP
  server, token-gated, that lets the separate executor process deliver input into the
  in-process ConPTY. Its host/port/token are written to the bridge dir.

### 4.3 Cross-process injection (`omnigent/claude_native_bridge.py`)
`inject_user_message` / `inject_slash_command` / the interrupt path each have an
`IS_WINDOWS` branch that connects to the loopback injection server instead of shelling
out to tmux. `write_tmux_target` additionally advertises `input_host/port/token` on
Windows. The POSIX uid-ownership security checks are skipped when `os.getuid()` is
unavailable (`my_uid == -1`), since NTFS uses ACLs, not POSIX mode bits.

### 4.4 Transcript forwarder (`omnigent/claude_native_forwarder.py`)
Identical to upstream **except** `_json_safe()` recursively scrubs lone surrogates from
every outgoing payload before the httpx POST. Claude's JSONL can contain a lone
surrogate; without scrubbing, `json.dumps` → `UnicodeEncodeError` crashes the forwarder,
the supervisor restarts it, and on restart it replays the transcript — **doubling every
turn** in the chat view while the terminal view stays correct. This was the doubling bug.

### 4.5 ConPTY → WebSocket bridge (`omnigent/terminals/ws_bridge.py`)
`bridge_conpty_to_websocket` subscribes to the instance's output fan-out and forwards
chunks to the browser as binary frames. Client→server resize frames route through
`set_client_size` (feeding smallest-wins), and a size-forwarder task drains the
instance's size channel and sends `{type:"resize",cols,rows}` text frames so clients pin
to the shared grid. `terminal_attach.py` routes Windows attaches here instead of the
tmux PTY bridge.

### 4.6 Hooks (`claude_native_hook.py`, `claude_native_status.py`, `claude_native_message_display_hook.py`)
Claude invokes these as separate Python processes. Each reads stdin via
`sys.stdin.buffer.read().decode("utf-8", "replace")` rather than text-mode stdin, which
on Windows would decode as cp1252 and reintroduce mojibake.

### 4.7 Frontend terminal client (`ap-web/src/components/blocks/TerminalSession.ts`)
Handles the server-pushed `resize` control frames: when the server pins a shared size,
the client resizes its xterm grid to it (`serverPinnedSize`) and paints the leftover
margin with a dim per-cell dot pattern (`updateLetterbox`) — the web analog of tmux
filling a larger client's inactive region with `·`. `sendResize` reports the container's
true capacity via `proposeDimensions()` (falling back to the current grid when there's
no measurable layout, e.g. jsdom) without committing the grid, so smallest-wins
negotiation sees real sizes.

## 4.8 OpenCode native on Windows (chat-only)

The opencode-native harness is a **different transport** from claude-native: instead of
mirroring a TUI, the runner spawns `opencode serve` (a local HTTP server), injects web
turns over its REST API, and tails its SSE stream into the chat view. A second, optional
`opencode attach` process rides the ConPTY to mirror opencode's own terminal UI into the
**terminal view**. The two channels are independent.

Two Windows-specific facts shape the fork's behavior:

- **`opencode serve` needs `SystemRoot`.** `filtered_server_env`
  (`omnigent/opencode_native_app_server.py`) builds a minimal allowlisted env for the
  serve child. On Windows that allowlist must include the OS essentials
  (`WINDOWS_ENV_PASSTHROUGH` from `omnigent/_platform.py`) — without `SystemRoot` the
  Bun-compiled `opencode` binary fast-fails at startup with exit `0xC0000409`
  (`STATUS_STACK_BUFFER_OVERRUN`), which surfaced as the generic "not supported on
  Windows" terminal-start error. The passthrough is computed at call-time behind
  `IS_WINDOWS`, so POSIX env is unchanged.

- **`opencode attach` can't render on Windows.** opencode's OpenTUI render library fails
  to load its embedded native DLL on Windows (upstream Bun/Windows limitation —
  `error 126`), so `attach` crashes on boot. The chat channel is unaffected, so
  `_auto_create_opencode_terminal` (`omnigent/runner/app.py`) **degrades gracefully**:
  on `IS_WINDOWS` it launches a lightweight PowerShell placeholder
  (`_OPENCODE_WINDOWS_CHAT_ONLY_BANNER`) explaining the session is chat-only instead of
  the doomed `attach`, keeping the server + forwarder (chat) fully live. POSIX still
  attaches the real TUI.

## 5. Alignment with upstream — merge surface

The fork stays mergeable with upstream because almost everything is additive:

- **Fork-only files** (rarely conflict): `omnigent/inner/terminal_windows.py`,
  `tests/inner/test_terminal_windows.py`, plus the ConPTY branch of `ws_bridge.py` and
  the additions in `TerminalSession.ts`.
- **Touched-both files** (the real conflict surface): `omnigent/runner/app.py`,
  `omnigent/claude_native_hook.py`, `omnigent/host/connect.py`,
  `omnigent/claude_native_bridge.py`, `README.md`. Conflicts here are usually small
  because the Windows logic is in `IS_WINDOWS` blocks.
- **Sync** is a manual merge from the `upstream` remote (this is not a real GitHub
  fork). See `CLAUDE.md` and the `upstream-sync` memory.

## 6. Known limitations (Windows path)

- **Claude Code (full TUI) + OpenCode (chat-only).** The claude-native path has both
  chat and terminal views. The opencode-native path works as **chat-only** (see §4.8);
  its TUI can't render on Windows. Other native harnesses (Codex, Cursor, Goose, Qwen)
  still require tmux and are untested on Windows.
- The browser **Files** panel and terminal-list resource endpoints return `502` on
  Windows (resource proxy not wired up there yet); chat and terminal views are
  unaffected.
- Output is **best-effort raw**, not a `pyte`-rendered screen.
- `keep_alive_after_exit` (tmux `remain-on-exit`) is not emulated — the ConPTY closes
  when `claude` exits.
- Interaction is via the **browser** terminal view; local `omnigent claude` TTY attach
  in a PowerShell window is not covered.
- No tmux-style out-of-process **session persistence daemon** yet — reconnect resilience
  is in-process (survives browser reconnects, not a runner restart).
