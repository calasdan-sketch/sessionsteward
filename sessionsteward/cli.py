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
  sessionsteward diet                  # what's eating the context window
  sessionsteward why <sha1> [sha2]     # which instructions were lost between two snapshots
  sessionsteward guard status|set      # the shrink guard (a hook); see README
  sessionsteward prune [--yes]         # drop old snapshots by a keep-N / keep-D-days rule
  sessionsteward install-hooks [--write]  # Claude Code hooks JSON
  sessionsteward watch                 # daemon: auto-snapshot on change
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import subprocess
import sys
import time

from . import __version__
from . import core as core_mod
from . import diet as diet_mod
from . import doctor as doctor_mod
from . import guardhook as guard_mod
from . import retention as retention_mod
from . import secretscan as secret_mod
from . import why as why_mod

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
    # Two reasons share the list: a file over the per-file limit, and a file
    # that fit the limit but was refused for the per-snapshot cap.
    capped = [(p, n) for p, n in too_big if limit is None or n <= limit]
    too_big = [(p, n) for p, n in too_big if (p, n) not in capped]
    if capped:
        cap = core_mod.max_snapshot_bytes()
        _warn(f"{len(capped)} file(s) were not snapshotted: one snapshot "
              f"holds at most {cap / 1048576:g} MB (first: "
              f"{_short(capped[0][0], root)}); "
              f"SESSIONSTEWARD_MAX_SNAPSHOT_MB changes the cap")
    if not too_big:
        return
    over = f"over the {limit / 1048576:g} MB limit" if limit else "too big"
    path, size = too_big[0]
    first = f"{_short(path, root)} ({size / 1048576:.1f} MB)"
    if len(too_big) == 1:
        what = f"not snapshotted, {over}: {first}"
    else:
        what = (f"{len(too_big)} files {over} were not snapshotted "
                f"(first: {first})")
    _warn(f"{what}; SESSIONSTEWARD_MAX_FILE_MB changes the limit")


def _warn_left_out(skipped: dict, root: str) -> None:
    """Warn about what a snapshot's walk left behind without being asked to:
    links that lead outside a watched folder and folders below the depth
    limit. (A link to a folder already walked, an ignored name and a virtual
    environment are left out on purpose and are not mentioned.) `undo` and
    `restore` leave such items where they are, so nothing is lost - they are
    simply not protected by the snapshot."""
    outside = skipped.get("outside") or []
    if outside:
        first = _short(outside[0], root)
        _warn(f"{len(outside)} link(s) leading outside the watched folders "
              f"were not snapshotted (first: {first}); "
              f"`sessionsteward watch --follow-links` includes them")
    deep = skipped.get("deep") or []
    if deep:
        _warn(f"{len(deep)} folder(s) more than {core_mod.MAX_WALK_DEPTH} "
              f"levels deep were not snapshotted (first: "
              f"{_short(deep[0], root)})")


def _warn_secrets(skipped: dict, root: str) -> None:
    """Say which files were left out because they look like they hold a
    secret. Names the file and the kind of secret, never the secret."""
    found = skipped.get("secret") or []
    if not found:
        return
    first = found[0]
    kinds = secret_mod.file_kinds(first)
    why = f" - it looks like {kinds[0]}" if kinds else ""
    more = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
    _warn(f"not snapshotted: {_short(first, root)}{more}{why}. A snapshot "
          f"is kept for good, so a file that holds a secret is left out and "
          f"`undo` leaves it alone. Remove the secret from the file to have "
          f"it snapshotted; SESSIONSTEWARD_SECRET_SCAN=0 turns this check off")


def _snap_and_warn(root: str, message: str) -> str:
    """snap_report() plus every warning about what it left out."""
    skipped: dict = {}
    sha, too_big = core_mod.snap_report(root, message, skipped)
    _warn_too_big(too_big, root)
    _warn_left_out(skipped, root)
    _warn_secrets(skipped, root)
    return sha


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
        sha = _snap_and_warn(args.root, "initial snapshot")
        print(f"snapshot {sha[:7]} (initial)")
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
    if args.follow_links is not None:
        cfg["follow_links"] = args.follow_links
    core_mod.save_config(cfg)
    print("watching: " + ", ".join(core_mod.watch_list(cfg)))
    return 0


def _guard(root: str, sha: str) -> None:
    snaps = core_mod.log(2, project_root=root)
    if len(snaps) == 2 and snaps[0]["sha"] == sha:
        for line in core_mod.guard_lines(sha, snaps[1]["sha"]):
            print(line)


def cmd_snap(args) -> int:
    sha = _snap_and_warn(args.root, args.message or "manual snapshot")
    print(f"snapshot {sha[:7]}")
    _guard(args.root, sha)
    return 0


def cmd_precompact(args) -> int:
    sha = _snap_and_warn(args.root, "pre-compaction snapshot")
    print(f"pre-compaction snapshot {sha[:7]} — "
          f"`sessionsteward diff {sha[:7]}` after, to see what was dropped")
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
    if args.steps < 1:
        raise core_mod.StewardError("--steps must be 1 or more")
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
    steps = args.steps
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


def cmd_why(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet - run `sessionsteward init` first",
              file=sys.stderr)
        return 2
    sha2 = args.sha2
    if sha2 is None:
        snaps = core_mod.log(1, project_root=args.root)
        if not snaps:
            print(f"no snapshots yet for {os.path.abspath(args.root)}",
                  file=sys.stderr)
            return 2
        sha2 = snaps[0]["sha"]
    result = why_mod.compare(args.sha1, sha2, args.root)
    sys.stdout.write(why_mod.render(result, args.lines))
    return 0


def cmd_doctor(args) -> int:
    findings = doctor_mod.run_doctor(CLAUDE_SETTINGS)
    sys.stdout.write(doctor_mod.render_doctor(findings))
    return 1 if any(lvl == doctor_mod.FAIL for lvl, _ in findings) else 0


def cmd_config(args) -> int:
    sys.stdout.write(doctor_mod.render_config(doctor_mod.config_show()))
    return 0


def cmd_remind(args) -> int:
    return guard_mod.run_remind(args.root)


def cmd_guard(args) -> int:
    if args.guard_cmd == "hook":
        return guard_mod.run_hook()
    if args.guard_cmd == "set":
        if args.percent is None and args.mode is None:
            print("error: give --percent and/or --mode", file=sys.stderr)
            return 2
        guard_mod.set_settings(args.percent, args.mode)
    percent, mode = guard_mod.settings()
    wording = {"ask": "asks you first",
               "warn": "lets the edit through and tells Claude",
               "block": "refuses the edit"}[mode]
    print(f"shrink guard: an edit that would make a watched file more than "
          f"{percent}% smaller {wording} (mode: {mode})")
    print("  before an edit (Claude's Write/Edit tools): acts, as above")
    print("  after any change to CLAUDE.md / CLAUDE.local.md / AGENTS.md: "
          "warns you only")
    print("  files under 200 bytes are ignored")
    print(f"  installed in {CLAUDE_SETTINGS}: "
          f"{'yes' if _guard_installed() else 'no - run `sessionsteward install-hooks --write`'}")
    return 0


def _guard_installed() -> bool:
    try:
        with open(CLAUDE_SETTINGS, "rb") as f:
            settings = core_mod._parse_settings(f.read(), CLAUDE_SETTINGS)
    except (OSError, core_mod.SettingsError):
        return False
    entries = (settings.get("hooks") or {}).get("PreToolUse")
    return isinstance(entries, list) and any(
        core_mod._is_ours("PreToolUse", cmd, ["guard", "hook"])
        for cmd in core_mod._hook_commands(entries))


def cmd_prune(args) -> int:
    keep, days = retention_mod.policy(args.keep, args.days)
    if args.save:
        retention_mod.save_policy(keep, days)
    rule = " and ".join(part for part in (
        f"the newest {keep} per project" if keep is not None else "",
        f"everything from the last {days:g} days" if days is not None else ""
    ) if part)
    result = retention_mod.prune(keep, days, apply=args.yes)
    print(f"keeping {rule} (the newest snapshot of each project always stays)")
    if result["remove"] == 0:
        print(f"nothing to remove: {result['total']} snapshot(s) in all")
        return 0
    if not result["applied"]:
        print(f"would remove {result['remove']} of {result['total']} "
              f"snapshot(s) and keep {result['kept']}. Nothing was changed; "
              f"add --yes to do it. It cannot be undone, and the ids of "
              f"the snapshots after the first one removed change.")
        return 0
    print(f"removed {result['remove']} of {result['total']} snapshot(s); "
          f"{result['kept']} kept. Snapshot ids after the first one "
          f"removed changed: see `sessionsteward log`.")
    if result["bytes_before"] and result["bytes_after"] is not None:
        print(f"the snapshots repo went from "
              f"{result['bytes_before'] / 1048576:.1f} MB to "
              f"{result['bytes_after'] / 1048576:.1f} MB")
    for w in result["warnings"]:
        _warn(w)
    return 0


def cmd_diet(args) -> int:
    print(diet_mod.render(diet_mod.analyze(args.root)))
    return 0


def _warn_fallback_hooks() -> None:
    """The hooks cannot name an installed command: say what that means."""
    if core_mod._console_script() is not None:
        return
    _warn("the `sessionsteward` command was not found next to this Python "
          "(you are running from a source checkout?), so the hooks run "
          "`python -I -m sessionsteward`. The -I stops a cloned project "
          "from running its own code at session start, but it also means "
          "the hooks work only while sessionsteward is installed for that "
          "Python (`pip install .` or `pip install -e .` in the checkout); "
          "otherwise they fail silently at every session start")


def cmd_install_hooks(args) -> int:
    _warn_fallback_hooks()
    wiring = (f"Wiring: PreCompact -> `{core_mod.HOOK_PRECOMPACT_CMD}`, "
              f"SessionStart -> `{core_mod.HOOK_SESSIONSTART_CMD}`, "
              f"SessionStart (after a compaction) -> "
              f"`{core_mod.HOOK_REMIND_CMD}`, "
              f"PreToolUse (Write, Edit) and FileChanged (CLAUDE.md, "
              f"CLAUDE.local.md, AGENTS.md) -> `{core_mod.HOOK_GUARD_CMD}`.")
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
                    sha = _snap_and_warn(args.root, "auto: files changed")
                    print(f"change detected -> snapshot {sha[:7]}")
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
    wa.add_argument("--follow-links", dest="follow_links", default=None,
                    action="store_true",
                    help="Let snapshots follow links that lead outside a "
                         "watched folder (off by default; then exit).")
    wa.add_argument("--no-follow-links", dest="follow_links",
                    action="store_false", help="Turn --follow-links off.")
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
    sub.add_parser("diet", help="Context-diet report.")
    wy = sub.add_parser("why", help="Which instruction lines went missing "
                                    "between two snapshots.")
    wy.add_argument("sha1", help="The older snapshot (see `log`).")
    wy.add_argument("sha2", nargs="?", default=None,
                    help="The newer one (default: this project's newest).")
    wy.add_argument("--lines", type=int, default=why_mod.DEFAULT_LINES,
                    help="Lines to show per file (default %(default)s).")
    sub.add_parser("remind", help="(a hook) the note Claude gets after a "
                                  "compaction.")
    sub.add_parser("doctor", help="Check git, the hooks (do they point at "
                                  "this install?) and the lock wait.")
    cf = sub.add_parser("config", help="Show the settings in force and where "
                                       "each comes from.")
    csub = cf.add_subparsers(dest="config_cmd", required=True)
    csub.add_parser("show", help="Print every setting and its source.")
    gd = sub.add_parser("guard", help="The shrink guard: status, settings, "
                                      "and the hook command.")
    gsub = gd.add_subparsers(dest="guard_cmd", required=True)
    gsub.add_parser("status", help="Show the settings.")
    gs = gsub.add_parser("set", help="Change the settings.")
    gs.add_argument("--percent", type=int, default=None,
                    help="Act when an edit would make a file more than this "
                         "percent smaller, 1-99 (default 50).")
    gs.add_argument("--mode", choices=guard_mod.MODES, default=None,
                    help="ask (default): you decide; warn: tell Claude and "
                         "let it through; block: refuse the edit.")
    gsub.add_parser("hook", help="(a hook) reads a Claude Code event on "
                                 "stdin.")
    pr = sub.add_parser("prune", help="Remove old snapshots (reports first; "
                                      "--yes does it).")
    pr.add_argument("--keep", type=int, default=None, metavar="N",
                    help="Keep the newest N snapshots of each project.")
    pr.add_argument("--days", type=float, default=None, metavar="D",
                    help="Also keep every snapshot from the last D days.")
    pr.add_argument("--save", action="store_true",
                    help="Remember this rule as the default.")
    pr.add_argument("--yes", action="store_true",
                    help="Do it. Without this nothing is changed.")
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
        "install-hooks": cmd_install_hooks, "why": cmd_why,
        "remind": cmd_remind, "guard": cmd_guard, "prune": cmd_prune,
        "doctor": cmd_doctor, "config": cmd_config,
    }
    # The commands Claude Code runs as hooks wait less for the lock and log
    # what they skipped (see core.hook_mode).
    core_mod.hook_mode(
        args.cmd in ("precompact", "remind")
        or (args.cmd == "guard" and args.guard_cmd == "hook")
        or (args.cmd == "snap" and args.message == "session start"))
    editing = args.cmd == "watch" and bool(
        args.add or args.remove or args.follow_links is not None)
    if editing:
        cmds["watch"] = cmd_watch_edit
    # Commands that write take the lock for their whole run, so e.g. `undo`
    # cannot interleave with a hook's snap. `watch` (the daemon) locks only
    # around each snapshot it takes; read-only commands never wait.
    writes = args.cmd in ("init", "snap", "precompact", "undo", "restore",
                          "prune") or editing
    try:
        if args.cmd == "watch":
            args.interval = _poll_interval(args.interval)
        if args.cmd in ("init", "snap", "precompact", "undo", "restore") \
                or (args.cmd == "watch" and not editing):
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
