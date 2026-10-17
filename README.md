# Session Steward (`sessionsteward`)

**Undo for CLAUDE.md.** sessionsteward git-snapshots your agent memory
files — `CLAUDE.md`, `.claude/`, `AGENTS.md`, `~/.claude/CLAUDE.md` — and
`sessionsteward undo` brings them back.

```bash
pip install git+https://github.com/calasdan-sketch/sessionsteward
sessionsteward init                      # one-time: snapshots repo + defaults
sessionsteward install-hooks --write     # snapshot on every Claude Code session start and before compaction
sessionsteward undo                      # the agent clobbered CLAUDE.md? back it comes
```

Coding agents silently degrade: compaction drops constraints, and one
"remember this" can overwrite your 30-line CLAUDE.md with a single bullet —
no warning, no undo. Every snapshot is a git commit in a private repo
(`~/.sessionsteward/snapshots`), so you get instant undo, pre-compaction
snapshots, a shrink guard, and a context-diet report showing what's eating
the window.

```bash
$ sessionsteward snap -m "before letting the agent edit memory"
$ # agent overwrites your 30-line CLAUDE.md with one bullet
$ sessionsteward undo
undone 1 step(s) -> a3f9c21
  /home/you/proj/CLAUDE.md
```

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
sessionsteward watch --add NOTES.md ~/x.md # watch extra paths (defaults stay watched)
sessionsteward watch --remove AGENTS.md    # stop watching a path, defaults included

sessionsteward snap -m "before refactor"   # snapshot now
sessionsteward precompact                  # snapshot before compaction
sessionsteward watch                       # daemon: auto-snapshot on change

sessionsteward log                         # list snapshots (this project only, local time)
sessionsteward diff                        # what changed (e.g. what compaction dropped)
sessionsteward undo                        # restore the previous snapshot
sessionsteward restore <sha>               # restore a specific snapshot
sessionsteward diet                        # what's eating the context window
sessionsteward install-hooks               # print Claude Code hooks JSON
sessionsteward install-hooks --write       # merge it into ~/.claude/settings.json
```

Watched by default: `CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `AGENTS.md`
(project root) and `~/.claude/CLAUDE.md` (your user-level memory).
`watch --add` adds to that list — it never replaces it; `watch --remove`
drops a path, a default included.

**Shrink guard:** when a snapshot shows a watched file lost more than half
its size since the previous snapshot (the "compaction ate my CLAUDE.md"
case), or a watched file disappeared altogether, `snap`, `precompact`, the
`watch` daemon and `log` flag it:

```
$ sessionsteward log
b81d0e4  2026-10-16 09:12  auto: files changed
  ! guard: CLAUDE.md shrank 2412 -> 18 bytes (-99%) — `sessionsteward undo` to recover
```

Safety rules: snapshots are byte-exact (CRLF stays CRLF whatever your git's
`core.autocrlf` or `.gitattributes` say); identical content is never
re-committed, so two hooks snapping the same state don't bury the good one;
`undo`/`restore` never delete — files that didn't exist in the target
snapshot are moved to `~/.sessionsteward/trash/` instead, **directories
included** — and a file that cannot be written (read-only, or a directory
now sits in its place) is reported as `skipped … (left untouched)` with
exit code 1 while the rest is restored.
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

This wires **PreCompact** → `sessionsteward precompact` (snapshot before
compaction — run `sessionsteward diff` afterwards to see exactly what the
compaction dropped) and **SessionStart** → `sessionsteward snap -m "session
start"`. Point the steward at the same files your agent's memory hook
writes (`watch --add`) and those get the same protection.

The hooks call the Python that ran `install-hooks` by absolute path
(`"/path/to/python" -m sessionsteward ...`), because hook shells don't load
your aliases or activate your venv — re-run `install-hooks --write` if you
move or recreate that environment: any hook that runs sessionsteward is
rewritten in place (older bare `sessionsteward ...` hooks too), never
duplicated. `--write` merges into the existing settings file without
clobbering your other hooks, and re-running is a no-op.

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

## Tests

```bash
python3 -m unittest discover -s tests   # 43 tests, temp dirs only
```

Two tests (read-only files/dirs) are skipped when run as root, where
permission bits don't bind.

## License

MIT — see [LICENSE](LICENSE).
