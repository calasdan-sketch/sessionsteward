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
- A file of the project whose real location is outside it (an import of
  `~/...`, or a rule that is a symlink out of the project) is never quoted
  in the duplicate report (see the fixes below for the symlink case).

Panel review fixes (before release)
- A symlinked rule pointing outside the project was counted as loaded and
  its lines could appear under "Duplicated content". Any project file whose
  real path is outside the project is now external: listed apart as needing
  approval, not counted, not quoted, its imports not followed.
- Control characters (C0 and C1, bar tab) in paths, lines and warnings are
  shown as `?` in `diet`, its `check:` lines, `status` and `show`'s listing;
  `--json` escapes non-ASCII.
- `watch --add` is no longer suggested for imported files outside the
  project and `~/.claude` (they may be secrets); they are only counted.
- Rules files are recognised from the `.claude` folder down: a project
  under a folder named `rules` no longer counts `.claude/commands/` as
  context.
- Missing-import warnings are reported once per (file, import) and at most
  20 per file, then a count.
- Parser: fences behind list markers or `>` and indented fences, indented
  code blocks, block-level HTML comments (stripped, as Claude Code strips
  them), and rules front matter that does not parse (loads as if it had no
  `paths`, with a warning).
- `diet --check` / `--budget` failures exit 3, distinct from 1 (refused I/O
  or an unexpected error) and 2 (usage, git, config). Files that cannot be
  read are a warning instead of being skipped silently. The 200-line warning
  applies only to top-level CLAUDE.md, CLAUDE.local.md and AGENTS.md files.
- README examples use neutral names.

## 0.1.3 and earlier

See the git history.
