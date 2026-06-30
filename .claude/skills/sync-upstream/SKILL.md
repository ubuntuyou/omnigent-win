---
name: sync-upstream
description: Sync omnigent-win (the Windows fork) with omnigent-ai/omnigent upstream. Walks the full merge flow that recurs whenever upstream moves: trial-merge on a throwaway branch (never main, since a direct main push is classifier-blocked), resolve the two conflicts that recur every single time (CLAUDE.md add/add, and io.StringIO stdin mocks vs our .buffer-reading hooks), scan the merged tree for io.StringIO mocks that auto-merge in with NO conflict marker and would silently crash our hooks, tell expected native-Windows test failures apart from real regressions, then ship via a --merge PR and admin-merge past the secret-less E2E UI gate. Load when asked to "sync upstream", "merge upstream", "pull in upstream changes", or when `git fetch upstream` shows main behind. Ships clash_scan.py, which reports divergence, the both-sides file overlap, and the io.StringIO offenders.
---

# Syncing the Windows fork with upstream

`omnigent-win` is a Windows-native fork. It is **not** a real GitHub fork
(`isFork: false`); it shares history with `omnigent-ai/omnigent` via an `upstream`
remote, and the web "Sync fork" button does nothing. Syncing is a manual merge of
`upstream/main` that you must drive carefully because a handful of clashes recur on
every sync and one of them is **silent** (auto-merges clean, breaks at runtime).

> **Invariant (do not break):** the fork is additive and `IS_WINDOWS`-guarded. A sync
> must never alter POSIX/tmux behavior. When you resolve a conflict, you are choosing
> between upstream's line and the fork's `IS_WINDOWS` line, never rewriting either path.

The merge model that keeps future syncs clean: always integrate with a **merge commit**
(`--merge`, not squash/rebase) so `upstream/main` stays a recorded parent. Squashing the
sync would hide which upstream commits are integrated, and the *next* sync would try to
re-merge (and re-conflict) all of them.

---

## Step 0 — Preflight: fetch and measure

```bash
git fetch upstream
.venv/Scripts/python.exe .claude/skills/sync-upstream/clash_scan.py
```

`clash_scan.py` is read-only. It prints: how far `upstream/main` is ahead, how many
fork commits you carry, the merge-base, the **files changed by both sides** (the clash
surface), and any **io.StringIO stdin mocks** sitting in the claude-native hook tests
(the silent trap, see Step 3). Run it before AND after the merge.

## Step 1 — Trial-merge on a throwaway branch (NEVER on main)

Two reasons this never happens directly on `main`: the documented `git push origin main`
is **classifier-blocked** (direct main push), and you want CI to validate the merge before
it lands. So branch, then merge:

```bash
git checkout -b trial/upstream-sync main
git merge --no-commit --no-ff upstream/main
git diff --name-only --diff-filter=U      # the real conflict list
```

**Do not trust `git merge-tree --write-tree` for the conflict preview.** This repo's git
is old (2.24) and prints `fatal: unknown rev --write-tree`, which is easy to misread as
"0 conflicts." The `git merge --no-commit` trial above is the ground truth. If you ever
need to abandon, `git merge --abort` and delete the branch; `main` is untouched.

## Step 2 — Resolve the two recurring conflicts

These two show up on essentially every sync. Resolve both in the fork's favor.

1. **`CLAUDE.md` (add/add).** Upstream made its `CLAUDE.md` a **symlink to `AGENTS.md`**
   (the git blob content is literally the string `AGENTS.md`). Always keep ours:
   ```bash
   git checkout --ours -- CLAUDE.md && git add CLAUDE.md
   ```
   Upstream's `AGENTS.md` arrives as a new file; harmless, leave it.

2. **`tests/test_claude_native_hook.py` (content), the `io.StringIO` vs `fake_stdin`
   collision.** Our Claude hooks read `sys.stdin.buffer` (UTF-8 invariant,
   `_read_stdin_utf8`); upstream's tests mock stdin with `io.StringIO`, which has **no
   `.buffer`** and would `AttributeError`. Combine both sides: take upstream's payload /
   assertion changes, but keep `fake_stdin(...)` (from `tests/native_hook_helpers.py`) as
   the stdin mock. Do not pick one side wholesale.

## Step 3 — Scan for the SILENT io.StringIO clash

The dangerous case is not the conflict above. It is an upstream-added stdin mock that
**auto-merges with no conflict marker** and only fails at runtime against our `.buffer`
hook. Re-run the scanner on the merged tree:

```bash
.venv/Scripts/python.exe .claude/skills/sync-upstream/clash_scan.py
```

For every offender it reports in a **claude-native** hook test, convert
`io.StringIO(...)` to `fake_stdin(...)`. Scope matters:

- **Convert:** the hooks that read `sys.stdin.buffer` -> `claude_native_hook`,
  `claude_native_status`, `claude_native_message_display_hook`, and their tests.
- **Leave alone:** codex / cursor / kimi hooks read `sys.stdin.read()` (plain text), so
  `io.StringIO` is correct there. Those harnesses are not on the `.buffer` path. Converting
  them would be wrong.

Confirm zero leftover conflict markers anywhere: `git grep -n '^<<<<<<< \|^>>>>>>> '`.

## Step 4 — Verify natively, and know the two EXPECTED failures

Only `tests/inner/` runs cleanly on native Windows; the rest of the suite needs WSL2 or
Linux CI. Run what runs:

```bash
# bypass `uv run` here: the live runner holds omni.exe open and uv's reinstall fails on it
.venv/Scripts/python.exe -m pytest tests/test_claude_native_hook.py tests/inner/test_terminal_windows.py -q
```

Two tests fail on native Windows for **environmental, not merge, reasons**. Recognize them
so you do not chase a phantom regression:

| Failing test | Why it fails on Windows | Verdict |
| --- | --- | --- |
| `test_message_display_hook_writes_owner_only_file` | asserts POSIX `0o600`; Windows reports `0o666` (no POSIX perm bits) | expected, WSL2-only |
| `test_forwarder_migrates_line_cursor_state_to_byte_offset` | `os.replace` over an open file raises `WinError 5` on Windows; works on POSIX | expected, WSL2-only |

To **prove** a native failure is pre-existing and not your merge, diff the failing code
path against pre-merge `main`: `git diff main -- <file>`. If the failing function is
byte-identical to main, the merge did not cause it. (Both tests above sit on paths
unchanged by recent syncs.) The real full-suite gate is the PR's Linux CI.

## Step 5 — Commit, PR, and merge

```bash
git branch -m trial/upstream-sync chore/sync-upstream-<YYYYMMDD>
git commit --no-edit          # completes the merge with your staged resolutions
git push -u origin chore/sync-upstream-<YYYYMMDD>
```
Open the PR with the fork flags (the `upstream` remote makes `gh` default to the wrong
base repo): `gh pr create --repo ubuntuyou/omnigent-win --base main --head <branch> ...`.

When checks settle, **one required check will be red: `E2E UI Required`.** It is not your
merge. Its `e2e-ui-required/check.sh` calls an LLM judge over a gateway, but the fork has
no `OPENAI_BASE_URL`/`OPENAI_API_KEY` secret, so `curl` aborts with *"No host part in the
URL."* It is deterministic; re-running will not fix it. The real `E2E UI Tests (shard N/3)`
jobs still pass. Merge past it (preserving the upstream parent), with **explicit user
authorization**:

```bash
gh pr merge <#> --repo ubuntuyou/omnigent-win --merge --admin --delete-branch
```
(or have a maintainer add the `skip-e2e-ui-test` label, the gate's own escape hatch).

## Step 6 — Land it locally and clean up

```bash
git checkout main && git pull origin main
git branch -d chore/sync-upstream-<YYYYMMDD>
```
Verify the graph: the sync merge commit should show `upstream/main`'s tip as a second
parent (`git log --oneline --graph -4`). That parent link is what keeps the next sync clean.

---

## Quick reference

| Thing | Value |
| --- | --- |
| Sync command (push step is blocked) | `git fetch upstream && git merge upstream/main` then branch + PR |
| Merge style | `--merge` (preserve upstream parent), never squash |
| Recurring conflict #1 | `CLAUDE.md` add/add -> `git checkout --ours` |
| Recurring conflict #2 + silent clash | `io.StringIO` stdin mocks -> `fake_stdin` in claude-native hook tests only |
| Expected native failures | `0o600` perms; `os.replace`-over-open-file (both WSL2-only) |
| Always-red CI gate | `E2E UI Required` (no `OPENAI_BASE_URL` secret) -> `--admin` or `skip-e2e-ui-test` |
| `gh` path | `C:\Users\Joe\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_*\bin\gh.exe` |

See CLAUDE.md "The fork relationship (important)" for the canonical notes this skill operationalizes.
