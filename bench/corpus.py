"""A deterministic corpus of memory files with known, seeded defects.

Everything is generated from a seed: no network, no model, no file from
anybody's machine. The ground truth (which lines are duplicates, which paths
are dead, which lines contradict, where the secrets are) is returned with
the files, so a detector can be scored.

The lines are synthetic English instructions built from word lists; they are
not real CLAUDE.md files, and the numbers say how the detectors behave on
this corpus, not on yours.
"""

from __future__ import annotations

import os
import random

OPENERS = ["Always", "Never", "Prefer", "Avoid", "Make sure to", "Do not",
           "You must", "Remember to", "Try to", "Only"]
VERBS = ["run", "write", "review", "document", "rename", "split", "cache",
         "log", "validate", "mock", "pin", "format", "lint", "benchmark",
         "tag", "archive", "profile", "retry", "sandbox", "version"]
OBJECTS = ["database migrations", "API responses", "environment variables",
           "feature flags", "background jobs", "error messages", "CSV exports",
           "webhook payloads", "retry policies", "cache keys", "build scripts",
           "release notes", "test fixtures", "log lines", "config files",
           "user sessions", "rate limits", "queue consumers", "image uploads",
           "search indexes", "audit trails", "cron schedules", "feature specs",
           "pull requests", "dependency updates"]
WHERES = ["in the billing service", "before every release", "in the admin panel",
          "for the mobile client", "in the worker pool", "during code review",
          "in staging only", "across all services", "in the reporting module",
          "when touching the auth layer", "in the public SDK",
          "for partner integrations", "on the nightly build"]
ADJECTIVES = ["legacy", "public", "internal", "nightly", "shared", "critical",
              "experimental", "regional", "archived", "scheduled", "external",
              "generated", "tenant-scoped", "read-only"]
FILLERS = ["please", "carefully", "at all times", "as a rule", "without exception"]
SYNONYMS = {"run": "execute", "write": "author", "review": "inspect",
            "document": "describe", "rename": "relabel", "split": "divide",
            "validate": "verify", "format": "style", "lint": "check",
            "Always": "Consistently", "Never": "Under no circumstances",
            "Avoid": "Steer clear of", "Prefer": "Favour"}
EXTS = ["py", "ts", "go", "md", "sh", "json", "yaml"]
DIRS = ["src", "tools", "docs", "scripts", "services", "config", "lib"]


def _base_lines(rng: random.Random, n: int) -> list[str]:
    seen, out = set(), []
    while len(out) < n:
        line = (f"{rng.choice(OPENERS)} {rng.choice(VERBS)} "
                f"{rng.choice(ADJECTIVES)} {rng.choice(OBJECTS)} "
                f"{rng.choice(WHERES)}.")
        key = line.lower()
        # a distinct instruction = distinct verb+object+place
        sig = tuple(key.split()[-8:])
        if key in seen or sig in seen:
            continue
        seen.add(key)
        seen.add(sig)
        out.append(line)
    return out


def _flip(line: str) -> str:
    for a, b in (("Always", "Never"), ("Never", "Always"), ("Prefer", "Avoid"),
                 ("Avoid", "Prefer"), ("Make sure to", "Do not"),
                 ("Do not", "Make sure to"), ("You must", "Never"),
                 ("Only", "Never")):
        if line.startswith(a + " "):
            return b + line[len(a):]
    return "Never " + line[0].lower() + line[1:]


def _light_paraphrase(rng: random.Random, line: str) -> str:
    """The same words with a filler added or a verb form changed."""
    kind = rng.choice(["filler", "ing", "case"])
    if kind == "filler":
        return line[:-1] + " " + rng.choice(FILLERS) + "."
    if kind == "case":
        return "- " + line.upper()[0] + line[1:].replace(" the ", " THE ")
    return line.replace("migrations", "migration").replace("responses",
                                                          "response")


def _heavy_paraphrase(line: str) -> str:
    """Synonym swaps: a person sees the same rule, a word-overlap detector
    does not (and is not expected to)."""
    out = line
    for a, b in SYNONYMS.items():
        out = out.replace(a, b)
    return out


def build(root: str, seed: int = 7, n_lines: int = 160, n_files: int = 4,
          n_dup: int = 12, n_near: int = 12, n_heavy: int = 8,
          n_dead: int = 8, n_live: int = 8, n_contra: int = 8,
          n_secret: int = 5, n_benign: int = 12) -> dict:
    """Write the project under `root` and return the ground truth."""
    rng = random.Random(seed)
    base = _base_lines(rng, n_lines)
    # lay the distinct lines out over CLAUDE.md and rules files
    files: dict[str, list[str]] = {"CLAUDE.md": ["# Project rules", ""]}
    for i in range(n_files - 1):
        files[f".claude/rules/area{i}.md"] = [f"# Area {i}", ""]
    names = list(files)
    for i, line in enumerate(base):
        files[names[i % len(names)] if i % 3 else names[0]].append("- " + line)

    truth = {"exact_dups": [], "near_dups": [], "heavy_dups": [],
             "dead": [], "live": [], "contradictions": [], "secrets": [],
             "benign": []}
    pool = list(base)
    rng.shuffle(pool)
    used = iter(pool)

    def add(kind: str, text: str, fname: str | None = None, **extra) -> None:
        f = fname or rng.choice(names)
        files[f].append(text)
        truth[kind].append({"file": f, "text": text, **extra})

    for _ in range(n_dup):            # exact copies (same file)
        src = next(used)
        f = next(k for k, v in files.items() if "- " + src in v)
        add("exact_dups", "- " + src, f, original=src)
    for _ in range(n_near):           # light paraphrase of a rule
        src = next(used)
        add("near_dups", "- " + _light_paraphrase(rng, src).lstrip("- "),
            original=src)
    for _ in range(n_heavy):          # synonym paraphrase
        src = next(used)
        if _heavy_paraphrase(src) != src:
            add("heavy_dups", "- " + _heavy_paraphrase(src), original=src)
    for _ in range(n_contra):         # the opposite verdict on the same rule
        src = next(used)
        add("contradictions", "- " + _flip(src), original=src)
    for i in range(n_dead):           # reference to a file that is not there
        p = f"{rng.choice(DIRS)}/legacy{i}/{rng.choice(VERBS)}er.{rng.choice(EXTS)}"
        add("dead", f"- See `{p}` for how this is done.", ref=p)
    os.makedirs(root, exist_ok=True)
    for i in range(n_live):           # reference to a file that exists
        p = f"{rng.choice(DIRS)}/live{i}/{rng.choice(VERBS)}er.{rng.choice(EXTS)}"
        full = os.path.join(root, p)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write("x\n")
        add("live", f"- Entry point: `{p}`.", ref=p)
    # secrets are built at run time from parts: no contiguous credential
    # is stored anywhere in the repository
    alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    mixed = "aZ3kQ9xLm2PqR7sT1vWb8NcY4dFg6HjK"
    secret_makers = [
        lambda: "AK" + "IA" + "".join(rng.choice(alnum) for _ in range(16)),
        lambda: "gh" + "p_" + "".join(rng.choice(alnum + alnum.lower())
                                      for _ in range(36)),
        lambda: "sk" + "_live_" + "".join(rng.choice(alnum + alnum.lower())
                                          for _ in range(24)),
        lambda: "-----BEGIN RSA PRIV" + "ATE KEY-----",
        lambda: "xo" + "xb-" + "".join(rng.choice("0123456789") for _ in range(12))
                + "-" + "".join(rng.choice(alnum) for _ in range(12)),
    ]
    for i in range(n_secret):
        val = secret_makers[i % len(secret_makers)]()
        shape = rng.choice(["The deploy credential is {v} (ask ops).",
                            "export SERVICE_TOKEN={v}", "key: {v}"])
        add("secrets", "- " + shape.format(v=val), value=val)
    for _ in range(2):                # name = random value assignment
        val = "".join(rng.choice(mixed) for _ in range(20))
        add("secrets", f"- db_password = {val}", value=val)
    benign = [
        "- The commit {h} introduced the bug.".format(
            h="".join(rng.choice("0123456789abcdef") for _ in range(40))),
        "- Trace id format: {u}.".format(u="123e4567-e89b-12d3-a456-426614174000"),
        "- Set API_KEY=<your key here> in your shell.",
        "- Never paste a token into chat; use the secret manager.",
        "- The password policy requires 12 characters.",
        "- Rotate the api_key = changeme before the demo.",
        "- Checksum: {h}.".format(h="".join(rng.choice("0123456789abcdef")
                                           for _ in range(64))),
    ]
    for line in benign[:n_benign]:
        add("benign", line)
    os.makedirs(root, exist_ok=True)
    for rel, lines in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    truth["files"] = list(files)
    truth["base_lines"] = base
    return truth
