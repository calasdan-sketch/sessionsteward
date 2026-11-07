"""Instruction lines: the unit the `lost`, `diet --lint` and `diet --fix`
features compare.

A memory file is read as a list of lines; two lines are "the same
instruction" when they agree after list markers, emphasis, case and runs of
white space are removed. Token counts are estimates (characters / 4), the
same estimate `diet` uses - never a tokenizer's, never a paid model.
"""

from __future__ import annotations

import heapq
import re
from typing import NamedTuple

CHARS_PER_TOKEN = 4

# An instruction shorter than this (after normalising) is a label, not a rule.
MIN_NORM_CHARS = 12

_BULLET = re.compile(r"^\s*(?:>\s*)*(?:[-*+]|\d{1,9}[.)])\s+(?:\[[ xX]\]\s+)?")
_EMPHASIS = re.compile(r"[*_`~]+")
_WORDS = re.compile(r"[a-z0-9][a-z0-9'_\-./]*")
_DIGITS = re.compile(r"\d+")
_FENCE = re.compile(r"^\s*(?:[-*+]\s+)?(`{3,}|~{3,})")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")

# Words that carry no subject: used when matching "the same rule, opposite
# verdict" and when deciding two lines are about one thing.
STOPWORDS = frozenset(
    "a an the and or but if then else for to of in on at by with from as is "
    "are was were be been it its this that these those you your we our they "
    "them i my me when while before after during any all each every some "
    "more most can could would will shall may might also just only not no "
    "so than too very into onto over under up down out off per via".split())


class Unit(NamedTuple):
    """One line of a memory file."""
    line: int        # 1-based line number in the file
    raw: str         # the line as written (no line ending)
    norm: str        # normalised, see normalise()
    heading: bool


def normalise(line: str) -> str:
    """The line with its list marker, emphasis marks and code backticks
    removed, lower-cased, white space collapsed, trailing punctuation cut."""
    s = _BULLET.sub("", line)
    s = _EMPHASIS.sub("", s)
    s = " ".join(s.lower().split())
    return s.rstrip(" .;:,")


def words(norm: str) -> list[str]:
    return _WORDS.findall(norm)


def shape(norm: str) -> str:
    """norm with every run of digits replaced by `#`: lines that differ only
    in a counter or a version number ("rule 3 ...", "rule 4 ...") match."""
    return _DIGITS.sub("#", norm)


def stem(w: str) -> str:
    """A crude stem (committing -> commit, tests -> test) so that a line
    reworded with another verb form still matches. Not linguistics: only
    for deciding two lines are alike."""
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
        if len(w) > 3 and w[-1] == w[-2]:
            w = w[:-1]
        return w
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def content_words(norm: str) -> frozenset[str]:
    return frozenset(stem(w) for w in words(norm) if w not in STOPWORDS)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def units(text: str, *, keep_code: bool = True) -> list[Unit]:
    """The lines of `text` that say something: not blank, not a rule line,
    not a code-fence marker, not shorter than MIN_NORM_CHARS once
    normalised. Lines inside a fenced block are kept unless keep_code is
    False (a command in a fence is an instruction too)."""
    out: list[Unit] = []
    fence = None
    for no, raw in enumerate(text.split("\n"), 1):
        raw = raw.rstrip("\r")
        m = _FENCE.match(raw)
        if m:
            mark = m.group(1)
            if fence is None:
                fence = mark[0]
            elif mark[0] == fence:
                fence = None
            continue
        if fence is not None and not keep_code:
            continue
        if not raw.strip() or _RULE.match(raw):
            continue
        norm = normalise(raw)
        if len(norm) < MIN_NORM_CHARS:
            continue
        out.append(Unit(no, raw, norm, raw.lstrip().startswith("#")))
    return out


def est_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


class Index:
    """Inverted index over the content words of a set of lines, to find the
    lines that share enough words with one line without comparing all pairs.
    """

    def __init__(self, sets: list[frozenset], max_posting: int = 400):
        self.sets = sets
        self.post: dict[str, list[int]] = {}
        for i, s in enumerate(sets):
            for w in s:
                self.post.setdefault(w, []).append(i)
        # a word in very many lines ("rule", "always") says nothing
        self.common = {w for w, p in self.post.items()
                       if len(p) > max_posting}

    def candidates(self, i: int, limit: int = 30) -> list[int]:
        """Indexes of the other lines that share the most words with line i."""
        return self.query(self.sets[i], limit, exclude=i)

    def query(self, wordset, limit: int = 30, exclude: int = -1) -> list[int]:
        counts: dict[int, int] = {}
        for w in wordset:
            if w in self.common or w not in self.post:
                continue
            for j in self.post[w]:
                if j != exclude:
                    counts[j] = counts.get(j, 0) + 1
        return heapq.nsmallest(limit, counts, key=lambda j: (-counts[j], j))


_NEG = re.compile(
    r"\b(?:never|don'?t|do not|must not|mustn'?t|should not|shouldn'?t|"
    r"avoid|stop using|no longer|forbidden|prohibited|not allowed|"
    r"cannot|can'?t|won'?t|refrain from)\b", re.I)
_POS = re.compile(
    r"\b(?:always|must|should|ensure|make sure|be sure to|required?|"
    r"prefer|use|do|try to|remember to|only|need to|have to)\b", re.I)
def polarity(norm: str) -> int:
    """-1 when the line forbids ("never", "don't", "avoid"), +1 when it
    requires or recommends ("always", "must", "use"), 0 when it does
    neither."""
    if _NEG.search(norm):
        return -1
    return 1 if _POS.search(norm) else 0
