# Session Steward (`sessionsteward`)

Versioned snapshots for agent working state. Coding agents silently
degrade: compaction drops constraints, and one "remember this" can
overwrite your 30-line CLAUDE.md with a single bullet — no warning, no
undo. sessionsteward git-versions your agent config + memory files on
every change: instant undo, pre-compaction snapshots, and a context-diet
report showing what's eating the window.

**Scope:** session *ops* (snapshots, undo, compaction guard) — not memory
*storage*. That boundary stays clean.

## Install

Zero dependencies — Python 3.10+ stdlib only (uses your system's git).

```bash
pip install git+https://github.com/calasdan-sketch/sessionsteward
```

Or from a clone (console script `sessionsteward`):

```bash
git clone https://github.com/calasdan-sketch/sessionsteward && cd sessionsteward
pip install .
```

Install it before running `install-hooks`: the hooks call the installed
package with the interpreter you ran it from.

## Usage

```bash
sessionsteward init                        # set up the snapshots repo
sessionsteward watch --add NOTES.md ~/x.md # watch extra paths

sessionsteward snap -m "before refactor"   # snapshot now
sessionsteward precompact                  # snapshot before compaction
sessionsteward watch                       # daemon: auto-snapshot on change

sessionsteward log                         # list snapshots (this project only)
sessionsteward diff                        # what changed (e.g. what compaction dropped)
sessionsteward undo                        # restore the previous snapshot
sessionsteward restore <sha>               # restore a specific snapshot
sessionsteward diet                        # what's eating the context window
sessionsteward install-hooks               # print Claude Code hooks JSON
sessionsteward install-hooks --write       # merge it into ~/.claude/settings.json
```

Watched by default: `CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `AGENTS.md`
(project root) and `~/.claude/CLAUDE.md` (your user-level memory).

The horror-story scenario, fixed:

```bash
$ sessionsteward snap -m "before letting the agent edit memory"
$ # agent overwrites your 30-line CLAUDE.md with one bullet
$ sessionsteward undo
undone 1 step(s) -> a3f9c21
  /home/you/proj/CLAUDE.md
```

**Shrink guard:** when a snapshot shows a watched file lost more than half
its size since the previous snapshot (the "compaction ate my CLAUDE.md"
case), `snap`, `precompact`, the `watch` daemon and `log` flag it:

```
$ sessionsteward log
b81d0e4  2026-10-16 09:12  auto: files changed
  ! guard: CLAUDE.md shrank 2412 -> 18 bytes (-99%) — `sessionsteward undo` to recover
```

Safety rules: empty snaps are skipped (no noise commits); `undo`/`restore`
never delete — files that didn't exist in the target snapshot are moved to
`~/.sessionsteward/trash/` instead, **directories included**.
`log`/`diff`/`undo`/`restore` are scoped to the current project (`--root`,
default: cwd): snapshots record their project root, and a `restore` of a
snapshot from another project is refused rather than clobbering your files.
Undoing (or restoring) with unsaved changes first snapshots the current
state as `pre-undo`/`pre-restore`, so every undo is reversible — redo is
`restore` of that snapshot.

Ignored on every snap and by the watch daemon: `*.log`, `*transcript*`,
`__pycache__`, `.git`, `node_modules` (transcripts would otherwise balloon
the snapshots repo).

## Claude Code hooks

```bash
sessionsteward install-hooks            # print the hooks JSON + where it goes
sessionsteward install-hooks --write   # merge into ~/.claude/settings.json
```

This wires **PreCompact** → `sessionsteward precompact` and **SessionStart**
→ `sessionsteward snap -m "session start"`. The hooks call the Python that
ran `install-hooks` by absolute path (`"/path/to/python" -m sessionsteward ...`),
because hook shells don't load your aliases or activate your venv — re-run
`install-hooks` if you move or recreate that environment. `--write` merges
into the existing settings file without clobbering your other hooks
(re-running is a no-op, and older bare `sessionsteward ...` hooks are
upgraded in place).

## Context diet

```
# Context diet

Watched files: 3, total ~12,400 chars (~3,100 tokens)

## Biggest files
| file | chars | lines |
...
## Duplicated content (4 lines in 2+ files)
- _Be helpful and concise in all responses_ …
  - `/proj/CLAUDE.md`
  - `/proj/.claude/settings.json`
```

## Suggested hook wiring

- **Pre-compaction:** run `sessionsteward precompact` (or add it to your
  agent's pre-compact hook) — then `sessionsteward diff` afterwards to see
  exactly what the compaction dropped.
- **Session start:** `sessionsteward snap -m "session start"`.
- **Memory writes:** point the steward at the same files your agent's
  memory hook writes.

## Tests

```bash
python3 -m unittest discover -s tests   # 31 tests, temp dirs only
```

## License

MIT — see [LICENSE](LICENSE).
