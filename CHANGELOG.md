# Changelog

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
