"""`diet --lint`: dead, duplicated and contradicting lines in the files that
load into context, with the tokens each kind would give back.

Every finding is a pointer for you to look at, not a verdict:

- exact duplicates inside one file (the cross-file ones `diet` already lists);
- near-duplicates: lines that say the same thing in other words, or differ
  only in a number ("rule 3 ..." / "rule 4 ...") - grouped, one group per
  idea, and the tokens you would save by keeping one line of each group;
- possibly dead references: a path in backticks that does not exist in the
  project (generated folders and placeholders are left out);
- possible contradictions: two lines about the same thing, one telling the
  agent to do it ("always use X") and one telling it not to ("never use X").
  Only a flip of that kind is found; "prefer tabs" against "prefer spaces"
  is not.

Token counts are the characters / 4 estimate used everywhere else.
"""

from __future__ import annotations

import os
import re

from . import diet as diet_mod
from . import textlines as tl

NEAR_DUP_MIN_WORDS = 5          # content words in a line to take part
NEAR_DUP_THRESHOLD = 0.75
CONTRADICTION_SUBJECT = 0.7
CONTRADICTION_MIN_WORDS = 3
MAX_LINES_PER_ANALYSIS = 20000
MAX_REPORTED = 15               # items per kind in the text report

_POLARITY_WORDS = frozenset(tl.stem(w) for w in
    "never don't dont do not must mustn't should shouldn't avoid stop using "
    "no longer forbidden prohibited allowed cannot can't won't refrain "
    "always ensure make sure be required require prefer use".split())

_TICKS = re.compile(r"`([^`\n]{2,200})`")
_EXT = (r"py|js|jsx|ts|tsx|mjs|cjs|md|mdc|json|ya?ml|toml|ini|cfg|sh|bash|zsh|"
        r"ps1|go|rs|rb|java|kt|c|h|cpp|hpp|cs|php|swift|css|scss|html|sql|"
        r"txt|lock|env|gradle|xml")
_PATHISH = re.compile(rf"^(?:\.{{0,2}}/)?[\w@.\-]+(?:/[\w@.\-]+)+/?$|"
                      rf"^[\w@.\-]+\.(?:{_EXT})$")
_LINESUFFIX = re.compile(r"(?::\d+(?::\d+)?|#L?\d+.*)$")
_GENERATED = ("dist/", "build/", "out/", "target/", "node_modules/", ".venv/",
              "venv/", "__pycache__/", ".git/", "coverage/", ".next/",
              ".cache/", "tmp/", "temp/", "logs/", "log/")
_PLACEHOLDER = re.compile(
    r"(?i)(?:^|[/_.\-])(?:path|to|your|my|foo|bar|baz|example|sample|xxx|"
    r"name|file|dir|folder|something|filename|project|repo|module)(?:[/_.\-]|$)"
    r"|[<>{}$*?|\\\[\]\"' ]|^\.\.\.|://|\.\.\.$|^[~@]")
_ENV_FILE = re.compile(r"(^|/)\.env(\.|$)")


def _loaded_texts(report: dict) -> dict[str, str]:
    """{path: text} of every file the diet report counted as loaded."""
    out = {}
    for f in report["files"]:
        got = diet_mod._read_text(f["path"])
        if isinstance(got, str):
            continue
        text, _ = got
        out[f["path"]] = text
    return out


def _code_lines(text: str) -> set[int]:
    inside, fence = set(), None
    for no, raw in enumerate(text.split("\n"), 1):
        m = re.match(r"^\s*(?:[-*+]\s+)?(`{3,}|~{3,})", raw)
        if m:
            fence = m.group(1)[0] if fence is None else (
                None if m.group(1)[0] == fence else fence)
            continue
        if fence:
            inside.add(no)
    return inside


def exact_in_file(texts: dict[str, str]) -> list[dict]:
    """A line that appears twice in one file: the second and later copies."""
    out = []
    for path, text in texts.items():
        first: dict[str, int] = {}
        for u in tl.units(text, keep_code=False):
            if u.heading:
                continue
            if u.norm in first:
                out.append({"path": path, "line": u.line,
                            "first_line": first[u.norm], "text": u.raw.strip(),
                            "tokens": tl.est_tokens(u.raw) + 1})
            else:
                first[u.norm] = u.line
    return out


def near_duplicates(texts: dict[str, str]) -> list[dict]:
    """Groups of lines that say (nearly) the same thing; one entry per group.
    Exact copies are not groups of their own: they join the group of their
    near neighbours, or are left to exact_in_file / diet's cross-file list.
    """
    items = []
    for path, text in sorted(texts.items()):
        for u in tl.units(text, keep_code=False):
            if u.heading:
                continue
            cw = tl.content_words(tl.shape(u.norm))
            if len(cw) >= NEAR_DUP_MIN_WORDS:
                items.append((path, u, cw, tl.polarity(u.norm)))
    if len(items) > MAX_LINES_PER_ANALYSIS:
        items = items[:MAX_LINES_PER_ANALYSIS]
    sets = [it[2] for it in items]
    index = tl.Index(sets, max_posting=250)
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(items)):
        for j in index.candidates(i, limit=20):
            # "always X" next to "never X" is a contradiction, not a repeat
            if (j > i and items[i][3] * items[j][3] >= 0
                    and tl.jaccard(sets[i], sets[j]) >= NEAR_DUP_THRESHOLD):
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        # one line of the group stays: the first; it is the example
        # (exact copies inside one file are exact_in_file's, skip a group
        # made only of those)
        norms = {(items[m][0], items[m][1].norm) for m in members}
        if len(norms) < 2:
            continue
        keep = members[0]
        extra = members[1:]
        out.append({
            "example": items[keep][1].raw.strip(),
            "example_at": f"{items[keep][0]}:{items[keep][1].line}",
            "count": len(members),
            "files": sorted({items[m][0] for m in members}),
            "tokens_saved": sum(tl.est_tokens(items[m][1].raw) + 1
                                for m in extra),
            "at": [f"{items[m][0]}:{items[m][1].line}" for m in members[:50]],
        })
    out.sort(key=lambda g: (-g["tokens_saved"], g["example_at"]))
    return out


def contradictions(texts: dict[str, str]) -> list[dict]:
    """Groups of lines about one thing where some say to do it and some say
    not to. One entry per group: an example of each side and the counts."""
    items = []
    for path, text in sorted(texts.items()):
        for u in tl.units(text, keep_code=False):
            if u.heading:
                continue
            pol = tl.polarity(u.norm)
            if not pol:
                continue
            subj = frozenset(w for w in tl.content_words(tl.shape(u.norm))
                             if w not in _POLARITY_WORDS)
            if len(subj) >= CONTRADICTION_MIN_WORDS:
                items.append((path, u, pol, subj))
    items = items[:MAX_LINES_PER_ANALYSIS]
    sets = [s for _, _, _, s in items]
    index = tl.Index(sets, max_posting=250)
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(items)):
        for j in index.candidates(i, limit=20):
            if j > i and tl.jaccard(sets[i], sets[j]) >= CONTRADICTION_SUBJECT:
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        pos = [m for m in members if items[m][2] > 0]
        neg = [m for m in members if items[m][2] < 0]
        if not pos or not neg:
            continue
        a, b = items[pos[0]], items[neg[0]]
        out.append({"a": f"{a[0]}:{a[1].line}", "a_text": a[1].raw.strip(),
                    "b": f"{b[0]}:{b[1].line}", "b_text": b[1].raw.strip(),
                    "do": len(pos), "dont": len(neg),
                    "at": [f"{items[m][0]}:{items[m][1].line}"
                           for m in members[:50]],
                    "score": round(tl.jaccard(a[3], b[3]), 2)})
    out.sort(key=lambda c: (-(c["do"] + c["dont"]), c["a"]))
    return out


def dead_references(texts: dict[str, str], project_root: str) -> list[dict]:
    out = []
    root = os.path.abspath(project_root)
    for path, text in sorted(texts.items()):
        here = os.path.dirname(path)
        code = _code_lines(text)
        seen = set()
        for no, raw in enumerate(text.split("\n"), 1):
            if no in code:
                continue
            for m in _TICKS.finditer(raw):
                ref = m.group(1).strip()
                if " " in ref or not _PATHISH.match(_LINESUFFIX.sub("", ref)):
                    continue
                ref = _LINESUFFIX.sub("", ref)
                low = ref.lower().lstrip("./")
                if (low.startswith(_GENERATED) or _PLACEHOLDER.search(ref)
                        or _ENV_FILE.search(low)):
                    continue
                if (path, ref) in seen:
                    continue
                seen.add((path, ref))
                cands = [os.path.join(root, ref), os.path.join(here, ref)]
                if os.path.isabs(ref):
                    cands = [ref]
                if any(os.path.lexists(c) for c in cands):
                    continue
                out.append({"path": path, "line": no, "ref": ref,
                            "tokens": tl.est_tokens(raw) + 1,
                            "text": raw.strip()})
    return out


def lint(report: dict, project_root: str) -> dict:
    """The lint section for a diet report (the files it counted as loaded).
    """
    texts = _loaded_texts(report)
    exact = exact_in_file(texts)
    near = near_duplicates(texts)
    contra = contradictions(texts)
    dead = dead_references(texts, project_root)
    saved_exact = sum(x["tokens"] for x in exact)
    saved_near = sum(g["tokens_saved"] for g in near)
    return {
        "exact_in_file": exact,
        "near_duplicates": near,
        "contradictions": contra,
        "dead_references": dead,
        "tokens_saved_if_deduped": saved_exact + saved_near,
        "tokens_in_dead_reference_lines": sum(x["tokens"] for x in dead),
        # near-duplicate and contradiction analysis stops at this many
        # lines; say so rather than report a clean result for the rest
        "lines_not_analysed": max(
            0, sum(t.count("\n") + 1 for t in texts.values())
            - MAX_LINES_PER_ANALYSIS),
    }


def problems(lint_report: dict, name) -> list[str]:
    out = []
    for x in lint_report["exact_in_file"]:
        out.append(f"{name(x['path'])}:{x['line']}: same line as line "
                   f"{x['first_line']}")
    for g in lint_report["near_duplicates"]:
        out.append(f"{g['example_at']}: {g['count']} lines say the same "
                   f"thing (~{g['tokens_saved']} tokens)")
    for c in lint_report["contradictions"]:
        out.append(f"{c['a']} and {c['b']} may contradict each other "
                   f"({c['do']} vs {c['dont']} lines)")
    for d in lint_report["dead_references"]:
        out.append(f"{name(d['path'])}:{d['line']}: `{d['ref']}` does not "
                   f"exist")
    return out


def render(lint_report: dict, name, redact) -> str:
    safe = diet_mod.safe

    def q(t, n=110):
        t = safe(redact(t))
        return t if len(t) <= n else t[:n - 3] + "..."

    def at(s):          # "path:line" with the path shown short
        p, _, ln = s.rpartition(":")
        return f"{safe(name(p))}:{ln}"

    L = ["", "## Lint (a pointer for you to check, not a verdict)"]
    ex, nd = lint_report["exact_in_file"], lint_report["near_duplicates"]
    co, dr = lint_report["contradictions"], lint_report["dead_references"]
    L.append(f"~{lint_report['tokens_saved_if_deduped']:,} tokens would be "
             f"given back by keeping one line per idea; "
             f"`sessionsteward diet --fix` previews the safe part")
    if lint_report.get("lines_not_analysed"):
        L.append(f"NOTE: about {lint_report['lines_not_analysed']:,} lines "
                 f"past the first {MAX_LINES_PER_ANALYSIS:,} were not "
                 f"checked for near-duplicates or contradictions")
    if ex:
        L.append(f"\n### Same line twice in one file ({len(ex)})")
        for x in ex[:MAX_REPORTED]:
            L.append(f"- {safe(name(x['path']))}:{x['line']} repeats line "
                     f"{x['first_line']}: _{q(x['text'], 80)}_")
        if len(ex) > MAX_REPORTED:
            L.append(f"- ... {len(ex) - MAX_REPORTED} more")
    if nd:
        L.append(f"\n### Lines that say the same thing ({len(nd)} groups)")
        for g in nd[:MAX_REPORTED]:
            L.append(f"- {g['count']} lines, ~{g['tokens_saved']} tokens "
                     f"extra, e.g. {at(g['example_at'])}: "
                     f"_{q(g['example'], 80)}_")
        if len(nd) > MAX_REPORTED:
            L.append(f"- ... {len(nd) - MAX_REPORTED} more groups")
    if co:
        L.append(f"\n### Possible contradictions ({len(co)} topics)")
        for c in co[:MAX_REPORTED]:
            L.append(f"- {c['do']} line(s) say to, {c['dont']} say not to:")
            L.append(f"  {at(c['a'])}: _{q(c['a_text'], 70)}_")
            L.append(f"  vs {at(c['b'])}: _{q(c['b_text'], 70)}_")
        if len(co) > MAX_REPORTED:
            L.append(f"- ... {len(co) - MAX_REPORTED} more")
    if dr:
        L.append(f"\n### Paths that do not exist ({len(dr)})")
        for d in dr[:MAX_REPORTED]:
            L.append(f"- {safe(name(d['path']))}:{d['line']}: "
                     f"`{safe(d['ref'])}`")
        if len(dr) > MAX_REPORTED:
            L.append(f"- ... {len(dr) - MAX_REPORTED} more")
    if not (ex or nd or co or dr):
        L.append("\nNo duplicate, contradicting or dead lines found.")
    L.append("")
    return "\n".join(L)


# ---- diet --fix: the part that is safe to do for you ----------------------

MIN_FIX_NORM = 20     # a shorter repeated line may be a label that repeats


def _fix_bytes(raw: bytes) -> tuple[bytes, list[tuple[int, str]], int]:
    """(new bytes, [(line number, text) removed], blank lines collapsed).
    Removes the second and later copies of a line that repeats inside the
    file (outside code fences, headings and short labels excepted) and
    squeezes runs of blank lines to one. Line endings are kept as they
    are; the bytes of every line that stays are untouched."""
    lines = raw.split(b"\n")
    seen: set[str] = set()
    out: list[bytes] = []
    removed: list[tuple[int, str]] = []
    collapsed = 0
    fence = None
    blanks = 0
    for no, b in enumerate(lines, 1):
        text = b.decode("utf-8", "replace").rstrip("\r")
        m = re.match(r"^\s*(?:[-*+]\s+)?(`{3,}|~{3,})", text)
        if m:
            fence = m.group(1)[0] if fence is None else (
                None if m.group(1)[0] == fence else fence)
            blanks = 0
            out.append(b)
            continue
        if fence is None and not text.strip():
            blanks += 1
            if blanks > 1 and no < len(lines):
                collapsed += 1
                continue
            out.append(b)
            continue
        blanks = 0
        if fence is None and not text.lstrip().startswith("#"):
            norm = tl.normalise(text)
            if len(norm) >= MIN_FIX_NORM:
                if norm in seen:
                    removed.append((no, text.strip()))
                    continue
                seen.add(norm)
        out.append(b)
    return b"\n".join(out), removed, collapsed


def fix_plan(report: dict, project_root: str) -> list[dict]:
    """What `diet --fix` would change: only files that are watched (so a
    snapshot protects them), regular files and not links, and only if the
    result is smaller."""
    from . import core as core_mod
    watched = core_mod.watched_paths(project_root)
    plan = []
    for f in report["files"]:
        path = f["path"]
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        if not diet_mod._covered(path, watched):
            continue
        got = diet_mod._read_text(path)
        if isinstance(got, str) or got[1] > diet_mod.SKIPPED_BYTES:
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        new, removed, collapsed = _fix_bytes(raw)
        if new != raw and len(new) < len(raw):
            plan.append({"path": path, "old": raw, "new": new,
                         "removed": removed, "collapsed": collapsed,
                         "tokens": (len(raw) - len(new)) // tl.CHARS_PER_TOKEN})
    return plan


def apply_plan(plan: list[dict]) -> tuple[list[str], list[str]]:
    """Write the plan. A file that changed since it was planned is left
    alone. Each write goes to a temporary file next to the original and
    replaces it in one step, so a crash never leaves half a file.
    Returns (written paths, skipped 'path: reason')."""
    import tempfile
    written, skipped = [], []
    for item in plan:
        path = item["path"]
        try:
            if os.path.islink(path):
                skipped.append(f"{path}: is a link now"); continue
            with open(path, "rb") as fh:
                if fh.read() != item["old"]:
                    skipped.append(f"{path}: changed since it was read "
                                   f"(left untouched)"); continue
            mode = os.stat(path).st_mode & 0o7777
            fd, tmp = tempfile.mkstemp(prefix=".diet-", dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(item["new"])
                os.chmod(tmp, mode)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            written.append(path)
        except OSError as e:
            skipped.append(f"{path}: {e.strerror or e}")
    return written, skipped
