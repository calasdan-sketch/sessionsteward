"""Commands added in 0.3.0: lost, scan, pin, diet --lint / --fix / --history.

Kept apart from cli.py so each reads on its own; cli.py wires them in.
"""

from __future__ import annotations

import json
import os
import sys

from . import core as core_mod
from . import diet as diet_mod
from . import dietlint
from . import lost as lost_mod
from . import secrets as secrets_mod
from . import textlines as tl

CHECK_FAILED = 3
MAX_TEXT_BYTES = 1024 * 1024        # a memory file bigger than this is not text we compare
TEXT_EXT = {".md", ".mdc", ".markdown", ".txt", ""}


def _short(path: str, root: str) -> str:
    return diet_mod._short(path, root)


def _decode(raw: bytes) -> str | None:
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def _is_memory_text(path: str) -> bool:
    name = os.path.basename(path)
    return (os.path.splitext(name)[1].lower() in TEXT_EXT
            or name in diet_mod.CONTEXT_NAMES)


def live_texts(root: str, everything: bool = False) -> dict[str, str]:
    """{abspath: text} of the watched text files as they are now."""
    out = {}
    for fp in core_mod.iter_files(core_mod.watched_paths(root)):
        if not everything and not _is_memory_text(fp):
            continue
        try:
            if os.path.getsize(fp) > MAX_TEXT_BYTES:
                continue
            with open(fp, "rb") as f:
                text = _decode(f.read())
        except OSError:
            continue
        if text is not None:
            out[fp] = text
    return out


def snapshot_texts(sha: str, manifest: dict,
                   everything: bool = False) -> dict[str, str]:
    out = {}
    for ap, (blob, size) in core_mod.snapshot_files(sha, manifest).items():
        if not everything and not _is_memory_text(ap):
            continue
        if size > MAX_TEXT_BYTES:
            continue
        text = _decode(core_mod.read_blob(blob))
        if text is not None:
            out[ap] = text
    return out


# ---- lost ------------------------------------------------------------------

def cmd_lost(args) -> int:
    root = args.root
    if not core_mod.repo_ready():
        print("no snapshots yet", file=sys.stderr)
        return 2
    snaps = core_mod.log(1_000_000, project_root=root)
    if not snaps:
        print("no snapshots yet", file=sys.stderr)
        return 2
    try:
        if args.live:
            old_sha = (args.sha1 or snaps[0]["sha"])
            old_sha, old_m = core_mod.resolve_snapshot(old_sha, root)
            old = snapshot_texts(old_sha, old_m)
            new = live_texts(root)
            label = f"snapshot {old_sha[:7]} -> your files now"
        else:
            if args.sha1:
                new_sha = args.sha2 or snaps[0]["sha"]
                old_sha = args.sha1
            else:
                if len(snaps) < 2:
                    print("only one snapshot - nothing to compare")
                    return 0
                old_sha, new_sha = snaps[1]["sha"], snaps[0]["sha"]
            old_sha, old_m = core_mod.resolve_snapshot(old_sha, root)
            new_sha, new_m = core_mod.resolve_snapshot(new_sha, root)
            old, new = snapshot_texts(old_sha, old_m), snapshot_texts(new_sha, new_m)
            label = f"snapshot {old_sha[:7]} -> {new_sha[:7]}"
    except (core_mod.NothingToRestoreError, core_mod.CrossProjectError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    rep = lost_mod.compare(old, new)
    if args.json:
        def red(items, keys):
            for it in items:
                for k in keys:
                    if k in it:
                        it[k] = secrets_mod.redact_line(it[k])
        red(rep["dropped"], ("text",)); red(rep["reworded"], ("text", "to_text"))
        red(rep["moved"], ("text",)); red(rep["reversed"], ("text", "to_text"))
        print(json.dumps(rep, indent=2, ensure_ascii=True))
    else:
        print(f"lost: {label}")
        print(lost_mod.render(rep, lambda p: _short(p, root)))
    if args.check and (rep["dropped"] or rep["reversed"]):
        return CHECK_FAILED
    return 0


# ---- scan ------------------------------------------------------------------

def find_secrets(texts: dict[str, str]) -> list[dict]:
    out = []
    for path, text in texts.items():
        for f in secrets_mod.scan_text(text):
            out.append({"path": path, "line": f.line, "kind": f.kind,
                        "preview": f.preview})
    return out


def cmd_scan(args) -> int:
    root = args.root
    if args.sha:
        if not core_mod.repo_ready():
            print("no snapshots yet", file=sys.stderr)
            return 2
        try:
            sha, m = core_mod.resolve_snapshot(args.sha, root)
        except (core_mod.NothingToRestoreError, core_mod.CrossProjectError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        texts, where = snapshot_texts(sha, m, everything=True), f"snapshot {sha[:7]}"
    else:
        texts, where = live_texts(root, everything=True), "your watched files"
    found = find_secrets(texts)
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=True))
    elif not found:
        print(f"no secrets found in {where} ({len(texts)} file(s) scanned)")
    else:
        print(f"{len(found)} possible secret(s) in {where}:")
        for x in found[:100]:
            print(f"  {diet_mod.safe(_short(x['path'], root))}:{x['line']}  "
                  f"{x['kind']}  {diet_mod.safe(x['preview'])}")
        if len(found) > 100:
            print(f"  ... {len(found) - 100} more (use --json)")
        print("A snapshot copies these files as they are, and keeps old "
              "versions: remove the secret, rotate it if it was ever real, "
              "and keep ~/.sessionsteward private.")
    return CHECK_FAILED if found else 0


def snap_secret_warning(root: str) -> None:
    """One stderr line after a snapshot when a watched file holds something
    secret-shaped. Never fails a snapshot."""
    try:
        found = find_secrets(live_texts(root, everything=True))
    except Exception:       # noqa: BLE001 - a warning must never break a snap
        return
    if found:
        files = sorted({_short(x["path"], root) for x in found})
        names = ", ".join(diet_mod.safe(n) for n in files[:3])
        more = f" and {len(files) - 3} more" if len(files) > 3 else ""
        print(f"warning: {len(found)} possible secret(s) in {names}{more} "
              f"were copied into the snapshot - `sessionsteward scan` "
              f"lists them", file=sys.stderr)


# ---- pin -------------------------------------------------------------------

def _pin_norm(text: str) -> str:
    return tl.normalise(text)


def missing_pins(root: str, pins: list[str] | None = None) -> list[str]:
    if pins is None:
        pins = core_mod.load_config().get("pins") or []
    if not pins:
        return []
    blob = "\n".join(tl.normalise(line) for t in live_texts(root).values()
                     for line in t.split("\n"))
    return [p for p in pins if _pin_norm(p) not in blob]


def cmd_pin(args) -> int:
    cfg = core_mod.load_config()
    pins = list(cfg.get("pins") or [])
    if args.text and args.remove:
        norm = _pin_norm(args.text)
        keep = [p for p in pins if _pin_norm(p) != norm]
        if len(keep) == len(pins):
            print(f"error: no such pin: {args.text!r}", file=sys.stderr)
            return 1
        cfg["pins"] = keep
        core_mod.save_config(cfg)
        print(f"unpinned; {len(keep)} pin(s) left")
        return 0
    if args.text:
        if len(_pin_norm(args.text)) < tl.MIN_NORM_CHARS:
            print("error: a pin is a whole instruction (at least "
                  f"{tl.MIN_NORM_CHARS} characters), not a word",
                  file=sys.stderr)
            return 2
        if len(args.text) > 500 or "\n" in args.text:
            print("error: a pin is one line of at most 500 characters",
                  file=sys.stderr)
            return 2
        if secrets_mod.scan_line(args.text):
            print("error: that line looks like a secret; pins are stored "
                  "in plain text in ~/.sessionsteward/config.json",
                  file=sys.stderr)
            return 2
        if _pin_norm(args.text) not in {_pin_norm(p) for p in pins}:
            pins.append(args.text)
        cfg["pins"] = pins
        core_mod.save_config(cfg)
        miss = missing_pins(args.root, [args.text])
        print(f"pinned ({len(pins)} in all)"
              + ("; note: no watched file has this line right now"
                 if miss else ""))
        return 0
    if args.check:
        miss = missing_pins(args.root, pins)
        for p in miss:
            print(f"missing: {diet_mod.safe(secrets_mod.redact_line(p))}")
        if not miss:
            print(f"all {len(pins)} pinned line(s) are in your files")
        return CHECK_FAILED if miss else 0
    if not pins:
        print('no pins - `sessionsteward pin "Never force-push to main"` '
              "keeps a watch on a rule you cannot afford to lose")
        return 0
    miss = set(missing_pins(args.root, pins))
    for p in pins:
        print(("MISSING  " if p in miss else "ok       ")
              + diet_mod.safe(secrets_mod.redact_line(p)))
    return 0


def pin_lines(root: str) -> list[str]:
    """Lines for `status`/`snap`: which pinned rules are gone."""
    try:
        miss = missing_pins(root)
    except core_mod.StewardError:
        return []
    return [f"  ! pinned line missing: "
            f"{diet_mod.safe(secrets_mod.redact_line(p))[:120]} - "
            f"`sessionsteward lost --live` shows what else, "
            f"`sessionsteward undo` brings it back" for p in miss]


# ---- diet --history --------------------------------------------------------

JUMP_TOKENS = 500
JUMP_RATIO = 0.10


def history(root: str, limit: int) -> list[dict]:
    snaps = core_mod.log(1_000_000, project_root=root)[:limit]
    rows = []
    for s in snaps:
        sha, m = core_mod.resolve_snapshot(s["sha"], root)
        files = core_mod.snapshot_files(sha, m)
        chars = 0
        n = 0
        for ap, (_, size) in files.items():
            if diet_mod.loads_into_context(ap) and size >= 0:
                chars += size
                n += 1
        rows.append({"sha": sha, "date": s["date"], "message": s["message"],
                     "files": n, "tokens": chars // tl.CHARS_PER_TOKEN})
    for i, r in enumerate(rows):
        prev = rows[i + 1]["tokens"] if i + 1 < len(rows) else None
        r["delta"] = None if prev is None else r["tokens"] - prev
    return rows


def cmd_history(args) -> int:
    if not core_mod.repo_ready():
        print("no snapshots yet", file=sys.stderr)
        return 2
    limit = args.history
    if limit < 1:
        raise core_mod.StewardError("--history must be 1 or more")
    rows = history(args.root, limit)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=True))
        return 0
    if not rows:
        print("no snapshots yet for this project")
        return 0
    print("Session-start cost over time (files that load at session start, "
          "@imports not followed; characters / 4)")
    for r in rows:
        d = r["delta"]
        mark = ""
        if d is not None and (abs(d) >= JUMP_TOKENS
                              or abs(d) >= JUMP_RATIO * max(1, r["tokens"] - d)):
            mark = "  <- jump" if d > 0 else "  <- shrank"
        delta = "" if d is None else f" ({d:+,})"
        print(f"{r['sha'][:7]}  {r['date'][:16]}  ~{r['tokens']:>7,} tokens"
              f"{delta}{mark}  {diet_mod.safe(r['message'])[:50]}")
    return 0


# ---- diet --lint / --fix ---------------------------------------------------

def redact(t: str) -> str:
    return secrets_mod.redact_line(t)


def lint_section(report: dict, root: str) -> dict:
    """The lint findings, with every quoted line redacted: the JSON goes to
    logs and CI, and a line may hold a secret."""
    out = dietlint.lint(report, root)
    for x in out["exact_in_file"] + out["dead_references"]:
        x["text"] = redact(x["text"])
    for g in out["near_duplicates"]:
        g["example"] = redact(g["example"])
    for c in out["contradictions"]:
        c["a_text"], c["b_text"] = redact(c["a_text"]), redact(c["b_text"])
    return out


def cmd_fix(args, report: dict) -> int:
    """diet --fix [--write]. Without --write nothing is touched."""
    root = args.root
    plan = dietlint.fix_plan(report, root)
    if not plan:
        print("diet --fix: nothing it can safely remove (no repeated "
              "lines inside a file, no runs of blank lines)")
        return 0
    total = 0
    for item in plan:
        total += item["tokens"]
        name = diet_mod.safe(_short(item["path"], root))
        print(f"--- {name}: {len(item['removed'])} repeated line(s), "
              f"{item['collapsed']} extra blank line(s), "
              f"~{item['tokens']} tokens")
        for no, text in item["removed"][:20]:
            print(f"  - line {no}: {diet_mod.safe(redact(text))[:100]}")
        if len(item["removed"]) > 20:
            print(f"  ... {len(item['removed']) - 20} more")
    print(f"\ntotal ~{total:,} tokens would be given back from "
          f"{len(plan)} file(s)")
    if not args.write:
        print("nothing was changed; `sessionsteward diet --fix --write` "
              "applies it (a snapshot is taken first, so `undo` reverts it)")
        return 0
    before = core_mod.snap(root, "pre-diet snapshot")
    written, skipped = dietlint.apply_plan(plan)
    for s in skipped:
        print(f"skipped {diet_mod.safe(s)}", file=sys.stderr)
    if written:
        after = core_mod.snap(root, f"diet: removed repeated lines from "
                                    f"{len(written)} file(s)")
        print(f"done: {len(written)} file(s) written; snapshot "
              f"{after[:7]} holds the result, {before[:7]} the original "
              f"(`sessionsteward undo` goes back)")
    return 1 if skipped else 0
