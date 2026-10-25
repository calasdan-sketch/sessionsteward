# Session Steward (`sessionsteward`)

**Undo for CLAUDE.md.** sessionsteward git-snapshots your agent memory
files — `CLAUDE.md`, `.claude/`, `AGENTS.md`, `~/.claude/CLAUDE.md`, Claude
Code's auto-memory directory — and `sessionsteward undo` brings them back.

```bash
pip install sessionsteward
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
git is missing every command says so in one line and exits 2). Any git that
can `init`, `add` and `commit` works; `diff` uses `--end-of-options`, which
git added in 2.24 (2019), and with an older git it leaves that option out
(snapshot ids are checked first, so nothing that looks like an option reaches
git either way).

```bash
pip install sessionsteward
```

Or from source (console script `sessionsteward`):

```bash
git clone https://github.com/calasdan-sketch/sessionsteward && cd sessionsteward
pip install .
```

pip installs the `sessionsteward` command on every OS. Install it before
running `install-hooks`: the hooks call that command by its absolute path.

**Upgrading from 0.1.3 or earlier: re-run `sessionsteward install-hooks
--write` once.** Older versions wrote the hooks as `python -m
sessionsteward ...`, which runs code from the project folder if a cloned
repository carries its own `sessionsteward/` package (see
[SECURITY.md](SECURITY.md)). Re-running rewrites them in place.

## Usage

```bash
sessionsteward init                        # set up the snapshots repo + take the first snapshot
sessionsteward watch --add NOTES.md ~/x.md # watch extra paths (defaults stay watched)
sessionsteward watch --remove AGENTS.md    # stop watching a path, defaults included

sessionsteward snap -m "before refactor"   # snapshot now
sessionsteward precompact                  # snapshot before compaction
sessionsteward watch [--interval SECONDS]  # daemon: auto-snapshot on change (polls every 5 s)

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
`watch --add` refuses what could never be snapshotted - the project root
itself (`.`), your home directory (`~`), the root of a drive and
sessionsteward's own folder (`~/.sessionsteward`), also when a link leads
there - with one `error:` line (exit 2) that says what to name instead, and
changes nothing. An entry like that already in `config.json` (older versions
accepted them, and every snap, undo and restore then failed) is skipped: each
command says so in one `warning:` line per entry on stderr, naming the
command that drops it (`sessionsteward watch --remove .`), and snapshots
everything else as usual.

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
paths (bar the ignored names and the oversized files described below),
whether it is executable (a hook script comes back runnable) and, on Linux
and macOS, the group and other permission bits of every path that has fewer
of them than git's own modes give back (0644 for a file, 0755 for a folder
or an executable): a 0600 token file, a 0700 `.claude`. A restore gives a
path those bits, less any the live path has lost since, so it is never more
open than the snapshot recorded or than the path is now: a `.claude` (0700)
holding a `secret.json` (0600) comes back 0700 and 0600, also when the agent
had rewritten the file 0644. A path that is gone has nothing to be compared
with and gets the recorded bits. A snapshot taken before 0.1.3 recorded none:
there the live path's bits are the only limit, and a path that is gone comes
back 0644 or 0755.
Nothing else is recorded: not bits wider than that (a group-writable file
loses its group write bit when its folder is restored), not the owner's
bits (a restored folder or executable is rwx for you, any other file rw; a
file written in place keeps its owner bits), owners or timestamps;
not empty directories; not symlinks (a link is stored as the content behind
it, and a restore keeps a link that is in place but never recreates one that
is gone); not special files (a named pipe, socket or device is skipped,
never read - reading a pipe would block for ever - and a restore never
writes onto one: `skipped ... is not a regular file`); not the ignored names
or oversized files listed below. A restore replaces a watched directory as
a whole (the old one goes to the trash), but what a snapshot never holds is
**put back where it was**: ignored names (a skill's own `.git`, `node_modules`,
a virtual environment, logs), links that lead outside the folder, folders
deeper than 64 levels, files over the size limit or the snapshot cap, and
anything that is not a regular file. Each is reported as `kept …: not part of
any snapshot (left in place)`. Only files a snapshot could have held, and
that were not in it, go to the trash. A change that only toggles
a file's executable bit or narrows its permissions is recorded by `snap`, but
`undo` does not revert it.
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

**Windows and macOS (case-insensitive file systems).** A file or directory
renamed by case alone (`Notes.md` to `notes.md`) is snapshotted under its new
name. Git treats such a rename as the entry it already has - it keeps the
old-case path and never records the new one - so the snapshots repo's index
is rebuilt from the files being mirrored on every snapshot instead of being
updated in place. Version 0.1.1 (which refused every snapshot, `undo` and
`restore` after such a rename, leaving a clobbered CLAUDE.md unrecoverable)
left exactly that state behind; the next `snap` heals it, and a
SessionStart hook is enough.

A watch-list entry is recorded under the spelling the file system gives the
path, not the one that was typed. That covers `watch --add claude.md` for the
file `CLAUDE.md`; the default `~/.claude/CLAUDE.md` when the file is
`.claude/claude.md` (your home directory as the project); `watch --add
.claude/rules/style.md` for `Style.md` inside the watched `.claude`; a
folder entry in the other case (`.claude/Rules`); and an entry that names a
file by the case it had before it was renamed. Every name below the project
root (below your home directory for a path outside the project, below the
drive for any other) is matched with the names its folder lists, the way the
file system matches them, and replaced by the listed one. The project root
itself is left as given. Two entries that name one file are one entry, the
first in the list; two files that merely differ by case, on a file system
that allows it, stay two. Before 0.1.3 these cases made `init`, `snap`, `undo`
and `restore` fail with `snapshot incomplete ... (first:
files/.claude/CLAUDE.md)` until the entry was removed; the next `snap` now
heals a store they had wedged. If a snapshot is refused all the same for a
file that a watch-list entry names, the message names the entry and the
command that drops it (`sessionsteward watch --remove <entry>`). A snapshot
taken by an older version holds the spelling the list had: `undo` and
`restore` write the file in place under the name it has now (it is not moved
to the trash and written again under the other one), but the first
snapshot after the upgrade records the other spelling, so `diff` shows the
old-case name removed and the new one added, and the guard line calls the
old-case path `deleted`, as it does after any case-only rename.

The check that a snapshot holds every mirrored file compares names exactly -
a name that differs in case is a different file - apart from one allowance:
a name macOS keeps decomposed on disk (an accented letter as a letter plus a
combining mark) counts as the file git recorded in composed form. The
comparison of your files with a snapshot, which tells `undo`, `restore` and
`log` where you are, makes the same allowance (a file named that way would
otherwise look changed for ever, and `undo` would take an extra `pre-undo`
snapshot every time - seen with hand-made names, not on a Mac); two files
that differ only that way are still two, never taken for one. One
limit remains: a project is identified by its path as it is spelled, so the
same folder reached as `C:\Users\Dan\Proj` and as `c:\users\dan\proj` is two
projects with two histories (`undo` from the second says `nothing to
restore`); start sessionsteward from the same spelling every time, or pass
`--root`. The tests reproduce the rename on any OS by telling git to ignore
case, emulate the lookup of a path in another case, and run again on the file
system itself wherever the temp directory is case-insensitive; they have been
run on Linux over a case-insensitive FUSE mount (ciopfs), not on a Windows or
macOS machine, and the macOS allowance is tested with hand-made names only (a
decomposed name on disk, the composed one in the snapshot), never on a Mac.

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
daemon polls every `--interval` seconds (5 unless you say otherwise; from 0.1
to 86400 - anything else, `nan` and `0` included, is a one-line
`error: --interval must be ...`, exit 2). It keeps going when a snapshot fails
(busy lock, a git hiccup, a file that vanished mid-copy): it prints a
`warning: … will retry` line on stderr and tries again on the next tick.
A snapshot whose `manifest.json` cannot be read (older versions could
commit one when two snaps raced) is skipped by `log`/`undo` instead of
breaking them for every project, and refused by `restore`.

Ignored on every snap and by the watch daemon: `*.log`, `*transcript*`,
`__pycache__`, `.git`, `node_modules` (transcripts would otherwise balloon
the snapshots repo) and - directories only, so a script called `build` is
kept - Python virtual environments (`.venv`, or any directory with a
`pyvenv.cfg`) and the `dist`, `build` and `target` directories that sit next
to a build manifest (`package.json`, `pyproject.toml`, `setup.py`,
`setup.cfg`, `Cargo.toml`, `pom.xml`, `build.gradle`, `build.gradle.kts`,
`build.sbt`, `CMakeLists.txt`, `Makefile`, `meson.build`, `go.mod` or
`tsconfig.json`); a `rules/build/` of yours is never left out because of its
name.

A file found inside a watched directory that is bigger than 10 MB is left out
too: `init`, `snap`, `precompact` and the `watch` daemon say so in one
`warning:` line on stderr that names it, and `SESSIONSTEWARD_MAX_FILE_MB`
changes the limit (`0` lifts it, and so does a number too big to count, such
as `1e305`). A file you name yourself with `watch --add` is never left out
for its size, even where it lies inside a watched folder, and is never
reported as left out; naming the folder does not name the files in it. None
of the things left out is in a snapshot, so `undo` cannot bring one back if
you delete it yourself; a restore does not take them away (it leaves them in
place, see above).

**Who can read the store.** `~/.sessionsteward` (the snapshots repo, the
trash, the config and the lock) is created readable by you alone (mode 0700),
and an older one that other users could enter is tightened the next time a
command that writes runs; a mirrored file never gets more access than your
own (a 0600 file is mirrored 0600), and a restore never gives your own files
more access than they had either (see what a snapshot holds). Everything you
watch is copied, so a secret in `CLAUDE.local.md` is in the store too.
Windows has no such mode: the folder inherits the permissions of your
profile.

Mirrors live under `~/.sessionsteward/snapshots/files/` at their
project-relative path (`__home/…` for files elsewhere under your home
directory, `__abs/<drive>/…` for anything else), so a mirror can never
land outside the repo. On Windows that puts a few extra path components
under `%USERPROFILE%`: a very deeply nested project can hit the 260-char
`MAX_PATH` limit unless long paths are enabled (`git config --global
core.longpaths true` plus Windows' `LongPathsEnabled`). Restoring a folder
unpacks the copy next to it first, which adds about 20 characters to the
longest path inside it.

A corrupt `~/.sessionsteward/config.json` is reported with its path, as one
`error:` line (exit 2), and left in place - your watch list is never
silently replaced. Corrupt means: not JSON; not UTF-8 (UTF-16, which is what
PowerShell 5's `>` writes, gets a hint to save it as UTF-8; a UTF-8
byte-order mark is fine); or a `watch` or `unwatch` that is not a list of
paths (`"watch": "NOTES.md"` is refused, not read letter by letter).

## Links

Symlinks inside a watched folder are followed (the snapshot holds the content
behind them) with these limits: a link to the watched folder itself, to one
of its parents, or to a folder already walked is skipped (no loops); walks go
at most 64 folders deep; one snapshot holds at most 500 MB of walked files
(`SESSIONSTEWARD_MAX_SNAPSHOT_MB`, 0 = no cap); and **a link that leads
outside the watched folder is skipped**, so
`.claude/out -> ~/.ssh` cannot copy your keys into the snapshot store. Windows
junctions count as links (also on a Python that cannot tell them from folders:
anything that resolves outside the watched folder is treated alike). Loops and
repeats are skipped quietly; the other three - a file or folder over the
limits, a link leading outside, a folder deeper than 64 levels - are named in
one `warning:` line each on stderr, with a count and the first one, every time
a snapshot leaves something out. A restore leaves those items where they are.
If you
keep real content behind such links (a dotfiles repo), opt in with
`sessionsteward watch --follow-links` (or `SESSIONSTEWARD_FOLLOW_LINKS=1`);
`--no-follow-links` turns it off. A watched entry that is itself a link is
always followed.

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

The hooks call the installed `sessionsteward` command by absolute path
(for example `/path/to/venv/bin/sessionsteward snap ...`), because hook
shells don't load your aliases or activate your venv. They deliberately do
not use `python -m sessionsteward`: Claude Code runs hooks with the project
as the working directory, and `python -m` would then import a
`sessionsteward/` package from the project, so a repository you just cloned
could run code at session start. (Only when the command cannot be found - a run from a source checkout -
does `install-hooks` fall back to `python -I -m sessionsteward`, where `-I`
keeps the project folder off the import path. It says so in a `warning:`
line, because that form works only while sessionsteward is installed for that
Python, so run `pip install .` in the checkout first; otherwise the hooks
fail silently.) Each hook has a 30 second timeout.
Re-run `install-hooks --write` if you move or recreate that environment, and
once after upgrading from 0.1.3 or earlier: the hooks sessionsteward wrote
itself (`sessionsteward precompact` and `sessionsteward snap -m "session
start"` as the first version wrote them, the same commands run as
`<python> -m sessionsteward ...`, or run by the console script's path) are
rewritten in place, never duplicated. Every other hook is yours and is left alone, even one that
mentions sessionsteward (`cd ~/code/sessionsteward && git pull`,
`sessionsteward snap -m checkpoint`); `--write` adds the two hooks beside
it. It merges into the existing settings file without clobbering your other
hooks, and re-running is a no-op (nothing is written when the hooks are
already there).

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
where a directory belongs). Whatever else goes wrong - a bug in
sessionsteward, input nobody foresaw - is also one line, `error: unexpected
...`, exit code 1, never a traceback.

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
python -m unittest discover -s tests   # temp dirs only; never touches ~/.sessionsteward
```

Every test runs against a temporary HOME; the newer ones also give git a
private config and remove every `GIT_*` variable, and everything a run
creates sits in one directory under `TMPDIR` that is removed when the run
ends. The tests for read-only files and directories are skipped when run as
root, where permission bits don't bind; tests that need POSIX features
(symlinks, named pipes, execute and permission bits, shell-script hooks) are
skipped on Windows; the tests that rename a file by case alone, or list a
path in another case, on the file system itself are skipped wherever the temp
directory is case-sensitive (set `TMPDIR` to a case-insensitive mount to run
them on Linux: ciopfs, mounted with `-o
use_ino,entry_timeout=0,attr_timeout=0,negative_timeout=0` so that both
spellings of a file report one file id, as NTFS and APFS do, and nothing is
cached across a rename). The same cases also run everywhere with only the
lookup of a path in another case emulated.

## License

MIT — see [LICENSE](LICENSE).
