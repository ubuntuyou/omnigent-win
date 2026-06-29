# CLAUDE.md — omnigent-win

## What this is

`omnigent-win` is a **Windows-native fork** of [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent).
Upstream's native terminal harnesses run the agent CLI inside **tmux**, which is
POSIX-only, so on Windows the terminal layer raised *"not supported."* This fork
adds a parallel **ConPTY backend** (via [`pywinpty`](https://pypi.org/project/pywinpty/))
so native agent harnesses run on **Windows 11**, streamed to the Omnigent web UI.

**Working native harnesses on Windows:** **Claude Code** (`claude`), **OpenCode**
(`opencode`), **Pi** (`pi`), and **Codex** (`codex`). The remaining tmux-only ones
(cursor / goose / qwen / kimi / hermes / kiro / antigravity) still need the port —
the `diagnose-windows-native-harness` skill walks the recurring failure chain.

Every change is **purely additive and `IS_WINDOWS`-guarded** — the POSIX/tmux path is
untouched. See `ARCHITECTURE.md` for the data-flow detail and `architecture.mmd` for
the diagram.

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
- **Codex on Windows runs via an OpenAI-compatible provider, not an OpenAI login.** Codex
  needs auth omnigent recognizes; with no OpenAI account it routes through an `omnigent
  setup` `key` provider (e.g. **Ollama Cloud**, `~/.omnigent/config.yaml` → `providers:` →
  `kind: key`, `openai:` family, `base_url: https://ollama.com/v1`, `wire_api: responses`).
  Four things make this work, all `IS_WINDOWS`-guarded & additive:
  (1) **Wire:** codex ≥0.137 only speaks `responses` (it hard-fails on `wire_api="chat"`);
  Ollama Cloud **does** implement `/v1/responses`, so they're compatible — verified, not
  assumed. (2) **Auth:** the POSIX override uses `auth={command="sh",args=["-c","printf …"]}`
  — there is no `sh`/`printf` on a Windows codex child, so `_provider_codex_config_overrides`
  emits an inline `http_headers={Authorization="Bearer …"}` for a static key instead.
  (3) **Env:** `_clean_codex_env` adds `WINDOWS_ENV_PASSTHROUGH` (`SystemRoot`) or the
  `app-server` child fast-fails (`0xC0000409`). (4) **Readiness:** the gate
  (`_codex_auth_unavailable_reason`) only checks `auth.json`; on Windows it now also accepts
  a configured routable provider (`_codex_configured_provider_routes`) so the picker unblocks.
  Custom provider IDs **cannot** be named `ollama` (reserved built-in = local Ollama) — omnigent
  uses `omnigent_provider`. Gotcha when repro'ing by hand: `codex exec` **hangs reading stdin**
  unless stdin is closed — the real path spawns with `stdin=DEVNULL`, so it's a test artifact,
  not a bug (don't write codex off as broken from a manual hang).
- **Codex CLI version is pinned to `0.139.0` — do NOT install latest.** omnigent's codex-native
  transport spawns `codex app-server --listen <ws://…>` (JSON-RPC over a loopback WebSocket).
  codex **0.142** removed the `--listen` flag (app-server went stdio-only + `daemon`/`proxy`
  subcommands), so the app-server exits early with a clap usage error → the runner masks it as
  the generic `native_terminal_start_failed` *"not supported on Windows"* (a **lie** — the ConPTY
  terminal started fine; the real cause is `RuntimeError: Codex app-server exited early | Usage:
  codex …` in the runner log). This is **platform-independent** version drift, not a Windows bug.
  Pin: `npm install -g @openai/codex@0.139.0` (the `.github/ci-deps/package.json` pin omnigent is
  validated against). Until omnigent's transport is ported to codex's new stdio app-server, stay
  on 0.139.x.

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
| Codex spawn env (SystemRoot) + Windows http_headers auth | `omnigent/inner/codex_executor.py` (`_clean_codex_env`, `_provider_codex_config_overrides`) |
| Codex provider routing (static-key bearer threading) | `omnigent/codex_native_app_server.py` (`_codex_provider_launch`, `resolve_native_codex_launch`) |
| Codex readiness gate (configured-provider on Windows) | `omnigent/codex_native.py` (`_codex_auth_unavailable_reason`, `_codex_configured_provider_routes`) |
| Codex provider config (Ollama Cloud) | `~/.omnigent/config.yaml` `providers:` (`kind: key`, `openai`, `wire_api: responses`) |
| Codex Windows unit tests | `tests/test_native_codex_provider.py`, `tests/test_codex_native.py` |

## Verifying a change

1. Backend: `uvx ruff@0.15.16 check <files>` + `uvx ruff@0.15.16 format --check <files>`.
2. Logic tests: `uv run --extra dev python -m pytest tests/inner/test_terminal_windows.py`.
3. Frontend: `cd ap-web && npx tsc --noEmit && npx vitest run src/components/blocks/TerminalSession.test.ts`, then `npm run build`.
4. Restart the runner/server, hard-refresh the browser, dogfood the actual flow.
5. Runner logs live at `~/.omnigent/logs/host-runner/runner-*.log` — both the mojibake
   and doubling bugs leave fingerprints there (`UnicodeEncodeError: '\udcXX'` for the
   old crash path).
