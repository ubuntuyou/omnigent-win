# CLAUDE.md — omnigent-win

## What this is

`omnigent-win` is a **Windows-native fork** of [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent).
Upstream's native terminal harnesses run the agent CLI inside **tmux**, which is
POSIX-only, so on Windows the terminal layer raised *"not supported."* This fork
adds a parallel **ConPTY backend** (via [`pywinpty`](https://pypi.org/project/pywinpty/))
so the **Claude Code** (`claude`) native harness runs on **Windows 11**, streamed to
the Omnigent web UI.

The change is **purely additive and `IS_WINDOWS`-guarded** — the POSIX/tmux path is
untouched — and is scoped to the Claude Code harness. See `ARCHITECTURE.md` for the
data-flow detail and `architecture.mmd` for the diagram.

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
- **The auto-mode classifier blocks** direct pushes to `main` and edits to
  `.github/MAINTAINER` / CI approval gates without explicit user authorization. Hand the
  user the exact command instead.
- **OpenCode runs chat-only on Windows.** `opencode serve` (chat over HTTP/SSE) works,
  but `opencode attach` (the native TUI) can't load its OpenTUI render DLL on Windows
  (upstream Bun limitation, `error 126`), so the terminal view is replaced by a
  placeholder banner. Two Windows-only requirements: (1) `opencode serve` needs
  `SystemRoot` in its env or it fast-fails with `0xC0000409` — `filtered_server_env`
  adds `WINDOWS_ENV_PASSTHROUGH` behind `IS_WINDOWS`; (2) `_auto_create_opencode_terminal`
  skips the doomed `attach` on `IS_WINDOWS`. Don't "fix" the missing TUI — it's upstream.

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
| OpenCode serve env (SystemRoot passthrough) | `omnigent/opencode_native_app_server.py` (`filtered_server_env`, `IS_WINDOWS`) |
| OpenCode chat-only degrade (skip `attach`) | `omnigent/runner/app.py` (`_auto_create_opencode_terminal`, `_OPENCODE_WINDOWS_CHAT_ONLY_BANNER`) |

## Verifying a change

1. Backend: `uvx ruff@0.15.16 check <files>` + `uvx ruff@0.15.16 format --check <files>`.
2. Logic tests: `uv run --extra dev python -m pytest tests/inner/test_terminal_windows.py`.
3. Frontend: `cd ap-web && npx tsc --noEmit && npx vitest run src/components/blocks/TerminalSession.test.ts`, then `npm run build`.
4. Restart the runner/server, hard-refresh the browser, dogfood the actual flow.
5. Runner logs live at `~/.omnigent/logs/host-runner/runner-*.log` — both the mojibake
   and doubling bugs leave fingerprints there (`UnicodeEncodeError: '\udcXX'` for the
   old crash path).
