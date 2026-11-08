# Round 5: specialists

Branch `expert-2`. Nothing was published, tagged or pushed. Registry check:
PyPI holds 0.1.0 only (`pip download sessionsteward==0.1.0 --no-deps`, read-only).

## How it was done (honest)

No independent sub-agents were available. One author played each role in turn
and ran real commands; the command is the proof, not the role name.

## Specialists and findings

| Role | Finding | Proving command |
|---|---|---|
| Claude Code power user | Started in `packages/web` of a monorepo, sessionsteward watched nothing and committed an EMPTY snapshot; Claude Code loads the CLAUDE.md of every folder above. `diet` counted 0 files. Fixed: parent `CLAUDE.md` / `CLAUDE.local.md` are watched (`<folders above>/CLAUDE.md`). | `cd proj/packages/web && sessionsteward diet` |
| Claude Code power user | Hooks reference (fetched 2026-11-08): PreCompact exit 2 blocks compaction; SessionStart stdout goes into the agent's context. Our hooks exited 2 on any StewardError and printed `snapshot abc1234` every session. Fixed with `--hook` (silent when fine, exit 1 on failure). | `sessionsteward precompact --hook` with a broken config.json: exit 1 |
| Git internals engineer | A killed snapshot leaves `.git/index.lock`; every later snap failed (`rc=2`) until deleted by hand. Fixed: lock older than 120 s is removed under our own lock, with a warning. A fresh lock still fails (a live git may own it). | `touch .../snapshots/.git/index.lock; sessionsteward snap` |
| Git internals engineer | Commits relied on the repo's own `user.name`; a store without it (copied machine-to-machine) failed "Author identity unknown". Fixed: identity set on every internal git call. | OldStore test |
| Data-loss / recovery specialist | One flipped byte in a stored blob made `log`/`snap` print "CLAUDE.md deleted" (a false alarm, exit 0). Now "damaged - run doctor"; new `doctor` runs `git fsck` and exits 3. | corrupt an object file, `sessionsteward log; sessionsteward doctor` |
| Data-loss / recovery specialist | `CLAUDE.md -> AGENTS.md` (a common setup): `undo` restored the content but printed `skipped CLAUDE.md` and exited 1. The link is now judged after its target is restored: `unchanged`, exit 0. | LinkedMemory test |
| Developer-productivity researcher | Cost of the hook on every session start: about 0.6 s per `snap --hook` on a tiny project (5 runs, 3.2 s wall, 0.6 s CPU; mostly git and file sync). Not changed; a threshold the owner can judge. | `time sessionsteward snap --hook` |
| Upgrade-path tester | A real 0.1.0 store (made with the 0.1.0 wheel from the registry) opened with this tree: `log`, `status`, `lost`, `diff`, `undo`, new `snap`, `prune`, `diet --history` all work; no migration needed. Test builds the 0.1.0 layout by hand. | `OldStore` test; manual run with the downloaded wheel |

## Quality methods used this round

- Seeded property tests (hypothesis is not installed here; a seeded
  generator names the failing seed): 25 seeds of random bytes incl. CRLF and
  non-ASCII, `snap`, change, `undo` gives back the exact bytes; 15 seeds of
  shuffled rule lists never show a loss in `lost`.
- Mutation-style check: 7 hand-made mutants (flip the age test, the exit code,
  the guard ratio, the ancestor names, the link check, the damage check, the
  hook silence). The new tests killed 6; the guard-ratio one was killed by the
  older `tests/test_steward.py` (7 failures).
- Golden-shape tests for `log`, `status`, `lost`, `scan`, `diet` JSON keys and
  the `--version` line.
- Lint: `ruff --select F,E9` clean on package, bench and the new test file.
- mypy 2.3.1 ran: 31 complaints, all inference or Optional noise, none a
  defect I could trigger; the package is NOT claimed to be typed.
- Hostile/edge: root `/`, a directory called `CLAUDE.md` in a parent, spaces
  in paths, `LC_ALL=C`, a damaged blob, a stale lock: no traceback.

## Competitors (read-only, 2026-11-08)

- Claude Code `/rewind` ([checkpointing](https://code.claude.com/docs/en/checkpointing)):
  still says Bash-only changes, outside edits and other sessions are not
  tracked, snapshots are swept after about 30 days, and it "doesn't rewind
  symlinked or hard-linked files". sessionsteward restores through the
  link's target and reports it.
- [ccundo](https://github.com/gmh5225/ccundo): a fork of RonitSachdev/ccundo,
  undo/redo of file operations read from session files, backups in
  `~/.ccundo/backups/`; says Bash commands cannot be undone automatically.
  It has `redo`; sessionsteward has `restore <sha>` for going forward. Page
  showed no releases. Claude Vigil and the memory stores were not re-fetched
  (unverified for today).

## Scored ideas (value / differentiation / effort / risk, 1-5; 5 = most)

| # | Idea | V | D | E | R | Status |
|---|---|---|---|---|---|---|
| 1 | Watch parent-folder CLAUDE.md files | 5 | 4 | 2 | 2 | DONE |
| 2 | `--hook` mode: silent, never exit 2 | 5 | 4 | 2 | 1 | DONE |
| 3 | Clear a stale index.lock | 4 | 2 | 1 | 2 | DONE |
| 4 | Damaged blob is not "deleted" | 4 | 3 | 1 | 1 | DONE |
| 5 | `doctor` (git, fsck, lock, hooks) | 4 | 3 | 2 | 1 | DONE (was NOT-DONE #20 in round 4) |
| 6 | Linked CLAUDE.md -> AGENTS.md: unchanged, exit 0 | 3 | 3 | 1 | 1 | DONE |
| 7 | Fixed commit identity | 3 | 1 | 1 | 1 | DONE |
| 8 | `__author__` and pinned author tests | 2 | 1 | 1 | 1 | DONE |
| 9 | 0.1.0 store upgrade test | 4 | 1 | 2 | 1 | DONE |
| 10 | Seeded property tests | 3 | 1 | 2 | 1 | DONE |
| 11 | Golden JSON shapes | 3 | 1 | 1 | 1 | DONE |
| 12 | mypy run | 2 | 1 | 2 | 1 | DONE (run only; not claimed typed) |
| 13 | `diff --json` | 2 | 2 | 2 | 1 | NOT-DONE |
| 14 | `py.typed` | 2 | 1 | 4 | 2 | NOT-DONE (31 mypy complaints first) |
| 15 | Real Windows run | 5 | 1 | 3 | 1 | NOT-DONE (no Windows here; CI on push) |
| 16 | Python 3.10 run | 3 | 1 | 2 | 1 | NOT-DONE |
| 17 | `pipx` install line | 2 | 1 | 1 | 2 | NOT-DONE (not run) |
| 18 | Trusted-publishing workflow | 4 | 1 | 3 | 3 | NOT-DONE (owner's decision) |
| 19 | Shell completion | 2 | 2 | 3 | 1 | NOT-DONE |
| 20 | `redo` as an alias for going forward | 3 | 2 | 3 | 2 | NOT-DONE (`restore <sha>` does it) |
| 21 | Use hook JSON `systemMessage` so warnings reach the user, not the agent | 4 | 4 | 3 | 3 | NOT-DONE (SessionStart placement not confirmed in docs I could read) |
| 22 | Auto `git gc` / size report in `doctor` | 2 | 1 | 2 | 2 | NOT-DONE |
| 23 | Watch nested CLAUDE.md in subfolders Claude loads on demand | 3 | 3 | 4 | 3 | NOT-DONE (loads lazily; unclear what counts) |
| 24 | Hook latency under 0.3 s | 3 | 2 | 4 | 3 | NOT-DONE |
| 25 | Hypothesis-based tests | 2 | 1 | 2 | 1 | NOT-DONE (not installed; seeded version used) |
| 26 | Re-verify memory-store competitors | 2 | 1 | 1 | 1 | NOT-DONE (not fetched) |

## Numbers

- Before: 434 passed, 21 skipped (200-285 s depending on load).
- After: 456 passed, 21 skipped (277 s); 22 new tests in
  `tests/test_round5_experts.py`.
- Benchmark detectors unchanged; speeds in `bench/RESULTS.md` (diet --lint
  about 4.7 s, lost 0.24 s, scan 0.24 s; within load noise).
- Wheel built from the tree: `twine check --strict` passes; METADATA says
  `Author: Calas Automations Inc.`; LICENSE inside the wheel says
  `Copyright (c) 2026 Calas Automations Inc.`.

## What could not be tested

- Windows and macOS (static review only); Python 3.10 (only parsed).
- That Claude Code really shows exit-1 hook output as non-blocking: taken from
  its hooks reference (exit 2 blocks PreCompact; the page I could read did not
  spell out exit 1 for PreCompact, only that 2 is the blocking code).
- The hooks run by a real Claude Code, `pip install sessionsteward` from the
  registry after release, hypothesis, a read-only home as non-root.
