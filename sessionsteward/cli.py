"""Session steward: versioned snapshots for agent working state.

Coding agents silently degrade: compaction drops constraints, and one
"remember this" can overwrite your 30-line CLAUDE.md with a single
bullet — no warning, no undo. sessionsteward git-versions your agent
config + memory files on every change. Instant undo, pre-compaction
snapshots, and a context-diet report.

Scope note: this is *session ops* (snapshots, undo, compaction guard).
Memory *storage* is OneMemory's job — the boundary stays clean.

  sessionsteward init                  # set up the snapshots repo + first snapshot
  sessionsteward watch --add NOTES.md  # watch extra paths (also: ~/..., /abs/...)
  sessionsteward watch --remove AGENTS.md  # stop watching one (defaults too)
  sessionsteward snap -m "before refactor"
  sessionsteward precompact            # snapshot before compaction
  sessionsteward log                   # list snapshots
  sessionsteward diff [sha1] [sha2]    # what changed (e.g. what compaction dropped)
  sessionsteward undo                  # restore the previous snapshot
  sessionsteward restore <sha>         # restore a specific snapshot
  sessionsteward status                # newest snapshot, unsaved changes, hooks
  sessionsteward show <sha> CLAUDE.md  # print one file from a snapshot
  sessionsteward diet                  # what's eating the context window
  sessionsteward diet --check          # exit 3 on warnings (CI / pre-commit)
  sessionsteward install-hooks [--write]  # Claude Code hooks JSON
  sessionsteward watch                 # daemon: auto-snapshot on change
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import json
import os
import subprocess
import sys
import time

from . import __version__
from . import core as core_mod
from . import diet as diet_mod

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")

# `watch --interval`, in seconds: a daemon that polls more often than this
# only burns CPU walking the watched files, and one that sleeps for more than
# a day (or for ever) is not watching.
MIN_INTERVAL = 0.1
MAX_INTERVAL = 86400.0


def _harden_streams() -> None:
    """Never traceback on a console that cannot encode a path (Windows
    cp1252 pipes, LANG=C): a strict stdout/stderr replaces unencodable
    characters instead of raising UnicodeEncodeError mid-report."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        # Both handlers raise UnicodeEncodeError on a character the code
        # page lacks: "strict" (the default) and "surrogateescape" (what a
        # piped stdout reports on Windows 11 / Python 3.11, with cp1252).
        if reconfigure is None or getattr(stream, "errors", None) not in (
                "strict", "surrogateescape"):
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


# Tests patch this to exercise the Windows branch on any OS.
_WINDOWS = os.name == "nt"


def _pipe_gone(exc: BaseException) -> bool:
    """Did the reader of our stdout go away?

    POSIX raises BrokenPipeError (EPIPE). Windows reports the same thing as
    a plain OSError with errno EINVAL and no file name (measured on Windows
    11 / Python 3.11: errno 22, winerror None), raised by the FLUSH rather
    than by the print, and it fails a second time at interpreter exit
    (status 120) unless fd 1 is pointed at the null device first."""
    if isinstance(exc, BrokenPipeError):
        return True
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.EPIPE:
        return True
    return (_WINDOWS and exc.errno == errno.EINVAL
            and getattr(exc, "filename", None) is None)


def _stdout_gone() -> None:
    """The reader of our stdout went away (`sessionsteward log | head`):
    there is nothing left to say, so say it quietly. Python flushes
    stdout once more at exit, which would raise the same BrokenPipeError
    again — point the descriptor at the null device first."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:  # no real descriptor (captured stdout): nothing to do
        pass


def _short(path: str, root: str) -> str:
    """A path as the user knows it: project-relative inside the project,
    ~/... under home, otherwise as is."""
    root = os.path.abspath(root)
    home = os.path.abspath(os.path.expanduser("~"))
    for base, prefix in ((root, ""), (home, "~" + os.sep)):
        if path == base:
            return prefix.rstrip(os.sep) or "."
        if path.startswith(base.rstrip(os.sep) + os.sep):
            return prefix + path[len(base.rstrip(os.sep)) + 1:]
    return path


def _print_restored(restored: list[str], root: str) -> None:
    """One short line per path: `restored CLAUDE.md`, `unchanged .claude`,
    `moved .claude -> ~/.sessionsteward/trash/.claude.1760000000`,
    `kept NOTES.md: not watched when snapshot a3f9c21 was taken`. Keeps
    the undo/restore report (and hook output) to the essentials."""
    for r in restored:
        for tag in ("moved ", "unchanged ", "skipped ", "kept "):
            if r.startswith(tag):
                rest = r[len(tag):]
                if tag == "moved ":
                    src, _, dest = rest.partition(" -> ")
                    rest = f"{_short(src, root)} -> {_short(dest, root)}"
                elif tag in ("skipped ", "kept "):
                    path, _, why = rest.partition(": ")
                    rest = f"{_short(path, root)}: {why}"
                else:
                    rest = _short(rest, root)
                print(f"  {tag}{rest}")
                break
        else:
            print(f"  restored {_short(r, root)}")


def _warn(message: str) -> None:
    """A warning on stderr (stdout is what a hook feeds the agent). Not being
    able to print one never fails the command that already did its work."""
    try:
        print(f"warning: {message}", file=sys.stderr)
    except OSError:
        pass


def _warn_too_big(too_big: list, root: str) -> None:
    """One line about the files a snapshot left out for being over the size
    limit - a file that is silently missing from a snapshot is a file that
    `undo` cannot bring back."""
    if not too_big:
        return
    limit = core_mod.max_file_bytes()
    over = f"over the {limit / 1048576:g} MB limit" if limit else "too big"
    path, size = too_big[0]
    first = f"{_short(path, root)} ({size / 1048576:.1f} MB)"
    if len(too_big) == 1:
        what = f"not snapshotted, {over}: {first}"
    else:
        what = (f"{len(too_big)} files {over} were not snapshotted "
                f"(first: {first})")
    _warn(f"{what}; SESSIONSTEWARD_MAX_FILE_MB changes the limit")


def _warn_unwatchable(root: str) -> None:
    """One warning per watch-list entry that cannot be watched (`.`, `~`: see
    core.unwatchable_reason), naming the command that drops it. The command
    goes on with the rest of the list - a hook must keep protecting what it
    can. An unreadable config is not mentioned here: the command reports it."""
    try:
        bad = core_mod.unwatchable_entries(root)
    except core_mod.StewardError:
        return
    for entry, what in bad:
        drop = core_mod.shell_word(entry)
        _warn(f"not watching '{entry}': it is {what}; "
              f"`sessionsteward watch --remove {drop}` drops the entry")


def cmd_init(args) -> int:
    repo = core_mod.init_repo()
    print(f"snapshots repo: {repo}")
    print("watching: " + ", ".join(core_mod.watch_list()))
    if core_mod.log(1, project_root=args.root):
        pass  # re-run on a project that already has snapshots
    elif core_mod.watched_paths(args.root):
        # A first snapshot right away, so `undo` has somewhere to go the
        # moment the agent first clobbers a file.
        sha, too_big = core_mod.snap_report(args.root, "initial snapshot")
        print(f"snapshot {sha[:7]} (initial)")
        _warn_too_big(too_big, args.root)
    else:
        print(f"nothing to snapshot yet: no watched file exists in "
              f"{os.path.abspath(args.root)}")
    print("add more: sessionsteward watch --add <path>")
    return 0


def cmd_watch_edit(args) -> int:
    # before anything is created or written: a refused entry changes nothing
    core_mod.check_watch_additions(args.add or [], args.root)
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
    sha, too_big = core_mod.snap_report(args.root,
                                        args.message or "manual snapshot")
    print(f"snapshot {sha[:7]}")
    _warn_too_big(too_big, args.root)
    _guard(args.root, sha)
    return 0


def cmd_precompact(args) -> int:
    sha, too_big = core_mod.snap_report(args.root, "pre-compaction snapshot")
    print(f"pre-compaction snapshot {sha[:7]} — "
          f"`sessionsteward diff {sha[:7]}` after, to see what was dropped")
    _warn_too_big(too_big, args.root)
    _guard(args.root, sha)
    return 0


def cmd_log(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet — run `sessionsteward init` first")
        return 0
    snaps = core_mod.log(args.limit + 1, project_root=args.root)
    if not snaps:
        print(f"no snapshots yet for {os.path.abspath(args.root)} — "
              f"`sessionsteward snap` takes one")
        return 0
    # The "undo to recover" hint is only true while the live files ARE
    # the shrunken top snapshot; after a restore it would re-clobber.
    at_top = core_mod.worktree_matches_snapshot(snaps[0]["sha"], args.root)
    for i, s in enumerate(snaps[:args.limit]):
        print(f"{s['sha'][:7]}  {s['date'][:16]}  {s['message']}")
        if i + 1 < len(snaps):
            hint = ("undo" if at_top else None) if i == 0 else "restore"
            for line in core_mod.guard_lines(s["sha"], snaps[i + 1]["sha"],
                                             hint=hint):
                print(line)
    return 0


def _changed_lines(text: str) -> list[str]:
    """The added and removed lines of a unified diff, without its file
    headers. Headers are recognised by where they are (between `diff --git`
    and the first `@@`), not by how they start: a changed line that itself
    begins with `--` or `++` - a removed markdown rule or front-matter
    fence, `---` - looks exactly like a header. Only "\n" splits the text
    (str.splitlines also splits on U+2028 and friends, which may sit inside
    a changed line); a CR before it belongs to a CRLF file's line ending."""
    changed = []
    in_hunk = False
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-"):
            changed.append(line)
    return changed


DIFF_LINE_LIMIT = 200


def cmd_diff(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet")
        return 2
    snaps = core_mod.log(50, project_root=args.root)
    if not snaps:
        print("no snapshots yet")
        return 2
    sha1 = args.sha1 or (snaps[1]["sha"] if len(snaps) > 1 else None)
    # Default to THIS project's newest snapshot, never global HEAD: the
    # repo is shared, and HEAD may be another project's snapshot.
    sha2 = args.sha2 or snaps[0]["sha"]
    if not sha1:
        print("only one snapshot — nothing to diff")
        return 0
    out = core_mod.diff(sha1, sha2)
    kept = _changed_lines(out)
    if not kept:
        # nothing to show as lines is not the same as nothing changed
        sys.stdout.write("(no line changes - binary files or file modes "
                         "only)\n" if out.strip() else "(no changes)\n")
        return 0
    text = "\n".join(kept[:DIFF_LINE_LIMIT])
    if len(kept) > DIFF_LINE_LIMIT:
        text += (f"\n... {len(kept) - DIFF_LINE_LIMIT} more changed "
                 f"line(s) not shown")
    sys.stdout.write(text + "\n")
    return 0


def _restore_errors(fn):
    try:
        return fn(), 0
    except core_mod.NothingToRestoreError as e:
        print(e)
        return None, 1
    except (core_mod.CrossProjectError, core_mod.SnapshotDamagedError) as e:
        print(f"error: {e}")
        return None, 1


def cmd_undo(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet")
        return 2
    snaps = core_mod.log(50, project_root=args.root)
    if not snaps:
        print("nothing to restore — no snapshots yet")
        return 1
    # Where are we? The live files match the NEWEST snapshot only until
    # the first undo; after that they match an older one, and stepping
    # from snaps[0] would restore the state just undone (the clobber).
    pos = core_mod.current_position(snaps, args.root)
    if pos is None:
        # Dirty state: snapshot it first ("pre-undo") so undo is
        # reversible — redo is `restore` of the pre-undo snapshot.
        core_mod.snap(args.root, "pre-undo")
        snaps = core_mod.log(50, project_root=args.root)
        pos = 0
    if len(snaps) < 2:
        print("only one snapshot — nothing to undo to")
        return 0
    if pos == len(snaps) - 1:
        print(f"already at the oldest snapshot ({snaps[pos]['sha'][:7]}) — "
              f"nothing to undo to; `sessionsteward restore <sha>` goes forward")
        return 0
    steps = max(1, args.steps)
    last = len(snaps) - 1
    target_i = min(pos + steps, last)
    target = snaps[target_i]
    restored, rc = _restore_errors(
        lambda: core_mod.restore(target["sha"], project_root=args.root))
    if restored is None:
        return rc
    note = ""
    if pos + steps > last:
        note = (f" (asked for {steps} step(s); {target_i - pos} is the "
                f"oldest snapshot)")
    print(f"undone {target_i - pos} step(s) -> {target['sha'][:7]}  "
          f"{target['message']}{note}")
    _print_restored(restored, args.root)
    return 1 if core_mod.skipped(restored) else 0


def cmd_restore(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet")
        return 2
    # Validate before the pre-restore snap so a bad sha doesn't pollute log.
    _, rc = _restore_errors(lambda: core_mod.check_restore(args.sha, args.root))
    if rc:
        return rc
    # Redo support: if the current state isn't snapshotted yet (matches
    # no snapshot), snapshot it first ("pre-restore") so the restore is
    # reversible.
    snaps = core_mod.log(50, project_root=args.root)
    if snaps and core_mod.current_position(snaps, args.root) is None:
        core_mod.snap(args.root, "pre-restore")
    restored = core_mod.restore(args.sha, project_root=args.root)
    print(f"restored {args.sha[:7]}")
    _print_restored(restored, args.root)
    return 1 if core_mod.skipped(restored) else 0


def cmd_diet(args) -> int:
    if args.budget is not None and args.budget < 0:
        raise core_mod.StewardError("--budget must be a number of tokens, "
                                    "0 or more")
    report = diet_mod.analyze(args.root)
    if args.json:
        # ensure_ascii: a C1 control character in a path or line comes out
        # as \u0085, never raw
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(diet_mod.render(report, args.root))
    if args.check or args.budget is not None:
        found = diet_mod.problems(report, args.budget, args.root)
        for line in found:
            print(f"check: {line}", file=sys.stderr)
        if found:
            return CHECK_FAILED
        if not args.json:
            print("check: ok")
    return 0


# `diet --check` / `--budget` found something: not 1 (the system refused a
# read or write, or an unexpected error) or 2 (git, config, usage), so CI can
# tell a failed check from a broken run.
CHECK_FAILED = 3


def _ago(date: str) -> str:
    """`5 min ago` for a git iso-local date; '' when it cannot be read."""
    try:
        then = datetime.datetime.strptime(date[:25], "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return ""
    secs = (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
    if secs < 0:
        return "in the future"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "min")):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n > 1 and unit != 'min' else ''} ago"
    return "just now"


def cmd_status(args) -> int:
    root = args.root
    print(f"project: {os.path.abspath(root)}")
    watched = core_mod.watched_paths(root)
    print(f"watching: {len(watched)} existing path(s) - "
          + (", ".join(_short(p, root) for p in watched) or "none yet"))
    hooks = core_mod.hooks_installed(CLAUDE_SETTINGS)
    if hooks is None:
        print(f"hooks: {CLAUDE_SETTINGS} cannot be read")
    elif all(hooks.values()):
        print("hooks: installed (SessionStart + PreCompact)")
    else:
        missing = " + ".join(e for e, ok in hooks.items() if not ok)
        print(f"hooks: {missing} missing - `sessionsteward install-hooks "
              f"--write` adds them")
    if not core_mod.repo_ready():
        print("snapshots: none - `sessionsteward init` starts")
        return 0
    snaps = core_mod.log(1_000_000, project_root=root)
    if not snaps:
        print("snapshots: none for this project - `sessionsteward snap` "
              "takes one")
        return 0
    top = snaps[0]
    ago = _ago(top["date"])
    print(f"snapshots: {len(snaps)}, newest {top['sha'][:7]} "
          f"{top['date'][:16]}{f' ({ago})' if ago else ''}  {top['message']}")
    pos = core_mod.current_position(snaps[:50], root)
    if pos == 0:
        print(f"state: your files are snapshot {top['sha'][:7]} (nothing "
              f"unsaved)")
    elif pos is not None:
        s = snaps[pos]
        print(f"state: your files are snapshot {s['sha'][:7]} ({pos} "
              f"back, after an undo or restore)  {s['message']}")
    else:
        changes = core_mod.changes_since(top["sha"], root)
        if changes is None:
            print("state: unsaved changes (a watched file cannot be read)")
        else:
            print(f"state: {len(changes)} unsaved change(s) since "
                  f"{top['sha'][:7]}:")
            for tag, path in changes[:STATUS_LIMIT]:
                print(f"  {tag} {diet_mod.safe(_short(path, root))}")
            if len(changes) > STATUS_LIMIT:
                print(f"  ... {len(changes) - STATUS_LIMIT} more")
            print("  `sessionsteward snap` saves them, `sessionsteward diff` "
                  "shows snapshot changes, `sessionsteward undo` goes back")
    if len(snaps) > 1 and pos == 0:
        for line in core_mod.guard_lines(top["sha"], snaps[1]["sha"]):
            print(line)
    return 0


STATUS_LIMIT = 50


def _write_bytes(data: bytes) -> None:
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        sys.stdout.flush()
        buf.write(data)
        buf.flush()
    else:   # a text-only stdout (captured in tests)
        sys.stdout.write(data.decode("utf-8", "replace"))


def cmd_show(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet", file=sys.stderr)
        return 2
    try:
        sha, manifest = core_mod.resolve_snapshot(args.sha, args.root)
    except (core_mod.NothingToRestoreError, core_mod.CrossProjectError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    files = core_mod.snapshot_files(sha, manifest)
    if args.path is None:
        for ap in sorted(files):
            size = files[ap][1]
            print(f"{size:>10,}  {diet_mod.safe(_short(ap, args.root))}")
        print(f"{len(files)} file(s) in snapshot {sha[:7]} - "
              f"`sessionsteward show {sha[:7]} <file>` prints one")
        return 0
    want = core_mod.lookup_path(args.path, args.root)
    norm = {os.path.normcase(os.path.normpath(ap)): ap for ap in files}
    hit = norm.get(os.path.normcase(want))
    if hit is None:
        # a unique case-insensitive match (Windows and macOS spellings)
        folded = [ap for k, ap in norm.items()
                  if k.casefold() == os.path.normcase(want).casefold()]
        hit = folded[0] if len(folded) == 1 else None
    if hit is None:
        prefix = os.path.normcase(want).rstrip(os.sep) + os.sep
        under = sorted(ap for k, ap in norm.items() if k.startswith(prefix))
        if under:
            for ap in under:
                print(f"{files[ap][1]:>10,}  "
                      f"{diet_mod.safe(_short(ap, args.root))}")
            print(f"{len(under)} file(s) under {args.path} in snapshot "
                  f"{sha[:7]}")
            return 0
        print(f"error: snapshot {sha[:7]} has no file {args.path} - "
              f"`sessionsteward show {sha[:7]}` lists what it holds",
              file=sys.stderr)
        return 1
    _write_bytes(core_mod.read_blob(files[hit][0]))
    return 0


def cmd_install_hooks(args) -> int:
    wiring = (f"Wiring: PreCompact -> `{core_mod.HOOK_PRECOMPACT_CMD}`, "
              f"SessionStart -> `{core_mod.HOOK_SESSIONSTART_CMD}`.")
    if args.write:
        try:
            path = core_mod.write_hooks_file(CLAUDE_SETTINGS)
        except core_mod.SettingsError as e:
            # Never guess at a file we could not read: say so, leave it
            # alone, and print what to add by hand.
            print(f"error: {e}", file=sys.stderr)
            print(f'Add this to the "hooks" section of {CLAUDE_SETTINGS} '
                  f"by hand, or fix the file and re-run with --write:")
            print(json.dumps(core_mod.build_hooks_json(), indent=2))
            print(wiring)
            return 2
        print(f"merged hooks into {path} (existing settings preserved; "
              f"re-running is a no-op)")
        print(wiring)
        return 0
    payload = core_mod.build_hooks_json()
    print(json.dumps(payload, indent=2))
    print()
    print("Copy the JSON above into the \"hooks\" section of "
          "~/.claude/settings.json, or re-run with --write to merge it "
          "automatically (existing settings are preserved).")
    print(wiring)
    return 0


def cmd_watch(args) -> int:
    core_mod.init_repo()
    print(f"watching for changes (every {args.interval}s, Ctrl-C to stop)…")
    mtimes = core_mod.file_mtimes(core_mod.watched_paths(args.root))
    last_problem = None
    try:
        while True:
            time.sleep(args.interval)
            try:
                now = core_mod.file_mtimes(core_mod.watched_paths(args.root))
                if now != mtimes:
                    sha, too_big = core_mod.snap_report(
                        args.root, "auto: files changed")
                    print(f"change detected -> snapshot {sha[:7]}")
                    _warn_too_big(too_big, args.root)
                    _guard(args.root, sha)
                    mtimes = now
                last_problem = None
            except (core_mod.StewardError, OSError) as e:
                # A daemon outlives a busy lock, a git hiccup or a file
                # that vanished mid-copy: say so once per distinct problem
                # and try again on the next tick (mtimes is unchanged, so
                # the change is still pending). A reader that went away is
                # not a problem to retry - main() handles that.
                if isinstance(e, OSError) and _pipe_gone(e):
                    raise
                problem = str(e)
                if problem != last_problem:
                    print(f"warning: snapshot failed ({problem}); will retry "
                          f"in {args.interval:g}s", file=sys.stderr)
                last_problem = problem
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def _poll_interval(text) -> float:
    """`watch --interval` as seconds: a finite number from MIN_INTERVAL to
    MAX_INTERVAL. Anything else (negative, zero, nan, inf, text, a value
    time.sleep cannot take) is an error, never a traceback or a busy loop."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        value = float("nan")
    if not MIN_INTERVAL <= value <= MAX_INTERVAL:   # false for nan, too
        raise core_mod.StewardError(
            f"--interval must be a number of seconds from {MIN_INTERVAL:g} "
            f"to {MAX_INTERVAL:g} (got {text!r})")
    return value


def build_parser() -> argparse.ArgumentParser:
    # RawDescription: the docstring is a paragraph plus a list of examples,
    # one per line - the default formatter folds it into a single block.
    p = argparse.ArgumentParser(
        prog="sessionsteward", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    p.add_argument("--root", default=os.getcwd(),
                   help="Project root for relative watch paths.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Set up the snapshots repo and take a "
                               "first snapshot.")
    wa = sub.add_parser("watch", help="Watch daemon / manage watch list.")
    wa.add_argument("--add", nargs="+", default=None,
                    help="Add paths to the watch list (then exit). "
                         "The defaults stay watched.")
    wa.add_argument("--remove", nargs="+", default=None,
                    help="Drop paths from the watch list, defaults "
                         "included (then exit).")
    wa.add_argument("--interval", default="5", metavar="SECONDS",
                    help=f"Poll seconds for the daemon, from {MIN_INTERVAL:g} "
                         f"to {MAX_INTERVAL:g} (default 5).")
    sn = sub.add_parser("snap", help="Snapshot now.")
    sn.add_argument("-m", "--message", default=None)
    sub.add_parser("precompact", help="Snapshot before compaction.")
    lg = sub.add_parser("log", help="List snapshots.")
    lg.add_argument("--limit", type=int, default=20)
    df = sub.add_parser("diff", help="Diff two snapshots (default: the "
                                     "previous one against the newest).")
    df.add_argument("sha1", nargs="?")
    df.add_argument("sha2", nargs="?", default=None)
    un = sub.add_parser("undo", help="Restore the previous snapshot.")
    un.add_argument("--steps", type=int, default=1,
                    help="How many snapshots to step back (clamped to the "
                         "oldest; the report says when).")
    rs = sub.add_parser("restore", help="Restore a specific snapshot.")
    rs.add_argument("sha")
    sub.add_parser("status", help="Where you are: newest snapshot, "
                                  "unsaved changes, hooks.")
    sh = sub.add_parser("show", help="Print one file from a snapshot (or "
                                     "list the files it holds).")
    sh.add_argument("sha")
    sh.add_argument("path", nargs="?", default=None)
    dt = sub.add_parser("diet", help="Context-diet report.")
    dt.add_argument("--json", action="store_true",
                    help="Print the report as JSON.")
    dt.add_argument("--check", action="store_true",
                    help="Exit 3 when there are warnings (CI, pre-commit).")
    dt.add_argument("--budget", type=int, default=None, metavar="TOKENS",
                    help="With or without --check: exit 3 when more than "
                         "TOKENS (estimated) load at session start.")
    ih = sub.add_parser("install-hooks",
                        help="Print (or --write) Claude Code hooks JSON.")
    ih.add_argument("--write", action="store_true",
                    help="Merge the hooks into ~/.claude/settings.json.")
    return p


def _fail(exc: BaseException, code: int) -> int:
    """`error: <one line>` on stderr (a hook's output is read by the agent,
    and a multi-line message is a traceback in all but name); exit `code`."""
    message = " ".join(str(exc).split())
    if len(message) > 300:
        message = message[:297] + "..."
    try:
        print(f"error: {message}", file=sys.stderr)
    except OSError:
        pass  # nowhere to say it
    return code


def main(argv=None) -> int:
    _harden_streams()
    args = build_parser().parse_args(argv)
    cmds = {
        "init": cmd_init, "watch": cmd_watch, "snap": cmd_snap,
        "precompact": cmd_precompact, "log": cmd_log, "diff": cmd_diff,
        "undo": cmd_undo, "restore": cmd_restore, "diet": cmd_diet,
        "status": cmd_status, "show": cmd_show,
        "install-hooks": cmd_install_hooks,
    }
    if args.cmd == "watch" and (args.add or args.remove):
        cmds["watch"] = cmd_watch_edit
    # Commands that write take the lock for their whole run, so e.g. `undo`
    # cannot interleave with a hook's snap. `watch` (the daemon) locks only
    # around each snapshot it takes; read-only commands never wait.
    writes = args.cmd in ("init", "snap", "precompact", "undo", "restore") \
        or (args.cmd == "watch" and (args.add or args.remove))
    try:
        if args.cmd == "watch":
            args.interval = _poll_interval(args.interval)
        if args.cmd in ("init", "snap", "precompact", "undo", "restore") \
                or (args.cmd == "watch" and not (args.add or args.remove)):
            _warn_unwatchable(args.root)   # not for the edit that fixes it
        with core_mod.locked() if writes else contextlib.nullcontext():
            rc = cmds[args.cmd](args)
        if sys.stdout is not None:
            sys.stdout.flush()  # a closed pipe surfaces here, not at exit
        return rc
    except OSError as e:
        if _pipe_gone(e):
            _stdout_gone()
            return 0
        # the system refused a read or write (an unwritable home directory,
        # a full disk, a file where a directory belongs): one line, exit 1
        return _fail(e, 1)
    except (core_mod.StewardError, RuntimeError) as e:
        # git missing, a stale .git/index.lock, a corrupt config or
        # manifest: one line on stderr, exit 2 — never a traceback (as a
        # SessionStart hook that would be pasted into the agent's context).
        return _fail(e, 2)
    except subprocess.SubprocessError as e:
        return _fail(e, 1)
    except Exception as e:   # noqa: BLE001 - input nobody foresaw, or a bug
        # Still one line and not a traceback (a hook's output is read by the
        # agent); exit 1 like any other failure of the system around us.
        return _fail(RuntimeError(
            f"unexpected {type(e).__name__}: {e} - this is a bug in "
            f"sessionsteward; please report it"), 1)


if __name__ == "__main__":
    sys.exit(main())
