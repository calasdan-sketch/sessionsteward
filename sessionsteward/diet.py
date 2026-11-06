"""Context diet: what's eating the agent's context window.

Reports what Claude Code loads into context at session start, counted the
way its memory docs (https://code.claude.com/docs/en/memory) say it loads
them:

- the watched CLAUDE.md / CLAUDE.local.md / AGENTS.md files, `.claude/rules/`
  markdown and the auto-memory index MEMORY.md;
- every file those pull in with an `@path` import (relative to the importing
  file, `~/...` or absolute; `\\ ` for a space; never inside a code span or a
  fenced code block; up to four hops deep) - imports organise a long file but
  do not make it cheaper, because the imported file loads with it;
- only the first 200 lines (or 25 KB) of MEMORY.md - the rest is not loaded,
  which the report warns about, since a note past that line is invisible;
- a rules file with `paths:` front matter only on demand, so it is listed
  apart and kept out of the session-start total.

It also finds duplicated lines across the loaded files, warns about files
over the recommended 200 lines, files over the 4 MiB Claude Code skips and
`@...md` imports whose file does not exist, and lists imported files that no
watched path covers - those are loaded into context but have no undo.

Settings, commands, hooks and other files under .claude/ are watched for undo
but are not context, so they are not counted. Token counts are estimates
(characters / 4), not a tokenizer's.
"""

from __future__ import annotations

import collections
import os
import re

from . import core as core_mod

MIN_LINE_LEN = 40  # ignore short lines when hunting duplicates

# Files loaded into context at session start (per the Claude Code memory
# docs). Skills/commands and memory topic files load on demand only.
CONTEXT_NAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "MEMORY.md"}

# Documented limits (https://code.claude.com/docs/en/memory).
MAX_IMPORT_DEPTH = 4                 # "a maximum depth of four hops"
MEMORY_INDEX_LINES = 200             # MEMORY.md: "first 200 lines ..."
MEMORY_INDEX_BYTES = 25 * 1024       # "... or the first 25KB"
RECOMMENDED_LINES = 200              # "target under 200 lines per file"
SKIPPED_BYTES = 4 * 1024 * 1024      # "Claude Code skips a file over 4 MiB"
CHARS_PER_TOKEN = 4                  # rough estimate, not a tokenizer

# Never read more than this from one file: the 4 MiB skip already says the
# file is not loaded, and a 2 GB file must not be pulled into memory.
_READ_CAP = SKIPPED_BYTES + 1

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
# `@` at the start of a line or after whitespace (so `me@example.com` is not
# an import), then the path: anything up to whitespace, `\ ` kept as a space.
_IMPORT = re.compile(r"(?:^|(?<=\s))@((?:\\ |[^\s])+)")
_TRAILING = ".,;:!?)]}>'\""


def loads_into_context(path: str) -> bool:
    name = os.path.basename(path)
    if name in CONTEXT_NAMES:
        return True
    parents = os.path.dirname(path).replace(os.sep, "/").split("/")
    return name.endswith(".md") and "rules" in parents


def _is_rules_file(path: str) -> bool:
    name = os.path.basename(path)
    parents = os.path.dirname(path).replace(os.sep, "/").split("/")
    return (name not in CONTEXT_NAMES and name.endswith(".md")
            and "rules" in parents)


def _read_text(path: str) -> tuple[str, int] | None:
    """(text, size in bytes) - at most _READ_CAP bytes of text are read; the
    size is the file's real size. None when it cannot be read."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(_READ_CAP)
    except (OSError, ValueError):
        return None
    return raw.decode("utf-8", errors="replace"), size


def split_front_matter(text: str) -> tuple[str | None, str]:
    """(front matter or None, body). Front matter is a `---` line at the very
    top, closed by the next `---` line."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def is_path_scoped(text: str) -> bool:
    """A rules file with a `paths:` key in its front matter loads only when
    Claude works with matching files."""
    fm, _ = split_front_matter(text)
    return fm is not None and re.search(r"(?m)^paths\s*:", fm) is not None


def find_imports(text: str) -> list[str]:
    """The `@path` imports in a memory file, in order, as written (`\\ `
    turned into a space). Fenced code blocks and code spans are skipped,
    as Claude Code skips them."""
    out = []
    fence = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        line = _CODE_SPAN.sub(" ", line)
        for im in _IMPORT.finditer(line):
            raw = im.group(1)
            if raw[:1] in "\"'":
                continue   # a quoted path is not imported
            out.append(raw.replace("\\ ", " "))
    return out


def _resolve_import(raw: str, importer: str) -> str | None:
    """Absolute path of an import as written in `importer`, trying it as
    written and then without trailing sentence punctuation (`see @README.`).
    None when neither is an existing file."""
    candidates = [raw]
    stripped = raw.rstrip(_TRAILING)
    if stripped and stripped != raw:
        candidates.append(stripped)
    for cand in candidates:
        if "\0" in cand:
            return None
        if cand == "~" or cand.startswith(("~/", "~\\")):
            p = os.path.expanduser(cand)
        elif os.path.isabs(cand):
            p = cand
        else:
            p = os.path.join(os.path.dirname(importer), cand)
        try:
            if os.path.isfile(p):
                return os.path.abspath(p)
        except (OSError, ValueError):
            pass
    return None


def _looks_like_file(raw: str) -> bool:
    """Only an import that is plainly a markdown file is reported missing:
    `@types/node` or `@alice` in prose is not an import that failed."""
    return raw.rstrip(_TRAILING).lower().endswith(".md")


def _key(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(path)


def _covered(path: str, watched: list[str]) -> bool:
    k = _key(path)
    for w in watched:
        wk = _key(w)
        if k == wk or k.startswith(wk.rstrip(os.sep) + os.sep):
            return True
    return False


def analyze(project_root: str, top_dupes: int = 10) -> dict:
    paths = core_mod.watched_paths(project_root)
    files = list(core_mod.iter_files(paths))  # honours IGNORE_PATTERNS
    context = [fp for fp in files if loads_into_context(fp)]
    per_file: list[dict] = []
    on_demand: list[dict] = []
    warnings: list[dict] = []
    unprotected: list[str] = []
    line_owners: dict[str, set[str]] = {}
    seen: set[str] = set()

    def warn(code: str, path: str, message: str) -> None:
        warnings.append({"code": code, "path": path, "message": message})

    # breadth-first: (path, imported_by, depth); top-level files first, so a
    # file that is both watched and imported is counted once, as itself
    # from_project: the chain started at a file inside the project (not a
    # user-level one such as ~/.claude/CLAUDE.md, which you wrote yourself)
    queue = collections.deque((fp, None, 0, _covered(fp, [project_root]))
                              for fp in context)
    while queue:
        fp, importer, depth, from_project = queue.popleft()
        key = _key(fp)
        if key in seen:
            continue
        seen.add(key)
        got = _read_text(fp)
        if got is None:
            continue
        text, size = got
        if size > SKIPPED_BYTES:
            warn("skipped", fp, f"{size / 1048576:.1f} MiB - Claude Code "
                 f"skips a file over 4 MiB, so none of it is loaded")
            continue
        if importer is None and _is_rules_file(fp) and is_path_scoped(text):
            on_demand.append({"path": fp, "chars": len(text),
                              "lines": len(text.splitlines())})
            continue
        if _is_rules_file(fp):
            _, text = split_front_matter(text)   # stripped before loading
        lines = text.splitlines()
        entry = {"path": fp, "chars": len(text), "lines": len(lines)}
        external = False
        if importer is not None:
            entry["imported_by"] = importer
            if not _covered(fp, paths):
                unprotected.append(fp)
            # A project file importing something outside the project (Claude
            # Code asks before it loads one): counted, but its lines are not
            # quoted in the duplicate report - `@~/.ssh/...` in a cloned
            # repo's CLAUDE.md must not get a secret printed.
            external = from_project and not _covered(fp, [project_root])
            if external:
                entry["external"] = True
        loaded = text
        if os.path.basename(fp) == "MEMORY.md" and importer is None:
            cut, nbytes = [], 0
            for ln in lines[:MEMORY_INDEX_LINES]:
                nbytes += len(ln.encode("utf-8")) + 1
                if nbytes > MEMORY_INDEX_BYTES:
                    break
                cut.append(ln)
            if len(cut) < len(lines):
                warn("memory-index-truncated", fp,
                     f"only the first {len(cut)} of {len(lines)} lines load "
                     f"at session start (limit: {MEMORY_INDEX_LINES} lines or "
                     f"25 KB) - Claude does not see the other "
                     f"{len(lines) - len(cut)} unless it opens the file")
                loaded = "\n".join(cut) + "\n"
                entry["loaded_lines"] = len(cut)
                entry["chars"] = len(loaded)
        elif len(lines) > RECOMMENDED_LINES:
            warn("long-file", fp, f"{len(lines)} lines - Claude Code "
                 f"recommends under {RECOMMENDED_LINES} per file (longer "
                 f"files cost context and reduce adherence)")
        per_file.append(entry)
        owned = set()
        for ln in ([] if external else loaded.splitlines()):
            s = ln.strip()
            if len(s) >= MIN_LINE_LEN and s not in owned:
                owned.add(s)
                line_owners.setdefault(s, set()).add(fp)
        if os.path.basename(fp) == "MEMORY.md" and importer is None:
            continue   # an index lists topic files; they load on demand
        for raw in find_imports(text):
            target = _resolve_import(raw, fp)
            if target is None:
                if _looks_like_file(raw):
                    warn("missing-import", fp, f"imports "
                         f"@{raw.rstrip(_TRAILING)}, which does not exist")
                continue
            if depth + 1 > MAX_IMPORT_DEPTH:
                if _key(target) not in seen:
                    warn("import-too-deep", fp,
                         f"@{raw} is more than {MAX_IMPORT_DEPTH} imports "
                         f"deep - Claude Code does not load it")
                continue
            queue.append((target, fp, depth + 1, from_project))

    dupes = [{"line": ln[:100], "files": sorted(owners), "count": len(owners)}
             for ln, owners in line_owners.items() if len(owners) > 1]
    dupes.sort(key=lambda d: (-d["count"], -len(d["line"])))
    per_file.sort(key=lambda f: -f["chars"])
    total = sum(f["chars"] for f in per_file)
    return {"files": per_file, "total_chars": total,
            "total_tokens": total // CHARS_PER_TOKEN,
            "on_demand": on_demand,
            "duplicates": dupes[:top_dupes],
            "n_duplicates": len(dupes),
            "n_not_context": len(files) - len(context),
            "warnings": warnings,
            "unprotected": sorted(set(unprotected))}


def problems(report: dict, budget: int | None = None,
             project_root: str | None = None) -> list[str]:
    """What `diet --check` fails on: every warning, and a session-start
    total over `budget` tokens."""
    def name(p: str) -> str:
        return _short(p, project_root) if project_root else p
    out = [f"{name(w['path'])}: {w['message']}" for w in report["warnings"]]
    if budget is not None and report["total_tokens"] > budget:
        out.append(f"~{report['total_tokens']:,} tokens load at session "
                   f"start, over the budget of {budget:,}")
    return out


def render(report: dict, project_root: str | None = None) -> str:
    def name(p: str) -> str:
        return _short(p, project_root) if project_root else p

    lines = ["\n# Context diet\n",
             f"Files loaded into context: {len(report['files'])}, "
             f"total ~{report['total_chars']:,} chars "
             f"(~{report['total_chars'] // CHARS_PER_TOKEN:,} tokens)"]
    if report.get("n_not_context"):
        lines.append(f"({report['n_not_context']} other watched file(s) — "
                     f"settings, commands, hooks — are not loaded into "
                     f"context and are not counted)")
    lines += ["",
              "## Biggest files",
              "| file | chars | lines |",
              "|---|---|---|"]
    for f in report["files"][:10]:
        note = ""
        if f.get("imported_by"):
            note = f" (imported by `{name(f['imported_by'])}`)"
        if "loaded_lines" in f:
            note += f" (first {f['loaded_lines']} lines)"
        lines.append(f"| `{name(f['path'])}`{note} | {f['chars']:,} | "
                     f"{f['lines']} |")
    if len(report["files"]) > 10:
        lines.append(f"| … {len(report['files']) - 10} more | | |")
    lines.append("")
    if report.get("on_demand"):
        lines.append("## Loaded on demand (rules with `paths:`), not counted")
        for f in report["on_demand"]:
            lines.append(f"- `{name(f['path'])}` — {f['chars']:,} chars")
        lines.append("")
    if report.get("warnings"):
        lines.append("## Warnings")
        for w in report["warnings"]:
            lines.append(f"- `{name(w['path'])}`: {w['message']}")
        lines.append("")
    if report.get("unprotected"):
        lines.append("## Imported but not protected by undo")
        for p in report["unprotected"]:
            lines.append(f"- `{name(p)}` — `sessionsteward watch --add "
                         f"{core_mod.shell_word(name(p))}`")
        lines.append("")
    if report["duplicates"]:
        lines.append(f"## Duplicated content "
                     f"({report['n_duplicates']} lines in 2+ files)")
        for d in report["duplicates"]:
            lines.append(f"- _{d['line']}_ …")
            for fp in d["files"]:
                lines.append(f"  - `{name(fp)}`")
    else:
        lines.append("No significant duplication across context files.")
    lines.append("")
    return "\n".join(lines)


def _short(path: str, root: str) -> str:
    """Project-relative inside the project, ~/... under home, else as is."""
    root = os.path.abspath(root)
    home = os.path.abspath(os.path.expanduser("~"))
    for base, prefix in ((root, ""), (home, "~" + os.sep)):
        b = base.rstrip(os.sep)
        if path.startswith(b + os.sep):
            return prefix + path[len(b) + 1:]
    return path
