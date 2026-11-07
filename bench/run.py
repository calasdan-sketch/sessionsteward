#!/usr/bin/env python3
"""Offline benchmark for sessionsteward's memory-hygiene features.

    python bench/run.py            # print the tables, write bench/RESULTS.md
    python bench/run.py --check    # exit 1 if a headline number regressed

No network, no model, no paid service: lines are synthetic (see corpus.py),
token counts are the characters / 4 estimate the tool uses. The numbers say
how the detectors behave on this corpus, not on your files.
"""

from __future__ import annotations

import difflib
import json
import os
import random
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

SEEDS = list(range(1, 21))
# headline floors, enforced by --check (and by tests/test_bench.py)
FLOORS = {"exact_recall": 1.0, "near_recall": 0.9, "near_precision": 0.9,
          "contra_recall": 0.9, "dead_recall": 1.0, "dead_precision": 1.0,
          "secret_recall": 1.0, "secret_precision": 0.9,
          "lost_recall": 0.95, "lost_precision": 0.9,
          "tokens_saved_pct": 5.0}


def _env():
    home = tempfile.mkdtemp(prefix="ss-bench-home-")
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    return home


def _norm(t):
    from sessionsteward import textlines as tl
    return tl.normalise(t)


def _line_text(root, ref):
    path, _, no = ref.rpartition(":")
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")[int(no) - 1]


def _count_in_file(root, rel, norm):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return sum(1 for ln in fh.read().split("\n") if _norm(ln) == norm)


def detection(seed, root):
    import corpus
    from sessionsteward import diet, dietlint
    truth = corpus.build(root, seed=seed)
    # a git repo so project root is a project
    rep = diet.analyze(root)
    lint = dietlint.lint(rep, root)
    out = {}
    # exact in-file duplicates
    found = {_norm(x["text"]) for x in lint["exact_in_file"]}
    want = {_norm(x["text"]) for x in truth["exact_dups"]}
    # a "paraphrase" that normalises to its original is an exact duplicate
    # (only when its original is in the same file: across files it is
    # `diet`'s cross-file duplicate list, not this detector's)
    want |= {_norm(x["text"]) for x in truth["near_dups"]
             if _norm(x["text"]) == _norm("- " + x["original"])
             and _count_in_file(root, x["file"], _norm(x["text"])) >= 2}
    out["exact"] = (len(found & want), len(found), len(want))
    # near duplicates: a truth line counts when it sits in a group together
    # with its original
    members = []
    for g in lint["near_duplicates"]:
        members.append({_norm(_line_text(root, r)) for r in g["at"]})

    def grouped(kind):
        hit = 0
        for x in truth[kind]:
            if _norm(x["text"]) == _norm("- " + x["original"]):
                continue            # counted as an exact duplicate
            if any(_norm(x["text"]) in m and _norm("- " + x["original"]) in m
                   for m in members):
                hit += 1
        return hit, sum(1 for x in truth[kind]
                        if _norm(x["text"]) != _norm("- " + x["original"]))
    h, n = grouped("near_dups")
    # precision: groups that contain a truth near line (or an exact copy)
    truth_norms = {_norm(x["text"]) for x in
                   truth["near_dups"] + truth["exact_dups"]
                   + truth["heavy_dups"]}
    orig_norms = {_norm("- " + x["original"]) for x in
                  truth["near_dups"] + truth["exact_dups"]
                  + truth["heavy_dups"]}
    good = sum(1 for m in members if m & (truth_norms | orig_norms))
    out["near"] = (h, n, good, len(members))
    out["heavy"] = grouped("heavy_dups")
    # contradictions
    cm = [{_norm(_line_text(root, r)) for r in c["at"]}
          for c in lint["contradictions"]]
    hit = sum(1 for x in truth["contradictions"]
              if any(_norm(x["text"]) in m and _norm("- " + x["original"]) in m
                     for m in cm))
    cflip = {_norm(x["text"]) for x in truth["contradictions"]}
    corig = {_norm("- " + x["original"]) for x in truth["contradictions"]}
    good = sum(1 for m in cm if m & (cflip | corig))
    out["contra"] = (hit, len(truth["contradictions"]), good, len(cm))
    # dead references
    refs = {d["ref"] for d in lint["dead_references"]}
    dead = {x["ref"] for x in truth["dead"]}
    live = {x["ref"] for x in truth["live"]}
    out["dead"] = (len(refs & dead), len(refs), len(dead), len(refs & live))
    # secrets
    from sessionsteward import extras
    texts = extras.live_texts(root, everything=True)
    sec = extras.find_secrets(texts)
    secl = {_norm(_line_text(root, f"{x['path']}:{x['line']}")) for x in sec}
    swant = {_norm(x["text"]) for x in truth["secrets"]}
    ben = {_norm(x["text"]) for x in truth["benign"]}
    out["secret"] = (len(secl & swant), len(secl), len(swant), len(secl & ben))
    return out, truth


def fix_savings(seed, root):
    """Tokens before/after `diet --fix`, and that no distinct instruction is
    lost by it."""
    import corpus
    from sessionsteward import diet, dietlint, lost, extras
    corpus.build(root, seed=seed)
    before_texts = extras.live_texts(root)
    rep = diet.analyze(root)
    tokens0 = rep["total_tokens"]
    lint = dietlint.lint(rep, root)
    plan = dietlint.fix_plan(rep, root)
    written, skipped = dietlint.apply_plan(plan)
    rep2 = diet.analyze(root)
    after_texts = extras.live_texts(root)
    l = lost.compare(before_texts, after_texts)
    return {"before": tokens0, "after": rep2["total_tokens"],
            "lint_potential": lint["tokens_saved_if_deduped"],
            "lost_by_fix": len(l["dropped"]) + len(l["reworded"]),
            "skipped": len(skipped)}


def compaction(seed, root):
    """Simulate a bad compaction of a corpus with known edits, then score
    `lost` against the truth, and a plain line diff as the baseline."""
    import corpus
    from sessionsteward import lost
    truth = corpus.build(root, seed=seed, n_dup=0, n_near=0, n_heavy=0,
                         n_dead=0, n_live=0, n_contra=0, n_secret=0,
                         n_benign=0)
    rng = random.Random(seed * 31)
    old = {}
    for rel in truth["files"]:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            old[rel] = f.read()
    new = {k: v.split("\n") for k, v in old.items()}
    cand = [(rel, i) for rel, ls in new.items() for i, ln in enumerate(ls)
            if ln.startswith("- ")]
    rng.shuffle(cand)
    k = max(3, len(cand) // 10)
    dropped = cand[:k]
    reworded = cand[k:2 * k]
    moved = cand[2 * k:3 * k // 2 + 2 * k]
    gone = {(r, i) for r, i in dropped}
    dropped_texts = {_norm(new[r][i]) for r, i in dropped}
    reworded_texts = {_norm(new[r][i]) for r, i in reworded}
    # edits
    for r, i in reworded:
        ln = new[r][i]
        new[r][i] = ln[:-1] + " (see the wiki)." if ln.endswith(".") else ln
    moved_lines = {}
    other = [f for f in new if f != "CLAUDE.md"][0]
    for r, i in moved:
        dest = "CLAUDE.md" if r != "CLAUDE.md" else other
        moved_lines.setdefault(dest, []).append(new[r][i])
        gone.add((r, i))
    out = {}
    for rel, ls in new.items():
        keep = [ln for i, ln in enumerate(ls) if (rel, i) not in gone]
        head = [ln for ln in keep if not ln.startswith("- ")]
        body = [ln for ln in keep if ln.startswith("- ")]
        rng.shuffle(body)                        # compaction reorders
        out[rel] = head + body
    for dest, lines_ in moved_lines.items():
        out[dest] = out[dest] + lines_
    out["CLAUDE.md"] = out["CLAUDE.md"] + [
        "- A brand new note about nothing in particular."]
    moved_all = [x for v in moved_lines.values() for x in v]
    new_text = {k: "\n".join(v) + "\n" for k, v in out.items()}
    rep = lost.compare(old, new_text)
    got = {_norm(x["text"]) for x in rep["dropped"]}
    tp = len(got & dropped_texts)
    precision = tp / len(got) if got else 1.0
    recall = tp / len(dropped_texts)
    rw = {_norm(x["text"]) for x in rep["reworded"]}
    rw_hit = len(rw & reworded_texts)
    mv_hit = len({_norm(x["text"]) for x in rep["moved"]}
                 & {_norm(x) for x in moved_all})
    # baseline: a line diff of every file
    removed = 0
    for rel in old:
        a = old[rel].split("\n")
        b = new_text.get(rel, "").split("\n")
        for ln in difflib.unified_diff(a, b, lineterm="", n=0):
            if ln.startswith("-") and not ln.startswith("---"):
                removed += 1
    read_ours = len(rep["dropped"]) + len(rep["reworded"])
    return {"truth_dropped": len(dropped_texts), "flagged": len(got),
            "tp": tp, "precision": precision, "recall": recall,
            "reworded_found": rw_hit, "reworded": len(reworded_texts),
            "moved_found": mv_hit, "moved": len(moved_all),
            "diff_removed_lines": removed, "ours_to_read": read_ours}


def speed():
    import corpus
    from sessionsteward import diet, dietlint, lost, extras
    root = tempfile.mkdtemp(prefix="ss-bench-big-")
    try:
        corpus.build(root, seed=99, n_lines=3000, n_files=12, n_dup=100,
                     n_near=100, n_heavy=50, n_dead=50, n_live=50, n_contra=50)
        t0 = time.perf_counter()
        rep = diet.analyze(root)
        t1 = time.perf_counter()
        dietlint.lint(rep, root)
        t2 = time.perf_counter()
        texts = extras.live_texts(root)
        lost.compare(texts, texts)
        t3 = time.perf_counter()
        extras.find_secrets(extras.live_texts(root, everything=True))
        t4 = time.perf_counter()
        n = sum(len(t.split("\n")) for t in texts.values())
        return {"lines": n, "diet_s": t1 - t0, "lint_s": t2 - t1,
                "lost_s": t3 - t2, "scan_s": t4 - t3}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def ratio(a, b):
    return a / b if b else 1.0


def main(argv):
    home = _env()
    work = tempfile.mkdtemp(prefix="ss-bench-")
    try:
        agg = {k: [0, 0, 0, 0] for k in ("exact", "near", "heavy", "contra",
                                          "dead", "secret")}
        for seed in SEEDS:
            root = os.path.join(work, f"d{seed}")
            r, _ = detection(seed, root)
            for k, v in r.items():
                for i, x in enumerate(v):
                    agg[k][i] += x
        fx = [fix_savings(s, os.path.join(work, f"f{s}")) for s in SEEDS]
        cp = [compaction(s, os.path.join(work, f"c{s}")) for s in SEEDS]
        sp = speed()
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)

    e, n, h, c, d, s = (agg[k] for k in ("exact", "near", "heavy", "contra",
                                         "dead", "secret"))
    m = {
        "exact_recall": ratio(e[0], e[2]), "exact_precision": ratio(e[0], e[1]),
        "near_recall": ratio(n[0], n[1]), "near_precision": ratio(n[2], n[3]),
        "heavy_recall": ratio(h[0], h[1]),
        "contra_recall": ratio(c[0], c[1]), "contra_precision": ratio(c[2], c[3]),
        "dead_recall": ratio(d[0], d[2]), "dead_precision": ratio(d[0], d[1]),
        "dead_false_alarms_on_live": d[3],
        "secret_recall": ratio(s[0], s[2]), "secret_precision": ratio(s[0], s[1]),
        "secret_false_alarms_on_benign": s[3],
    }
    t0 = sum(x["before"] for x in fx)
    t1 = sum(x["after"] for x in fx)
    m["tokens_before"] = t0
    m["tokens_after_fix"] = t1
    m["tokens_saved_pct"] = 100 * (t0 - t1) / t0
    m["tokens_potential_pct"] = 100 * sum(x["lint_potential"] for x in fx) / t0
    m["lines_lost_by_fix"] = sum(x["lost_by_fix"] for x in fx)
    tp = sum(x["tp"] for x in cp)
    fl = sum(x["flagged"] for x in cp)
    td = sum(x["truth_dropped"] for x in cp)
    m["lost_recall"] = ratio(tp, td)
    m["lost_precision"] = ratio(tp, fl)
    m["lost_reworded_found"] = ratio(sum(x["reworded_found"] for x in cp),
                                     sum(x["reworded"] for x in cp))
    m["lost_moved_found"] = ratio(sum(x["moved_found"] for x in cp),
                                  sum(x["moved"] for x in cp))
    m["lines_to_read_line_diff"] = sum(x["diff_removed_lines"] for x in cp)
    m["lines_to_read_lost"] = sum(x["ours_to_read"] for x in cp)
    m["true_losses"] = td
    m["speed"] = sp
    text = render(m)
    print(text)
    if "--check" in argv:
        bad = [k for k, floor in FLOORS.items() if m[k] < floor]
        if bad:
            print("REGRESSED: " + ", ".join(f"{k}={m[k]:.3f} < {FLOORS[k]}"
                                            for k in bad), file=sys.stderr)
            return 1
        return 0
    with open(os.path.join(HERE, "RESULTS.md"), "w") as f:
        f.write(text + "\n")
    if "--json" in argv:
        print(json.dumps(m, indent=2, sort_keys=True))
    return 0


def pct(x):
    return f"{100 * x:.0f}%"


def render(m):
    sp = m["speed"]
    return f"""# sessionsteward benchmark ({len(SEEDS)} seeded corpora)

Synthetic memory files with known defects; token counts are characters / 4.
Re-run: `python bench/run.py`. Detector numbers describe this corpus only.

## Detectors (`diet --lint`, `scan`)
| detector | recall | precision | note |
|---|---|---|---|
| same line twice in a file | {pct(m['exact_recall'])} | {pct(m['exact_precision'])} | |
| near-duplicate (filler, case, plural) | {pct(m['near_recall'])} | {pct(m['near_precision'])} | |
| partial synonym swap | {pct(m['heavy_recall'])} | - | word overlap only; a full rewrite is not found |
| do vs. do-not on one topic | {pct(m['contra_recall'])} | {pct(m['contra_precision'])} | polarity flips only |
| path that does not exist | {pct(m['dead_recall'])} | {pct(m['dead_precision'])} | {m['dead_false_alarms_on_live']} false alarms on existing paths |
| secret-shaped value | {pct(m['secret_recall'])} | {pct(m['secret_precision'])} | {m['secret_false_alarms_on_benign']} false alarms on hashes, uuids, prose |

## `diet --fix` (safe part only)
- session-start tokens: {m['tokens_before']:,} -> {m['tokens_after_fix']:,} ({m['tokens_saved_pct']:.1f}% fewer); `--lint` sees {m['tokens_potential_pct']:.1f}% in reach if near-duplicates are merged by hand
- distinct instructions lost by `--fix`: {m['lines_lost_by_fix']}

## `lost` vs. a line diff (simulated compaction: drop 10%, reword 10%, move 5%, shuffle, add)
| | line diff | `lost` |
|---|---|---|
| lines you must read | {m['lines_to_read_line_diff']} removed lines | {m['lines_to_read_lost']} (dropped + reworded) |
| truly dropped lines found | all {m['true_losses']}, buried | recall {pct(m['lost_recall'])}, precision {pct(m['lost_precision'])} |
| reworded lines paired with their new wording | no | {pct(m['lost_reworded_found'])} |
| lines moved to another file recognised | no | {pct(m['lost_moved_found'])} |

## Speed ({sp['lines']:,} lines, 12 files)
diet {sp['diet_s']:.2f} s, diet --lint {sp['lint_s']:.2f} s, lost {sp['lost_s']:.2f} s, scan {sp['scan_s']:.2f} s
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
