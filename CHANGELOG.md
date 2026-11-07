# Changelog

## 0.3.0 (unreleased)

Round 3 (hostile-input and fail-open fixes)

Fixed
- Python 3.10 and 3.11 could not import the package: an f-string in
  `core.py` held a backslash in its braces (a SyntaxError before 3.12).
  A test now checks every source file for it.
- The secret scanner took quadratic time on one long line: 20,000
  characters of `a.a.a.` cost 47 s, and it ran on every `snap`, so a cloned
  repo's CLAUDE.md could stall every SessionStart hook for minutes. The
  assignment pattern is linear now and long lines are scanned in overlapping
  pieces.
- A secret more than 20,000 characters into a line was neither found by
  `scan` nor removed by `show --redact` (the rest of the line was printed
  unredacted).
- `lost`, `scan` (and `snap`'s warning) skipped files over 1 MiB, UTF-16
  files and binary files without saying so, then reported "nothing lost" or
  "no secrets". They now list what they did not compare, UTF-16 with a BOM
  is read, the size limit is 4 MiB (what Claude Code loads), and
  `lost --check` exits 3 while a file is unreadable. JSON keeps its shape
  (`lost --json` gains `not_compared`).
- A link inside `.claude/` to a huge folder was copied on every snapshot
  (3 GB, two-minute timeout). A snapshot now refuses past 20,000 files or
  256 MB (env vars lift or change it); reading commands are unaffected.
- A snapshot message or file name with an escape sequence or a bidirectional
  control reached the terminal raw through `log`, `status`, `undo` and the
  guard lines; `safe()` now also covers bidi controls.
- `diet --lint` silently analysed only the first 20,000 lines; it says so now.

Added
- `undo --dry-run`, `restore <sha> --dry-run`: list what would be rewritten,
  recreated or moved to the trash; takes no snapshot, changes nothing.
- `prune [--keep N] [--write]`: forget old snapshots and delete their
  objects (see README, "Forgetting history").
- Secret shapes: npm, GitLab, Hugging Face, SendGrid and Google OAuth tokens
  (shape only; lengths are the common ones, not a spec check).
- `bench/`: worst-case scan line and a far-token recall check.

Added
- `sessionsteward lost [sha1 [sha2]] [--live] [--json] [--check]`: what
  compaction or an agent edit really dropped. Compares instructions, not
  lines (list markers, case, emphasis ignored): kept, moved to another file,
  reworded (paired with the new wording), reversed (a "do" that became a "do
  not"), dropped (with the tokens it cost).
- `sessionsteward pin "<rule>"`, `pin --check`, `pin --remove`: `snap`,
  `precompact` and `status` report a pinned line that no watched file holds.
- `diet --lint`: lines repeated inside a file, near-duplicate lines (grouped),
  do-versus-do-not on one topic, and backticked paths that do not exist, each
  with its token cost; with `--check` they fail the check.
- `diet --fix` (preview) and `diet --fix --write`: remove repeated lines and
  extra blank lines. Takes a snapshot first and another after, keeps CRLF and
  file modes, writes through a temporary file, skips links, unwatched files
  and files that changed since they were read.
- `diet --history [N]`: session-start token cost at each snapshot, jumps marked.
- `sessionsteward scan [--sha SHA]` and a one-line `snap`/`precompact`
  warning when a watched file holds a secret-shaped value (provider key
  prefixes, private-key headers, `password = <random value>`); previews are
  four characters. `show <sha> <file> --redact`.
- `bench/`: an offline, seeded benchmark (`python bench/run.py [--check]`);
  `tests/test_expert2.py` runs it with floors on each headline number.

Changed
- `diet` no longer prints a secret-shaped value in the duplicate list.
- config.json may hold `"pins"` (a list of lines); a bad value is a one-line
  error like the other keys.

Not changed: every existing command and exit code, except `lost --check` (3 when a file could not be compared) and a refused oversized snapshot (2).

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
