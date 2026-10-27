"""Keep secrets out of snapshots.

A snapshot is a git commit in a private repository that lives for as long as
you keep it. A key pasted into CLAUDE.md or a notes file would be copied into
it, stay in its history after you delete the key from the file, and be
included in a backup of your home folder. So a file that looks like it holds
a secret is left out of the snapshot (and `undo` / `restore` leave it where it
is) and the command says so.

This is a pattern check, not a guarantee: it knows the shapes of the common
tokens and password-like assignments. It misses a secret in a shape it does
not know, and it can flag a harmless sample key. It reads the whole file, in
pieces, up to SCAN_LIMIT bytes (the largest a snapshot could hold; past that
the rest is not read). `SESSIONSTEWARD_SECRET_SCAN=0`, or `"secret_scan": false` in
~/.sessionsteward/config.json, turns it off.

Nothing here ever returns, logs or prints the text that matched: only the
name of the kind of secret.
"""

from __future__ import annotations

import os
import re

#: Bytes read per piece, and how many bytes of the previous piece are kept in
#: front of the next so a secret cut by the boundary is still seen.
CHUNK = 4 * 1024 * 1024
OVERLAP = 4096
#: Most bytes of one file that are read (the default per-snapshot limit).
SCAN_LIMIT = 500 * 1024 * 1024
#: Files remembered by (path, size, mtime) so a polling daemon does not read
#: every file again every few seconds.
_CACHE_MAX = 4096

_OFF = {"0", "off", "false", "no"}

#: (name shown to the user, pattern). Names say what it looks like, never
#: what it is.
PATTERNS: tuple[tuple[str, "re.Pattern[bytes]"], ...] = (
    ("an AWS access key id",
     re.compile(rb"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("a private key",
     re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")),
    ("a GitHub token",
     re.compile(rb"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}"
                rb"|github_pat_[A-Za-z0-9_]{30,})")),
    ("a Slack token",
     re.compile(rb"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an API key (sk-...)",
     re.compile(rb"(?<![A-Za-z0-9])sk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("a Google API key",
     re.compile(rb"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])")),
    ("a Stripe live key",
     re.compile(rb"(?<![A-Za-z0-9])[sr]k_live_[0-9A-Za-z]{16,}")),
    ("a JSON web token",
     re.compile(rb"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\."
                rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a password inside a web address",
     re.compile(rb"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+.-]*://"
                rb"[^\s/:@]+:[^\s/@]{3,}@")),
)

#: `password = hunter2hunter2`, `api_key: "abc123..."`. Only a long value
#: that mixes letters and digits counts: prose like "token: use the cache"
#: must not.
_ASSIGNMENT = re.compile(
    rb"(?i)(?<![A-Za-z0-9])(?:password|passwd|secret|api[_-]?key|"
    rb"access[_-]?token|auth[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*"
    rb"[\"']?([A-Za-z0-9+/_=.\-]{16,})")
#: A value that is nothing but letters is a passphrase when it is this long
#: (correcthorsebatterystaple): no digit, so the rule above misses it.
_PASSPHRASE_MIN = 20
_KIND_ASSIGNMENT = "a password or key assignment"

_cache: dict = {}


def enabled() -> bool:
    """Is the check on? Environment first, then the config file. A config
    file that cannot be read leaves it on."""
    env = os.environ.get("SESSIONSTEWARD_SECRET_SCAN", "").strip().lower()
    if env:
        return env not in _OFF
    try:
        from . import core
        return core.load_config().get("secret_scan", True) is not False
    except Exception:   # noqa: BLE001 - never let the scan break a snapshot
        return True


def _kinds(data: bytes) -> list[str]:
    kinds = [name for name, pattern in PATTERNS if pattern.search(data)]
    for m in _ASSIGNMENT.finditer(data):
        value = m.group(1).decode("ascii", "replace")     # decoded once
        if value.startswith(("http", "/", "./", "$", "{")):
            continue
        has_digit = any(c.isdigit() for c in value)
        has_alpha = any(c.isalpha() for c in value)
        if (has_digit and has_alpha) or (
                len(value) >= _PASSPHRASE_MIN and value.isalpha()
                and value.isascii()):
            kinds.append(_KIND_ASSIGNMENT)
            break
    return kinds


def _merge(into: list[str], more: list[str]) -> None:
    for k in more:
        if k not in into:
            into.append(k)


def scan(data: bytes) -> list[str]:
    """The kinds of secret `data` looks like it holds (each once, in a fixed
    order); [] for none. All of `data` is looked at."""
    return _kinds(data)


def scan_stream(f, limit: int = SCAN_LIMIT) -> list[str]:
    """scan() of an open binary file, a piece at a time (with OVERLAP bytes
    carried over), reading at most `limit` bytes."""
    kinds: list[str] = []
    carry = b""
    read = 0
    while read < limit:
        piece = f.read(min(CHUNK, limit - read))
        if not piece:
            break
        read += len(piece)
        _merge(kinds, _kinds(carry + piece))
        carry = piece[-OVERLAP:]
    order = [name for name, _ in PATTERNS] + [_KIND_ASSIGNMENT]
    return sorted(kinds, key=order.index)


def file_kinds(path: str, size: int | None = None) -> list[str]:
    """scan() of a file, cached by (path, size, modification time) taken from
    the OPEN handle (fstat), so the file that is read is the file that was
    measured. A file that cannot be read has no kinds (the snapshot reports
    it anyway)."""
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        try:
            st = os.fstat(f.fileno())
        except OSError:
            return []
        key = (path, st.st_size, st.st_mtime_ns)
        hit = _cache.get(key)
        if hit is not None:
            return hit
        try:
            kinds = scan_stream(f)
        except OSError:
            return []
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = kinds
    return kinds


def hide(line: str) -> str:
    """`line`, or a placeholder when it looks like it holds a secret (for
    anything that prints lines of a file back)."""
    if scan(line.encode("utf-8", "replace")):
        return "[hidden: this line looks like it holds a secret]"
    return line
