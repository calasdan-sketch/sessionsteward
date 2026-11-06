# Changelog

## 0.2.0 (unreleased)

Added
- `sessionsteward status`: one read-only screen - newest snapshot and its age,
  whether your files are that snapshot, an older one (after `undo`) or have
  unsaved changes (listed as modified / added / removed), whether the
  SessionStart and PreCompact hooks are installed, and the shrink guard.
- `sessionsteward show <sha> [file]`: list a snapshot's files, or print one
  file byte-exact to stdout - recover a single file without a restore.
  Only hex shas are accepted; another project's snapshot is refused.
- `diet` now counts what Claude Code actually loads, per its memory docs:
  `@path` imports (recursive, four hops, code spans and fences skipped,
  `\ ` escapes), only the first 200 lines / 25 KB of `MEMORY.md`, and
  `paths:`-scoped rules listed apart as on demand (front matter not counted).
- `diet` warnings: files over 200 lines, files over 4 MiB (skipped by Claude
  Code), missing `@...md` imports, imports too deep, a truncated MEMORY.md.
- `diet` lists imported files that no watched path covers, with the
  `watch --add` command that protects them.
- `diet --check` and `diet --budget TOKENS` (exit 1, reasons on stderr) for CI
  and pre-commit; `diet --json` for scripts.

Changed
- `diet` shows paths project-relative (`~/...` under home) instead of
  absolute; `--json` keeps them absolute.
- The `total_chars` of `diet` no longer includes path-scoped rules or the part
  of MEMORY.md past its cut-off, and does include imported files.

Security
- Lines of a file that a project's CLAUDE.md imports from outside the project
  are never quoted in the duplicate report.

## 0.1.3 and earlier

See the git history.
