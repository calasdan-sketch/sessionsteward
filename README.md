# Session Steward (`sessionsteward`)

**Undo for CLAUDE.md.** sessionsteward git-snapshots your agent memory
files — `CLAUDE.md`, `.claude/`, `AGENTS.md`, `~/.claude/CLAUDE.md`, Claude
Code's auto-memory directory — and `sessionsteward undo` brings them back.

```bash
pip install git+https://github.com/calasdan-sketch/sessionsteward
sessionsteward init                      # one-time: snapshots repo + first snapshot
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
undone 1 step(s) -> a3f9c21  before letting the agent edit memory
  restored CLAUDE.md
  unchanged .claude
```

**Scope:** session *ops* (snapshots, undo, compaction guard) — not memory
*storage*. That boundary stays clean.

## Install

Zero dependencies — Python 3.10+ stdlib only (uses your system's git; if
git is missing every command says so in one line and exits 2).

```bash
pip install git+https://github.com/calasdan-sketch/sessionsteward
```

Or from a clone (console script `sessionsteward`):

```bash
git clone https://github.com/calasdan-sketch/sessionsteward && cd sessionsteward
pip install .
```

pip installs the `sessionsteward` command on every OS. Install it before
running `install-hooks`: the hooks call the installed package with the
interpreter you ran it from.

## Usage

```bash
sessionsteward init                        # set up the snapshots repo + take the first snapshot
sessionsteward watch --add NOTES.md ~/x.md # watch extra paths (defaults stay watched)
sessionsteward watch --remove AGENTS.md    # stop watching a path, defaults included

sessionsteward snap -m "before refactor"   # snapshot now
sessionsteward precompact                  # snapshot before compaction
sessionsteward watch                       # daemon: auto-snapshot on change

sessionsteward log                         # list snapshots (this project only, local time)
sessionsteward diff                        # what changed (e.g. what compaction dropped)
sessionsteward undo                        # step back one snapshot (--steps N for more)
sessionsteward restore <sha>               # restore a specific snapshot
sessionsteward diet                        # what's eating the context window
sessionsteward install-hooks               # print Claude Code hooks JSON
sessionsteward install-hooks --write       # merge it into ~/.claude/settings.json
```

Watched by default: `CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `AGENTS.md`
(project root), `~/.claude/CLAUDE.md` (your user-level memory) and Claude
Code's auto-memory directory for the project,
`~/.claude/projects/<project>/memory/` (`MEMORY.md` + topic files), when it
exists — `<project>` is Claude Code's name for the repository (the path
with non-alphanumerics turned into `-`; `CLAUDE_CONFIG_DIR` is honoured).
If yours is not picked up, add it explicitly:
`sessionsteward watch --add ~/.claude/projects/<project>/memory`.
`watch --add` adds to that list — it never replaces it; `watch --remove`
drops a path, a default included. Paths may be project-relative, `~/…`
(`~\…` on Windows) or absolute, including files on another drive.

**Shrink guard:** when a snapshot shows a watched file lost more than half
its size since the previous snapshot (the "compaction ate my CLAUDE.md"
case), or a watched file disappeared altogether, `snap`, `precompact`, the
`watch` daemon and `log` flag it:

```
$ sessionsteward log
b81d0e4  2026-10-16 09:12  auto: files changed
  ! guard: CLAUDE.md shrank 2412 -> 18 bytes (-99%) — `sessionsteward undo` to recover
2f0c9aa  2026-10-16 08:40  session start
```

Files are named as you know them: project-relative (`CLAUDE.md`,
`.claude/rules/testing.md`), `~/.claude/CLAUDE.md`, or absolute. The
"`undo` to recover" hint appears only while your files *are* that
shrunken top snapshot; once you have restored, older entries point at the
snapshot that holds the previous version (`restore <sha>`) instead.

Safety rules: snapshots are byte-exact (CRLF stays CRLF whatever your git's
`core.autocrlf` or `.gitattributes` say); identical content is never
re-committed — it is compared with *this project's* newest snapshot, so two
hooks snapping the same state, or another project snapping in between,
don't bury the good one. After `undo`/`restore` your watched files *are*
the target snapshot: whatever is watched now but didn't exist in it is
moved to `~/.sessionsteward/trash/` — never deleted, **directories
included** — and a file that cannot be written (read-only, or a directory
now sits in its place) is reported as `skipped … (left untouched)` with
exit code 1 while the rest is restored. Symlinks are never written
through: the link stays where it is, and when what is behind it differs
from the snapshot that is `skipped … is a symlink` (exit 1). A path added
with `watch --add` *after* a snapshot was taken is outside that snapshot's
scope — restoring it leaves the path alone and says so (`kept NOTES.md:
not watched when snapshot a3f9c21 was taken`); every snapshot records its
watch list for this.
The trash keeps the newest 20 entries; paths that are already at the
snapshot's state are left alone (`unchanged .claude`) rather than swapped.
`log`/`diff`/`undo`/`restore` are scoped to the current project (`--root`,
default: cwd): snapshots record their project root (one repo is shared by
all your projects and the whole history is scanned, so another project's
snapshots never hide yours), and a `restore` of a snapshot from another
project is refused rather than clobbering your files.
`undo` steps back from wherever you are: because a restore leaves your
files equal to its target, after one undo (or a `restore <sha>`) they match
that snapshot, and the next `undo` continues from there instead of
bringing the undone state back (`--steps N` stops at the oldest snapshot
and says so). Undoing or restoring with unsaved changes — a state that
matches no snapshot — first snapshots it as `pre-undo`/`pre-restore`, so
every undo is reversible: redo is `restore` of that snapshot (or of the
snapshot you came from).

Ignored on every snap and by the watch daemon: `*.log`, `*transcript*`,
`__pycache__`, `.git`, `node_modules` (transcripts would otherwise balloon
the snapshots repo).

Mirrors live under `~/.sessionsteward/snapshots/files/` at their
project-relative path (`__home/…` for files elsewhere under your home
directory, `__abs/<drive>/…` for anything else), so a mirror can never
land outside the repo. On Windows that puts a few extra path components
under `%USERPROFILE%`: a very deeply nested project can hit the 260-char
`MAX_PATH` limit unless long paths are enabled (`git config --global
core.longpaths true` plus Windows' `LongPathsEnabled`).

A corrupt `~/.sessionsteward/config.json` is reported with its path and
left in place (your watch list is never silently replaced).

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

Note that what a SessionStart or PreCompact hook prints on stdout is
added to Claude's context. sessionsteward keeps it to one line
(`snapshot a3f9c21`) plus any guard warnings, and reports problems (git
missing, a stale `index.lock`, a corrupt config) as a single `error: …`
line on stderr with exit code 2 — never a traceback.

## Context diet

```
# Context diet

Files loaded into context: 3, total ~12,400 chars (~3,100 tokens)
(2 other watched file(s) — settings, commands, hooks — are not loaded into context and are not counted)

## Biggest files
| file | chars | lines |
...
## Duplicated content (4 lines in 2+ files)
- _Be helpful and concise in all responses_ …
  - `/proj/CLAUDE.md`
  - `/proj/.claude/rules/style.md`
```

Counted: what Claude Code loads at session start — `CLAUDE.md`,
`CLAUDE.local.md`, `AGENTS.md`, `.claude/rules/**/*.md`, `~/.claude/CLAUDE.md`
and the auto-memory index `MEMORY.md`. Settings, commands/skills, hooks,
transcripts and logs are watched for undo but never counted.

## Tests

```bash
python3 -m unittest discover -s tests   # 71 tests, temp dirs only
```

Two tests (read-only files/dirs) are skipped when run as root, where
permission bits don't bind; the symlink test is skipped on Windows.

## License

MIT — see [LICENSE](LICENSE).
