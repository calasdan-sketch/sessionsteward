"""Context diet: what's eating the agent's context window.

Reports what Claude Code loads into context at session start, counted the
way its memory docs (https://code.claude.com/docs/en/memory) say it loads
them:

- the watched CLAUDE.md / CLAUDE.local.md / AGENTS.md files, `.claude/rules/`
  markdown and the auto-memory index MEMORY.md;
- every file those pull in with an `@path` import (relative to the importing
  file, `~/...` or absolute; `\\ ` for a space; never inside a code span or a
  code block; up to four hops deep) - imports organise a long file but do
  not make it cheaper, because the imported file loads with it;
- only the first 200 lines (or 25 KB) of MEMORY.md - the rest is not loaded,
  which the report warns about, since a note past that line is invisible;
- a rules file with `paths:` front matter only on demand, so it is listed
  apart and kept out of the session-start total;
- block-level HTML comments are stripped before loading, so not counted;
- a project file whose real location is outside the project (a symlinked
  rule, an import of `~/...`) needs your approval in Claude Code before it
  loads: it is listed apart, not counted, and its lines are never quoted.

It also finds duplicated lines across the loaded files, warns about long
CLAUDE.md files, files over the 4 MiB Claude Code skips, files it cannot
read, and `@...md` imports whose file does not exist, and lists imported
files inside the project or ~/.claude that no watched path covers - those
are loaded into context but have no undo.

Settings, commands, hooks and other files under .claude/ are watched for undo
but are not context, so they are not counted. Token counts are estimates
(characters / 4), not a tokenizer's. Everything the report prints from a
file is passed through safe(), so a control character in a memory file can
never reach your terminal as an escape sequence.
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
# "target under 200 lines per CLAUDE.md file" - AGENTS.md stands in for one
INSTRUCTION_NAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md"}

# Documented limits (https://code.claude.com/docs/en/memory).
MAX_IMPORT_DEPTH = 4                 # "a maximum depth of four hops"
MEMORY_INDEX_LINES = 200             # MEMORY.md: "first 200 lines ..."
MEMORY_INDEX_BYTES = 25 * 1024       # "... or the first 25KB"
RECOMMENDED_LINES = 200              # "target under 200 lines per file"
SKIPPED_BYTES = 4 * 1024 * 1024      # "Claude Code skips a file over 4 MiB"
CHARS_PER_TOKEN = 4                  # rough estimate, not a tokenizer

# Missing imports listed per file; the rest are summed up in one line.
MAX_MISSING_PER_FILE = 20

# Never read more than this from one file: the 4 MiB skip already says the
# file is not loaded, and a 2 GB file must not be pulled into memory.
_READ_CAP = SKIPPED_BYTES + 1

# A fence may sit behind list markers or blockquote marks (`- ```` ,
# `1. ~~~`, `> ````) and be indented.
_FENCE = re.compile(r"^(?:[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+|[ \t]*>[ \t]?)*"
                    r"[ \t]*(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_COMMENT_START = re.compile(r"^ {0,3}<!--")
# `@` at the start of a line or after whitespace (so `me@example.com` is not
# an import), then the path: anything up to whitespace, `\ ` kept as a space.
_IMPORT = re.compile(r"(?:^|(?<=\s))@((?:\\ |[^\s])+)")
_TRAILING = ".,;:!?)]}>'\""
# C0 and C1 control characters but tab: ESC, BEL, CSI (0x9b), ...
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def safe(text: str) -> str:
    """text with every control character but tab shown as `?` - nothing a
    memory file says can move the cursor, clear the screen or write to the
    clipboard of whoever reads the report (or a CI log)."""
    return _CONTROL.sub("?", text)


def _parts(path: str) -> list[str]:
    return os.path.normpath(path).replace("\\", "/").split("/")


def _is_rules_file(path: str) -> bool:
    """A markdown file under `.claude/rules/` (the project's or your user
    one, ~/.claude/rules/ or $CLAUDE_CONFIG_DIR/rules/) - judged from the
    `.claude` folder down, so a project that itself lives under some folder
    called `rules` is not taken for one."""
    name = os.path.basename(path)
    if name in CONTEXT_NAMES or not name.endswith(".md"):
        return False
    parts = _parts(os.path.dirname(path))
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == ".claude":
            return i + 1 < len(parts) and parts[i + 1] == "rules"
    cfg = _parts(core_mod._claude_config_dir())
    return (parts[:len(cfg)] == cfg and len(parts) > len(cfg)
            and parts[len(cfg)] == "rules")


def loads_into_context(path: str) -> bool:
    return os.path.basename(path) in CONTEXT_NAMES or _is_rules_file(path)


def _read_text(path: str) -> tuple[str, int] | str:
    """(text, size in bytes) - at most _READ_CAP bytes of text are read; the
    size is the file's real size. The reason, as a string, when it cannot
    be read."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(_READ_CAP)
    except (OSError, ValueError) as e:
        return getattr(e, "strerror", None) or str(e) or type(e).__name__
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


def _balanced(value: str) -> bool:
    """Brackets and quotes of a YAML flow value close, in order."""
    stack, quote = [], None
    pairs = {"]": "[", "}": "{"}
    i = 0
    while i < len(value):
        c = value[i]
        if quote:
            if c == quote:
                if quote == "'" and value[i + 1:i + 2] == "'":
                    i += 1   # '' is an escaped quote
                else:
                    quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "\"'" and (value[:i].rstrip()[-1:] in ("", "[", "{", ",",
                                                          ":")):
            quote = c   # a quote opens a scalar only where a scalar starts
        elif c == "#" and (i == 0 or value[i - 1] in " \t") and not stack:
            break   # a comment
        elif c in "[{":
            stack.append(c)
        elif c in "]}":
            if not stack or stack.pop() != pairs[c]:
                return False
        i += 1
    return not stack and quote is None


def front_matter_parses(fm: str) -> bool:
    """A light check, not a YAML parser (the package has no dependencies):
    False for what YAML always rejects - a tab used to indent, a top-level
    line that is not `key: value`, a list item or a comment, and a flow
    value (`[...]`, `{...}`, a quoted string) that never closes. Anything
    subtler is taken as parsing."""
    for line in fm.split("\n"):
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("\t") or re.match(r"^ +\t", line):
            return False
        if line[:1] in " -":
            body = line.strip()
            if body.startswith("- "):
                body = body[2:]
            elif body == "-":
                continue
            if body.startswith(("[", "{", '"', "'")) and not _balanced(body):
                return False
            continue
        m = re.match(r"^(\"[^\"]*\"|'[^']*'|[^\s:#][^:#]*?)\s*:(?:\s+(.*))?$",
                     line)
        if not m:
            return False
        value = (m.group(2) or "").strip()
        if value.startswith(("[", "{", '"', "'")) and not _balanced(value):
            return False
    return True


def is_path_scoped(text: str) -> bool:
    """A rules file with a `paths:` key in front matter that parses loads
    only when Claude works with matching files. Front matter that does not
    parse is ignored by Claude Code: the rule loads as if it had no
    `paths`."""
    fm, _ = split_front_matter(text)
    return (fm is not None and re.search(r"(?m)^paths\s*:", fm) is not None
            and front_matter_parses(fm))


def _scan(text: str, strip_comments: bool = True) -> tuple[str, list[str]]:
    """(text as Claude Code loads it, `@path` imports in order).

    Code blocks - fenced (also behind a list marker or `>`) and indented by
    four spaces or a tab after a blank line, outside a list - and code spans
    are never searched for imports. Block-level HTML comments outside code
    blocks are dropped from the text (and not searched)."""
    kept: list[str] = []
    imports: list[str] = []
    fence = None
    in_comment = False
    indented = False      # inside an indented code block
    prev_blank = True
    in_list = False
    for line in text.split("\n"):
        bare = line.rstrip("\r")
        if fence is not None:
            m = _FENCE.match(bare)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not bare[m.end():].strip():
                fence = None
            kept.append(line)
            continue
        if in_comment:
            end = bare.find("-->")
            if end < 0:
                continue
            in_comment = False
            bare = line = bare[end + 3:]
            if not bare.strip():
                continue
        blank = not bare.strip()
        if indented and (blank or _INDENTED.match(bare)):
            kept.append(line)
            prev_blank = blank
            continue
        indented = False
        if (_INDENTED.match(bare) and prev_blank and not in_list
                and not blank):
            indented = True
            kept.append(line)
            prev_blank = False
            continue
        m = _FENCE.match(bare)
        if m:
            fence = m.group(1)
            kept.append(line)
            prev_blank = False
            continue
        if strip_comments and _COMMENT_START.match(bare):
            start = bare.index("<!--")
            end = bare.find("-->", start + 4)
            if end < 0:
                in_comment = True
                continue
            rest = bare[end + 3:]
            if not rest.strip():
                continue   # a one-line comment block
            bare = line = rest
        if not blank:
            if _LIST_ITEM.match(bare):
                in_list = True
            elif not _INDENTED.match(bare) and prev_blank:
                in_list = False
        prev_blank = blank
        kept.append(line)
        for im in _IMPORT.finditer(_CODE_SPAN.sub(" ", bare)):
            raw = im.group(1)
            if raw[:1] in "\"'":
                continue   # a quoted path is not imported
            imports.append(raw.replace("\\ ", " "))
    return "\n".join(kept), imports


def find_imports(text: str) -> list[str]:
    """The `@path` imports in a memory file, in order, as written (`\\ `
    turned into a space)."""
    return _scan(text)[1]


def strip_comments(text: str) -> str:
    return _scan(text)[0]


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
            p = os.path.expanduser("~" + cand[1:].replace("\\", os.sep))
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


def _under(k: str, base: str) -> bool:
    return k == base or k.startswith(base.rstrip(os.sep) + os.sep)


def _covered(path: str, watched: list[str]) -> bool:
    """Is path - its real location - inside one of `watched` (real)?"""
    k = _key(path)
    return any(_under(k, _key(w)) for w in watched)


def _spelled_under(path: str, base: str) -> bool:
    """Inside base as spelled, links not followed: where a file SAYS it is."""
    return _under(os.path.normcase(os.path.abspath(path)),
                  os.path.normcase(os.path.abspath(base)))


def analyze(project_root: str, top_dupes: int = 10) -> dict:
    paths = core_mod.watched_paths(project_root)
    files = list(core_mod.iter_files(paths))  # honours IGNORE_PATTERNS
    context = [fp for fp in files if loads_into_context(fp)]
    cfg_dir = core_mod._claude_config_dir()
    per_file: list[dict] = []
    on_demand: list[dict] = []
    external: list[dict] = []
    warnings: list[dict] = []
    unprotected: set[str] = set()
    unprotected_elsewhere: set[str] = set()
    line_owners: dict[str, set[str]] = {}
    seen: set[str] = set()
    missing: dict[str, set[str]] = {}

    def warn(code: str, path: str, message: str) -> None:
        warnings.append({"code": code, "path": path, "message": message})

    # breadth-first: top-level files first, so a file that is both watched
    # and imported is counted once, as itself. from_project: the chain
    # started at a file the project holds (as spelled) - not a user-level
    # one such as ~/.claude/CLAUDE.md, which you wrote yourself and Claude
    # Code trusts.
    queue = collections.deque(
        (fp, None, 0, _spelled_under(fp, project_root)) for fp in context)
    while queue:
        fp, importer, depth, from_project = queue.popleft()
        key = _key(fp)
        if key in seen:
            continue
        seen.add(key)
        got = _read_text(fp)
        if isinstance(got, str):
            warn("unreadable", fp, f"cannot be read ({got}) - not counted")
            continue
        text, size = got
        if size > SKIPPED_BYTES:
            warn("skipped", fp, f"{size / 1048576:.1f} MiB - Claude Code "
                 f"skips a file over 4 MiB, so none of it is loaded")
            continue
        if from_project and not _covered(fp, [project_root]):
            # A symlinked rule or an import that leads out of the project:
            # Claude Code loads it only once you approve external imports.
            # Never counted as loaded, never quoted, never followed further.
            external.append({"path": fp, "chars": len(text),
                             **({"imported_by": importer} if importer else {})})
            continue
        if importer is None and _is_rules_file(fp) and is_path_scoped(text):
            on_demand.append({"path": fp, "chars": len(text),
                              "lines": len(text.splitlines())})
            continue
        is_index = os.path.basename(fp) == "MEMORY.md" and importer is None
        if _is_rules_file(fp):
            fm, body = split_front_matter(text)
            if fm is not None:
                if front_matter_parses(fm):
                    text = body   # stripped before loading
                elif re.search(r"(?m)^paths\s*:", fm):
                    warn("bad-front-matter", fp, "its front matter does not "
                         "parse, so Claude Code ignores its `paths` and loads "
                         "it at every session start")
        text, imports = _scan(text, strip_comments=not is_index)
        lines = text.splitlines()
        entry = {"path": fp, "chars": len(text), "lines": len(lines)}
        if importer is not None:
            entry["imported_by"] = importer
            if not _covered(fp, paths):
                # Suggest `watch --add` only for what is plainly memory: in
                # the project or in ~/.claude. Anything else may be a secret
                # (`@~/.aws/credentials.md`) - copying it into the snapshots
                # repo is the user's call, not ours.
                if _covered(fp, [project_root, cfg_dir]):
                    unprotected.add(fp)
                else:
                    unprotected_elsewhere.add(fp)
        loaded = text
        if is_index:
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
        elif (importer is None and os.path.basename(fp) in INSTRUCTION_NAMES
              and len(lines) > RECOMMENDED_LINES):
            warn("long-file", fp, f"{len(lines)} lines - Claude Code "
                 f"recommends under {RECOMMENDED_LINES} per CLAUDE.md file "
                 f"(longer files cost context and reduce adherence)")
        per_file.append(entry)
        owned = set()
        for ln in loaded.splitlines():
            s = ln.strip()
            if len(s) >= MIN_LINE_LEN and s not in owned:
                owned.add(s)
                line_owners.setdefault(s, set()).add(fp)
        if is_index:
            continue   # an index lists topic files; they load on demand
        for raw in imports:
            target = _resolve_import(raw, fp)
            if target is None:
                name = raw.rstrip(_TRAILING)
                if _looks_like_file(raw) and name not in missing.setdefault(
                        fp, set()):
                    missing[fp].add(name)
                    if len(missing[fp]) <= MAX_MISSING_PER_FILE:
                        warn("missing-import", fp,
                             f"imports @{name}, which does not exist")
                continue
            if depth + 1 > MAX_IMPORT_DEPTH:
                if _key(target) not in seen:
                    warn("import-too-deep", fp,
                         f"@{raw} is more than {MAX_IMPORT_DEPTH} imports "
                         f"deep - Claude Code does not load it")
                continue
            queue.append((target, fp, depth + 1, from_project))
    for fp, names in missing.items():
        if len(names) > MAX_MISSING_PER_FILE:
            warn("missing-import", fp,
                 f"... and {len(names) - MAX_MISSING_PER_FILE} more missing "
                 f"imports")

    dupes = [{"line": ln[:100], "files": sorted(owners), "count": len(owners)}
             for ln, owners in line_owners.items() if len(owners) > 1]
    dupes.sort(key=lambda d: (-d["count"], -len(d["line"])))
    per_file.sort(key=lambda f: -f["chars"])
    total = sum(f["chars"] for f in per_file)
    return {"files": per_file, "total_chars": total,
            "total_tokens": total // CHARS_PER_TOKEN,
            "on_demand": on_demand,
            "external": external,
            "duplicates": dupes[:top_dupes],
            "n_duplicates": len(dupes),
            "n_not_context": len(files) - len(context),
            "warnings": warnings,
            "unprotected": sorted(unprotected),
            "n_unprotected_elsewhere": len(unprotected_elsewhere)}


def problems(report: dict, budget: int | None = None,
             project_root: str | None = None) -> list[str]:
    """What `diet --check` fails on: every warning, and a session-start
    total over `budget` tokens. Already passed through safe()."""
    def name(p: str) -> str:
        return _short(p, project_root) if project_root else p
    out = [safe(f"{name(w['path'])}: {w['message']}")
           for w in report["warnings"]]
    if budget is not None and report["total_tokens"] > budget:
        out.append(f"~{report['total_tokens']:,} tokens load at session "
                   f"start, over the budget of {budget:,}")
    return out


def render(report: dict, project_root: str | None = None) -> str:
    def name(p: str) -> str:
        return safe(_short(p, project_root) if project_root else p)

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
    if report.get("external"):
        lines.append("## Outside the project: loads only once you approve "
                     "external imports in Claude Code, not counted")
        for f in report["external"]:
            via = (f" (imported by `{name(f['imported_by'])}`)"
                   if f.get("imported_by") else " (a link out of the project)")
            lines.append(f"- `{name(f['path'])}`{via} — {f['chars']:,} chars")
        lines.append("")
    if report.get("warnings"):
        lines.append("## Warnings")
        for w in report["warnings"]:
            lines.append(f"- `{name(w['path'])}`: {safe(w['message'])}")
        lines.append("")
    if report.get("unprotected") or report.get("n_unprotected_elsewhere"):
        lines.append("## Imported but not protected by undo")
        for p in report["unprotected"]:
            lines.append(f"- `{name(p)}` — `sessionsteward watch --add "
                         f"{safe(core_mod.shell_word(name(p)))}`")
        n = report.get("n_unprotected_elsewhere") or 0
        if n:
            lines.append(f"- {n} imported file(s) outside the project and "
                         f"~/.claude: not suggested, since they may hold "
                         f"secrets that a snapshot would copy")
        lines.append("")
    if report["duplicates"]:
        lines.append(f"## Duplicated content "
                     f"({report['n_duplicates']} lines in 2+ files)")
        for d in report["duplicates"]:
            lines.append(f"- _{safe(d['line'])}_ …")
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
