# Security

## Reporting

Please report a vulnerability privately through GitHub: Security tab >
"Report a vulnerability" on https://github.com/calasdan-sketch/sessionsteward
(a private advisory). If you cannot, email calasdan@gmail.com. Please do not
open a public issue for a vulnerability. You will get an answer within a few days.

## Scope and supported versions

In scope: a way for untrusted input (a repository, a config file, a snapshot
store you did not make) to make sessionsteward run code or write or read
outside what it is documented to touch, or to leak a secret. Out of scope:
anything that needs an attacker who already controls your account or machine,
and what the README lists under its limits. Only the latest release on PyPI
receives fixes.

## What to know before you rely on sessionsteward

- **Hooks run the installed command, not the project's code.** `install-hooks
  --write` writes the absolute path of the `sessionsteward` console script.
  Before 0.1.4 it wrote `python -m sessionsteward`, which runs a
  `sessionsteward/` package found in the project folder (Claude Code runs
  hooks with the project as the working directory). Upgrade, then re-run
  `sessionsteward install-hooks --write`.
- **Restore trusts the snapshot store.** `undo` and `restore` write files at
  the absolute paths recorded in the snapshot's `manifest.json`
  (`~/.sessionsteward/snapshots`). Anyone who can write to that folder can
  therefore make a restore write to any path you can write to. Keep it private
  (sessionsteward creates it owner-only, where the OS supports that), do not
  share it, do not restore from a copy of a store you did not make, and do not
  put it in a synced or shared folder. Restore checks that its own mirror
  paths stay inside the store, refuses a snapshot taken for another project,
  and never writes through symlinks; it does not validate the destination
  paths in the manifest.
- **Snapshots hold the content of your memory files.** A secret you paste into
  `CLAUDE.md` is in every snapshot. Links that lead outside a watched folder
  are not followed unless you opt in (`watch --follow-links`).
- **Walk limits.** A snapshot walks at most 64 folders deep and 500 MB of
  files (`SESSIONSTEWARD_MAX_SNAPSHOT_MB`), and never follows a link to the
  watched folder, a parent or a folder already walked.
