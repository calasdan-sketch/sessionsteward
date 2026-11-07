"""What did compaction (or an agent edit) drop?

`diff` shows every added and removed line, so a file that was merely
re-ordered or lightly reworded looks like a wall of changes. `lost`
compares the *instructions*: a line that is still there (same words, any
list marker or case) is kept; a line that moved to another file is moved;
a line that came back reworded is paired with its new wording; what is left
is what was really dropped, with the tokens it used to cost.
"""

from __future__ import annotations

from . import secrets as secrets_mod
from . import textlines as tl

REWORD_THRESHOLD = 0.6      # shared content words / all content words
CONTAINED_THRESHOLD = 0.85  # an old line whose words are all still in a new one
MIN_CONTAINED_WORDS = 4
MAX_LINES = 50000           # per side; the rest is ignored (and said so)


def compare(old: dict[str, str], new: dict[str, str]) -> dict:
    """old, new: {path: text}. Returns the report (see keys below)."""
    old_units = []
    new_units = []
    truncated = False
    for path in sorted(old):
        for u in tl.units(old[path]):
            old_units.append((path, u))
    for path in sorted(new):
        for u in tl.units(new[path]):
            new_units.append((path, u))
    if len(old_units) > MAX_LINES or len(new_units) > MAX_LINES:
        truncated = True
        old_units = old_units[:MAX_LINES]
        new_units = new_units[:MAX_LINES]

    where: dict[str, list[str]] = {}
    for p, u in new_units:
        where.setdefault(u.norm, []).append(p)
    new_sets = [tl.content_words(u.norm) for _, u in new_units]
    index = tl.Index(new_sets, max_posting=2000)

    kept = 0
    moved, reworded, reversed_, dropped = [], [], [], []
    seen = set()
    claimed: set[int] = set()   # new lines that explain an old one
    for p, u in old_units:
        key = (p, u.norm)
        if key in seen:
            continue
        seen.add(key)
        homes = where.get(u.norm)
        if homes:
            if p in homes:
                kept += 1
            else:
                moved.append({"path": p, "line": u.line, "text": u.raw.strip(),
                              "to": homes[0]})
            continue
        best, best_score = None, 0.0
        a = tl.content_words(u.norm)
        if a:
            for j in index.query(a, limit=30):
                b = new_sets[j]
                score = tl.jaccard(a, b)
                if len(a) >= MIN_CONTAINED_WORDS:
                    contained = len(a & b) / len(a)
                    if contained >= CONTAINED_THRESHOLD:
                        score = max(score, contained)
                if score > best_score:
                    best, best_score = j, score
        if best is not None and best_score >= REWORD_THRESHOLD:
            tp, tu = new_units[best]
            claimed.add(best)
            pa, pb = tl.polarity(u.norm), tl.polarity(tu.norm)
            (reversed_ if pa and pb and pa != pb else reworded).append({"path": p, "line": u.line, "text": u.raw.strip(),
                             "to": tp, "to_line": tu.line,
                             "to_text": tu.raw.strip(),
                             "score": round(best_score, 2)})
        else:
            dropped.append({"path": p, "line": u.line, "text": u.raw.strip(),
                            "tokens": max(1, tl.est_tokens(u.raw) + 1),
                            "heading": u.heading})
    old_norms = {u.norm for _, u in old_units}
    added = sum(1 for i, (_, u) in enumerate(new_units)
                if u.norm not in old_norms and i not in claimed)
    gone = sorted(p for p in old if p not in new)
    return {"kept": kept, "moved": moved, "reworded": reworded, "reversed": reversed_,
            "dropped": dropped, "added": added, "files_gone": gone,
            "tokens_dropped": sum(d["tokens"] for d in dropped),
            "truncated": truncated}


def render(report: dict, name, limit: int = 60) -> str:
    """Text for the terminal. `name` maps a path to what is shown for it;
    everything quoted from a file goes through redaction and `safe`."""
    from . import diet as diet_mod
    safe = diet_mod.safe

    def q(text: str, n: int = 140) -> str:
        text = secrets_mod.redact_line(text)
        text = safe(text)
        return text if len(text) <= n else text[:n - 3] + "..."

    d, rw, mv = report["dropped"], report["reworded"], report["moved"]
    rv = report["reversed"]
    out = []
    if not (d or rw or mv or rv):
        out.append("nothing lost: every instruction of the older version is "
                   f"still there ({report['kept']} kept, "
                   f"{report['added']} new)")
        return "\n".join(out)
    out.append(f"{len(d)} dropped (~{report['tokens_dropped']:,} tokens), "
               f"{len(rw)} reworded, {len(rv)} reversed, {len(mv)} moved to another file, "
               f"{report['kept']} kept, {report['added']} new")
    if report["files_gone"]:
        out.append("files gone: " + ", ".join(safe(name(p))
                                              for p in report["files_gone"]))
    if report.get("truncated"):
        out.append(f"(only the first {MAX_LINES:,} lines of each side were "
                   f"compared)")
    if d:
        out.append("")
        out.append("DROPPED (in the older version, nowhere in the newer):")
        for x in d[:limit]:
            out.append(f"  - {safe(name(x['path']))}:{x['line']}  "
                       f"{q(x['text'])}")
        if len(d) > limit:
            out.append(f"  ... {len(d) - limit} more (use --json)")
    if rv:
        out.append("")
        out.append("REVERSED (the rule now says the opposite):")
        for x in rv[:limit]:
            out.append(f"  - {safe(name(x['path']))}:{x['line']}  "
                       f"{q(x['text'], 90)}")
            out.append(f"    -> {q(x['to_text'], 90)}")
        if len(rv) > limit:
            out.append(f"  ... {len(rv) - limit} more (use --json)")
    if rw:
        out.append("")
        out.append("REWORDED (check the meaning survived):")
        for x in rw[:limit]:
            out.append(f"  - {safe(name(x['path']))}:{x['line']}  "
                       f"{q(x['text'], 90)}")
            out.append(f"    -> {q(x['to_text'], 90)}   "
                       f"[{int(x['score'] * 100)}% alike]")
        if len(rw) > limit:
            out.append(f"  ... {len(rw) - limit} more (use --json)")
    if mv:
        out.append("")
        out.append(f"MOVED: {len(mv)} line(s) now live in another file "
                   f"(not lost)")
    return "\n".join(out)
