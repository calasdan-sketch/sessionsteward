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

`diff` prints every added (`+`) and removed (`-`) line between two snapshots
across all watched files, file headers left out, at most 200 lines (it says
how many more there are); when the only differences have no lines to show
(a binary file, or just a file mode) it says `no line changes` instead of
`(no changes)`.

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
now sits in its place), or a path that cannot be moved to the trash, is
reported as `skipped … (left untouched)` with exit code 1 while the rest is
restored (a directory that was moved to the trash and could not be replaced
is moved back, never left missing). Symlinks are never written
through: the link stays where it is, and when what is behind it differs
from the snapshot that is `skipped … is a symlink` (exit 1). A path added
with `watch --add` *after* a snapshot was taken is outside that snapshot's
scope — restoring it leaves the path alone and says so (`kept NOTES.md:
not watched when snapshot a3f9c21 was taken`); every snapshot records its
watch list for this.
The trash keeps the 20 most recently trashed entries (ordered by when they
were moved there, never by the age of the file, and never including what the
restore in progress has just put there); paths that are already at the
snapshot's state are left alone (`unchanged .claude`) rather than swapped.
**What a snapshot holds:** the bytes of every regular file under the watched
paths, and whether it is executable (a hook script comes back runnable).
Nothing else is recorded: not other permission bits, owners or timestamps;
not empty directories; not symlinks (a link is stored as the content behind
it, and a restore keeps a link that is in place but never recreates one that
is gone); not special files (a named pipe, socket or device is skipped,
never read - reading a pipe would block for ever - and a restore never
writes onto one: `skipped ... is not a regular file`); not the ignored names
listed below (so a skill you cloned into `.claude/skills/` loses its `.git`
when `.claude` is restored). A restore
replaces a watched directory as a whole, so whatever a snapshot cannot hold
is *not* carried over - it stays in the trash copy the restore names
(`moved .claude -> ~/.sessionsteward/trash/...`). A change that only toggles
a file's executable bit is recorded by `snap`, but `undo` does not revert it.
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

Every watched file is stored whatever git's ignore rules say: a global
ignore file, or a `.gitignore` inside `.claude/`, never hides one from a
snapshot (a snapshot that git left a mirrored file out of is not committed
at all: `error: snapshot incomplete`). Each snapshot also records whether a
watched path was a file or a directory, and `undo`/`restore` check the
snapshot against that list *before* they touch a single file: a snapshot
that cannot supply everything it lists (a torn one, as older versions could
commit when two snaps raced) is refused — `error: refusing to restore
snapshot …`, exit 1, nothing changed — instead of replacing a file with an
empty directory. Snapshots taken by 0.1.0 do not say file or directory; for
those a path with no stored content is refused only when restoring it would
replace live content.

**Your git setup stays out of it.** The snapshots repo is private, and
every git command sessionsteward runs names that repo outright (a broken
`.git` there is an error, never an excuse to use whatever repository your
home directory is) and drops the `GIT_*` environment variables that could
point git somewhere else (`GIT_DIR`, `GIT_INDEX_FILE`, ... - git sets
several of them for its own hooks and aliases) or stamp another identity on
a snapshot (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`). On its own command line
only, it also overrides the parts of *your* git configuration that would
change a snapshot or stop one from being made: commit signing, hooks
(`core.hooksPath`, and hooks a template dir copies into a new repo), a
global ignore file, `core.fsmonitor`, automatic gc and colour. Your config
files are never edited, and the variables that only say where they live
(`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`) or where
git's programs are (`GIT_EXEC_PATH`) are kept. `diff` prints a plain unified
diff whatever your colour, external-diff or rename settings are. A snapshots
repo whose first commit never happened (a failed `init`) is finished by the
next command; one git cannot read is reported - `error: the snapshots repo
... cannot be used`, exit 2 - and left alone.

**One writer at a time.** All your projects, hooks and the `watch` daemon
share one snapshots repo, so every command that writes (`init`, `snap`,
`precompact`, `undo`, `restore`, `watch --add/--remove`, and each snapshot
the daemon takes) first takes a lock — a file lock on
`~/.sessionsteward/steward.lock`, dropped by the operating system the
moment the process ends, even under `kill -9`, so it can never go stale.
A second command simply waits its turn (up to 30 seconds; set
`SESSIONSTEWARD_LOCK_TIMEOUT` to change that) and then fails with a one-line
`error: another sessionsteward is still working …`, exit 2. The `watch`
daemon keeps going when a snapshot fails (busy lock, a git hiccup, a file
that vanished mid-copy): it prints a `warning: … will retry` line on stderr
and tries again on the next tick. A snapshot whose `manifest.json` cannot be
read (older versions could commit one when two snaps raced) is skipped by
`log`/`undo` instead of breaking them for every project, and refused by
`restore`.

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
clobbering your other hooks, and re-running is a no-op (nothing is written
when the hooks are already there).

`--write` never guesses at a settings file it cannot read: one that is not
strict JSON (a comment, a trailing comma, a truncated file, broken UTF-8) or
whose `hooks` is not the usual shape is **refused** — `error: …`, exit 2, the
file untouched — and the hooks JSON is printed so you can paste it in by
hand. A UTF-8 BOM is accepted (and not written back). When it does write, it
writes atomically, keeps your previous file as `settings.json.bak` (the
state just before the latest write), keeps your line endings and
indentation, and writes through a symlinked `settings.json` instead of
replacing the link.

Note that what a SessionStart or PreCompact hook prints on stdout is
added to Claude's context. sessionsteward keeps it to one line
(`snapshot a3f9c21`) plus any guard warnings, and reports the problems it
knows about as a single `error: …` line on stderr instead of a traceback:
exit code 2 for git (missing, unusable, a stale `index.lock`), a corrupt
config or manifest, or a busy lock; exit code 1 when the operating system
refuses a read or write (an unwritable home directory, a full disk, a file
where a directory belongs). Only a bug in sessionsteward itself can still
print a traceback.

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
python3 -m unittest discover -s tests   # temp dirs only; never touches ~/.sessionsteward
```

Every test runs against a temporary HOME; the newer ones also give git a
private config and remove every `GIT_*` variable. Two tests (read-only
files/dirs) are skipped when run as root,
where permission bits don't bind; tests that need POSIX features (symlinks,
named pipes, execute bits, shell-script hooks) are skipped on Windows.

## License

MIT — see [LICENSE](LICENSE).
