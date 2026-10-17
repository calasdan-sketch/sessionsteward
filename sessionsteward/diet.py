"""Context diet: what's eating the agent's context window.

Reports per-file sizes of the watched files Claude Code actually loads
into context — CLAUDE.md / CLAUDE.local.md / AGENTS.md, `.claude/rules/`
(and `~/.claude/rules/`) markdown, and the auto-memory index MEMORY.md —
and finds duplicated content across them (the same instructions pasted
into CLAUDE.md and a rules file). Settings, commands, hooks and other
files under .claude/ are watched for undo but are not context, so they
are not counted.
"""

from __future__ import annotations

import os

from . import core as core_mod

MIN_LINE_LEN = 40  # ignore short lines when hunting duplicates

# Files loaded into context at session start (per the Claude Code memory
# docs). Skills/commands and memory topic files load on demand only.
CONTEXT_NAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "MEMORY.md"}


def loads_into_context(path: str) -> bool:
    name = os.path.basename(path)
    if name in CONTEXT_NAMES:
        return True
    parents = os.path.dirname(path).replace(os.sep, "/").split("/")
    return name.endswith(".md") and "rules" in parents


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def analyze(project_root: str, top_dupes: int = 10) -> dict:
    paths = core_mod.watched_paths(project_root)
    files = list(core_mod.iter_files(paths))  # honours IGNORE_PATTERNS
    context = [fp for fp in files if loads_into_context(fp)]
    per_file = []
    line_owners: dict[str, set[str]] = {}
    for fp in context:
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
            "n_duplicates": len(dupes),
            "n_not_context": len(files) - len(context)}


def render(report: dict) -> str:
    lines = ["\n# Context diet\n",
             f"Files loaded into context: {len(report['files'])}, "
             f"total ~{report['total_chars']:,} chars "
             f"(~{report['total_chars'] // 4:,} tokens)"]
    if report.get("n_not_context"):
        lines.append(f"({report['n_not_context']} other watched file(s) — "
                     f"settings, commands, hooks — are not loaded into "
                     f"context and are not counted)")
    lines += ["",
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
        lines.append("No significant duplication across context files.")
    lines.append("")
    return "\n".join(lines)
