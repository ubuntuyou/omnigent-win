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

- **`opencode attach` (the TUI) needs a writable `%TEMP%`.** The attach process renders
  opencode's UI via OpenTUI, whose native library is a DLL bun embeds in the compiled
  `opencode.exe`. At startup bun **extracts** that DLL into `%TEMP%` and `dlopen`s the
  real copy (the `B:/~BUN/root/...` shown in failures is just bun's virtual *source*
  path). If `%TEMP%` is a restricted dir — e.g. `C:\WINDOWS\temp`, which the launch chain
  can inherit — the extraction/exec is blocked and the load fails with `error 126`, so the
  terminal view stays dead. The fix pins a guaranteed-writable, per-session temp dir:
  `_opencode_windows_tempdir(bridge_dir)` → `<bridge_dir>/tmp`, injected as `TEMP`/`TMP`
  by both `filtered_server_env` (serve) and `opencode_terminal_env` (attach) under
  `IS_WINDOWS`. With that, opencode on Windows runs the **full** TUI exactly like POSIX.
  (This was originally misdiagnosed as an unfixable upstream limitation — every "broken"
  repro had run under a sandbox `TEMP=C:\WINDOWS\temp`.)

## 4.9 Codex native on Windows (via an OpenAI-compatible provider)

The codex-native harness is a **server transport** like opencode, not a TUI mirror:
the runner spawns `codex app-server --listen ws://127.0.0.1:<port>` (JSON-RPC over a
loopback WebSocket), drives turns over that socket, and rides a ConPTY for the terminal
view. The defining Windows wrinkle is **auth**: Codex normally expects an OpenAI login,
which the user doesn't have, so on Windows it routes through an omnigent **`key` provider**
(Ollama Cloud) configured in `~/.omnigent/config.yaml`. Five facts shape the fork, all
additive and `IS_WINDOWS`-guarded; the POSIX OpenAI-login path is untouched.

- **Wire API.** codex ≥0.137 only speaks the **Responses** API (it hard-fails on
  `wire_api="chat"`). Ollama Cloud implements `/v1/responses`, so the provider block sets
  `wire_api: responses` — verified compatible, not assumed.

- **Auth (the core additive change).** The POSIX provider override authenticates with
  `auth={command="sh",args=["-c","printf …"]}` — but there is no `sh`/`printf` on a
  Windows codex child. `_provider_codex_config_overrides` (`omnigent/inner/codex_executor.py`)
  takes a `bearer_token` and, on Windows with a static key, emits an inline
  `http_headers={Authorization="Bearer …"}` instead. `_codex_provider_launch`
  (`omnigent/codex_native_app_server.py`) threads the configured key through as that bearer.

- **Env.** `_clean_codex_env` adds `WINDOWS_ENV_PASSTHROUGH` (notably `SystemRoot`) to the
  app-server child's allowlist, or it fast-fails with `0xC0000409` — the same Bun/Windows
  startup trap as opencode (§4.8).

- **Readiness gate.** `_codex_auth_unavailable_reason` (`omnigent/codex_native.py`) used to
  gate solely on `auth.json`. On Windows it now also accepts a configured routable provider
  (`_codex_configured_provider_routes`), so the harness picker unblocks with no OpenAI login.

- **Spawning the real exe, not the shim.** `shutil.which("codex")` returns `codex.CMD`, a
  batch shim that relaunches node with `%*`, re-parsing argv through `cmd.exe` and mangling
  the `-c` provider/MCP overrides (embedded quotes + spaces get split → `unexpected argument`
  → masked as "not supported on Windows"). `_find_codex_cli` resolves the vendored
  `…/@openai/codex-win32-*/**/codex.exe` and spawns it directly, bypassing the re-parse.

The CLI is **pinned to `0.139.0`**: codex 0.142 removed `app-server --listen` (stdio-only),
which would break the loopback-WebSocket transport. Custom provider IDs can't be named
`ollama` (reserved built-in = local Ollama); omnigent uses `omnigent_provider`.

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

- **Working native harnesses: Claude Code, OpenCode, Pi, Codex.** All four have chat
  **and** terminal views on Windows (opencode needs the `%TEMP%` fix in §4.8; codex needs
  the provider/auth/exe wiring in §4.9). The remaining native harnesses (Cursor, Goose,
  Qwen, Kimi, Hermes, Kiro, Antigravity) still require tmux and are untested on Windows.
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
