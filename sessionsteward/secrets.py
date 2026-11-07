"""Find secrets in memory files, and keep them out of what is printed.

A snapshot copies every watched file byte for byte, so a key pasted into a
CLAUDE.md ("the deploy token is ...") is copied into the snapshots repo and
stays in its history after you delete the line. This module finds the usual
suspects by their shape (provider prefixes, private-key headers, and
`password = value` style assignments of a random-looking value) so that
`scan` and `snap` can say so, and `show --redact` / `lost` can print a file
without the secret.

It is a pattern scanner, not a guarantee: a secret in a shape it does not
know is not found, and it can flag a harmless random-looking value. Matched
text is never printed in full by sessionsteward itself: only a kind and the
first four characters.

The patterns are plain regular expressions; no real or fake credential is
stored in this package or its tests (the tests build theirs at run time).
"""

from __future__ import annotations

import math
import re
from typing import NamedTuple

# (kind, pattern). Provider-prefixed tokens first: they are specific.
_PATTERNS = [
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key", re.compile(
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("google-oauth-token", re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("url-credentials", re.compile(
        r"\b[a-z][a-z0-9+.\-]{1,15}://[^\s:/@]{1,64}:[^\s:/@]{3,}@[^\s/]+")),
]
# NAME = value, where NAME says it is a secret and value looks random. The
# keyword is found first and the text around it is bounded: a pattern that
# began with an open-ended `[\w.\-]*` took quadratic time on one long line
# of `a.a.a.a.` (47 s for 20,000 characters).
_ASSIGN = re.compile(
    r"""(?ix)
    (?:api[_\-]?key|secret|token|passwd|password|credential)[\w.\-]{0,40}
    \s{0,8}[:=]\s{0,8}
    ["']?([A-Za-z0-9_\-+/=.!@\#$%^&*]{12,4000})["']?""")

# Values that are placeholders, not secrets.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:your|my|the|example|sample|dummy|test|changeme|change[-_]me|"
    r"xxx|<|\$|\{|%|\*|\.\.\.|none|null|todo|redacted|placeholder)")


class Finding(NamedTuple):
    kind: str
    line: int       # 1-based
    start: int      # offset in that line
    end: int
    preview: str    # kind-safe: first four characters only


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    return -sum(c / n * math.log2(c / n) for c in freq.values())


def _random_looking(value: str) -> bool:
    if _PLACEHOLDER.match(value):
        return False
    if not re.search(r"[0-9A-Z]", value):
        return False            # lower-case words and dashes: a phrase
    classes = sum(bool(re.search(p, value)) for p in
                  ("[a-z]", "[A-Z]", "[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and _entropy(value) >= 3.2


CHUNK = 4000        # a line is scanned in pieces this long ...
OVERLAP = 300       # ... that share this much, so no token is cut in two


def scan_line(line: str) -> list[tuple[str, int, int]]:
    """(kind, start, end) of every secret-looking span in one line. A very
    long line (a minified JSON file) is scanned in overlapping pieces: the
    whole line is covered, in time linear in its length."""
    if len(line) <= CHUNK + OVERLAP:
        return _scan_piece(line)
    spans: list[tuple[str, int, int]] = []
    seen = set()
    for off in range(0, len(line), CHUNK):
        for kind, a, b in _scan_piece(line[off:off + CHUNK + OVERLAP]):
            sp = (kind, a + off, b + off)
            if sp[1:] not in seen:
                seen.add(sp[1:])
                spans.append(sp)
    return _drop_nested(spans)


def _scan_piece(line: str) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(line):
            spans.append((kind, m.start(), m.end()))
    for m in _ASSIGN.finditer(line):
        if _random_looking(m.group(1)):
            spans.append(("secret-assignment", m.start(1), m.end(1)))
    return _drop_nested(spans)


def _drop_nested(spans):
    # drop spans inside another (a key seen by two patterns is one finding)
    spans.sort(key=lambda s: (s[1], -(s[2] - s[1])))
    out: list[tuple[str, int, int]] = []
    for sp in spans:
        if out and sp[1] < out[-1][2]:
            continue
        out.append(sp)
    return out


def scan_text(text: str) -> list[Finding]:
    found = []
    for no, line in enumerate(text.split("\n"), 1):
        for kind, a, b in scan_line(line):
            found.append(Finding(kind, no, a, b, line[a:a + 4] + "..."))
    return found


def redact_text(text: str) -> tuple[str, int]:
    """(text with every finding replaced by [REDACTED:kind], count)."""
    n = 0
    out = []
    for line in text.split("\n"):
        spans = scan_line(line) if line else []
        if spans:
            for kind, a, b in reversed(spans):
                line = line[:a] + f"[REDACTED:{kind}]" + line[b:]
                n += 1
        out.append(line)
    return "\n".join(out), n


def redact_line(line: str) -> str:
    return redact_text(line)[0]
