"""`sessionsteward why A B`: which instructions went missing between two
snapshots.

`diff` shows every changed line in git's order, including lines that merely
moved. This answers the question you actually have after a bad compaction or
a "remember this": which lines of my instruction files are no longer there,
which are new, and in which file. Lines are compared by their text (leading
and trailing spaces and blank lines do not count), so a paragraph that was
only moved does not show up as lost.

A line that looks like it holds a secret is shown as a placeholder, not as
itself: old snapshots from before sessionsteward 0.2.0 may still contain one.
"""

from __future__ import annotations

import collections
import os

from . import core
from . import secretscan

#: Lines shown per file and per kind (lost / added).
DEFAULT_LINES = 30
#: A longer line is cut here.
LINE_WIDTH = 160


def _full_id(sha: str) -> str:
    core._require_snapshot_id(sha)
    p = core._run_git(core.REPO_DIR, ("rev-parse", "--verify", "--quiet",
                                      f"{sha}^{{commit}}"))
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0 or not out:
        raise core.NothingToRestoreError(
            f"no such snapshot: {sha!r} (`sessionsteward log` lists them)")
    return out


def _lines(data: bytes | None) -> list[str] | None:
    """The stripped, non-blank lines of a file; None for a binary file."""
    if data is None:
        return []
    if b"\0" in data[:8192]:
        return None
    text = data.decode("utf-8", "replace")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _vanished(old: list[str], new: list[str]) -> list[str]:
    """The lines of `old` that `new` no longer has (as many times as they
    appeared), in the order of `old`."""
    left = collections.Counter(new)
    out = []
    for line in old:
        if left[line] > 0:
            left[line] -= 1
        else:
            out.append(line)
    return out


def _blob(blob_id: str | None) -> bytes | None:
    if blob_id is None:
        return None
    p = core._run_git(core.REPO_DIR, ("cat-file", "blob", blob_id))
    return p.stdout if p.returncode == 0 else None


def _nice(path: str, root: str) -> str:
    """The path as the user knows it: relative to the project folder when it
    is inside it, ~/... for the home folder, else absolute."""
    for base, prefix in ((root, ""), (os.path.expanduser("~"), "~/")):
        if base:
            try:
                rel = os.path.relpath(path, base)
            except ValueError:   # another drive
                continue
            if not rel.startswith(".."):
                return prefix + rel.replace(os.sep, "/")
    return path


def compare(sha1: str, sha2: str, project_root: str | None = None) -> dict:
    """What changed in the watched files between two snapshots:
    {"from": id, "to": id, "files": [{"name", "status", "old_bytes",
    "new_bytes", "lost", "added", "binary"}]}, files in name order, only the
    ones that changed."""
    a, b = _full_id(sha1), _full_id(sha2)
    manifests = core._manifests_at([a, b], strict=True)
    for sha in (a, b):
        if not manifests.get(sha):
            raise core.NothingToRestoreError(
                f"{sha[:7]} is not a snapshot (it holds no file list)")
    roots = {core._norm_root(manifests[s].get("project_root")) for s in (a, b)}
    if len(roots) > 1:
        raise core.CrossProjectError(
            "those two snapshots are from different projects; compare two "
            "snapshots of the same project (`sessionsteward log`)")
    if project_root is not None and core._norm_root(project_root) not in roots:
        raise core.CrossProjectError(
            "those snapshots belong to another project than "
            f"{os.path.abspath(project_root)}")
    old_files = core.snapshot_tree(a, manifests[a])
    new_files = core.snapshot_tree(b, manifests[b])
    root = manifests[b].get("project_root") or ""
    out = []
    for path in sorted(set(old_files) | set(new_files)):
        if old_files.get(path) == new_files.get(path):
            continue
        old_bytes = _blob(old_files.get(path))
        new_bytes = _blob(new_files.get(path))
        old_lines, new_lines = _lines(old_bytes), _lines(new_bytes)
        entry = {"name": _nice(path, root),
                 "status": ("added" if path not in old_files
                            else "deleted" if path not in new_files
                            else "changed"),
                 "old_bytes": None if old_bytes is None else len(old_bytes),
                 "new_bytes": None if new_bytes is None else len(new_bytes),
                 "binary": old_lines is None or new_lines is None,
                 "lost": [], "added": []}
        if not entry["binary"]:
            entry["lost"] = _vanished(old_lines, new_lines)
            entry["added"] = _vanished(new_lines, old_lines)
            if not entry["lost"] and not entry["added"] \
                    and entry["status"] == "changed":
                entry["moved_only"] = True
        out.append(entry)
    return {"from": a, "to": b, "files": out}


def _cut(line: str) -> str:
    line = secretscan.hide(line)
    return line if len(line) <= LINE_WIDTH else line[:LINE_WIDTH - 3] + "..."


def render(result: dict, lines: int = DEFAULT_LINES) -> str:
    """The plain-language report for compare()'s result."""
    head = (f"Between {result['from'][:7]} and {result['to'][:7]}: ")
    files = result["files"]
    if not files:
        return head + "no differences in the watched files."
    total_lost = sum(len(f["lost"]) for f in files)
    out = [head + f"{len(files)} file(s) differ, {total_lost} line(s) are "
           f"no longer anywhere in their file.", ""]
    for f in files:
        size = ""
        if f["old_bytes"] is not None and f["new_bytes"] is not None:
            size = f"  ({f['old_bytes']:,} -> {f['new_bytes']:,} bytes)"
        elif f["status"] == "deleted":
            size = f"  ({f['old_bytes']:,} bytes)"
        elif f["status"] == "added":
            size = f"  ({f['new_bytes']:,} bytes)"
        out.append(f"{f['name']}  {f['status']}{size}")
        if f["binary"]:
            out.append("  (not a text file: contents not compared)")
        elif f.get("moved_only"):
            out.append("  only reordered or re-spaced: no line was lost")
        else:
            summary = []
            if f["lost"]:
                summary.append(f"{len(f['lost'])} line(s) lost")
            if f["added"]:
                summary.append(f"{len(f['added'])} added")
            if summary:
                out.append("  " + ", ".join(summary))
            for kind, sign in (("lost", "-"), ("added", "+")):
                for line in f[kind][:lines]:
                    out.append(f"  {sign} {_cut(line)}")
                extra = len(f[kind]) - lines
                if extra > 0:
                    out.append(f"  {sign} ... {extra} more")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
