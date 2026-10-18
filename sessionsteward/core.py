"""Snapshot backend: git-versions watched files. Every snap is a commit.

Layout inside the snapshots repo:
  files/<rel>          mirrored watched files (only this snapshot's project)
  manifest.json        {"project_root": ..., "files": {rel: abspath},
                        "kinds": {rel: "file" | "dir"},
                        "watch": [the watch list in force]} for the snap

rel is the path relative to the project root; files outside the project
are mirrored under __home/... (inside your home directory) or
__abs/<drive-or-root>/... (anywhere else). A mirror path can therefore
never climb out of files/ — and snap() verifies that before touching
anything.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import fnmatch
import functools
import hashlib
import io
import json
import os
import random
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata

try:  # POSIX: advisory whole-file locks
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:  # Windows: byte-range locks
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

REPO_DIR = os.path.expanduser("~/.sessionsteward/snapshots")
CONFIG_PATH = os.path.expanduser("~/.sessionsteward/config.json")
TRASH_DIR = os.path.expanduser("~/.sessionsteward/trash")

# Undo/restore never delete: replaced files and directories go to
# TRASH_DIR. Only the newest TRASH_KEEP entries are kept there.
TRASH_KEEP = 20

# Claude Code's auto-memory directory: the <project> folder name is derived
# from the project's git repository (or its root outside git); see
# auto_memory_dir(). Only watched when it exists.
AUTO_MEMORY = "~/.claude/projects/<project>/memory"

# ~/.claude/CLAUDE.md is Claude Code's user-level memory; CLAUDE.local.md
# is the per-project private memory file.
DEFAULT_WATCH = ["CLAUDE.md", "CLAUDE.local.md", ".claude", "AGENTS.md",
                 "~/.claude/CLAUDE.md", AUTO_MEMORY]

# Names skipped when walking watched directories (snap + watch daemon).
# Transcripts and logs balloon the snapshots repo; they are never
# snapshotted. `.git` is always skipped too.
IGNORE_PATTERNS = ["*.log", "*transcript*", "__pycache__", ".git",
                   "node_modules"]

# Dependencies and build output are never your memory, and a virtualenv can
# be hundreds of megabytes, so these directories are skipped too, wherever
# they sit inside a watched directory (directories only: a FILE called
# `build` or `target` is a script somebody wrote):
#   - a Python virtual environment: `.venv`, or any directory with a
#     pyvenv.cfg (`venv`, `env`, ...);
#   - `dist`, `build` and `target` next to a build manifest - only there,
#     because `rules/build/` or `commands/dist/` is the user's own and must
#     never be left out of a snapshot on the strength of its name alone.
BUILD_OUTPUT_DIRS = ["dist", "build", "target"]
BUILD_MANIFESTS = ["package.json", "pyproject.toml", "setup.py", "setup.cfg",
                   "Cargo.toml", "pom.xml", "build.gradle",
                   "build.gradle.kts", "build.sbt", "CMakeLists.txt",
                   "Makefile", "meson.build", "go.mod", "tsconfig.json"]

# A file found INSIDE a watched directory that is bigger than this is not
# snapshotted (`snap` says so); a file you named yourself is exempt.
# SESSIONSTEWARD_MAX_FILE_MB changes the limit, 0 lifts it.
MAX_FILE_MB = 10.0

# Snapshots must be byte-exact: never let a user's global core.autocrlf
# (common on Windows) rewrite CRLF/LF on add, show or archive. Paths must
# be byte-exact too: with core.quotePath (git's default) a non-ASCII name
# such as commands/Καλάς.md is listed as "\316\232..." and never matches
# the name on disk.
#
# The same goes for the rest of YOUR git configuration that could change
# what a snapshot is, or stop one from being made in this private repo:
# signing (a commit that needs a passphrase or a missing gpg fails), hooks
# (your core.hooksPath, or hooks a template dir copied into the new repo),
# a global ignore file, a file-system monitor (it may start a daemon that
# keeps the repo busy), automatic gc/maintenance (it detaches and outlives
# the command that started it) and colour. Every internal git call
# overrides them on its own command line; your config files are not touched.
_GIT_CONFIG = (
    ("core.autocrlf", "false"),
    ("core.quotePath", "false"),
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),   # git's own spelling of "no such dir"
    ("core.excludesFile", ""),
    ("commit.gpgsign", "false"),
    ("tag.gpgsign", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("color.ui", "false"),
)
GIT = ["git"] + [arg for key, value in _GIT_CONFIG
                 for arg in ("-c", f"{key}={value}")]

# GIT_* variables that only say WHICH config files git reads or where its
# own programs live: your git is set up through them, so they are kept.
# Every other GIT_* variable is dropped from the environment of the git
# calls: GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE / GIT_OBJECT_DIRECTORY...
# would point git at a repository that is not ours (git exports several of
# them to its hooks and aliases), GIT_AUTHOR_* / GIT_COMMITTER_* would
# stamp their identity and dates on our commits, and GIT_CONFIG_COUNT /
# GIT_CONFIG_PARAMETERS would inject configuration.
_GIT_ENV_KEEP = frozenset({"GIT_EXEC_PATH", "GIT_CONFIG_GLOBAL",
                           "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})

# Written to <repo>/.git/info/attributes (highest attribute precedence):
# a global `* text=auto` attributes file, or a .gitattributes inside a
# watched dir that gets mirrored into the repo, would otherwise still
# rewrite CRLF->LF on add/archive, run filters, or drop files from the
# restore archive.
ATTRIBUTES = "* -text -filter -ident -export-ignore -export-subst\n"


class StewardError(Exception):
    """Base for clean, user-facing sessionsteward errors."""


class GitError(StewardError, RuntimeError):
    """git is missing, or a git command failed (stale index.lock, ...)."""


class NothingToRestoreError(StewardError):
    """Raised when a snapshot has no file manifest (e.g. the init commit)."""


class CrossProjectError(StewardError):
    """Raised when a snapshot belongs to a different project root."""


class LockTimeoutError(StewardError):
    """Raised when another sessionsteward kept the lock too long."""


class SettingsError(StewardError):
    """The Claude Code settings file cannot be merged into without risking
    the user's settings; it was left untouched."""


class SnapshotDamagedError(StewardError):
    """Raised when a restore would replace live files with something the
    snapshot cannot provably supply (a torn or incomplete snapshot)."""


def _ignored_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in IGNORE_PATTERNS)


def _ignored_dir(parent: str, name: str) -> bool:
    """Is the directory parent/name skipped when a watched directory is
    walked: an IGNORE_PATTERNS name, a virtual environment, or build output
    (see BUILD_OUTPUT_DIRS)."""
    if _ignored_name(name):
        return True
    full = os.path.join(parent, name)
    if fnmatch.fnmatch(name, ".venv") \
            or os.path.isfile(os.path.join(full, "pyvenv.cfg")):
        return True
    return (any(fnmatch.fnmatch(name, pat) for pat in BUILD_OUTPUT_DIRS)
            and any(os.path.isfile(os.path.join(parent, m))
                    for m in BUILD_MANIFESTS))


def max_file_bytes() -> int | None:
    """The size above which a file inside a watched directory is left out of
    snapshots, or None for no limit. SESSIONSTEWARD_MAX_FILE_MB (megabytes
    of 1024 * 1024 bytes, fractions allowed) overrides MAX_FILE_MB; 0 means
    no limit; anything that is not a finite number >= 0 is ignored."""
    raw = os.environ.get("SESSIONSTEWARD_MAX_FILE_MB")
    try:
        megabytes = float(raw) if raw and raw.strip() else MAX_FILE_MB
    except ValueError:
        megabytes = MAX_FILE_MB
    if not 0 <= megabytes < float("inf"):   # also false for nan
        megabytes = MAX_FILE_MB
    return int(megabytes * 1024 * 1024) or None


def _regular_size(path: str) -> int | None:
    """Size of the regular file at path (a link to one counts); None for
    anything else - a pipe, a socket, a device, a dangling link - or what
    cannot be examined."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def _norm_root(root: str | None) -> str | None:
    if root is None:
        return None
    return os.path.abspath(os.path.expanduser(root))


# ---------------------------------------------------------------------------
# One writer at a time
# ---------------------------------------------------------------------------
# Every project, hook, `watch` daemon and manual command shares ONE snapshots
# repo (one index, one files/ mirror, one manifest.json). Two processes in it
# at once collided on git's index.lock, and - worse - one deleted files/
# while the other was adding them, committing a snapshot whose tree lacked
# files (or a half-written manifest). Everything that writes takes this lock.
#
# The lock is an OS-level file lock (flock on POSIX, msvcrt.locking on
# Windows) on a file NEXT TO the snapshots repo (never inside the work tree
# `git add -A` commits from). The OS drops it when the process dies, even
# under `kill -9`, so there is no stale lock to clean up.

LOCK_TIMEOUT = 30.0   # seconds to wait for another sessionsteward
_LOCK_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK,
              getattr(errno, "EDEADLOCK", errno.EDEADLK)}
_held = threading.local()   # per thread: how deep the nested `locked()` is


def lock_path() -> str:
    return os.path.join(os.path.dirname(REPO_DIR) or ".", "steward.lock")


def _private_dir(path: str) -> None:
    """Create the folder that holds the snapshots repo, the trash, the config
    and the lock (~/.sessionsteward) so that only you can enter it: the repo
    mirrors your memory files - CLAUDE.local.md is private by design - and the
    trash holds whatever undo replaced. 0700 on POSIX, and an older folder
    that is open to group or others is tightened (nothing else is touched).
    Windows has no such mode; the folder inherits your profile's access."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "nt":
        return
    try:
        st = os.stat(path)
        if st.st_mode & 0o077 and st.st_uid == os.geteuid():
            os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o077)
    except OSError:
        pass


def _lock_timeout() -> float:
    raw = os.environ.get("SESSIONSTEWARD_LOCK_TIMEOUT")
    try:
        value = float(raw) if raw else LOCK_TIMEOUT
    except ValueError:
        return LOCK_TIMEOUT
    return value if value == value and value >= 0 else LOCK_TIMEOUT


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt: True = acquired, False = held elsewhere.
    Anything else wrong with locking is an error, never a silent no-lock."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            raise StewardError("this platform has no file locking "
                               "(fcntl or msvcrt), so snapshots cannot be "
                               "serialised safely")
    except OSError as e:
        if e.errno in _LOCK_BUSY:
            return False
        raise StewardError(f"cannot lock {lock_path()}: "
                           f"{e.strerror or e}") from None
    return True


def _unlock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # closing the descriptor releases it anyway


@contextlib.contextmanager
def locked(timeout: float | None = None):
    """Hold the sessionsteward lock for the duration of the block. Waits up
    to `timeout` seconds (default 30, SESSIONSTEWARD_LOCK_TIMEOUT overrides)
    for another sessionsteward, then raises LockTimeoutError. Re-entrant:
    commands that call other locking functions (undo -> snap) nest freely."""
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = lock_path()
    _private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        wait = _lock_timeout() if timeout is None else timeout
        deadline = time.monotonic() + wait
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"another sessionsteward is still working in {REPO_DIR} "
                    f"(waited {wait:g}s for {path}); try again in a moment")
            time.sleep(0.02 + random.random() * 0.06)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            _unlock(fd)
    finally:
        os.close(fd)


def _git_env() -> dict[str, str]:
    """Your environment minus the GIT_* variables that would redirect or
    reconfigure our git calls (see _GIT_ENV_KEEP). Windows upper-cases
    variable names; the comparison does too."""
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith("GIT_") or k.upper() in _GIT_ENV_KEEP}


def _is_snapshots_repo(repo: str) -> bool:
    return os.path.abspath(repo) == os.path.abspath(REPO_DIR)


def _run_git(repo: str, args: tuple[str, ...], **kw) -> subprocess.CompletedProcess:
    """subprocess.run of a git command with the failure modes that are not
    git's own exit code turned into GitError: no git on PATH, a hang.

    The snapshots repo is named outright (--git-dir/--work-tree): without
    that, git "discovers" a repository by walking up from the directory,
    and past a .git it finds unusable that is whatever repository the home
    directory happens to be (a dotfiles repo) - which would then receive our
    `add -A` and our commits."""
    where = ["-C", repo]
    if _is_snapshots_repo(repo):
        where += ["--git-dir", os.path.join(repo, ".git"), "--work-tree", repo]
    try:
        return subprocess.run([*GIT, *where, *args], capture_output=True,
                              timeout=60, env=_git_env(), **kw)
    except FileNotFoundError:
        raise GitError("git not found — sessionsteward needs git on PATH "
                       "(https://git-scm.com/downloads)") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} did not finish within 60s") \
            from None
    except OSError as e:   # not executable, wrong format, out of processes
        raise GitError(f"cannot run git: {e.strerror or e}") from None


def _git(repo: str, *args: str) -> str:
    # git prints paths as UTF-8 bytes on every platform; decode them the
    # way os.walk() names files so the two always compare equal.
    p = _run_git(repo, args, encoding="utf-8", errors="surrogateescape")
    if p.returncode != 0:
        # one line: hook output and `error:` lines must stay greppable
        raise GitError(f"git {' '.join(args)} failed: "
                       f"{' '.join(p.stderr.split())[:200]}")
    return p.stdout


def repo_ready() -> bool:
    """True when the snapshots repo exists AND has its first commit. A repo
    that `git init` created but whose first commit never happened (signing,
    a rejecting hook, a crash between the two steps) is not ready: init_repo()
    finishes it. A repo git cannot read at all raises GitError - guessing
    would mean committing into something we do not understand."""
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        return False
    p = _run_git(REPO_DIR, ("rev-parse", "--verify", "--quiet",
                            "HEAD^{commit}"))
    if p.returncode == 0:
        return True
    refs = _run_git(REPO_DIR, ("for-each-ref", "--count=1"))
    if refs.returncode == 0 and not refs.stdout.strip():
        return False   # initialised, never committed to
    detail = ' '.join((refs.stderr or p.stderr).decode(
        "utf-8", "replace").split())[:160] or "its HEAD cannot be read"
    raise GitError(f"the snapshots repo {REPO_DIR} cannot be used ({detail}); "
                   f"nothing was changed - move it aside to start over")


def _ensure_attributes() -> None:
    """Write ATTRIBUTES to .git/info/attributes. Idempotent; runs on init
    and on every snap/restore so existing repos pick it up."""
    path = os.path.join(REPO_DIR, ".git", "info", "attributes")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == ATTRIBUTES:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(ATTRIBUTES)


def init_repo() -> str:
    with locked():
        os.makedirs(REPO_DIR, exist_ok=True)
        if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
            _git(REPO_DIR, "init", "-qb", "main")
        if not repo_ready():   # new, or left without its first commit
            _git(REPO_DIR, "config", "--local", "user.email",
                 "sessionsteward@local")
            _git(REPO_DIR, "config", "--local", "user.name", "sessionsteward")
            _git(REPO_DIR, "commit", "-q", "--allow-empty", "-m", "init")
        _ensure_attributes()
        if not os.path.exists(CONFIG_PATH):
            save_config({"watch": []})
        return REPO_DIR


def load_config() -> dict:
    """The watch config. A missing file means defaults; a corrupt one is
    an error (and is left in place) — silently replacing it would throw
    away the user's watch list."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {"watch": []}
    try:
        cfg = json.loads(raw)
    except ValueError as e:
        raise StewardError(
            f"watch config {CONFIG_PATH} is not valid JSON ({e}); fix it "
            f"or delete it to start from the defaults (it was left as is)")
    if not isinstance(cfg, dict):
        raise StewardError(f"watch config {CONFIG_PATH} must be a JSON "
                           f"object (it was left as is)")
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def watch_list(cfg: dict | None = None) -> list[str]:
    """The effective watch list: DEFAULT_WATCH (minus anything dropped
    with `watch --remove`, kept in cfg["unwatch"]) plus the user's own
    additions (cfg["watch"]), deduped in order. Adding a path never
    replaces the defaults."""
    cfg = load_config() if cfg is None else cfg
    unwatch = set(cfg.get("unwatch") or [])
    out: list[str] = []
    for rel in [*DEFAULT_WATCH, *(cfg.get("watch") or [])]:
        if rel not in unwatch and rel not in out:
            out.append(rel)
    return out


def edit_watch_list(cfg: dict, add: list[str] = (),
                    remove: list[str] = ()) -> dict:
    """Apply `watch --add/--remove` to cfg in place and return it."""
    watch = cfg.setdefault("watch", [])
    unwatch = cfg.setdefault("unwatch", [])
    for rel in add:
        if rel in unwatch:
            unwatch.remove(rel)
        elif rel not in DEFAULT_WATCH and rel not in watch:
            watch.append(rel)
    for rel in remove:
        if rel in watch:
            watch.remove(rel)
        if rel in DEFAULT_WATCH and rel not in unwatch:
            unwatch.append(rel)
    if not unwatch:
        del cfg["unwatch"]
    return cfg


def _claude_config_dir() -> str:
    # normpath: expanduser("~/.claude") keeps the forward slash on Windows,
    # which would otherwise leak into every path built from it.
    return os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR")
                            or os.path.expanduser("~/.claude"))


def _project_slug(path: str) -> str:
    """Claude Code names a project's folder under ~/.claude/projects/ after
    its absolute path with every non-alphanumeric character replaced by
    '-' (/home/me/proj -> -home-me-proj, C:\\code\\proj -> C--code-proj)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def auto_memory_dir(project_root: str) -> str | None:
    """Claude Code's auto-memory directory for project_root
    (~/.claude/projects/<project>/memory), or None when none exists.

    <project> is derived from the git repository the project is in, so a
    subdirectory or worktree shares the main checkout's memory; outside
    git it is the project root itself. Candidates are tried from the
    repository root down to project_root, and only an existing directory
    is returned — a guess that is wrong on some setup just leaves the
    memory dir unwatched (add it with `watch --add` then).
    """
    root = _norm_root(project_root)
    candidates = []
    try:
        p = _run_git(root, ("rev-parse", "--path-format=absolute",
                            "--git-common-dir", "--show-toplevel"),
                     encoding="utf-8", errors="surrogateescape")
        if p.returncode == 0:
            # split on "\n" only: str.splitlines() also splits on U+2028 and
            # the like, which a directory name may contain
            common, top = (p.stdout.split("\n") + ["", ""])[:2]
            if common.strip():
                candidates.append(os.path.dirname(os.path.abspath(common)))
            if top.strip():
                candidates.append(os.path.abspath(top))
    except GitError:
        pass
    candidates.append(root)
    projects = os.path.join(_claude_config_dir(), "projects")
    for cand in candidates:
        d = os.path.join(projects, _project_slug(cand), "memory")
        if os.path.isdir(d):
            return d
    return None


def _expand_watch_entry(rel: str, project_root: str) -> str | None:
    """Absolute path for a watch-list entry, or None when it has no
    location (the auto-memory dir of a project without one)."""
    if rel == AUTO_MEMORY:
        return auto_memory_dir(project_root)
    if rel[:1] == "~" and (len(rel) == 1 or rel[1] in "/\\"):
        # `~\x.md` (Windows spelling) must expand like `~/x.md` everywhere
        return os.path.expanduser("~" + rel[1:].replace("\\", os.sep))
    if os.path.isabs(rel):
        return rel
    return os.path.join(project_root, rel)


def _same_entry(a: str, b: str) -> bool:
    """Are these two paths one directory entry? Compared as lstat sees them,
    so a symlink is itself and not what it points at."""
    try:
        return os.path.samestat(os.lstat(a), os.lstat(b))
    except OSError:
        return False


def _watched_entries(project_root: str) -> list[tuple[str, str]]:
    """[(watch-list entry, absolute path)] for the watched paths that
    exist now, deduped by path (the first entry naming a path wins).

    So are two spellings that differ only by case and name one file: on a
    case-insensitive file system `claude.md` next to the default `CLAUDE.md`
    is the same path, and mirrored twice the second spelling would land on
    the first one's file - git records a single name and the completeness
    check would refuse every snapshot. (On a case-sensitive one two files
    that merely differ by case are two paths, and both are kept.)"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    by_fold: dict[str, list[str]] = {}
    for rel in watch_list():
        p = _expand_watch_entry(rel, project_root)
        # A regular file or a directory (or a link to one). A named pipe,
        # socket or device cannot be snapshotted - reading a pipe blocks
        # for ever - so it is as good as absent.
        if p and (os.path.isfile(p) or os.path.isdir(p)):
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            twins = by_fold.setdefault(ap.casefold(), [])
            if any(_same_entry(ap, other) for other in twins):
                continue
            seen.add(ap)
            twins.append(ap)
            out.append((rel, ap))
    return out


def watched_paths(project_root: str) -> list[str]:
    """Absolute paths of watched files that currently exist."""
    return [ap for _, ap in _watched_entries(project_root)]


def _in_scope(entry: str, manifest: dict) -> bool:
    """Was this watch-list entry on the list when the snapshot was taken?

    A path added with `watch --add` after a snapshot is outside that
    snapshot's scope: it neither makes the snapshot look "dirty" nor is
    swept into the trash by a restore of it. Snapshots without a recorded
    list (older repos) scope to the whole current list.
    """
    was = manifest.get("watch")
    return was is None or entry in was


def _scoped_paths(project_root: str, manifest: dict) -> list[str]:
    """The watched paths that exist now, limited to the snapshot's scope."""
    return [ap for entry, ap in _watched_entries(project_root)
            if _in_scope(entry, manifest)]


def _inside(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _abs_rel(abspath: str) -> str:
    """__abs/<drive-or-root>/<path> for a file outside home and project.
    The drive letter or UNC host is a plain name, never `D:` (a colon is
    not a valid filename character on Windows)."""
    drive, tail = os.path.splitdrive(abspath)
    if drive:
        label = drive.strip("\\/:").replace("\\", "/")
        if drive.startswith(("\\\\", "//")):
            label = "UNC/" + label
        tail = tail.replace("\\", "/")
    else:
        label = "root"
        tail = tail.replace(os.sep, "/")
    return f"__abs/{label}/{tail.lstrip('/')}"


def _rel(abspath: str, project_root: str) -> str:
    """Path of abspath inside files/, always with forward slashes (git's
    tree paths are '/'-separated on every platform).

    Inside the project: the project-relative path, so a guard line names
    `CLAUDE.md` whether or not the project lives under your home dir.
    Elsewhere under home: __home/<path from home>. Anywhere else:
    __abs/<drive-or-root>/<path>. Never a relpath with `..` — that used to
    let a mirror path escape files/ and, at the wrong depth, resolve to
    the watched file itself.
    """
    project_root = _norm_root(project_root)
    home = os.path.abspath(os.path.expanduser("~"))
    if abspath == project_root or abspath == home:
        raise StewardError(f"cannot watch {abspath}: it is the project root "
                           f"or your home directory itself")
    if _inside(abspath, project_root):
        rel = os.path.relpath(abspath, project_root)
    elif _inside(abspath, home):
        rel = os.path.join("__home", os.path.relpath(abspath, home))
    else:
        return _abs_rel(abspath)
    return rel.replace(os.sep, "/")


def display_rel(rel: str) -> str:
    """A snapshot rel as the user knows the path: CLAUDE.md, ~/.claude/…,
    /abs/… or D:/…"""
    if rel.startswith("__home/"):
        return "~/" + rel[len("__home/"):]
    if rel.startswith("__abs/"):
        rest = rel[len("__abs/"):]
        if rest.startswith("root/"):
            return "/" + rest[len("root/"):]
        if rest.startswith("UNC/"):
            return "//" + rest[len("UNC/"):]
        drive, _, tail = rest.partition("/")
        return f"{drive}:/{tail}"
    return rel


def _mirror_path(files_dir: str, rel: str) -> str:
    """files_dir/rel, verified to stay inside files_dir. Belt and braces:
    _rel() can no longer produce an escaping path, but nothing in the
    repo is removed or written before this check passes."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise StewardError(f"refusing to mirror {rel!r}: not a plain "
                           f"relative path")
    dest = os.path.join(files_dir, *parts)
    base = os.path.realpath(files_dir)
    real = os.path.realpath(dest)
    try:
        ok = os.path.commonpath([base, real]) == base and real != base
    except ValueError:  # different drives on Windows
        ok = False
    if not ok:
        raise StewardError(f"refusing to mirror {rel!r}: {dest} is outside "
                           f"the snapshots repo")
    return dest


def _make_writable(path: str) -> None:
    """Add the owner write bit (also clears Windows' read-only attribute)."""
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    except OSError:
        pass


def _remove_mirror(path: str) -> None:
    """Remove a mirrored file or tree even if parts of it are read-only
    (a 0444 file, a 0555 dir, the Windows read-only attribute): make the
    offending path and its parent writable, then retry once."""
    def retry(func, p, _exc):
        _make_writable(p)
        _make_writable(os.path.dirname(p))
        func(p)

    if os.path.isdir(path) and not os.path.islink(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:  # onerror(func, path, exc_info) — same shape for us
            shutil.rmtree(path, onerror=retry)
    else:
        try:
            os.unlink(path)
        except OSError:
            retry(os.unlink, path, None)


def _not_copied(dirpath: str, names: list[str],
                too_big: list | None = None) -> set[str]:
    """copytree's ignore: the IGNORE_PATTERNS, the virtual environments and
    build output (_ignored_dir), everything that is not a regular file or a
    directory once links are followed - copying a named pipe blocks for ever
    or fails, a socket or a device cannot be copied at all, a dangling link
    has nothing behind it - and regular files over max_file_bytes(), which
    are appended to too_big as (path, size)."""
    limit = max_file_bytes()
    skip = set()
    for name in names:
        full = os.path.join(dirpath, name)
        if _ignored_name(name):
            skip.add(name)
        elif os.path.isdir(full):
            if _ignored_dir(dirpath, name):
                skip.add(name)
        else:
            size = _regular_size(full)
            if size is None:
                skip.add(name)
            elif limit is not None and size > limit:
                skip.add(name)
                if too_big is not None:
                    too_big.append((full, size))
    return skip


def _copy_content(src: str, dst: str) -> str:
    """Copy a file's bytes and its permission bits - nothing else, and never
    more access than the source gives (a 0600 file is not mirrored 0644).
    The owner can always read and replace the copy, so the mirror never
    inherits a read-only mode that the next snap could not get past. git
    records only one bit from the copy's mode, the owner's execute bit, and a
    hook script that loses its +x in the snapshot comes back from `undo`
    unable to run. Windows has only a read-only flag and no execute bit."""
    shutil.copyfile(src, dst)
    try:
        mode = os.stat(src).st_mode
        if os.name == "nt":
            os.chmod(dst, 0o755 if mode & stat.S_IXUSR else 0o644)
        else:
            os.chmod(dst, (mode & 0o777) | 0o600)
    except OSError:
        pass
    return dst


def _set_executable(path: str, executable: bool) -> None:
    """Make path's execute bits match the snapshot's file mode (100755 or
    100644). The bit is added wherever the file is readable, the way
    `chmod +x` would; Windows files have no such bit."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        want = (mode | ((mode & 0o444) >> 2)) if executable else mode & ~0o111
        if want != mode:
            os.chmod(path, want)
    except OSError:
        pass


def _composed(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _check_staged(files_dir: str, kinds: dict[str, str]) -> None:
    """Refuse to commit a snapshot that lacks something it mirrored: every
    file under files/ (and every watched file) must be in the index, under
    the name the mirror gives it.

    Names are compared exactly - in particular NOT ignoring case: _snap
    rebuilds the index from the mirror every time, so a name that differs
    only in case is another file, one git did not record. The one rewrite of
    a name that is expected is Unicode composition: macOS keeps a name like
    e-acute decomposed on disk (e + U+0301) and git, with
    core.precomposeunicode, records it composed (U+00E9). A name that
    differs from an index entry only that way is accepted - once per index
    entry, so two files cannot hide behind one."""
    mirrored = set()
    for root, _dirs, names in os.walk(files_dir):
        for name in names:
            full = os.path.join(root, name)
            mirrored.add(os.path.relpath(full, REPO_DIR).replace(os.sep, "/"))
    mirrored.update(f"files/{rel}" for rel, kind in kinds.items()
                    if kind == "file")
    staged = {p for p in
              _git(REPO_DIR, "ls-files", "-z", "--", "files").split("\0") if p}
    missing = sorted(mirrored - staged)
    if missing:
        spare = collections.Counter(_composed(p) for p in staged - mirrored)
        unmatched = []
        for name in missing:
            if spare[_composed(name)] > 0:
                spare[_composed(name)] -= 1
            else:
                unmatched.append(name)
        missing = unmatched
    if missing:
        raise StewardError(
            f"snapshot incomplete: git did not record {len(missing)} of the "
            f"mirrored file(s) (first: {missing[0]}) - nothing was committed")


def snap(project_root: str, message: str) -> str:
    """Copy watched files into the repo and commit. Returns commit sha.
    Serialised with every other sessionsteward process (see locked())."""
    with locked():
        return _snap(project_root, message)


def snap_report(project_root: str, message: str) -> tuple[str, list]:
    """snap(), plus the files it left out for being over max_file_bytes():
    (sha, [(path, size), ...]) in path order."""
    too_big: list = []
    with locked():
        sha = _snap(project_root, message, too_big)
    return sha, sorted(set(too_big))


def _snap(project_root: str, message: str, too_big: list | None = None) -> str:
    if not repo_ready():
        init_repo()
    _ensure_attributes()
    paths = watched_paths(project_root)
    files_dir = os.path.join(REPO_DIR, "files")
    # Plan every mirror path first: nothing is removed or written until
    # all of them are known to sit inside files/.
    plan = []
    for ap in paths:
        rel = _rel(ap, project_root)
        plan.append((rel, ap, _mirror_path(files_dir, rel)))
    manifest = {rel: ap for rel, ap, _ in plan}
    # What each watched path IS: a path with a manifest entry but nothing in
    # the tree is an empty directory only if it was one. (A file that git
    # silently skipped used to look exactly like that, and `undo` then
    # turned the live file into an empty directory.)
    kinds = {rel: ("dir" if os.path.isdir(ap) else "file")
             for rel, ap, _ in plan}
    # files/ holds exactly this snapshot's files: stale mirrors of other
    # projects (the repo is shared) would otherwise ride along in every
    # commit and show up in `diff`.
    if os.path.lexists(files_dir):
        _remove_mirror(files_dir)
    os.makedirs(files_dir)
    for rel, ap, dest in plan:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # _copy_content, not copy2/copytree's default: the mirror must never
        # inherit read-only modes, or the next snap cannot replace it.
        # (dirs_exist_ok / the lexists check: a watched file inside a
        # watched dir, e.g. ~/.claude/CLAUDE.md with ~ as the project.)
        if os.path.isdir(ap):
            # Symlinks are followed: the mirror holds the content behind
            # them (restore() never writes it back through the link).
            shutil.copytree(ap, dest, copy_function=_copy_content,
                            ignore=functools.partial(_not_copied,
                                                     too_big=too_big),
                            dirs_exist_ok=True)
        elif not os.path.lexists(dest):
            _copy_content(ap, dest)
    # No timestamp in the manifest: identical content must produce an
    # identical tree so the re-snap below is skipped. (It used to carry
    # "at"; two hooks snapping the same clobbered state seconds apart
    # made two commits, and `undo` then stepped back to the clobbered one.)
    # "watch" records the effective watch list, so a restore knows which
    # paths were in this snapshot's scope (see _in_scope).
    with open(os.path.join(REPO_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump({"project_root": _norm_root(project_root),
                   "files": manifest, "kinds": kinds,
                   "watch": watch_list()}, f, indent=2)
    # The index is rebuilt from the mirror on every snapshot, never updated
    # in place. On a case-insensitive file system (Windows, macOS) git sees
    # a file or directory renamed by case alone (Notes.md -> notes.md) as
    # the entry it already has: the old-case path stays in the index (the
    # file "still exists") and the new-case name is never added, so the
    # snapshot would hold the wrong name - or, with the check below, be
    # refused for good. Starting from an empty index makes the tree what
    # the mirror holds, spelled the way the mirror spells it. Whatever an
    # earlier, refused snapshot left in the index goes with it.
    _git(REPO_DIR, "read-tree", "--empty")
    # -f: the snapshot must hold EVERY mirrored file. Without it `git add`
    # obeys the ignore rules - the user's global ignore file, but also a
    # .gitignore that was itself mirrored out of a watched directory - and
    # silently leaves matching files out of the tree.
    _git(REPO_DIR, "add", "-A", "-f")
    _check_staged(files_dir, kinds)
    # Skip the commit when nothing changed FOR THIS PROJECT: compare the
    # index tree with the tree of this project's newest snapshot, not with
    # global HEAD — the repo is shared, and with two projects snapping in
    # turn (A, B, A) HEAD is B's, so A's identical state was re-committed
    # and the next `undo` stepped onto that duplicate instead of the good
    # snapshot before it.
    tree = _git(REPO_DIR, "write-tree").strip()
    newest = log(1, project_root=project_root)
    if newest:
        prev = _git(REPO_DIR, "rev-parse",
                    f"{newest[0]['sha']}^{{tree}}").strip()
        if prev == tree:
            return newest[0]["sha"]
    _git(REPO_DIR, "commit", "-q", "-m", message)
    return _git(REPO_DIR, "rev-parse", "HEAD").strip()


def _manifests_at(shas: list[str], strict: bool = False) -> dict[str, dict]:
    """{sha: manifest} for many snapshots in one git call (cat-file
    --batch); a commit without manifest.json maps to {}. A manifest that
    cannot be parsed (a torn write from an older version) maps to {} too -
    one broken snapshot must not take log/undo/snap down for every project
    - unless strict, which raises StewardError."""
    if not shas:
        return {}
    p = _run_git(REPO_DIR, ("cat-file", "--batch"),
                 input="".join(f"{s}:manifest.json\n" for s in shas).encode())
    if p.returncode != 0:
        raise GitError(f"git cat-file --batch failed: "
                       f"{p.stderr.decode('utf-8', 'replace').strip()[:200]}")
    out, data, pos = {}, p.stdout, 0
    for sha in shas:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] != "blob":  # "<spec> missing"
            out[sha] = {}
            continue
        size = int(header[2])
        raw = data[pos:pos + size]
        pos += size + 1
        try:
            out[sha] = json.loads(raw.decode("utf-8", "surrogateescape"))
        except ValueError as e:
            if strict:
                raise StewardError(f"snapshot {sha[:7]} has a corrupt "
                                   f"manifest.json ({e})") from None
            out[sha] = {}
    return out


def _manifest_at(sha: str, strict: bool = False) -> dict:
    try:
        full = _git(REPO_DIR, "rev-parse", "--verify", "--quiet",
                    f"{sha}^{{commit}}").strip()
    except GitError:
        return {}
    return _manifests_at([full], strict).get(full, {})


def log(limit: int = 20, project_root: str | None = None) -> list[dict]:
    """List snapshots, newest first.

    When project_root is given, only snapshots whose manifest records
    that project root are returned (manifest-less commits like the
    initial `init` commit belong to no project and are excluded). The
    whole history is scanned — the repo is shared between projects, so
    this project's snapshots may sit behind any number of others'.
    """
    # NUL-separated (-z, %x00): a subject may hold any character but NUL -
    # a newline-split listing broke on U+2028, U+0085, ... in a message
    args = ["log", "-z", "--pretty=format:%H%x00%ad%x00%s", "--date=iso-local"]
    if project_root is None:
        args.insert(1, f"-{limit}")
    out = _git(REPO_DIR, *args)
    want = _norm_root(project_root)
    fields = out.split("\0")
    rows = [fields[i:i + 3] for i in range(0, len(fields) - 2, 3)]
    manifests = _manifests_at([r[0] for r in rows]) if want else {}
    snaps = []
    for sha, date, msg in rows:
        entry = {"sha": sha, "date": date, "message": msg}
        if want is not None:
            m = manifests.get(sha) or {}
            if not m or _norm_root(m.get("project_root")) != want:
                continue
            entry["project_root"] = m.get("project_root")
        snaps.append(entry)
        if len(snaps) >= limit:
            break
    return snaps


def _read_snapshot_file(sha: str, rel: str) -> tuple[bytes | None, bool]:
    """(content, is_dir) for files/<rel> at sha."""
    ls = _git(REPO_DIR, "ls-tree", sha, f"files/{rel}").strip()
    if not ls:
        return None, False
    if ls.startswith("040000"):
        return None, True
    p = _run_git(REPO_DIR, ("show", f"{sha}:files/{rel}"))
    return (p.stdout if p.returncode == 0 else None), False


def _move_to_trash(ap: str, trash_dir: str) -> str:
    """Move a path into the trash dir; returns the trash destination."""
    os.makedirs(trash_dir, exist_ok=True)
    base = os.path.basename(ap) + f".{int(time.time())}"
    dest = os.path.join(trash_dir, base)
    n = 1
    while os.path.lexists(dest):
        n += 1
        dest = os.path.join(trash_dir, f"{base}.{n}")
    shutil.move(ap, dest)
    return dest


# _move_to_trash names an entry <name>.<epoch seconds>[.<n>]
_TRASH_STAMP = re.compile(r"\.(\d{9,})(?:\.(\d{1,4}))?$")


def _trashed_at(name: str, path: str) -> tuple[float, int]:
    """When a trash entry was put there: the stamp in its name. Not its
    mtime - a moved path keeps the mtime it had, which says nothing about
    when it was trashed (a directory untouched for a year would look like
    the oldest entry the moment it arrived). Entries without a stamp fall
    back to their mtime."""
    m = _TRASH_STAMP.search(name)
    if m:
        return float(m.group(1)), int(m.group(2) or 1)
    try:
        return os.lstat(path).st_mtime, 0
    except OSError:
        return 0.0, 0


def prune_trash(trash_dir: str, keep: int | None = None,
                protect: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Delete all but the newest `keep` (default TRASH_KEEP) trash
    entries, newest = trashed last. Entries named in `protect` (what the
    running restore has just put there) are never deleted. Returns the
    removed entries."""
    keep = TRASH_KEEP if keep is None else keep
    protected = {os.path.abspath(p) for p in protect}
    try:
        names = os.listdir(trash_dir)
    except OSError:
        return []
    entries = []
    for name in names:
        p = os.path.join(trash_dir, name)
        stamp, n = _trashed_at(name, p)
        entries.append((stamp, n, name, p))
    entries.sort(reverse=True)
    removed = []
    for _stamp, _n, _name, p in entries[keep:]:
        if os.path.abspath(p) in protected:
            continue
        try:
            _remove_mirror(p)
            removed.append(p)
        except OSError:
            pass
    return removed


def _tree_blobs(sha: str) -> dict[str, str]:
    """{path under files/: git file mode} for every file in snapshot sha.
    NUL-separated, so no file name can confuse the parse."""
    out = _git(REPO_DIR, "ls-tree", "-r", "-z", sha, "--", "files")
    blobs = {}
    for rec in out.split("\0"):
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob" and path.startswith("files/"):
            blobs[path[len("files/"):]] = parts[0]
    return blobs


def _has_live_content(ap: str) -> bool:
    """Would replacing the live path ap lose anything but ignored names?
    (A symlink is never replaced by a restore, so it never counts.)"""
    if os.path.islink(ap):
        return False
    if os.path.isdir(ap):
        return next(iter_files([ap]), None) is not None
    return os.path.lexists(ap)


def _snapshot_problems(sha: str, manifest: dict) -> list[str]:
    """Everything that stops snapshot sha from being restored faithfully:
    watched paths its manifest records that its tree cannot supply (or
    supplies as the wrong type). Empty list = safe to restore.

    A path with a manifest entry and no tree entry is legitimate only as an
    empty directory, which new snapshots say explicitly (manifest "kinds").
    For a snapshot without that information the entry is ambiguous - it may
    be an empty directory, or a file git never recorded - so it is accepted
    only when nothing live would be replaced by it."""
    files = manifest.get("files") or {}
    if not files:
        return []
    kinds = manifest.get("kinds") or {}
    blobs = _tree_blobs(sha)
    dirs = set()
    for path in blobs:
        parts = path.split("/")
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    problems = []
    for rel, ap in files.items():
        name = display_rel(rel)
        is_file, is_dir = rel in blobs, rel in dirs
        kind = kinds.get(rel)
        if kind == "file":
            if is_dir:
                problems.append(f"{name} is recorded as a file but the "
                                f"snapshot holds a directory there")
            elif not is_file:
                problems.append(f"{name} is recorded as a file but the "
                                f"snapshot has no content for it")
        elif kind == "dir":
            if is_file:
                problems.append(f"{name} is recorded as a directory but the "
                                f"snapshot holds a file there")
        elif kind is not None:
            problems.append(f"{name} has an unknown type {kind!r}")
        elif not is_file and not is_dir and _has_live_content(ap):
            problems.append(f"{name} has no content in the snapshot (taken "
                            f"by an older version?) and restoring it would "
                            f"replace the live {name} with an empty "
                            f"directory")
    return problems


def check_restore(sha: str, project_root: str | None = None) -> dict:
    """Validate that sha is restorable for project_root.

    Returns the snapshot's manifest. Raises NothingToRestoreError for a
    manifest-less snapshot (e.g. the `init` commit) or an unknown sha,
    CrossProjectError when project_root is given and the snapshot belongs
    to a different project, and SnapshotDamagedError when the snapshot
    cannot supply everything its manifest records (checked BEFORE any live
    file is touched).
    """
    manifest = _manifest_at(sha, strict=True)
    if not manifest:
        p = _run_git(REPO_DIR, ("rev-parse", "--verify", "--quiet",
                                f"{sha}^{{commit}}"))
        if p.returncode == 0:
            raise NothingToRestoreError(
                f"nothing to restore — no snapshots yet "
                f"(snapshot {sha[:7]} has no file manifest)")
        raise NothingToRestoreError(f"no such snapshot: {sha[:7]}")
    if project_root is not None:
        want = _norm_root(project_root)
        got = _norm_root(manifest.get("project_root"))
        if got != want:
            raise CrossProjectError(
                f"refusing cross-project restore: snapshot {sha[:7]} "
                f"belongs to project {manifest.get('project_root')!r}, "
                f"not the current project {want!r}")
    problems = _snapshot_problems(sha, manifest)
    if problems:
        raise SnapshotDamagedError(
            f"refusing to restore snapshot {sha[:7]}: it is incomplete - "
            + "; ".join(problems) + ". Nothing was changed; "
            "`sessionsteward log` lists the other snapshots")
    return manifest


# ---------------------------------------------------------------------------
# Tree comparison: git blob ids, computed locally
# ---------------------------------------------------------------------------

def iter_files(paths: list[str]):
    """Yield every regular file under the watched paths (directories
    walked, IGNORE_PATTERNS applied, virtual environments and build output
    left out, files inside a directory over max_file_bytes() left out; pipes,
    sockets, devices and dangling links skipped), in a stable order. This is
    the set snap() mirrors: what is compared with a snapshot must be what
    went into it.

    Symlinked directories are walked (snap() mirrors what is behind
    them, so the live tree must see the same files), with a guard so a
    link back into the tree cannot loop forever."""
    limit = max_file_bytes()
    for ap in paths:
        if os.path.isdir(ap):
            seen: set[str] = set()
            for root, dirs, files in os.walk(ap, followlinks=True):
                seen.add(os.path.realpath(root))
                keep = []
                for d in sorted(dirs):
                    if _ignored_dir(root, d):
                        continue
                    full = os.path.join(root, d)
                    if os.path.islink(full) and os.path.realpath(full) in seen:
                        continue
                    keep.append(d)
                dirs[:] = keep
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    # regular files only (links followed): the callers
                    # open() what they get, and a named pipe never returns
                    size = None if _ignored_name(fn) else _regular_size(full)
                    if size is not None and (limit is None or size <= limit):
                        yield full
        elif os.path.isfile(ap):
            yield ap


def _blob_id(data: bytes, algo: str = "sha1") -> str:
    """git's object id for a blob with this content."""
    h = hashlib.new(algo)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def live_tree(paths: list[str], algo: str = "sha1") -> dict[str, str] | None:
    """{abspath: blob id} for every file under `paths`; None when a file
    cannot be read."""
    tree = {}
    for fp in iter_files(paths):
        try:
            with open(fp, "rb") as f:
                tree[fp] = _blob_id(f.read(), algo)
        except OSError:
            return None
    return tree


def snapshot_tree(sha: str, manifest: dict | None = None) -> dict[str, str]:
    """{abspath: blob id} recorded by snapshot sha, mapped through its
    manifest (files/<rel>/<sub> -> <abspath of rel>/<sub>)."""
    manifest = _manifest_at(sha) if manifest is None else manifest
    files = manifest.get("files", {})
    if not files:
        return {}
    out = {}
    listing = _git(REPO_DIR, "ls-tree", "-r", "-z", sha, "--",
                   *(f"files/{rel}" for rel in files))
    for line in listing.split("\0"):   # -z: names arrive unquoted
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or parts[1] != "blob":
            continue
        rel = path[len("files/"):]
        if rel in files:
            out[files[rel]] = parts[2]
            continue
        for key, ap in files.items():
            if rel.startswith(key + "/"):
                sub = rel[len(key) + 1:].split("/")
                out[os.path.join(ap, *sub)] = parts[2]
                break
    return out


def _blob_algo(tree: dict[str, str]) -> str:
    return "sha256" if any(len(v) == 64 for v in tree.values()) else "sha1"


def worktree_matches_snapshot(sha: str, project_root: str,
                              manifest: dict | None = None) -> bool:
    """True if the current watched files are byte-identical to the
    snapshot's recorded state (redo is possible without a pre-snap)."""
    manifest = _manifest_at(sha) if manifest is None else manifest
    if not manifest:
        return False
    current = _scoped_paths(project_root, manifest)
    if set(manifest.get("files", {}).values()) != set(current):
        return False
    snap = snapshot_tree(sha, manifest)
    live = live_tree(current, _blob_algo(snap))
    return live is not None and live == snap


def current_position(snaps: list[dict], project_root: str) -> int | None:
    """Index of the newest snapshot the live files match, or None when
    the current state is not snapshotted (dirty). Each snapshot is
    compared within its own scope (the paths watched when it was taken)."""
    if not snaps:
        return None
    manifests = _manifests_at([s["sha"] for s in snaps])
    entries = _watched_entries(project_root)
    live_all = None
    for i, s in enumerate(snaps):
        m = manifests.get(s["sha"]) or {}
        if not m:
            continue
        current = [ap for entry, ap in entries if _in_scope(entry, m)]
        if set(m.get("files", {}).values()) != set(current):
            continue
        snap = snapshot_tree(s["sha"], m)
        if live_all is None:
            live_all = live_tree([ap for _, ap in entries], _blob_algo(snap))
            if live_all is None:
                return None
        live = {fp: blob for fp, blob in live_all.items()
                if any(_inside(fp, ap) for ap in current)}
        if live == snap:
            return i
    return None


def _subtree(tree: dict[str, str], ap: str) -> dict[str, str]:
    return {k: v for k, v in tree.items() if _inside(k, ap)}


def _live_matches(ap: str, snap_tree: dict[str, str], algo: str) -> bool:
    """True when the file(s) at ap (a file, a dir, or a link to either)
    are byte-identical to what the snapshot recorded there."""
    return live_tree([ap], algo) == _subtree(snap_tree, ap)


def _links_under(top: str) -> list[str]:
    """Every symlink (to a file or a dir) inside the directory top."""
    links = []
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not _ignored_dir(root, d)]
        for name in dirs + files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                links.append(full)
    return links


def _keep_links(live_dir: str, staged: str, snap_tree: dict[str, str],
                algo: str) -> list[str]:
    """Carry the symlinks of live_dir over into the staged restore copy.

    snap() mirrors the CONTENT behind a link, so the archive holds a real
    file or dir where the live tree has a link. Restoring that copy would
    materialise the link away (and a top-level write would go through
    it): instead the link itself is kept, and when what is behind it
    differs from the snapshot that is reported as skipped — sessionsteward
    never writes through a link. Links the snapshot knows nothing about
    stay in the old dir (which goes to trash). Returns the report lines.
    """
    lines = []
    for link in _links_under(live_dir):
        sub = os.path.relpath(link, live_dir).split(os.sep)
        target = os.path.join(staged, *sub)
        if not os.path.lexists(target):
            continue
        tmp_link = target + ".steward-link"
        try:
            os.symlink(os.readlink(link), tmp_link)
            _remove_mirror(target)
            os.rename(tmp_link, target)
        except OSError as e:  # e.g. no symlink privilege on Windows
            lines.append(f"skipped {link}: is a symlink and could not be "
                         f"kept ({e.strerror or e})")
            continue
        if not _live_matches(link, snap_tree, algo):
            lines.append(f"skipped {link}: is a symlink (left untouched, "
                         f"never written through)")
    return lines


def _swap_via_trash(ap: str, put_in_place, trash_dir: str,
                    report: list[str], trashed: list[str]) -> bool:
    """Replace the live path ap: move it to the trash, then call
    put_in_place() to create the new one. Never leaves a watched path empty:
    if the move fails nothing was touched, and if putting the new one in
    place fails the old one is moved back. True = ap now holds the new
    content; False = it holds what it held before (reported as skipped)."""
    dest = None
    if os.path.lexists(ap):
        try:
            dest = _move_to_trash(ap, trash_dir)
        except OSError as e:
            report.append(f"skipped {ap}: could not move it to the trash "
                          f"({e.strerror or e}) (left untouched)")
            return False
        report.append(f"moved {ap} -> {dest}")
        trashed.append(dest)
    try:
        put_in_place()
    except OSError as e:
        why = e.strerror or e
        if dest is None:
            report.append(f"skipped {ap}: {why} (left untouched)")
            return False
        try:
            shutil.move(dest, ap)
        except OSError:
            report.append(f"skipped {ap}: {why}; the previous version is "
                          f"in {dest}")
            return False
        report.remove(f"moved {ap} -> {dest}")
        trashed.remove(dest)
        report.append(f"skipped {ap}: {why} (left untouched)")
        return False
    return True


def restore(sha: str, trash_dir: str | None = None,
            project_root: str | None = None) -> list[str]:
    """Restore watched files from a snapshot, so that afterwards the
    watched files ARE the snapshot. Files that exist now but didn't in
    the snapshot are moved to trash instead of deleted — directories
    included (never `rmtree`'d). Paths already at the snapshot's state
    are left alone (reported as `unchanged …`); paths that were not on
    the watch list when the snapshot was taken are left alone too
    (`kept …`); symlinks are never written through (`skipped …`).

    Raises NothingToRestoreError for a manifest-less snapshot (e.g. the
    `init` commit), CrossProjectError when project_root is given and the
    snapshot belongs to a different project, and SnapshotDamagedError
    (before anything is touched) when the snapshot cannot supply what its
    manifest lists. Serialised with every other sessionsteward process.
    """
    with locked():
        return _restore(sha, trash_dir, project_root)


def _restore(sha: str, trash_dir: str | None,
             project_root: str | None) -> list[str]:
    manifest = check_restore(sha, project_root)
    _ensure_attributes()  # `git archive` below honours attributes
    restored = []
    trash_dir = trash_dir or TRASH_DIR
    trashed: list[str] = []   # what this restore put in the trash
    files = manifest["files"]
    root = _norm_root(project_root if project_root is not None
                      else manifest.get("project_root"))
    # Sweep what is on disk NOW: every watched path that has no place in
    # the target snapshot goes to trash. (It used to compare against this
    # project's NEWEST snapshot, so after `restore <older>` the extra
    # files survived, the worktree matched nothing, and the next `undo`
    # snapped that mess as pre-undo and stepped FORWARD onto the
    # clobbered snapshot — moving the user's file to trash on the way.)
    targets = set(files.values())
    for entry, ap in (_watched_entries(root) if root else []):
        if ap in targets or not os.path.lexists(ap):
            continue
        try:
            if _rel(ap, root) in files:
                continue
        except StewardError:
            continue  # never sweep a path that cannot even be mirrored
        if not _in_scope(entry, manifest):
            restored.append(f"kept {ap}: not watched when snapshot "
                            f"{sha[:7]} was taken")
            continue
        try:
            dest = _move_to_trash(ap, trash_dir)
        except OSError as e:
            restored.append(f"skipped {ap}: could not move it to the trash "
                            f"({e.strerror or e}) (left untouched)")
            continue
        restored.append(f"moved {ap} -> {dest}")
        trashed.append(dest)
    snap_tree = snapshot_tree(sha, manifest)
    algo = _blob_algo(snap_tree)
    modes = _tree_blobs(sha)   # git file mode of every file in the snapshot
    for rel, ap in files.items():
        data, is_dir = _read_snapshot_file(sha, rel)
        if os.path.islink(ap):
            # A top-level watched path that is a link: the snapshot holds
            # the content behind it, and writing it back would go THROUGH
            # the link into a file that was never watched as such.
            if _live_matches(ap, snap_tree, algo):
                restored.append(f"unchanged {ap}")
            else:
                restored.append(f"skipped {ap}: is a symlink (left "
                                f"untouched, never written through)")
            continue
        if is_dir:
            if os.path.isdir(ap) and _live_matches(ap, snap_tree, algo):
                restored.append(f"unchanged {ap}")
                continue
            # Extract next to the live dir and verify BEFORE touching it,
            # so a failed extract leaves the live dir as it was and the
            # final swap is a same-filesystem rename. tarfile, not `tar`:
            # works on Windows too.
            parent = os.path.dirname(ap)
            os.makedirs(parent, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".steward-restore-", dir=parent)
            try:
                src = os.path.join(tmp, "files", *rel.split("/"))
                arch = _run_git(REPO_DIR, ("archive", sha, f"files/{rel}"))
                try:
                    with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tf:
                        if hasattr(tarfile, "tar_filter"):
                            tf.extractall(tmp, filter="tar")
                        else:  # Python without extraction filters
                            tf.extractall(tmp)
                except (tarfile.TarError, OSError):
                    pass
                if arch.returncode != 0 or not os.path.isdir(src):
                    restored.append(f"skipped {ap}: could not extract "
                                    f"snapshot {sha[:7]} (left untouched)")
                    continue
                if os.path.isdir(ap):
                    restored.extend(_keep_links(ap, src, snap_tree, algo))
                # Never delete: the existing path (dir, file or link) goes
                # to trash before the snapshot copy takes its place - and
                # comes back if that fails.
                if _swap_via_trash(ap, lambda: os.rename(src, ap), trash_dir,
                                   restored, trashed):
                    restored.append(ap)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        elif data is not None:
            if os.path.lexists(ap) and not (os.path.isfile(ap)
                                            or os.path.isdir(ap)):
                # opening a named pipe for writing waits for a reader
                restored.append(f"skipped {ap}: is not a regular file (a "
                                f"named pipe, socket or device; left "
                                f"untouched)")
                continue
            if os.path.isfile(ap):
                try:
                    with open(ap, "rb") as f:
                        same = f.read() == data
                except OSError:
                    same = False
                if same:
                    restored.append(f"unchanged {ap}")
                    continue
            # One unwritable target (0444 / read-only attribute, or a dir
            # now sitting where the file was) must not abort the restore
            # half-way: report it and carry on with the rest.
            try:
                os.makedirs(os.path.dirname(ap), exist_ok=True)
                with open(ap, "wb") as f:
                    f.write(data)
            except OSError as e:
                restored.append(f"skipped {ap}: {e.strerror or e} "
                                f"(left untouched)")
                continue
            _set_executable(ap, modes.get(rel) == "100755")
            restored.append(ap)
        else:
            # Recorded in the manifest but nothing in the tree: a watched
            # directory that was empty or held only ignored files (git
            # tracks no empty directories). The live path must become an
            # empty directory too, or the snapshot could never be "the
            # current position" again. (It used to be skipped outright:
            # files added since stayed, and `undo` jumped forward.)
            if os.path.isdir(ap) and live_tree([ap], algo) == {}:
                restored.append(f"unchanged {ap}")
                continue
            if _swap_via_trash(ap, lambda: os.makedirs(ap), trash_dir,
                               restored, trashed):
                restored.append(ap)
    if trashed:
        prune_trash(trash_dir, protect=trashed)
    return restored


def skipped(restored: list[str]) -> list[str]:
    """The 'skipped ...' lines of a restore() result (nothing was written
    for those paths; the CLI exits 1 when any exist)."""
    return [r for r in restored if r.startswith("skipped ")]


def _sizes(sha: str) -> dict[str, int]:
    """{path under files/: blob size} at sha, limited to the paths the
    snapshot's own manifest records (the repo is shared between projects,
    and older repos kept stale mirrors in files/)."""
    rels = list(_manifest_at(sha).get("files", {}))
    if not rels:
        return {}
    out = {}
    for line in _git(REPO_DIR, "ls-tree", "-r", "-l", "-z", sha, "--",
                     *(f"files/{rel}" for rel in rels)).split("\0"):
        meta, _, path = line.partition("\t")
        size = (meta.split() or [""])[-1]
        if size.isdigit():
            out[path[len("files/"):]] = int(size)
    return out


def shrunk(sha: str, prev_sha: str, ratio: float = 0.5) -> list[tuple]:
    """Guard: files that lost more than `ratio` of their size between two
    snapshots — the "compaction ate my CLAUDE.md" case — and watched files
    that disappeared altogether (new_size None).
    Returns [(rel, old_size, new_size)]."""
    old, new = _sizes(prev_sha), _sizes(sha)
    hits = [(rel, old[rel], new[rel]) for rel in sorted(new)
            if rel in old and new[rel] < old[rel] * (1 - ratio)]
    hits += [(rel, old[rel], None) for rel in sorted(old) if rel not in new]
    return hits


def guard_lines(sha: str, prev_sha: str, hint: str | None = "undo") -> list[str]:
    """Shrink-guard report lines for snapshot sha against prev_sha.

    hint: "undo" when `sessionsteward undo` would bring the file back
    (the live files are at sha), "restore" to name the snapshot that has
    the previous version, None for a purely historical entry.
    """
    if hint == "undo":
        tail = " — `sessionsteward undo` to recover"
    elif hint == "restore":
        tail = f" — `sessionsteward restore {prev_sha[:7]}` has the previous version"
    else:
        tail = ""
    lines = []
    for rel, o, n in shrunk(sha, prev_sha):
        name = display_rel(rel)
        if n is None:
            lines.append(f"  ! guard: {name} deleted ({o} bytes){tail}")
        else:
            lines.append(f"  ! guard: {name} shrank {o} -> {n} bytes "
                         f"(-{100 - round(n * 100 / o)}%){tail}")
    return lines


def diff(sha1: str, sha2: str) -> str:
    # Plain unified diff whatever your git config says: no colour (escape
    # codes hid every line from the +/- filter), no external diff tool or
    # textconv filter (their output is not a diff), and no rename detection
    # (a renamed file would show as "similarity index 100%" and no lines).
    return _git(REPO_DIR, "diff", "--no-color", "--no-ext-diff",
                "--no-textconv", "--no-renames", f"{sha1}..{sha2}", "--",
                "files/")


def file_mtimes(paths: list[str]) -> dict[str, float]:
    mtimes = {}
    for fp in iter_files(paths):
        try:
            mtimes[fp] = os.path.getmtime(fp)
        except OSError:
            pass
    return mtimes


# ---------------------------------------------------------------------------
# Claude Code hook wiring (`install-hooks`)
# ---------------------------------------------------------------------------

def _self_cmd() -> str:
    """Absolute, quoted `python -m sessionsteward` for hook shells.

    Hook shells don't load your aliases or activate your venv, so a bare
    `sessionsteward` fails silently there. Windows paths with spaces need
    double quotes (cmd and Git Bash both accept them); POSIX gets shlex.
    """
    exe = sys.executable
    exe = f'"{exe}"' if os.name == "nt" else shlex.quote(exe)
    return f"{exe} -m sessionsteward"


HOOK_PRECOMPACT_CMD = f"{_self_cmd()} precompact"
HOOK_SESSIONSTART_CMD = f'{_self_cmd()} snap -m "session start"'
# The arguments each event's hook has when sessionsteward wrote it.
_HOOK_ARGS = {"PreCompact": ["precompact"],
              "SessionStart": ["snap", "-m", "session start"]}


def _is_ours(event: str, cmd) -> bool:
    """Did sessionsteward write this hook command for this event? Exactly
    that, and nothing that merely mentions it: the bare `sessionsteward ...`
    the first version wrote, or `<any python> -m sessionsteward ...` (ours
    from another, moved or recreated, environment). merge_hooks rewrites
    these in place rather than appending a second, stale-plus-fresh pair;
    every other hook is the user's - `cd ~/code/sessionsteward && git pull`,
    `sessionsteward snap -m checkpoint` - and is never touched."""
    if not isinstance(cmd, str):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    args = _HOOK_ARGS[event]
    return (words == ["sessionsteward", *args]
            or (len(words) == len(args) + 3
                and words[1:3] == ["-m", "sessionsteward"]
                and words[3:] == args))


def build_hooks_json() -> dict:
    """The hooks payload for Claude Code's settings.json `hooks` section."""
    return {
        "hooks": {
            "PreCompact": [
                {"matcher": "",
                 "hooks": [{"type": "command",
                            "command": HOOK_PRECOMPACT_CMD}]}
            ],
            "SessionStart": [
                {"matcher": "",
                 "hooks": [{"type": "command",
                            "command": HOOK_SESSIONSTART_CMD}]}
            ],
        }
    }


def _hook_commands(entries: list) -> list[str]:
    """Extract command strings from hook entries, defensively."""
    cmds = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        for h in e.get("hooks", []) or []:
            if isinstance(h, dict) and "command" in h:
                cmds.append(h["command"])
    return cmds


def merge_hooks(settings: dict) -> dict:
    """Merge our hooks into a settings dict without clobbering anything.

    Existing keys, events and other tools' hook entries are preserved.
    Per event, the first existing hook that sessionsteward itself wrote
    (see _is_ours: from any interpreter path, or an old bare command) is
    rewritten to the current command and further copies are dropped; our
    entry is appended only when none exists, and a hook of the user's own is
    never changed, whatever it says. Re-running is idempotent. Raises
    SettingsError for a `hooks` value of an unexpected shape (it cannot be
    merged into without replacing something the user wrote).
    """
    ours = build_hooks_json()["hooks"]
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = settings["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise SettingsError('"hooks" in the settings file is not an object')
    for event, entries in ours.items():
        existing = hooks.get(event)
        if existing is None:
            existing = hooks[event] = []
        elif not isinstance(existing, list):
            raise SettingsError(f'"hooks.{event}" in the settings file is '
                                f'not a list')
        cmd = entries[0]["hooks"][0]["command"]
        found = False
        for e in list(existing):
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            kept = []
            for h in e["hooks"]:
                if isinstance(h, dict) and _is_ours(event, h.get("command")):
                    if found:
                        continue  # stale duplicate
                    h["command"] = cmd
                    found = True
                kept.append(h)
            if kept:
                e["hooks"] = kept
            elif e["hooks"]:
                existing.remove(e)  # we emptied it
        if not found:
            existing.append(entries[0])
    return settings


def _parse_settings(raw: bytes | None, path: str) -> dict:
    """The settings dict in raw (None / blank = no settings yet). Anything
    that is not a strict-JSON object - a comment, a trailing comma, broken
    UTF-8, a truncated file - raises SettingsError: guessing would mean
    rewriting the file without the settings we could not read."""
    if raw is None:
        return {}
    try:
        text = raw.decode("utf-8-sig")   # tolerates the BOM Windows tools add
    except UnicodeDecodeError as e:
        raise SettingsError(f"{path} is not valid UTF-8 ({e.reason} at byte "
                            f"{e.start}); it was not modified") from None
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except ValueError as e:
        raise SettingsError(f"{path} is not strict JSON ({e}) - comments "
                            f"and trailing commas are not allowed; it was "
                            f"not modified") from None
    if not isinstance(settings, dict):
        raise SettingsError(f"{path} does not hold a JSON object; it was "
                            f"not modified")
    return settings


def write_hooks_file(settings_path: str) -> str:
    """Merge our hooks into the Claude Code settings file at settings_path.
    Returns settings_path.

    The user's settings are never lost: a file that cannot be read as a
    strict-JSON object, or whose `hooks` have an unexpected shape, is
    refused with SettingsError and left exactly as it was. Writing is
    atomic (a temp file beside it, then replace), keeps the previous
    content as <name>.bak, writes through a symlinked settings.json (dotfile
    managers) instead of replacing the link, and keeps the file's line
    endings and indentation. When the hooks are already installed nothing
    is written at all.
    """
    real = os.path.realpath(settings_path)
    try:
        with open(real, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = None
    except OSError as e:
        raise SettingsError(f"cannot read {settings_path}: "
                            f"{e.strerror or e}") from None
    settings = _parse_settings(raw, settings_path)
    before = json.dumps(settings, sort_keys=True)
    try:
        merge_hooks(settings)
    except SettingsError as e:
        raise SettingsError(f"{settings_path}: {e}; it was not modified") \
            from None
    if raw is not None and json.dumps(settings, sort_keys=True) == before:
        return settings_path
    text = (raw or b"").decode("utf-8-sig")
    indent = re.search(r"^([ \t]+)\S", text, re.M)
    newline = "\r\n" if b"\r\n" in (raw or b"") else "\n"
    data = (json.dumps(settings, indent=indent.group(1) if indent else 2,
                       ensure_ascii=False) + "\n"
            ).replace("\n", newline).encode("utf-8")
    tmp = f"{real}.steward-{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
        if raw is not None and raw.strip():
            with open(real + ".bak", "wb") as f:
                f.write(raw)
        with open(tmp, "wb") as f:
            f.write(data)
        if raw is not None:
            try:
                shutil.copymode(real, tmp)
            except OSError:
                pass
        os.replace(tmp, real)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise SettingsError(f"could not write {settings_path}: "
                            f"{e.strerror or e}") from None
    return settings_path
