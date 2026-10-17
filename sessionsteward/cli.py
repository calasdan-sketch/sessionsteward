"""Session steward: versioned snapshots for agent working state.

Coding agents silently degrade: compaction drops constraints, and one
"remember this" can overwrite your 30-line CLAUDE.md with a single
bullet — no warning, no undo. sessionsteward git-versions your agent
config + memory files on every change. Instant undo, pre-compaction
snapshots, and a context-diet report.

Scope note: this is *session ops* (snapshots, undo, compaction guard).
Memory *storage* is OneMemory's job — the boundary stays clean.

  sessionsteward init                  # set up the snapshots repo
  sessionsteward watch --add NOTES.md  # watch extra paths (also: ~/..., /abs/...)
  sessionsteward watch --remove AGENTS.md  # stop watching one (defaults too)
  sessionsteward snap -m "before refactor"
  sessionsteward precompact            # snapshot before compaction
  sessionsteward log                   # list snapshots
  sessionsteward diff [sha1] [sha2]    # what changed (e.g. what compaction dropped)
  sessionsteward undo                  # restore the previous snapshot
  sessionsteward restore <sha>         # restore a specific snapshot
  sessionsteward diet                  # what's eating the context window
  sessionsteward install-hooks [--write]  # Claude Code hooks JSON
  sessionsteward watch                 # daemon: auto-snapshot on change
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import core as core_mod
from . import diet as diet_mod

VERSION = "0.1.0"

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")


def _harden_streams() -> None:
    """Never traceback on a console that cannot encode a path (Windows
    cp1252 pipes, LANG=C): a strict stdout/stderr replaces unencodable
    characters instead of raising UnicodeEncodeError mid-report."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or getattr(stream, "errors", None) != "strict":
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def cmd_init(args) -> int:
    repo = core_mod.init_repo()
    print(f"snapshots repo: {repo}")
    print("watching: " + ", ".join(core_mod.watch_list()))
    print("add more: sessionsteward watch --add <path>")
    return 0


def cmd_watch_edit(args) -> int:
    core_mod.init_repo()
    cfg = core_mod.edit_watch_list(core_mod.load_config(),
                                   add=args.add or [], remove=args.remove or [])
    core_mod.save_config(cfg)
    print("watching: " + ", ".join(core_mod.watch_list(cfg)))
    return 0


def _guard(root: str, sha: str) -> None:
    snaps = core_mod.log(2, project_root=root)
    if len(snaps) == 2 and snaps[0]["sha"] == sha:
        for line in core_mod.guard_lines(sha, snaps[1]["sha"]):
            print(line)


def cmd_snap(args) -> int:
    sha = core_mod.snap(args.root, args.message or "manual snapshot")
    print(f"snapshot {sha[:7]}")
    _guard(args.root, sha)
    return 0


def cmd_precompact(args) -> int:
    sha = core_mod.snap(args.root, "pre-compaction snapshot")
    print(f"pre-compaction snapshot {sha[:7]} — "
          f"`sessionsteward diff {sha[:7]}` after, to see what was dropped")
    _guard(args.root, sha)
    return 0


def cmd_log(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet — run `sessionsteward init` first")
        return 0
    snaps = core_mod.log(args.limit + 1, project_root=args.root)
    for i, s in enumerate(snaps[:args.limit]):
        print(f"{s['sha'][:7]}  {s['date'][:16]}  {s['message']}")
        if i + 1 < len(snaps):
            for line in core_mod.guard_lines(s["sha"], snaps[i + 1]["sha"]):
                print(line)
    return 0


def cmd_diff(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet")
        return 2
    snaps = core_mod.log(50, project_root=args.root)
    sha1 = args.sha1 or (snaps[1]["sha"] if len(snaps) > 1 else None)
    sha2 = args.sha2 or "HEAD"
    if not sha1:
        print("only one snapshot — nothing to diff")
        return 0
    out = core_mod.diff(sha1, sha2)
    kept = [ln for ln in out.splitlines() if ln[:1] in "+-"
            and not ln.startswith(("+++", "---"))]
    sys.stdout.write("\n".join(kept[:200]) + ("\n" if kept else "(no changes)\n"))
    return 0


def cmd_undo(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet")
        return 2
    snaps = core_mod.log(50, project_root=args.root)
    if not snaps:
        print("nothing to restore — no snapshots yet")
        return 1
    if core_mod.worktree_matches_snapshot(snaps[0]["sha"], args.root):
        # Clean state: the current state is already snapshotted, so undo
        # needs no pre-snap and redo is just `restore` of snaps[0].
        if len(snaps) < 2:
            print("only one snapshot — nothing to undo to")
            return 0
        target = snaps[min(args.steps, len(snaps) - 1)]["sha"]
    else:
        # Dirty state: snapshot it first ("pre-undo") so undo is
        # reversible — redo is `restore` of the pre-undo snapshot.
        core_mod.snap(args.root, "pre-undo")
        snaps = core_mod.log(50, project_root=args.root)
        target = snaps[min(args.steps, len(snaps) - 1)]["sha"]
    try:
        restored = core_mod.restore(target, project_root=args.root)
    except core_mod.NothingToRestoreError as e:
        print(e)
        return 1
    except core_mod.CrossProjectError as e:
        print(f"error: {e}")
        return 1
    print(f"undone {args.steps} step(s) -> {target[:7]}")
    for r in restored:
        print(f"  {r}")
    return 1 if core_mod.skipped(restored) else 0


def cmd_restore(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet")
        return 2
    # Validate before the pre-restore snap so a bad sha doesn't pollute log.
    try:
        core_mod.check_restore(args.sha, args.root)
    except core_mod.NothingToRestoreError as e:
        print(e)
        return 1
    except core_mod.CrossProjectError as e:
        print(f"error: {e}")
        return 1
    # Redo support: if the current state isn't snapshotted yet, snapshot
    # it first ("pre-restore") so the restore is reversible.
    snaps = core_mod.log(50, project_root=args.root)
    if snaps and not core_mod.worktree_matches_snapshot(snaps[0]["sha"],
                                                        args.root):
        core_mod.snap(args.root, "pre-restore")
    restored = core_mod.restore(args.sha, project_root=args.root)
    print(f"restored {args.sha[:7]}")
    for r in restored:
        print(f"  {r}")
    return 1 if core_mod.skipped(restored) else 0


def cmd_diet(args) -> int:
    print(diet_mod.render(diet_mod.analyze(args.root)))
    return 0


def cmd_install_hooks(args) -> int:
    payload = core_mod.build_hooks_json()
    print(json.dumps(payload, indent=2))
    print()
    print("Copy the JSON above into the \"hooks\" section of "
          "~/.claude/settings.json, or re-run with --write to merge it "
          "automatically (existing settings are preserved).")
    print(f"Wiring: PreCompact -> `{core_mod.HOOK_PRECOMPACT_CMD}`, "
          f"SessionStart -> `{core_mod.HOOK_SESSIONSTART_CMD}`.")
    if args.write:
        path = core_mod.write_hooks_file(CLAUDE_SETTINGS)
        print(f"merged hooks into {path}")
    return 0


def cmd_watch(args) -> int:
    core_mod.init_repo()
    print(f"watching for changes (every {args.interval}s, Ctrl-C to stop)…")
    mtimes = core_mod.file_mtimes(core_mod.watched_paths(args.root))
    try:
        while True:
            time.sleep(args.interval)
            now = core_mod.file_mtimes(core_mod.watched_paths(args.root))
            if now != mtimes:
                sha = core_mod.snap(args.root, "auto: files changed")
                print(f"change detected -> snapshot {sha[:7]}")
                _guard(args.root, sha)
                mtimes = now
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sessionsteward", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--root", default=os.getcwd(),
                   help="Project root for relative watch paths.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Set up the snapshots repo.")
    wa = sub.add_parser("watch", help="Watch daemon / manage watch list.")
    wa.add_argument("--add", nargs="+", default=None,
                    help="Add paths to the watch list (then exit). "
                         "The defaults stay watched.")
    wa.add_argument("--remove", nargs="+", default=None,
                    help="Drop paths from the watch list, defaults "
                         "included (then exit).")
    wa.add_argument("--interval", type=float, default=5.0,
                    help="Poll seconds for the daemon.")
    sn = sub.add_parser("snap", help="Snapshot now.")
    sn.add_argument("-m", "--message", default=None)
    sub.add_parser("precompact", help="Snapshot before compaction.")
    lg = sub.add_parser("log", help="List snapshots.")
    lg.add_argument("--limit", type=int, default=20)
    df = sub.add_parser("diff", help="Diff two snapshots.")
    df.add_argument("sha1", nargs="?")
    df.add_argument("sha2", nargs="?", default="HEAD")
    un = sub.add_parser("undo", help="Restore the previous snapshot.")
    un.add_argument("--steps", type=int, default=1)
    rs = sub.add_parser("restore", help="Restore a specific snapshot.")
    rs.add_argument("sha")
    sub.add_parser("diet", help="Context-diet report.")
    ih = sub.add_parser("install-hooks",
                        help="Print (or --write) Claude Code hooks JSON.")
    ih.add_argument("--write", action="store_true",
                    help="Merge the hooks into ~/.claude/settings.json.")
    return p


def main(argv=None) -> int:
    _harden_streams()
    args = build_parser().parse_args(argv)
    if args.cmd == "watch" and (args.add or args.remove):
        return cmd_watch_edit(args)
    cmds = {
        "init": cmd_init, "watch": cmd_watch, "snap": cmd_snap,
        "precompact": cmd_precompact, "log": cmd_log, "diff": cmd_diff,
        "undo": cmd_undo, "restore": cmd_restore, "diet": cmd_diet,
        "install-hooks": cmd_install_hooks,
    }
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
