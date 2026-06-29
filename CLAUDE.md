# CLAUDE.md — omnigent-win

## What this is

`omnigent-win` is a **Windows-native fork** of [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent).
Upstream's native terminal harnesses run the agent CLI inside **tmux**, which is
POSIX-only, so on Windows the terminal layer raised *"not supported."* This fork
adds a parallel **ConPTY backend** (via [`pywinpty`](https://pypi.org/project/pywinpty/))
so native agent harnesses run on **Windows 11**, streamed to the Omnigent web UI.

**Working native harnesses on Windows (this branch):** **Claude Code** (`claude`),
**OpenCode** (`opencode`), and **Goose** (`goose`). Codex (`codex`) and Pi (`pi`) are
ported on their own in-flight branches; the remaining tmux-only ones (cursor / qwen /
kimi / hermes / kiro / antigravity) still need the port — the
`diagnose-windows-native-harness` skill walks the recurring failure chain.

Every change is **purely additive and `IS_WINDOWS`-guarded** — the POSIX/tmux path is
untouched. See `ARCHITECTURE.md` for the data-flow detail and `architecture.mmd` for the
diagram.

## The fork relationship (important)

- `ubuntuyou/omnigent-win` is **NOT a GitHub fork** (`isFork: false`). It was seeded by
  pushing a clone, so it shares full git history with upstream but the web "Sync fork"
  button does nothing.
- Sync via the `upstream` remote with a merge:
  ```
  git checkout main && git fetch upstream && git merge upstream/main && git push origin main
  ```
- `gh` is **not on PATH**; it lives at
  `C:\Users\Joe\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe`.
- Because the `upstream` remote exists, `gh pr create` defaults its base repo to
  `omnigent-ai/omnigent` and fails. **Always** pass
  `--repo ubuntuyou/omnigent-win --base main --head <branch>` for internal PRs.
- CI inherits upstream's gates. **Maintainer Approval** passes because `ubuntuyou` is in
  `.github/MAINTAINER`. The upstream Linux E2E/pytest shards don't exercise the Windows
  ConPTY path, so they're admin-merged past once Maintainer Approval + Pre-commit are green.

## Invariants — do not break these

1. **Additive, `IS_WINDOWS`-guarded.** Never change the POSIX/tmux behavior. Every
   Windows-specific branch sits behind `IS_WINDOWS`; the fork-only files
   (`terminal_windows.py`, the ConPTY branch of `ws_bridge.py`, `TerminalSession.ts`
   additions) are the only places that own new behavior.
2. **UTF-8 end to end.** Windows defaults to cp1252 and will produce `â€"` / `â†'`
   mojibake at every Python decode boundary. The ConPTY child gets
   `env.setdefault("PYTHONUTF8", "1")` (PEP 540; `setdefault` so a user override wins),
   and all three Claude hooks read stdin via `sys.stdin.buffer.read().decode("utf-8", "replace")`.
3. **Outgoing forwarder payloads are surrogate-scrubbed.** Claude's transcript JSONL can
   carry lone surrogates; `_json_safe()` in `claude_native_forwarder.py` scrubs them
   before every POST. Without it the forwarder crashes → supervisor restarts it →
   replays the transcript → **turns double wholesale** in the chat view (terminal view
   stays fine). This was the root cause of the doubling bug.
4. **Slash commands are typed literally, not bracket-pasted.** `/compact`, `/effort`,
   `/model` go in as `C-u` → literal command → `Enter`. A bracketed paste makes the TUI
   treat the text as data and submit it as a normal turn.
5. **Smallest-wins is the multi-client contract.** A ConPTY has one dimension. With
   several browsers attached, the pane is sized to the **min** rows/cols; larger clients
   pin to the shared grid and letterbox the margin. Don't reintroduce per-client resize
   that drives the shared ConPTY.

## Gotchas

- **The served web bundle is prebuilt and gitignored.** The server serves
  `omnigent/server/static/web-ui` (built by `vite build`). Frontend edits do nothing
  until you `cd ap-web && npm run build`, then hard-refresh the browser. A stale bundle
  is the #1 "my fix didn't take" cause.
- **Changes only take effect after a runner/server restart.** Backend edits load at
  launch; always restart before dogfooding.
- **`start-omnigent.ps1` is the local LAN-launch script and is intentionally untracked.**
  Don't commit it.
- **Native Windows can't run the full test/lint suite** (POSIX-only deps, `.venv/bin`
  hook paths). `ruff` runs via `uvx ruff@0.15.16`. Pure-logic unit tests under
  `tests/inner/` run natively; the full suite needs WSL2.
- **The Bash/PowerShell tools lie about `%TEMP%`.** They run with
  `TEMP=C:\WINDOWS\temp` (a restricted system dir); the omnigent **runner** gives a
  child a writable temp instead. So a Windows subprocess you repro by hand can fail in
  ways the real session never hits — **re-run any "this binary is broken on Windows"
  finding under a real user `%TEMP%`** (`C:\Users\<you>\AppData\Local\Temp`) before
  believing it. This exact trap once got opencode's TUI written off as upstream-broken.
  The `diagnose-windows-native-harness` skill (`.claude/skills/`) automates the check
  and the rest of the Windows native-harness failure chain.
- **The auto-mode classifier blocks** direct pushes to `main` and edits to
  `.github/MAINTAINER` / CI approval gates without explicit user authorization. Hand the
  user the exact command instead.
- **OpenCode on Windows: full TUI, but two env essentials.** Both run behind `IS_WINDOWS`
  in the opencode env builders. (1) `opencode serve` needs `SystemRoot` or it fast-fails
  with `0xC0000409` — `filtered_server_env` adds `WINDOWS_ENV_PASSTHROUGH`. (2) The attach
  TUI needs a **writable `TEMP`/`TMP`**: bun extracts opencode's embedded OpenTUI DLL into
  `%TEMP%` and `dlopen`s it; a restricted temp like `C:\WINDOWS\temp` fails with `error 126`
  ("module not found" — the `B:/~BUN/root/...` in the message is bun's *virtual* path, not
  the real load target). Both `filtered_server_env` and `opencode_terminal_env` pin
  `<bridge_dir>/tmp` via `_opencode_windows_tempdir`. The TUI is **not** an upstream-broken
  dead end — that was a misdiagnosis from testing under a bad sandbox `%TEMP%`.
- **Goose on Windows: a TUI-mirror harness like Claude, plus two platform branches.**
  Goose ships a real Rust `goose.exe` (no `.CMD` shim, no `%TEMP%` extraction), is
  provider-agnostic, and owns its own auth — the user runs `goose configure` once (e.g.
  Ollama Cloud → host `https://ollama.com` + key). Omnigent writes **no** Goose config.
  (1) **Seeding gap (platform-independent).** Goose is a first-class native agent in
  `native_coding_agents.py` but was never seeded into the agent DB at startup, so it was
  absent from the new-session picker on *every* platform. `_ensure_default_goose_agent` in
  `omnigent/server/app.py` now seeds it (mirrors cursor). Hermes still has this gap.
  (2) **Injection.** Like Claude, web-chat turns reach the TUI by injection, not a server
  transport. On Windows `goose_native_bridge.inject_user_message`/`inject_interrupt` route
  through the runner's loopback injection server (reusing the single client in
  `claude_native_bridge`) instead of tmux `send-keys`; `_auto_create_goose_terminal`
  stands up `ensure_injection_server` and advertises `host/port/token` in `tmux.json`.
  (3) **Transcript store path.** The forwarder tails Goose's SQLite `sessions.db`. On
  Windows that lives at `%APPDATA%\Block\goose\data\sessions\sessions.db` (etcetera
  `Block`/`goose` strategy → Roaming AppData), not the POSIX `~/.local/share/goose/...`;
  `default_sessions_db` branches on `IS_WINDOWS` (override: `GOOSE_SESSIONS_DB` — confirm
  with `goose info -v`). (4) **Approval mirror is deferred on Windows.** Goose runs
  `GOOSE_MODE=smart_approve`; approve/deny tool calls **in the embedded terminal view**.
  Web approval cards need a raw-keystroke injection kind (the cliclack selector is driven
  with arrow keys) — a follow-up, see `goose_native_permissions.py`.

## Pointers — where things live

| Concern | File |
| --- | --- |
| ConPTY terminal instance (the tmux replacement) | `omnigent/inner/terminal_windows.py` |
| Windows branch of terminal creation | `omnigent/inner/terminal.py` (`create_terminal_instance`, `IS_WINDOWS`) |
| Cross-process injection + Windows bridge fns | `omnigent/claude_native_bridge.py` (`IS_WINDOWS` branches) |
| Transcript forwarder + surrogate scrub | `omnigent/claude_native_forwarder.py` (`_json_safe`, `forward_claude_transcript_to_session`) |
| ConPTY → WebSocket bridge | `omnigent/terminals/ws_bridge.py` (`bridge_conpty_to_websocket`) |
| UTF-8 stdin in hooks | `omnigent/claude_native_{hook,status,message_display_hook}.py` |
| Frontend terminal client (pin/letterbox) | `ap-web/src/components/blocks/TerminalSession.ts` |
| Windows unit tests | `tests/inner/test_terminal_windows.py` |
| OpenCode Windows env (SystemRoot + writable TEMP) | `omnigent/opencode_native_app_server.py` (`filtered_server_env`, `opencode_terminal_env`, `_opencode_windows_tempdir`) |
| OpenCode attach-TUI launch | `omnigent/runner/app.py` (`_auto_create_opencode_terminal`) |
| Goose picker seeding | `omnigent/server/app.py` (`_ensure_default_goose_agent`, `_build_goose_native_bundle`) |
| Goose injection (Windows branch) + injection advertise | `omnigent/goose_native_bridge.py` (`inject_user_message`, `inject_interrupt`, `write_tmux_target`) |
| Goose terminal auto-create + injection server | `omnigent/runner/app.py` (`_auto_create_goose_terminal`) |
| Goose transcript forwarder + Windows SQLite path | `omnigent/goose_native_forwarder.py` (`default_sessions_db`) |
| Goose Windows unit tests | `tests/test_goose_native_bridge.py`, `tests/test_goose_native_forwarder.py` |

## Verifying a change

1. Backend: `uvx ruff@0.15.16 check <files>` + `uvx ruff@0.15.16 format --check <files>`.
2. Logic tests: `uv run --extra dev python -m pytest tests/inner/test_terminal_windows.py`.
3. Frontend: `cd ap-web && npx tsc --noEmit && npx vitest run src/components/blocks/TerminalSession.test.ts`, then `npm run build`.
4. Restart the runner/server, hard-refresh the browser, dogfood the actual flow.
5. Runner logs live at `~/.omnigent/logs/host-runner/runner-*.log` — both the mojibake
   and doubling bugs leave fingerprints there (`UnicodeEncodeError: '\udcXX'` for the
   old crash path).
