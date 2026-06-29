#!/usr/bin/env python
"""Spawn a Windows CLI/TUI under a real ConPTY and capture what it renders.

This is the load-bearing probe for diagnosing why a native harness
(opencode / codex / cursor / qwen / claude) misbehaves on Windows *inside*
omnigent's ConPTY backend — without needing the full runner stack or a
browser. It mirrors what ``WindowsTerminalInstance.launch`` actually does:

  - resolves the command via ``_resolve_windows_argv`` (wraps a ``.CMD``/``.bat``
    shim through ``cmd.exe`` — ``winpty``/``CreateProcess`` can't spawn those
    directly, only ``name.exe``),
  - spawns it in a real pseudo-console (``winpty.PtyProcess``) so a TUI that
    *requires a TTY* (``opencode --mini``, ``opencode attach``) actually starts,
  - lets you override ``%TEMP%``/env, because the #1 misdiagnosis is testing
    under the Claude Code tool sandbox's ``TEMP=C:\\WINDOWS\\temp`` (a restricted
    dir) instead of the writable per-session temp the runner gives the child.

Why this matters: a bare ``subprocess`` run with ``stdin=/dev/null`` makes a TUI
fail with a *generic* "requires a TTY" / "Effect.tryPromise" error that hides the
real cause. Under a real ConPTY the binary gets far enough to surface the actual
failure (e.g. OpenTUI ``error 126``, a missing DLL, an env fast-fail).

Run from the repo root with the project interpreter so ``omnigent`` and
``winpty`` import:

    .venv/Scripts/python.exe .claude/skills/diagnose-windows-native-harness/pty_probe.py \
        --temp "C:\\Users\\<you>\\AppData\\Local\\Temp" \
        -- opencode --mini

    # attach to a live session server (read url/secret from the bridge state):
    ... pty_probe.py --temp <good-temp> --env OPENCODE_SERVER_PASSWORD=<secret> \
        --env XDG_DATA_HOME=<bridge>/xdg-data --env XDG_CONFIG_HOME=<bridge>/xdg-config \
        -- opencode attach http://127.0.0.1:<port> --dir <workspace> --session <ses_id>

Compare two ``%TEMP%`` values to prove an env-sensitivity bug: run once with the
real user temp (expect the TUI to paint) and once with ``C:\\WINDOWS\\temp``
(expect the failure). If they differ, it's an env bug in *our* launch path, not
an upstream-broken binary.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time

_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_2CHAR = re.compile(r"\x1b[()][AB0]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_ansi(text: str) -> str:
    """Return *text* with the ANSI control noise removed for readability."""
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_2CHAR.sub("", text)
    return _CTRL.sub("", text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--temp",
        help=r"Value to force for TEMP and TMP (e.g. a writable user temp). "
        r"Omit to inherit. Use C:\WINDOWS\temp to reproduce the bad-sandbox failure.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra env var to set on the child (repeatable).",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for the child.")
    parser.add_argument(
        "--seconds", type=float, default=10.0, help="How long to read before giving up."
    )
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="The command and args, after a literal `--`.",
    )
    args = parser.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("provide a command after `--`, e.g. `-- opencode --mini`")

    if os.name != "nt":
        print("This probe only makes sense on native Windows (os.name == 'nt').", file=sys.stderr)
        return 2

    from winpty import PtyProcess

    from omnigent.inner.terminal_windows import _resolve_windows_argv

    env = os.environ.copy()
    if args.temp:
        env["TEMP"] = env["TMP"] = args.temp
    for pair in args.env:
        key, _, value = pair.partition("=")
        env[key] = value
    env.setdefault("PYTHONUTF8", "1")

    argv = _resolve_windows_argv(cmd[0], cmd[1:])
    print(f"TEMP   = {env.get('TEMP')!r}")
    print(f"argv   = {argv}")
    print(f"cwd    = {args.cwd}")
    print("-" * 60)

    try:
        proc = PtyProcess.spawn(argv, dimensions=(args.rows, args.cols), env=env, cwd=args.cwd)
    except Exception as exc:  # noqa: BLE001 - surface spawn failures verbatim.
        print(f"SPAWN FAILED: {exc!r}")
        return 1

    buf = ""
    start = time.time()
    while time.time() - start < args.seconds:
        try:
            data = proc.read(8192)
        except EOFError:
            break
        if data:
            buf += data
        else:
            time.sleep(0.15)
        if not proc.isalive():
            break

    clean = _strip_ansi(buf).strip()
    print(f"alive={proc.isalive()}  exitstatus={proc.exitstatus}  bytes={len(buf)}")
    print("-" * 60)
    print(clean[:3000] if clean else "(no text — process produced no readable output)")
    with contextlib.suppress(Exception):  # best-effort cleanup.
        proc.terminate(force=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
