#!/usr/bin/env python
"""Read-only pre/post-sync clash report for the omnigent-win fork.

Run it from the repo root (via .venv/Scripts/python.exe) BEFORE a sync to size the
merge, and AFTER staging the merge to catch the silent trap. It never writes, never
merges, never touches a branch. It reports three things:

  1. Divergence: how far upstream/main is ahead, how many fork commits you carry.
  2. Clash surface: files changed by BOTH sides since the merge-base (overlap = the
     files a merge might conflict in).
  3. The SILENT trap: io.StringIO stdin mocks living in the claude-native hook tests.
     Our Claude hooks read sys.stdin.buffer (UTF-8 invariant); io.StringIO has no
     .buffer, so such a mock AttributeErrors at runtime. These slip in via a clean
     auto-merge (no conflict marker), so they must be scanned for, not waited on.

Exit code is always 0 (it is a report). See the sync-upstream SKILL.md for the flow.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Claude-native hooks read sys.stdin.buffer, so their tests MUST use fake_stdin, never
# io.StringIO. Other harnesses (codex/cursor/kimi) read sys.stdin.read() (plain text),
# so io.StringIO is correct for them; do not flag those.
BUFFER_HOOK_TESTS = (
    "tests/test_claude_native_hook.py",
    "tests/test_claude_native_status.py",
    "tests/test_claude_native_message_display_hook.py",
)
UPSTREAM = "upstream/main"
BASE_BRANCH = "main"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout.strip()


def _have_ref(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref], capture_output=True
        ).returncode
        == 0
    )


def divergence() -> None:
    print("== divergence ==")
    if not _have_ref(UPSTREAM):
        print(f"  {UPSTREAM} not found -- run `git fetch upstream` first.")
        return
    ahead = _git("rev-list", "--count", f"{BASE_BRANCH}..{UPSTREAM}")
    behind = _git("rev-list", "--count", f"{UPSTREAM}..{BASE_BRANCH}")
    base = _git("merge-base", BASE_BRANCH, UPSTREAM)
    print(f"  {UPSTREAM} ahead of {BASE_BRANCH} by : {ahead} commits")
    print(f"  fork commits {BASE_BRANCH} carries   : {behind}")
    print(f"  merge-base                       : {base[:12]}")


def overlap() -> None:
    print("\n== clash surface (files changed by BOTH sides since merge-base) ==")
    if not _have_ref(UPSTREAM):
        print("  (skipped: no upstream/main)")
        return
    base = _git("merge-base", BASE_BRANCH, UPSTREAM)
    ours = set(_git("diff", "--name-only", base, BASE_BRANCH).splitlines())
    theirs = set(_git("diff", "--name-only", base, UPSTREAM).splitlines())
    both = sorted(ours & theirs)
    if not both:
        print("  (none)")
    for f in both:
        print(f"  {f}")
    print(f"  -> {len(both)} overlapping file(s)")


def stringio_offenders() -> int:
    print("\n== SILENT trap: io.StringIO stdin mocks in claude-native hook tests ==")
    root = Path(_git("rev-parse", "--show-toplevel") or ".")
    total = 0
    for rel in BUFFER_HOOK_TESTS:
        path = root / rel
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            # An actual call (io.StringIO with a paren), not prose. Skip comment lines
            # so a doc-comment that names io.StringIO is not a false positive.
            if "io.StringIO(" not in line or line.lstrip().startswith("#"):
                continue
            # Flag only when it is wiring stdin: same line or within a 2-line window
            # mentions stdin (covers the multi-line monkeypatch.setattr(sys, "stdin", ...)).
            window = "\n".join(lines[max(0, i - 2) : i + 1])
            if "stdin" in window:
                total += 1
                print(f"  {rel}:{i + 1}: {line.strip()}")
    if total:
        print(f"  -> {total} offender(s): convert io.StringIO(...) to fake_stdin(...).")
    else:
        print("  (clean: no io.StringIO stdin mocks in the .buffer-hook tests)")
    return total


def main() -> int:
    divergence()
    overlap()
    stringio_offenders()
    # Backstop: any conflict markers left in the tree?
    markers = _git("grep", "-l", "-E", r"^<<<<<<< |^>>>>>>> ")
    if markers:
        print("\n== leftover conflict markers ==")
        for f in markers.splitlines():
            print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
