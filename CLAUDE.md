# CLAUDE.md — omnigent-win

## What this is

`omnigent-win` is a **Windows-native fork** of [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent).
Upstream's native terminal harnesses run the agent CLI inside **tmux**, which is
POSIX-only, so on Windows the terminal layer raised *"not supported."* This fork
adds a parallel **ConPTY backend** (via [`pywinpty`](https://pypi.org/project/pywinpty/))
so native agent harnesses run on **Windows 11**, streamed to the Omnigent web UI.

**Working native harnesses on Windows:** **Claude Code** (`claude`), **OpenCode**
(`opencode`), **Pi** (`pi`), **Codex** (`codex`), **Goose** (`goose`), and **Qwen Code**
(`qwen`). The remaining tmux-only ones (cursor / kimi / hermes / kiro / antigravity) still
need the port — the `diagnose-windows-native-harness` skill walks the recurring failure
chain. (Pi needed no port — its injection is file/RPC-based, not tmux; see the Pi gotcha
below.)

Every change is **purely additive and `IS_WINDOWS`-guarded** — the POSIX/tmux path is
untouched. See `ARCHITECTURE.md` for the data-flow detail and `architecture.mmd` for
the diagram.

## The fork relationship (important)

- `ubuntuyou/omnigent-win` is **NOT a GitHub fork** (`isFork: false`). It was seeded by
  pushing a clone, so it shares full git history with upstream but the web "Sync fork"
  button does nothing.
- Sync via the `upstream` remote with a merge:
  ```
  git checkout main && git fetch upstream && git merge upstream/main
  ```
  The documented `git push origin main` is **classifier-blocked** (direct main push), so in
  practice commit the merge on a branch and open a sync PR. Merge it with **`--merge`, not
  squash**, so `upstream/main` stays a recorded parent and the *next* sync doesn't try to
  re-merge (and re-conflict) the same commits. Two clashes recur on essentially every sync:
  - **`io.StringIO` stdin mocks vs our `.buffer` hooks.** Upstream's hook tests mock stdin
    with `io.StringIO`, which has **no `.buffer`** — but our Claude hooks read
    `sys.stdin.buffer` (invariant #2, `_read_stdin_utf8`), so those mocks `AttributeError`.
    After each merge, grep the merged tree for `io.StringIO` feeding `sys.stdin` in the
    **claude-native** hook tests and convert to `fake_stdin` (`tests/native_hook_helpers.py`).
    They can sneak in via a *clean* auto-merge, not just a conflict, so scan don't assume.
    Leave codex/cursor/kimi alone — those hooks read `sys.stdin.read()` (plain text).
  - **`CLAUDE.md` add/add conflict.** Upstream made its `CLAUDE.md` a symlink to `AGENTS.md`;
    always keep **ours** (`git checkout --ours CLAUDE.md`). Their `AGENTS.md` arrives as a
    harmless new file.
- `gh` is **not on PATH**; it lives at
  `C:\Users\Joe\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe`.
- Because the `upstream` remote exists, `gh pr create` defaults its base repo to
  `omnigent-ai/omnigent` and fails. **Always** pass
  `--repo ubuntuyou/omnigent-win --base main --head <branch>` for internal PRs.
- CI inherits upstream's gates. **Maintainer Approval** passes because `ubuntuyou` is in
  `.github/MAINTAINER`. The upstream Linux E2E/pytest shards don't exercise the Windows
  ConPTY path, so they're admin-merged past once Maintainer Approval + Pre-commit are green.
  The **`E2E UI Required`** gate also fails in this fork by design: its
  `e2e-ui-required/check.sh` calls an LLM judge over a gateway, but the fork has no
  `OPENAI_BASE_URL`/`OPENAI_API_KEY` secret, so `curl` aborts with *"No host part in the
  URL"*. It's **deterministic — re-running won't fix it**. The real `E2E UI Tests (shard
  N/3)` jobs still pass, so merge past it with `gh pr merge --admin` (needs explicit user
  authorization) or have a maintainer add the `skip-e2e-ui-test` label (the gate's own
  suggested escape hatch).

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
  until you `cd web && npm run build`, then hard-refresh the browser. A stale bundle
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
- **Codex must be spawned via the vendored `codex.exe`, NOT the npm `codex.CMD` shim.** `shutil.which("codex")`
  on Windows returns `codex.CMD`, a batch wrapper that relaunches node with `%*` — re-parsing argv through
  `cmd.exe`. That **mangles `-c` override values containing embedded quotes + spaces**: the app-server passes
  the provider block (`model_providers.X={name="Omnigent Provider",…}`) and the MCP `args=["-m","omnigent…"]`
  as `-c` fragments, so codex sees a split token (`Provider",base_url=…`) → `error: unexpected argument` →
  exits with the **top-level** usage block (`Usage: codex [OPTIONS] [PROMPT]`, *not* the app-server usage) →
  masked by the runner as `native_terminal_start_failed` *"not supported on Windows"*. `_find_codex_cli`
  (`IS_WINDOWS`-guarded) resolves the real exe under
  `…/node_modules/@openai/codex/node_modules/@openai/codex-win32-*/**/codex.exe`; spawning it directly
  bypasses the shim's re-parse. **Repro trap:** testing with the vendored `codex.exe` path by hand passes
  (no shim), so a manual smoke test gives a false green — the bug only appears through `which`→`.CMD`. This is
  separate from the 0.139 pin: both had to be fixed for codex-native to launch.
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
- **Pi on Windows needs no dedicated port.** pi-native injects web turns through Pi's own
  file/RPC extension (the `omnigent_pi_native_extension.js` channel), **not** tmux
  `send-keys` — so once the harness-agnostic ConPTY terminal exists, pi-native runs on
  Windows unchanged. There is no `pi_native_bridge.py` `IS_WINDOWS` branch to add, and no
  Pi-specific Windows commit; "Pi works on Windows" is true precisely because the injection
  path was never tmux-coupled. (Contrast goose/cursor/claude, which simulate keystrokes and
  therefore each needed the injection-server branch.)
- **Qwen Code on Windows: a TUI-mirror harness like Claude/Goose, plus a resume-slug
  gotcha.** Qwen ships a real npm-installed CLI (`qwen`, behind the same `.CMD`-shim shape
  as Codex — spawn resolution matters, see the Codex shim gotcha) and is provider-agnostic
  via `~/.qwen/.env` (`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`), written by
  omnigent, not qwen's own login flow. **Model must carry the provider's full suffix**
  (e.g. `glm-5.2:cloud` for Ollama Cloud) — the bare model id from `config.yaml`'s
  `models.default` fails with "hard limit: 0" because qwen doesn't know the bare id's
  context window. Injection (`inject_user_message`/`inject_interrupt`) follows the same
  `IS_WINDOWS` injection-server branch as Goose; `kill_session` is an intentional no-op on
  Windows (unit-tested: `test_kill_session_windows_is_a_noop`) — the ConPTY teardown
  happens separately via `_teardown_session_terminals` in `runner/app.py`, same pattern as
  every other native harness. **Resume uses qwen's own internal session UUID**, not the
  omnigent conversation id — `omnigent` maps conversation -> qwen UUID via
  `qwen_session_id_for_conversation` and passes `--resume <uuid>`. qwen keys its on-disk
  chat history by a project slug (`~/.qwen/projects/<slug>/chats/`) computed from the
  *lowercased* realpath of the workspace with non-alphanumerics replaced by `-` (verified
  against a live v0.19.4 session: workspace `C:\` -> qwen creates `c--`, not `C--`) —
  `_qwen_project_slug()` in `qwen_native_bridge.py` matches this exactly (case included) so
  `--resume` can find qwen's history; NTFS is case-insensitive so a case mismatch wouldn't
  have broken it in practice, but it's the correct byte-for-byte match now regardless.
  **Interrupted turns show no "cancelled" badge** in the chat view — this is inherent to
  qwen's own event format, not a bug: qwen's `stop_reason` is `null` on every assistant
  message whether interrupted or not, with no other cancel signal in its event stream or
  on-disk chat log (contrast Claude, which writes literal `"[Request interrupted by user]"`
  into its own transcript that the frontend pattern-matches; contrast Codex, which sets a
  real `status: "interrupted"` field the forwarder reads into the generic
  `MessageData.interrupted` bool). Fixing this for qwen would need `inject_interrupt` to
  write a side-channel sentinel the forwarder correlates against the in-flight response — a
  real design change, not attempted here.
- **Clear Claude's input box with backspaces (`\x7f`), never C-k/C-u.** The message-path
  clear in `terminal_windows.py` `_InjectionServer._dispatch` sends `"\x7f" * _DRAFT_CLEAR_BACKSPACES`
  (600) before the bracketed paste. After an Escape-cancel (the web Stop button) Claude
  re-populates the input box with the previous prompt for re-editing; without a clear the new
  message pastes onto that stale draft (`oldtailnewmsg`, no separator).
  - **Why backspace, not the kill keys.** Claude's TUI scopes **C-k (`\x0b`) and C-u (`\x15`)
    to the current *visual row***, so a wrapped multi-row draft needs one press per row — and
    once the box is empty the *excess* `\x0b` stop being consumed as a keystroke and **insert
    literally**, rendering as `□` box glyphs. Shipping `"\x01" + "\x0b"*200` once put a solid
    row of ~200 boxes on **every** web message (the clear runs before every paste, even on an
    empty box). The leading `\x0b` are whitespace to Python so the *content* looked clean
    server-side, but the chat view renders each as a box (the transcript held 1401 ``).
    Backspace is **char-scoped** (crosses wrapped rows) and is a **true no-op on an empty box**
    (you can't delete what isn't there), so it fully clears a multi-row draft and can *never*
    insert anything. The injection write queue preserves order, so the backspaces drain before
    the paste — no interleaving into the new message.
  - **Bound:** 600 backspaces covers a long prompt; a *longer* cancelled draft only partially
    clears (concat, never boxes). Cursor sits at the draft end after re-populate, so plain
    backspace (no C-e/C-a prefix) clears it — verified. C-u×40 also clears cleanly, but
    backspace's empty-box safety is **structural**, not version-dependent, so it's the pick.
  - The **interrupt path stays a lone Escape** on purpose — the re-populate is Claude's
    intended re-edit UX and clearing there is timing-fragile.
  - **How it was verified (and a trap avoided).** Ground truth is Claude's **transcript JSONL**
    (the submitted string, where `\x0b` survives as ``) — *not* the terminal echo (hides
    control chars) and *not* a pyte screen (pyte reads `\x0b` as cursor-down → false green; that
    misdiagnosis is what shipped the box bug). The probe (`scratchpad/clearbox_v3.py`) drives a
    real `claude` via `WindowsTerminalInstance` and reads the transcript. **Gotcha:** a child
    `claude` inherits `CLAUDE_CODE_SESSION_ID` / `CLAUDE_CODE_CHILD_SESSION` from this session
    and then writes **no** transcript (it thinks it's a child turn) — the probe must `env_unset`
    both so it's a fresh top-level session under `~/.claude/projects/<cwd-key>/`.
  - The **POSIX twin** (`claude_native_bridge.py` `inject_user_message`, C-a/C-k via tmux
    send-keys) has the same row-scoped limitation; left untouched per invariant #1 (validate any
    POSIX fix under WSL2). If ported, use backspace there too — **not** repeated C-k.

## Pointers — where things live

| Concern | File |
| --- | --- |
| ConPTY terminal instance (the tmux replacement) | `omnigent/inner/terminal_windows.py` |
| Windows branch of terminal creation | `omnigent/inner/terminal.py` (`create_terminal_instance`, `IS_WINDOWS`) |
| Cross-process injection + Windows bridge fns | `omnigent/claude_native_bridge.py` (`IS_WINDOWS` branches) |
| Transcript forwarder + surrogate scrub | `omnigent/claude_native_forwarder.py` (`_json_safe`, `forward_claude_transcript_to_session`) |
| ConPTY → WebSocket bridge | `omnigent/terminals/ws_bridge.py` (`bridge_conpty_to_websocket`) |
| UTF-8 stdin in hooks | `omnigent/claude_native_{hook,status,message_display_hook}.py` |
| Frontend terminal client (pin/letterbox) | `web/src/components/blocks/TerminalSession.ts` |
| Windows unit tests | `tests/inner/test_terminal_windows.py` |
| OpenCode Windows env (SystemRoot + writable TEMP) | `omnigent/opencode_native_app_server.py` (`filtered_server_env`, `opencode_terminal_env`, `_opencode_windows_tempdir`) |
| OpenCode attach-TUI launch | `omnigent/runner/app.py` (`_auto_create_opencode_terminal`) |
| Codex spawn env (SystemRoot) + Windows http_headers auth | `omnigent/inner/codex_executor.py` (`_clean_codex_env`, `_provider_codex_config_overrides`, `_find_codex_cli`) |
| Codex provider routing (static-key bearer threading) | `omnigent/codex_native_app_server.py` (`_codex_provider_launch`, `resolve_native_codex_launch`) |
| Codex readiness gate (configured-provider on Windows) | `omnigent/codex_native.py` (`_codex_auth_unavailable_reason`, `_codex_configured_provider_routes`) |
| Codex provider config (Ollama Cloud) | `~/.omnigent/config.yaml` `providers:` (`kind: key`, `openai`, `wire_api: responses`) |
| Codex Windows unit tests | `tests/test_native_codex_provider.py`, `tests/test_codex_native.py`, `tests/inner/test_codex_executor.py` |
| Goose picker seeding | `omnigent/server/app.py` (`_ensure_default_goose_agent`, `_build_goose_native_bundle`) |
| Goose injection (Windows branch) + injection advertise | `omnigent/goose_native_bridge.py` (`inject_user_message`, `inject_interrupt`, `write_tmux_target`) |
| Goose terminal auto-create + injection server | `omnigent/runner/app.py` (`_auto_create_goose_terminal`) |
| Goose transcript forwarder + Windows SQLite path | `omnigent/goose_native_forwarder.py` (`default_sessions_db`) |
| Goose Windows unit tests | `tests/test_goose_native_bridge.py`, `tests/test_goose_native_forwarder.py` |
| Qwen picker readiness + executable/model resolution | `omnigent/qwen_native.py` (`resolve_qwen_executable`, `_configured_qwen_command`) |
| Qwen injection (Windows branch), interrupt, kill | `omnigent/qwen_native_bridge.py` (`inject_interrupt`, `kill_session`, `submit_user_message`) |
| Qwen terminal auto-create + interrupt/stop routes | `omnigent/runner/app.py` (`_auto_create_qwen_terminal`, `_handle_qwen_native_interrupt`, `_handle_qwen_native_stop`) |
| Qwen transcript forwarder | `omnigent/qwen_native_forwarder.py` (`forward_qwen_events_to_session`, `supervise_qwen_forwarder`) |
| Qwen resume slug + session recording | `omnigent/qwen_native_bridge.py` (`_qwen_project_slug`, `qwen_session_id_for_conversation`, `qwen_session_recording_path`) |
| Qwen approval mirror | `omnigent/qwen_native_permissions.py` (`supervise_qwen_approval_mirror`) |
| Qwen Windows unit tests | `tests/test_qwen_native_bridge.py`, `tests/test_qwen_native_forwarder.py`, `tests/test_qwen_native.py` |

## Verifying a change

1. Backend: `uvx ruff@0.15.16 check <files>` + `uvx ruff@0.15.16 format --check <files>`.
2. Logic tests: `uv run --extra dev python -m pytest tests/inner/test_terminal_windows.py`.
3. Frontend: `cd web && npx tsc --noEmit && npx vitest run src/components/blocks/TerminalSession.test.ts`, then `npm run build`.
4. Restart the runner/server, hard-refresh the browser, dogfood the actual flow.
5. Runner logs live at `~/.omnigent/logs/host-runner/runner-*.log` — both the mojibake
   and doubling bugs leave fingerprints there (`UnicodeEncodeError: '\udcXX'` for the
   old crash path).
