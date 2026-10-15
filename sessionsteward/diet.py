"""Context diet: what's eating the agent's context window.

Reports per-file sizes and finds duplicated content across watched
files — the usual bloat: the same instructions pasted into CLAUDE.md,
.claude/, and the project README.
"""

from __future__ import annotations

import os
from collections import Counter

from . import core as core_mod

MIN_LINE_LEN = 40  # ignore short lines when hunting duplicates


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _all_files(paths: list[str]) -> list[str]:
    out = []
    for ap in paths:
        if os.path.isdir(ap):
            for root, dirs, files in os.walk(ap):
                dirs[:] = [d for d in dirs if d != ".git"]
                out.extend(os.path.join(root, f) for f in files)
        elif os.path.isfile(ap):
            out.append(ap)
    return out


def analyze(project_root: str, top_dupes: int = 10) -> dict:
    paths = core_mod.watched_paths(project_root)
    files = _all_files(paths)
    per_file = []
    line_owners: dict[str, set[str]] = {}
    for fp in files:
        text = _read_text(fp)
        if text is None:
            continue
        lines = text.splitlines()
        per_file.append({"path": fp, "chars": len(text), "lines": len(lines)})
        seen = set()
        for ln in lines:
            s = ln.strip()
            if len(s) >= MIN_LINE_LEN and s not in seen:
                seen.add(s)
                line_owners.setdefault(s, set()).add(fp)
    dupes = [{"line": ln[:100], "files": sorted(owners), "count": len(owners)}
             for ln, owners in line_owners.items() if len(owners) > 1]
    dupes.sort(key=lambda d: (-d["count"], -len(d["line"])))
    per_file.sort(key=lambda f: -f["chars"])
    total = sum(f["chars"] for f in per_file)
    return {"files": per_file, "total_chars": total,
            "duplicates": dupes[:top_dupes],
            "n_duplicates": len(dupes)}


def render(report: dict) -> str:
    lines = ["\n# Context diet\n",
             f"Watched files: {len(report['files'])}, "
             f"total ~{report['total_chars']:,} chars "
             f"(~{report['total_chars'] // 4:,} tokens)\n",
             "## Biggest files",
             "| file | chars | lines |",
             "|---|---|---|"]
    for f in report["files"][:10]:
        lines.append(f"| `{f['path']}` | {f['chars']:,} | {f['lines']} |")
    lines.append("")
    if report["duplicates"]:
        lines.append(f"## Duplicated content "
                     f"({report['n_duplicates']} lines in 2+ files)")
        for d in report["duplicates"]:
            lines.append(f"- _{d['line']}_ …")
            for fp in d["files"]:
                lines.append(f"  - `{fp}`")
    else:
        lines.append("No significant duplication across watched files. 🎉")
    lines.append("")
    return "\n".join(lines)
