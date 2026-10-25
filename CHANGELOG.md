# Changelog

## 0.2.0

- Shrink guard in the session: `install-hooks --write` now also adds a `PreToolUse` hook (Write, Edit, MultiEdit) that, before an edit would make a watched file more than 50% smaller, takes a snapshot and asks you (or warns Claude, or refuses: `guard set --mode`), and a `FileChanged` hook for `CLAUDE.md`, `CLAUDE.local.md` and `AGENTS.md` that warns you after any change. `guard status` and `guard set --percent N --mode ask|warn|block`. The README lists what each event cannot do.
- Reminder after a compaction: a `SessionStart` hook with the matcher `compact` adds a short note to Claude's context (`sessionsteward remind`). `PostCompact` is not used because its output is discarded.
- Secrets are left out of snapshots: a file that looks like it holds a key, token or password is skipped with a warning that names the file and the kind of secret, never the secret. `undo` and `restore` leave such a file alone. `SESSIONSTEWARD_SECRET_SCAN=0` turns it off.
- `sessionsteward why <older> [newer]`: which instruction lines are no longer anywhere in their file between two snapshots, per file.
- `sessionsteward prune [--keep N] [--days D] [--save] [--yes]`: remove old snapshots by a keep-N / keep-D-days rule; reports first, never runs by itself.
- Hooks already installed keep working; run `install-hooks --write` again to add the new ones.

## 0.1.5

- Release workflow: the step that checks the tag against the version never ran (its script was folded into one line by the YAML, so a wrong tag could still publish). It is now a script, `.github/scripts/release_check.py`, that compares the tag with the version in the built files and refuses a tag whose commit is not on `main`. CI builds the source archive and runs the tests from the unpacked archive; Python 3.14 is in the CI matrix.
- Tests no longer fail when the `sessionsteward` command is installed next to the Python that runs them.
- A snapshot now says what it leaves behind that you did not ask it to: links leading outside a watched folder and folders more than 64 levels deep are counted in a `warning:` line (next to the existing size warnings).
- `undo` and `restore` no longer take away what a snapshot cannot hold. A restore still replaces a watched folder as a whole, but ignored names (a skill's own `.git`, `node_modules`, a virtual environment), links leading outside, files over the size limit and other things no snapshot contains are put back where they were (`kept …: not part of any snapshot`). Before, they ended up only in the trash.
- Windows junctions are treated like symlinks, and anything that resolves outside the watched folder is treated alike on a Python that cannot see a junction.
- `install-hooks` warns when it falls back to `python -I -m sessionsteward` (a source checkout): that works only while the package is installed.
- `diff` works with git older than 2.24 (no `--end-of-options`); README states the requirement.

## 0.1.4

Security release. **Re-run `sessionsteward install-hooks --write` after
upgrading** (it rewrites your existing hooks in place).

- Hooks no longer run `python -m sessionsteward`, which imports a
  `sessionsteward/` package from the project folder (Claude Code's working
  directory for hooks), so a cloned repository could run code at session start.
  They call the console script by absolute path (fallback
  `python -I -m sessionsteward`) and have a 30 second timeout. Old hooks are
  still recognised and rewritten in place.
- A symlink loop (`ln -s . .claude/a`, repeated) no longer multiplies the
  snapshot (it filled hundreds of MB in seconds while holding the lock). Links
  to the watched folder, a parent or an already-walked folder are skipped; walks
  stop at 64 folders deep and 500 MB per snapshot with a warning
  (`SESSIONSTEWARD_MAX_SNAPSHOT_MB`).
- Links that lead outside a watched folder are no longer copied into the store
  (`.claude/out -> ~/.ssh`). Opt in with `watch --follow-links` or
  `SESSIONSTEWARD_FOLLOW_LINKS=1`.
- `diff` and `restore` accept only hex snapshot ids (4-64 digits) or `HEAD`, and
  pass `--end-of-options` to git; `undo --steps 0` or below is an error.
- sdist includes `tests/__init__.py`, `tests/_isolate.py` and the other test
  files (new `MANIFEST.in`) so the tests run from the unpacked source.
- New `SECURITY.md` (restore trusts the manifest's paths), GitHub Actions CI and
  trusted-publishing release workflow, more package metadata.

## 0.1.3 and earlier

See the git history.
