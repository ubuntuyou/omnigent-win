#!/usr/bin/env python
"""Verify a keystroke-injection change against GROUND TRUTH: what Claude actually
submitted, read from its transcript JSONL.

Why this exists: the terminal echo HIDES control chars, and a pyte-rendered screen
INTERPRETS them (it reads \\x0b as cursor-down), so both can show a "clean" line while
a control char is really sitting in the submitted string. That false green is exactly
what shipped the box-glyph draft-clear bug. The transcript is the only honest signal:
the submitted text is stored verbatim, where an injected \\x0b survives as U+000B.

What it does: launch a fresh top-level `claude` on the ConPTY backend in a throwaway
cwd, optionally seed a draft (or reproduce the real submit-then-Escape re-populate that
Claude does for the web Stop button), inject your candidate sequence, paste a known
MARKER, submit, then read the last `user` transcript entry and report the submitted
string verbatim with a control-char census.

CRITICAL: a child `claude` inherits CLAUDE_CODE_SESSION_ID / CLAUDE_CODE_CHILD_SESSION
from this session and then writes NO transcript (it thinks it is a child turn). The
probe env_unsets them so it starts a fresh top-level session under
~/.claude/projects/<cwd-key>/<uuid>.jsonl. Without that you get NO-TRANSCRIPT.

Usage (run from repo root so omnigent + winpty import):
  .venv/Scripts/python.exe .claude/skills/diagnose-windows-native-harness/transcript_probe.py \
      --inject '\\x7f' --repeat 600 --repop
  # --inject takes a python-escaped string; --repeat multiplies it; --repop uses the
  # real cancel-then-retype path; --draft TEXT seeds a plain typed draft instead.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import time
from pathlib import Path

# omnigent is imported lazily inside run(); run this from the repo root so it resolves
# (matches pty_probe.py, and avoids a sys.path hack).

HAIKU = "claude-haiku-4-5-20251001"  # cheap; the probe only checks the submitted string
PROJECTS = Path.home() / ".claude" / "projects"
MARKER = "PROBEMARKER"
DRAFT_TOKEN = "DRAFTXX"
UNSET = [
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_DISABLE_AGENT_VIEW",
    "CLAUDECODE",
]


def _text(entry: dict) -> str:
    c = entry.get("message", {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _last_user_text(name: str, since: float) -> str | None:
    files = [f for d in PROJECTS.glob(f"*tprobe-{name}") for f in d.glob("*.jsonl")]
    files = [f for f in files if f.stat().st_mtime >= since - 10]
    if not files:
        return None
    f = max(files, key=lambda p: p.stat().st_mtime)
    last = None
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "user" and _text(e):
            last = _text(e)
    return last


def _classify(t: str | None) -> str:
    if t is None:
        return "NO-TRANSCRIPT (did you keep CLAUDE_CODE_SESSION_ID unset?)"
    vt = t.count("\x0b")
    ctrl = sum(1 for ch in t if ord(ch) < 32 and ch not in "\r\n\t")
    if vt:
        return f"BOXES: {vt} x U+000B in the submitted string (renders as box glyphs)"
    if DRAFT_TOKEN in t:
        return f"CONCAT: stale draft survived (len={len(t)}); clear was too weak"
    if t.strip() == MARKER:
        return f"CLEAN: marker only, {ctrl} other control char(s)"
    return f"OTHER({t.strip()[:50]!r})"


async def run(inject: str, draft: str, repop: bool, model: str) -> str | None:
    from omnigent.inner.terminal_windows import WindowsTerminalInstance, _build_paste_payload

    cwd = Path.home() / "AppData" / "Local" / "Temp" / "tprobe" / "run"
    shutil.rmtree(cwd, ignore_errors=True)
    (cwd / ".priv").mkdir(parents=True, exist_ok=True)
    tmp = cwd / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    inst = WindowsTerminalInstance(
        name="tprobe-run",
        session_key="run",
        private_dir=cwd / ".priv",
        command="claude",
        args=["--model", model],
        env={"TEMP": str(tmp), "TMP": str(tmp)},
        env_unset=UNSET,
        inherit_env=True,
    )
    q = inst.subscribe(replay=False)

    async def pump() -> None:
        while await q.get() is not None:
            pass

    started = time.time()
    await inst.launch(cwd=cwd)
    inst.set_client_size(q, 14, 40)  # narrow so a long draft wraps to many rows
    pt = asyncio.create_task(pump())
    text = None
    try:
        await inst.wait_until_ready(timeout_s=45.0)
        await asyncio.sleep(1.2)
        if repop:
            # The real cancel-then-retype path: submit the draft, Escape to cancel, and
            # Claude re-populates the input box with it for re-editing.
            inst.inject_payload(_build_paste_payload(draft or DRAFT_TOKEN))
            await inst.submit_injected()
            await asyncio.sleep(1.5)
            inst.inject_payload("\x1b")
            await asyncio.sleep(1.5)
        elif draft:
            inst.inject_payload(draft)
            await asyncio.sleep(1.0)
        if inject:
            inst.inject_payload(inject)
        await asyncio.sleep(1.8)  # let a large burst drain
        inst.inject_payload(_build_paste_payload(MARKER))
        await inst.submit_injected()
        await asyncio.sleep(3.0)  # let claude persist the user turn
        text = _last_user_text("run", started)  # READ BEFORE teardown force-kills
        inst.inject_payload("\x1b")
        await asyncio.sleep(0.6)
    finally:
        pt.cancel()
        with contextlib.suppress(Exception):
            await inst.close()
        await asyncio.sleep(0.3)
    return text if text is not None else _last_user_text("run", started)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inject", default="", help="python-escaped sequence, e.g. '\\x7f'")
    ap.add_argument("--repeat", type=int, default=1, help="repeat --inject N times")
    ap.add_argument("--draft", default="", help="seed a plain typed draft (default: none)")
    ap.add_argument("--repop", action="store_true", help="submit-then-Escape re-populate path")
    ap.add_argument("--model", default=HAIKU)
    a = ap.parse_args()
    inject = a.inject.encode("utf-8").decode("unicode_escape") * a.repeat
    draft = a.draft.encode("utf-8").decode("unicode_escape") if a.draft else ""
    print(
        f"inject={a.inject!r} x{a.repeat} (len={len(inject)})  repop={a.repop}  "
        f"draft_len={len(draft)}  pane=14x40\n"
    )
    text = asyncio.run(run(inject, draft, a.repop, a.model))
    print(f"verdict   : {_classify(text)}")
    print(f"submitted : {text!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
