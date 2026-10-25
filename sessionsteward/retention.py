"""`sessionsteward prune`: keep the snapshot history from growing for ever.

Every snapshot is a git commit that is never removed on its own. This command
removes old ones by the policy you give it:

    keep the newest N snapshots of each project   (--keep N)
    AND everything younger than D days            (--days D)

A snapshot is kept if EITHER rule keeps it, and the newest snapshot of each
project is always kept (N is at least 1). With no flags the saved policy is
used (`--save` stores one), and with none saved: 50 snapshots, 30 days.

What it does, exactly: it builds the same history again from the kept
snapshots only (same files, messages and dates, in the same order), points
the snapshots repository at it, and lets git delete what nothing refers to
any more. Consequences, stated plainly:

  * It cannot be undone. A removed snapshot is gone; so is the room it took.
  * The ids of kept snapshots after the first removed one change. Look at
    `sessionsteward log` again; do not rely on an id you wrote down.
  * Without --yes it only reports what it would do.
"""

from __future__ import annotations

import os
import time

from . import core

DEFAULT_KEEP = 50
DEFAULT_DAYS = 30


def policy(keep: int | None, days: float | None) -> tuple[int | None, float | None]:
    """The policy to use: the flags if any were given, else the saved one,
    else the defaults. Raises StewardError for a value that makes no sense."""
    if keep is None and days is None:
        saved = core.load_config().get("retention")
        saved = saved if isinstance(saved, dict) else {}
        keep, days = saved.get("keep"), saved.get("days")
        if keep is None and days is None:
            keep, days = DEFAULT_KEEP, DEFAULT_DAYS
    if keep is not None and (isinstance(keep, bool) or not isinstance(keep, int)
                             or keep < 1):
        raise core.StewardError("--keep must be a whole number, 1 or more")
    if days is not None and (isinstance(days, bool)
                             or not isinstance(days, (int, float))
                             or days != days or days < 0):
        raise core.StewardError("--days must be a number, 0 or more")
    return keep, days


def save_policy(keep: int | None, days: float | None) -> None:
    cfg = core.load_config()
    cfg["retention"] = {k: v for k, v in (("keep", keep), ("days", days))
                        if v is not None}
    core.init_repo()
    core.save_config(cfg)


def _history() -> list[dict]:
    """Every commit, oldest first: {"sha", "tree", "when", "date", "message",
    "project"}. "project" is None for a commit with no file list (init)."""
    out = core._git(core.REPO_DIR, "log", "--reverse", "-z",
                    "--pretty=format:%H%x00%T%x00%at%x00%aI%x00%B")
    fields = out.split("\0")
    rows = [fields[i:i + 5] for i in range(0, len(fields) - 4, 5)]
    manifests = core._manifests_at([r[0] for r in rows])
    commits = []
    for sha, tree, at, iso, body in rows:
        m = manifests.get(sha) or {}
        commits.append({
            "sha": sha.strip(), "tree": tree, "when": float(at), "date": iso,
            "message": body.rstrip("\n"),
            "project": core._norm_root(m.get("project_root")) if m else None})
    return commits


def plan(commits: list[dict], keep: int | None, days: float | None,
         now: float | None = None) -> list[dict]:
    """The commits to remove (oldest first). A commit without a project (the
    first, empty `init` commit) is never removed."""
    now = time.time() if now is None else now
    newest: dict = {}
    for c in reversed(commits):
        if c["project"] is not None:
            newest.setdefault(c["project"], []).append(c["sha"])
    drop = []
    for c in commits:
        if c["project"] is None:
            continue
        rank = newest[c["project"]].index(c["sha"])   # 0 = newest
        kept = rank == 0
        if keep is not None and rank < keep:
            kept = True
        if days is not None and now - c["when"] <= days * 86400:
            kept = True
        if not kept:
            drop.append(c)
    return drop


def _size() -> int:
    total = 0
    for root, _dirs, files in os.walk(os.path.join(core.REPO_DIR, ".git")):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def prune(keep: int | None, days: float | None, apply: bool,
          now: float | None = None) -> dict:
    """Plan (and, with apply, carry out) a prune. Returns {"total", "remove",
    "kept", "applied", "bytes_before", "bytes_after", "warnings"}. The caller
    holds the lock."""
    if not core.repo_ready():
        raise core.StewardError("no snapshots yet - nothing to prune")
    commits = _history()
    drop = plan(commits, keep, days, now)
    result = {"total": len(commits), "remove": len(drop),
              "kept": len(commits) - len(drop), "applied": False,
              "bytes_before": None, "bytes_after": None, "warnings": []}
    if not drop or not apply:
        return result
    gone = {c["sha"] for c in drop}
    result["bytes_before"] = _size()
    old_head = commits[-1]["sha"]
    parent = None
    for c in commits:
        if c["sha"] in gone:
            continue
        args = ["commit-tree", c["tree"]]
        if parent:
            args += ["-p", parent]
        p = core._run_git(core.REPO_DIR, tuple(args),
                          env_extra={"GIT_AUTHOR_DATE": c["date"],
                                     "GIT_COMMITTER_DATE": c["date"]},
                          input=(c["message"] + "\n").encode("utf-8"))
        if p.returncode != 0:
            raise core.GitError("git commit-tree failed: " + " ".join(
                p.stderr.decode("utf-8", "replace").split())[:200])
        parent = p.stdout.decode().strip()
    # Only now does the repository change; it is one ref move, checked
    # against the head we read, so a torn run leaves the old history.
    core._git(core.REPO_DIR, "update-ref", "-m", "sessionsteward prune",
              "HEAD", parent, old_head)
    result["applied"] = True
    try:
        core._git(core.REPO_DIR, "reflog", "expire", "--expire=now", "--all")
        core._git(core.REPO_DIR, "gc", "--prune=now", "--quiet")
    except core.GitError as e:
        result["warnings"].append(
            f"the history was shortened, but git could not free the space "
            f"yet ({e}); it will be freed by a later `git gc` in "
            f"{core.REPO_DIR}")
    result["bytes_after"] = _size()
    return result
