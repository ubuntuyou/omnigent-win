---
name: diagnose-windows-native-harness
description: Diagnose why an Omnigent native harness (opencode / codex / cursor / goose / qwen / claude) fails to launch its server or render its TUI on Windows. Walks the recurring failure chain — stale readiness, a serve/subprocess fast-fail from a stripped env, and a TUI that won't render because of a missing DLL or a bad %TEMP% — and ships a ConPTY PTY-probe (pty_probe.py) to reproduce the TUI launch outside the runner. Load when porting a native harness to Windows, when a Windows session shows "not configured" / "not supported on Windows" / a dead terminal panel, or when a Windows subprocess crashes with 0xC0000409 or DLL error 126. The remaining tmux-only harnesses (codex/cursor/goose/qwen) will each need this.
---

# Diagnosing a native harness on Windows

When a native harness works on Linux/macOS but fails on Windows in omnigent-win,
the cause is almost always one of **three env/lifecycle issues**, each of which
*masks* the next. Work them in order — fixing #1 reveals #2, fixing #2 reveals #3.
This is the exact loop that ported opencode (see CLAUDE.md "OpenCode on Windows").

> **Invariant (do not break):** every fix is additive and `IS_WINDOWS`-guarded.
> Never change the POSIX/tmux path. The Windows branch lives in the harness's own
> env builders / launch site.

---

## The #1 trap: the tool sandbox lies about `%TEMP%`

Before anything else, know this: the Claude Code **Bash and PowerShell tools run
with `TEMP=C:\WINDOWS\temp`** — a restricted system dir. The omnigent **runner**
gives a child a *different*, writable temp. So a subprocess you repro by hand can
fail in ways the real session never hits (and vice-versa). **Any "this binary is
broken on Windows" conclusion reached under tool-repro is suspect until you've
re-run it with a real user `%TEMP%`** (e.g. `C:\Users\<you>\AppData\Local\Temp`).
A whole investigation was once wasted writing off opencode's TUI as
upstream-broken when the only fault was the sandbox temp. `pty_probe.py --temp`
exists precisely to control for this.

---

## Step 1 — Readiness: is it stale, or genuinely missing?

Symptom: the picker says the harness "needs omnigent setup" / "not configured."

Readiness is `harness_cli_installed(<KEY>)` → `shutil.which(<binary>)`, sent in the
host daemon's hello frame and **recomputed on every (re)connect**
(`omnigent/host/connect.py`). If you installed the CLI *after* the daemon
connected, the snapshot is stale.

```bash
# Is it actually on PATH in the server's own interpreter?
uv run python -c "from omnigent.onboarding.harness_readiness import configured_harness_map as m; print(m().get('<harness>-native'))"
```

- `True` → stale snapshot. **Restart the server only** (not the host daemon): the
  daemon reconnects on its backoff loop and re-sends fresh readiness. This does
  **not** kill descendant sessions (a Claude session running *inside* the runner
  survives), because the server is a separate process from the daemon. Bounce it
  by re-running `start-omnigent.ps1` (frees the port, relaunches).
- `False`/`None` → the binary really isn't found; install it / fix PATH.

## Step 2 — Serve / subprocess fast-fails (exit `0xC0000409`)

Symptom: the runner log shows `<harness> serve exited early with code 3221226505`,
surfaced to the UI as the generic `native_terminal_start_failed` ("not supported
on Windows") message — which is a **mask**, not the cause. The real cause is in
the runner log with `exc_info=True`.

`3221226505` = `0xC0000409` = a Windows fast-fail. For a Bun/Node binary it almost
always means the launch **env is missing `SystemRoot`** (Winsock loads its
providers from `%SystemRoot%\system32\mswsock.dll`; without it the child dies
before `main`). Harnesses build a *filtered* env for the subprocess (an allowlist)
and the POSIX allowlist drops the Windows essentials.

Reproduce with the **real** filtered env (not ambient), then confirm the fix:

```bash
uv run python - <<'PY'
import subprocess, socket, tempfile
from pathlib import Path
from omnigent.opencode_native_app_server import filtered_server_env, find_opencode_cli, build_opencode_serve_args
bd = Path(tempfile.mkdtemp()); (bd/'xdg-data').mkdir(); (bd/'xdg-config').mkdir()
env = filtered_server_env(bridge_dir=bd, auth_secret='pw')
print('SystemRoot present:', any(k.upper()=='SYSTEMROOT' for k in env))
s=socket.socket(); s.bind(('127.0.0.1',0)); port=s.getsockname()[1]; s.close()
p=subprocess.Popen([find_opencode_cli(), *build_opencode_serve_args(hostname='127.0.0.1', port=port)],
                   cwd=str(bd), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import time; time.sleep(4)
print('exit (None = booted OK):', p.poll())   # 3221226505 = the crash
p.terminate()
PY
```

**Fix:** add `WINDOWS_ENV_PASSTHROUGH` (from `omnigent/_platform.py`:
`SYSTEMROOT`, `APPDATA`, …) to the harness's filtered-env allowlist, gated on
`IS_WINDOWS`. The tuple is empty on POSIX, so POSIX env is byte-for-byte
unchanged. (For opencode this is in `filtered_server_env`.)

## Step 3 — TUI won't render (DLL `error 126`, dead terminal panel)

Symptom: chat works but the terminal view is blank/dead; the `attach` process
exits immediately. A bare `subprocess` run with no TTY gives a useless generic
error ("requires a TTY", "Effect.tryPromise") — you must run it in a **real
ConPTY** to surface the truth. Use the bundled probe:

```bash
# Find the live session's URL + secret in the bridge dir:
#   ~/.omnigent/<harness>-native/<hash>/state.json  (server_base_url, opencode_session_id)
#   ~/.omnigent/<harness>-native/<hash>/auth.secret

# A) GOOD temp — expect the TUI to paint:
.venv/Scripts/python.exe .claude/skills/diagnose-windows-native-harness/pty_probe.py \
    --temp "C:\\Users\\<you>\\AppData\\Local\\Temp" -- opencode --mini

# B) BAD temp — expect the failure (proves it's env, not the binary):
.venv/Scripts/python.exe .claude/skills/diagnose-windows-native-harness/pty_probe.py \
    --temp "C:\\WINDOWS\\temp" -- opencode --mini
```

`error 126` = `ERROR_MOD_NOT_FOUND`. For a Bun-compiled binary it's usually the
**embedded native lib (e.g. OpenTUI) that bun extracts into `%TEMP%` and
`dlopen`s** — the `B:/~BUN/root/...` path in the message is bun's *virtual* source
path, not the real load target. A restricted/odd `%TEMP%` blocks the extraction →
126. (Other 126 causes: a genuinely missing dependent DLL — check the VC++ runtime
before blaming temp.)

**Fix:** pin a guaranteed-writable, per-session `TEMP`/`TMP` for the child, gated
on `IS_WINDOWS`. For opencode: `_opencode_windows_tempdir(bridge_dir)` →
`<bridge_dir>/tmp`, injected by both `filtered_server_env` (serve) and
`opencode_terminal_env` (attach). The terminal launch overlays `spec.env` over the
inherited env (`WindowsTerminalInstance.launch`), so the pinned `TEMP` overrides
whatever bad value the runner inherited.

---

## Windows exit-code / error cheat sheet

| Code | Meaning | Usual cause in a harness child |
| --- | --- | --- |
| `3221226505` / `0xC0000409` | `STATUS_STACK_BUFFER_OVERRUN` (fast-fail) | Stripped launch env missing `SystemRoot` (Step 2) |
| `error 126` / `ERROR_MOD_NOT_FOUND` | DLL or a dependency not found | Native lib extraction blocked by bad `%TEMP%`; or missing VC++ runtime (Step 3) |
| `error 225` | Operation blocked | AV/Defender quarantine of the extracted lib |
| generic "requires a TTY" / "Effect.tryPromise" | TUI started without a pseudo-console | You ran it via plain `subprocess`; use `pty_probe.py` |

## How the probe works (and why a ConPTY is required)

`pty_probe.py` resolves the command via `_resolve_windows_argv` (wraps a
`.CMD`/`.bat` shim through `cmd.exe` — winpty can't spawn those directly, only
`name.exe`), spawns it under `winpty.PtyProcess` (a real pseudo-console), lets you
override `%TEMP%`/env, reads for a few seconds, and prints the ANSI-stripped frame
plus `alive`/`exitstatus`. Run it from the repo root with `.venv/Scripts/python.exe`
so `omnigent` and `winpty` import.

## Where the load-bearing pieces live

| Concern | File |
| --- | --- |
| Readiness map (hello frame, per-reconnect) | `omnigent/host/connect.py`; `omnigent/onboarding/harness_readiness.py` |
| Windows env essentials allowlist | `omnigent/_platform.py` (`WINDOWS_ENV_PASSTHROUGH`, `IS_WINDOWS`) |
| OpenCode serve/attach env (template fix) | `omnigent/opencode_native_app_server.py` (`filtered_server_env`, `opencode_terminal_env`, `_opencode_windows_tempdir`) |
| Attach-TUI launch site | `omnigent/runner/app.py` (`_auto_create_opencode_terminal`) |
| ConPTY backend + argv resolver | `omnigent/inner/terminal_windows.py` (`WindowsTerminalInstance.launch`, `_resolve_windows_argv`) |
| Generic Windows error mask | `omnigent/runner/app.py` (`_native_terminal_start_error`, `IS_WINDOWS`) |
