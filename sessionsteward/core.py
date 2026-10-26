"""Snapshot backend: git-versions watched files. Every snap is a commit.

Layout inside the snapshots repo:
  files/<rel>          mirrored watched files (only this snapshot's project)
  manifest.json        {"project_root": ..., "files": {rel: abspath},
                        "kinds": {rel: "file" | "dir"},
                        "modes": {path under files/: "0600"},   (POSIX; only
                                  paths with narrower group/other bits than
                                  git's own 0644 / 0755; absent if none)
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
import sysconfig
import tarfile
import tempfile
import threading
import time
import unicodedata

from . import secretscan

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


def _is_transcript(path: str) -> bool:
    """A Claude Code session transcript: <...>/projects/<project>/<id>.jsonl.
    These grow without limit and are never your instructions, so a watched
    folder that happens to hold them (say `~/.claude/projects`) leaves them
    out. Only this exact shape: a .jsonl anywhere else is yours."""
    if not path.endswith(".jsonl"):
        return False
    grand = os.path.dirname(os.path.dirname(path))
    return os.path.basename(grand) == "projects"


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
    no limit, and so does a number so large that its byte count is more
    than a float can hold (1e305); anything that is not a finite number >= 0
    is ignored."""
    raw = os.environ.get("SESSIONSTEWARD_MAX_FILE_MB")
    try:
        megabytes = float(raw) if raw and raw.strip() else MAX_FILE_MB
    except ValueError:
        megabytes = MAX_FILE_MB
    if not 0 <= megabytes < float("inf"):   # also false for nan
        megabytes = MAX_FILE_MB
    try:
        return int(megabytes * 1024 * 1024) or None
    except OverflowError:   # the product is inf: bigger than any file
        return None


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


#: A hook is killed by Claude Code after HOOK_TIMEOUT_SECONDS; waiting for
#: the lock that long would be killed mid-wait and say nothing. In a hook the
#: wait is cut to this, and the skip is logged (see log_skip).
HOOK_LOCK_WAIT = 10.0
_hook = {"on": False}


def hook_mode(on: bool = True) -> None:
    """Called by the commands Claude Code runs as hooks: shorter lock wait,
    and a lock that stays busy is written to hook.log."""
    _hook["on"] = on


def hook_log_path() -> str:
    return os.path.join(os.path.dirname(REPO_DIR) or ".", "hook.log")


def log_skip(reason: str) -> None:
    """One line in ~/.sessionsteward/hook.log (kept under 64 KB) saying a
    hook did nothing and why. Never raises."""
    path = hook_log_path()
    line = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + " "
            + " ".join(str(reason).split())[:300] + "\n").encode("utf-8")
    try:
        _private_dir(os.path.dirname(path))
        try:
            if os.path.getsize(path) > 64 * 1024:
                with open(path, "rb") as f:
                    f.seek(-32 * 1024, os.SEEK_END)
                    tail = f.read()
                tail = tail[tail.find(b"\n") + 1:]
                with open(path, "wb") as f:
                    f.write(tail)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _lock_timeout() -> float:
    raw = os.environ.get("SESSIONSTEWARD_LOCK_TIMEOUT")
    try:
        value = float(raw) if raw else LOCK_TIMEOUT
    except ValueError:
        value = LOCK_TIMEOUT
    if not (value == value and value >= 0):
        value = LOCK_TIMEOUT
    return min(value, HOOK_LOCK_WAIT) if _hook["on"] else value


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
                if _hook["on"]:
                    log_skip(f"skipped: another sessionsteward held the lock "
                             f"for {wait:g}s")
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


def _run_git(repo: str, args: tuple[str, ...], env_extra: dict | None = None,
             **kw) -> subprocess.CompletedProcess:
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
        env = _git_env()
        env.update(env_extra or {})
        return subprocess.run([*GIT, *where, *args], capture_output=True,
                              timeout=60, env=env, **kw)
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


def _config_text(raw: bytes) -> str:
    """The text of a config file: UTF-8, with or without the byte-order mark
    that PowerShell 5 (Set-Content -Encoding utf8) and Notepad write. UTF-16
    - what PowerShell 5's `>` and Out-File write - is not read, but said so
    in words that name the cure."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw:
        raise StewardError(
            f"watch config {CONFIG_PATH} is UTF-16 (what PowerShell 5's > "
            f"writes), not UTF-8; save it as UTF-8 (Set-Content -Encoding "
            f"utf8) or delete it (it was left as is)")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise StewardError(
            f"watch config {CONFIG_PATH} is not valid UTF-8 ({e.reason} at "
            f"byte {e.start}); fix it or delete it (it was left as is)"
        ) from None


def load_config() -> dict:
    """The watch config. A missing file means defaults; a corrupt one - not
    JSON, not UTF-8, or not the shape `watch` and `unwatch` need (lists of
    paths) - is an error (and is left in place): silently replacing it would
    throw away the user's watch list, and guessing at it would watch
    something else."""
    try:
        with open(CONFIG_PATH, "rb") as f:
            raw = f.read()
    except OSError:
        return {"watch": []}
    text = _config_text(raw)
    try:
        cfg = json.loads(text)
    except ValueError as e:
        raise StewardError(
            f"watch config {CONFIG_PATH} is not valid JSON ({e}); fix it "
            f"or delete it to start from the defaults (it was left as is)")
    if not isinstance(cfg, dict):
        raise StewardError(f"watch config {CONFIG_PATH} must be a JSON "
                           f"object (it was left as is)")
    for key in ("watch", "unwatch"):
        value = cfg.get(key)
        if value is None:
            cfg.pop(key, None)   # null is the same as not set
        elif not (isinstance(value, list)
                  and all(isinstance(item, str) for item in value)):
            raise StewardError(
                f'watch config {CONFIG_PATH}: "{key}" must be a list of '
                f'paths such as ["NOTES.md"]; fix it or delete it (it was '
                f'left as is)')
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


def _spellings(candidates: list[str], root: str) -> list[str]:
    """Every way the same folders may be written. git reports a real path
    (macOS /private/var/..., Windows long names) while Claude Code named the
    project folder after the path it was started in (/var/..., 8.3 short
    names). For each candidate: its real path, and the parent of `root` (as
    given) that is the same folder."""
    out: list[str] = []

    def add(p):
        if p and p not in out:
            out.append(p)
    parents = []
    cur = root
    while True:
        parents.append(cur)
        up = os.path.dirname(cur)
        if up == cur:
            break
        cur = up
    for cand in candidates:
        add(cand)
        try:
            real = os.path.realpath(cand)
        except (OSError, ValueError):
            continue
        add(real)
        for anc in parents:
            try:
                if os.path.realpath(anc) == real:
                    add(anc)
            except (OSError, ValueError):
                pass
    return out


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
    candidates = _spellings(candidates, root)
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
    except (OSError, ValueError):   # ValueError: a NUL in a name
        return False


def _real(path: str) -> str:
    """path with its links followed - or as given when the system cannot
    look it up (a NUL in a name makes realpath raise ValueError)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _same_dir(a: str, b: str) -> bool:
    """Do these two paths name one directory entry - by spelling (Windows
    ignores case), or by what the file system says (macOS ignores it too)?"""
    return os.path.normcase(a) == os.path.normcase(b) or _same_entry(a, b)


def unwatchable_reason(abspath: str,
                       project_root: str) -> tuple[str, str] | None:
    """Why this path can never be a watch-list entry, as (what it is, what to
    do instead) - or None when it can. Four paths cannot be mirrored: the
    project root and your home directory (everything in them would be
    snapshotted, the snapshots repo included), the root of a drive, and the
    snapshots repo itself or a folder that holds it (it would end up as a
    mirror of its own mirror). Naming one made every later snap, undo and
    restore fail, and with them the hooks. A link to one of them is that path
    for a walk, so the path is looked at twice: as given, and with its links
    followed."""
    root = _norm_root(project_root)
    home = os.path.abspath(os.path.expanduser("~"))
    inside_it = "name the files or folders in it that matter"
    repo = os.path.abspath(REPO_DIR)
    stores = [os.path.normcase(p)
              for p in dict.fromkeys((repo, _real(repo)))]
    for path in dict.fromkeys((abspath, _real(abspath))):
        if _same_dir(path, root):
            return ("the project root itself",
                    f"{inside_it}, for example `watch --add docs/plan.md`")
        if _same_dir(path, home):
            return ("your home directory itself",
                    f"{inside_it}, for example `watch --add "
                    f"~/.claude/settings.json`")
        if os.path.dirname(path) == path:
            return ("the root of a drive",
                    f"{inside_it}, for example `watch --add docs/plan.md`")
        here = os.path.normcase(path)
        if any(_inside(store, here) or _inside(here, store)
               for store in stores):
            return ("where sessionsteward keeps its snapshots",
                    "that folder is not something to protect")
    return None


def check_watch_additions(entries, project_root: str) -> None:
    """Refuse (StewardError, nothing changed) an entry for `watch --add` that
    unwatchable_reason() rules out, saying what to name instead."""
    root = _norm_root(project_root)
    for entry in entries:
        path = _expand_watch_entry(entry, root)
        bad = unwatchable_reason(os.path.abspath(path), root) if path else None
        if bad:
            raise StewardError(f"cannot watch '{entry}': it is {bad[0]}; "
                               f"{bad[1]}")


def unwatchable_entries(project_root: str) -> list[tuple[str, str]]:
    """[(entry, what it is)] for the entries of the watch list that
    unwatchable_reason() rules out - from a config.json written by a version
    that accepted them, or by hand. They are left out of every snapshot (the
    others are still taken) and the commands say so."""
    root = _norm_root(project_root)
    out = []
    for entry in watch_list():
        path = (None if entry == AUTO_MEMORY
                else _expand_watch_entry(entry, root))
        bad = unwatchable_reason(os.path.abspath(path), root) if path else None
        if bad:
            out.append((entry, bad[0]))
    return out


def shell_word(text: str) -> str:
    """text as one shell word that can be pasted back: a bare ~ would be
    replaced by the shell with the path of your home directory."""
    if os.name == "nt":
        return text if re.fullmatch(r"[\w./\\:-]+", text) else f'"{text}"'
    return shlex.quote(text)


def _fold(name: str) -> str:
    """A name as a case-insensitive file system compares it: composed
    Unicode, case folded."""
    return unicodedata.normalize("NFC", name).casefold()


def _names_in(directory: str, cache: dict | None) -> list[str]:
    """The names a directory lists, sorted (none when it cannot be read);
    remembered in `cache` for as long as the caller keeps it."""
    if cache is not None and directory in cache:
        return cache[directory]
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    if cache is not None:
        cache[directory] = names
    return names


def _on_disk_spelling(path: str, project_root: str | None,
                      listings: dict | None = None) -> str:
    """`path` with every name below its anchor spelled the way the file
    system spells it.

    A case-insensitive file system (Windows, macOS) finds `claude.md` when
    the file is called `CLAUDE.md`, so a watch-list entry typed one way stays
    valid for the other. A snapshot must not carry that difference: its
    mirror and git's tree know a file under one name only, and an entry that
    spells a file differently from the walk of a watched folder around it
    met the same file under two names - the snapshot was refused for good,
    or (a folder entry nested in another) restored as an empty folder. So
    each name below the anchor - the project root when the path is inside it,
    else your home directory, else the drive - is replaced by the name the
    folder lists for it. The match is the file system's own: the same name
    once Unicode is composed and case ignored, and the two paths confirmed to
    be one directory entry. The anchor is left as given (it is how the
    project is identified); a name that does not exist, and what follows it,
    stays as typed. On a case-sensitive file system nothing changes.
    `listings` caches folder listings for the length of one pass."""
    path = os.path.abspath(path)
    anchor = None
    for base in (project_root, os.path.abspath(os.path.expanduser("~"))):
        if base and _inside(path, base) and (anchor is None
                                             or len(base) > len(anchor)):
            anchor = base
    if anchor is None:
        drive, tail = os.path.splitdrive(path)
        anchor = drive + tail[:1]
    parts = [p for p in path[len(anchor):].split(os.sep) if p]
    cur = anchor
    for i, part in enumerate(parts):
        here = os.path.join(cur, part)
        if not os.path.lexists(here):
            return os.path.join(cur, *parts[i:])
        names = _names_in(cur, listings)
        if part not in names:
            want = _fold(part)
            for name in names:
                if (_fold(name) == want
                        and _same_entry(os.path.join(cur, name), here)):
                    part = name
                    break
        cur = os.path.join(cur, part)
    return cur


def _on_disk_files(manifest: dict, project_root: str | None,
                   listings: dict | None = None) -> dict:
    """The manifest's {rel: abspath} with each abspath spelled as the file
    system spells it now. A snapshot taken by an older version holds the
    spelling the watch list used; compared with the live paths (which are
    spelled as on disk) it would look like another file."""
    files = manifest.get("files") or {}
    return {rel: (_on_disk_spelling(ap, project_root, listings)
                  if isinstance(ap, str) else ap)
            for rel, ap in files.items()}


def _watched_entries(project_root: str) -> list[tuple[str, str]]:
    """[(watch-list entry, absolute path)] for the watched paths that
    exist now, deduped by path (the first entry naming a path wins).
    Entries that unwatchable_reason() rules out are not among them.

    Each path is spelled as the file system spells it (_on_disk_spelling):
    `.claude/rules/style.md` for the file `Style.md` comes out as
    `.../Style.md`, the name the walk of a watched folder meets, and is
    recorded under that name.

    So are two spellings that differ only by case and name one file: on a
    case-insensitive file system `claude.md` next to the default `CLAUDE.md`
    is the same path, and mirrored twice the second spelling would land on
    the first one's file - git records a single name and the completeness
    check would refuse every snapshot. (On a case-sensitive one two files
    that merely differ by case are two paths, and both are kept.)"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    by_fold: dict[str, list[str]] = {}
    listings: dict[str, list[str]] = {}
    root = _norm_root(project_root)
    for rel in watch_list():
        p = _expand_watch_entry(rel, project_root)
        # A regular file or a directory (or a link to one). A named pipe,
        # socket or device cannot be snapshotted - reading a pipe blocks
        # for ever - so it is as good as absent.
        if p and (os.path.isfile(p) or os.path.isdir(p)):
            ap = os.path.abspath(p)
            if unwatchable_reason(ap, root):
                continue   # unwatchable_entries() is how the user hears of it
            ap = _on_disk_spelling(ap, root, listings)
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
                           f"or your home directory itself (drop the entry "
                           f"with `sessionsteward watch --remove <entry>`)")
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


# ---------------------------------------------------------------------------
# Permission bits
# ---------------------------------------------------------------------------
# git records one bit of a file's mode (the owner's execute bit), and the tar
# a restore extracts holds 0644 and 0755 - so a restored 0600 secret or 0700
# folder would come back readable by everybody. The group and other bits of
# a path are therefore recorded in the manifest wherever they are narrower
# than that default, and a restore gives a path those bits, minus any the live
# path has lost since: never more open than the snapshot recorded or than the
# path is now. Bits wider than the default (a group-writable file) are not
# carried over; the owner's bits are not recorded (a restore gives a folder or
# an executable rwx and any other file rw). POSIX only: Windows has no such
# bits.

def _default_go(mode: int) -> int:
    """The group and other bits git's own modes give a path of this kind:
    0755 for a folder or anything the owner can execute, else 0644."""
    return 0o055 if stat.S_ISDIR(mode) or mode & stat.S_IXUSR else 0o044


def _recorded_modes(plan: list) -> dict[str, str]:
    """{path under files/: "0600"} for every mirrored path (each watched path
    and everything the mirror holds under a watched folder) whose group or
    other bits are narrower than _default_go() - read from the mirror, which
    keeps the permission bits of what it copies. Sorted, so identical
    permissions make an identical manifest."""
    if os.name == "nt":
        return {}
    found: dict[str, str] = {}

    def note(path: str, key: str) -> None:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            return
        if not stat.S_ISLNK(mode) and _default_go(mode) & ~mode & 0o077:
            found[key] = "%04o" % stat.S_IMODE(mode)

    for rel, _ap, dest in plan:
        note(dest, rel)
        if os.path.isdir(dest) and not os.path.islink(dest):
            for root, dirs, files in os.walk(dest):
                dirs.sort()
                here = os.path.relpath(root, dest).replace(os.sep, "/")
                prefix = rel if here == "." else f"{rel}/{here}"
                if here != ".":
                    note(root, prefix)
                for name in sorted(files):
                    note(os.path.join(root, name), f"{prefix}/{name}")
    return dict(sorted(found.items()))


def _manifest_modes(manifest: dict) -> dict[str, int]:
    """The group and other bits a manifest records, {path under files/: bits}.
    An entry that is not an octal mode string (a hand-edited manifest) is
    dropped, not an error."""
    raw = manifest.get("modes")
    if os.name == "nt" or not isinstance(raw, dict):
        return {}
    return {key: int(text, 8) & 0o077 for key, text in raw.items()
            if isinstance(key, str) and isinstance(text, str)
            and re.fullmatch(r"[0-7]{3,4}", text)}


def _apply_modes(staged: str, live: str, rel: str,
                 recorded: dict[str, int]) -> None:
    """Set the permission bits of the extracted copy of a watched folder
    (`staged`, about to replace `live`; `rel` is its path under files/):
    owner rwx on a folder or an executable, rw on any other file; group and
    other get what the snapshot recorded for the path (else git's default),
    minus whatever the live path of that name does not have. Links kept from
    the live folder are left alone."""
    if os.name == "nt":
        return

    def fix(path: str, sub: list[str]) -> None:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            return
        if stat.S_ISLNK(mode):
            return
        default = _default_go(mode)
        want = recorded.get("/".join([rel, *sub]), default) & default
        try:
            seen = os.lstat(os.path.join(live, *sub)).st_mode
            have = 0o077 if stat.S_ISLNK(seen) else seen & 0o077
        except OSError:
            have = 0o077   # nothing lives there now: nothing to exceed
        new = (0o700 if default == 0o055 else 0o600) | (want & have)
        if new != stat.S_IMODE(mode):
            with contextlib.suppress(OSError):
                os.chmod(path, new)

    fix(staged, [])
    for root, dirs, files in os.walk(staged):
        here = [] if root == staged else os.path.relpath(
            root, staged).split(os.sep)
        for name in dirs + files:
            fix(os.path.join(root, name), here + [name])


def _narrow_to_recorded(path: str, rel: str, recorded: dict[str, int]) -> None:
    """After a file was written (or an empty folder made) in place: take away
    the group and other bits the snapshot recorded as absent for `rel`. Never
    adds one, and leaves a path the snapshot recorded nothing for as it is."""
    if os.name == "nt" or rel not in recorded:
        return
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        new = (mode & ~0o077) | (mode & recorded[rel])
        if new != mode:
            os.chmod(path, new)
    except OSError:
        pass


def _check_staged(files_dir: str, kinds: dict[str, str],
                  entries: dict[str, str] | None = None) -> None:
    """Refuse to commit a snapshot that lacks something it mirrored: every
    file under files/ (and every watched file) must be in the index, under
    the name the mirror gives it. `entries` maps a rel to the watch-list
    entry it came from, so the refusal can name the entry to drop.

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
        first = missing[0]
        entry = (entries or {}).get(first[len("files/"):])
        remedy = ""
        if entry is not None:
            remedy = (f"; the watch-list entry '{entry}' names it, and "
                      f"`sessionsteward watch --remove {shell_word(entry)}` "
                      f"drops that entry")
        raise StewardError(
            f"snapshot incomplete: git did not record {len(missing)} of the "
            f"mirrored file(s) (first: {first}) - nothing was committed"
            f"{remedy}")


def snap(project_root: str, message: str) -> str:
    """Copy watched files into the repo and commit. Returns commit sha.
    Serialised with every other sessionsteward process (see locked())."""
    with locked():
        return _snap(project_root, message)


def snap_report(project_root: str, message: str,
                skipped: dict | None = None) -> tuple[str, list]:
    """snap(), plus the files it left out for being over max_file_bytes():
    (sha, [(path, size), ...]) in path order. ``skipped``, when given, is
    filled with the other things a snapshot silently leaves behind:
    skipped["outside"] = links that lead outside a watched folder,
    skipped["deep"] = folders below MAX_WALK_DEPTH levels (sorted paths)."""
    too_big: list = []
    with locked():
        sha = _snap(project_root, message, too_big, skipped)
    return sha, sorted(set(too_big))


def _snap(project_root: str, message: str, too_big: list | None = None,
          skipped: dict | None = None) -> str:
    if not repo_ready():
        init_repo()
    _ensure_attributes()
    files_dir = os.path.join(REPO_DIR, "files")
    # Plan every mirror path first: nothing is removed or written until
    # all of them are known to sit inside files/.
    plan = []
    entry_of = {}   # rel -> the watch-list entry it came from
    for entry, ap in _watched_entries(project_root):
        rel = _rel(ap, project_root)
        plan.append((rel, ap, _mirror_path(files_dir, rel)))
        entry_of.setdefault(rel, entry)
    # A watched FILE that looks like it holds a secret is not copied (a
    # file inside a watched folder is left out by the walk below). It is
    # also not in the manifest, so a restore never touches it.
    if skipped is None:
        skipped = {}
    withheld: list[str] = []
    if secretscan.enabled():
        for item in list(plan):
            rel, ap, _dest = item
            if os.path.isfile(ap) and secretscan.file_kinds(ap):
                plan.remove(item)
                entry_of.pop(rel, None)
                skipped.setdefault("secret", []).append(ap)
                withheld.append(rel)
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
    budget = _Budget()
    for rel, ap, dest in plan:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # _copy_content, not copy2/copytree's default: the mirror must never
        # inherit read-only modes, or the next snap cannot replace it.
        # (dirs_exist_ok / the lexists check: a watched file inside a
        # watched dir, e.g. ~/.claude/CLAUDE.md with ~ as the project.)
        if os.path.isdir(ap):
            # Symlinks are followed within the limits of _walk_files: the
            # mirror holds the content behind them (restore() never writes
            # it back through the link), but never loops, never leaves the
            # watched folder unless asked, and stops at the snapshot cap.
            def mirror_dir(d, ap=ap, dest=dest):
                t = os.path.join(dest, os.path.relpath(d, ap))
                os.makedirs(t, exist_ok=True)
                if os.name != "nt":   # keep the folder's mode (owner can write)
                    try:
                        os.chmod(t, (os.stat(d).st_mode & 0o777) | 0o700)
                    except OSError:
                        pass
            for full, _size in _walk_files(ap, budget, too_big, mirror_dir,
                                           skipped):
                _copy_content(full, os.path.join(dest, os.path.relpath(full, ap)))
        elif not os.path.lexists(dest):
            _copy_content(ap, dest)
    for key in list(skipped):
        skipped[key] = sorted(set(skipped[key]))
    for rel, ap, _dest in plan:
        if kinds[rel] == "dir":
            prefix = ap.rstrip(os.sep) + os.sep
            withheld.extend(
                rel + "/" + path[len(prefix):].replace(os.sep, "/")
                for path in skipped.get("secret", [])
                if path.startswith(prefix))
    if too_big is not None:
        too_big.extend(budget.skipped)   # refused for the snapshot cap
    if too_big:
        # The limit is for what a walk of a watched folder finds. A file the
        # watch list names itself was copied above whatever its size - it is
        # in the snapshot, and `undo` brings it back - so it is not "left
        # out" (the walk skipped it first; that is all this list recorded).
        named = {os.path.normcase(ap) for rel, ap, _ in plan
                 if kinds[rel] == "file"}
        too_big[:] = [(path, size) for path, size in too_big
                      if os.path.normcase(path) not in named]
    # No timestamp in the manifest: identical content must produce an
    # identical tree so the re-snap below is skipped. (It used to carry
    # "at"; two hooks snapping the same clobbered state seconds apart
    # made two commits, and `undo` then stepped back to the clobbered one.)
    # "watch" records the effective watch list, so a restore knows which
    # paths were in this snapshot's scope (see _in_scope).
    document = {"project_root": _norm_root(project_root),
                "files": manifest, "kinds": kinds}
    modes = _recorded_modes(plan)
    if modes:
        document["modes"] = modes
    document["watch"] = watch_list()
    if withheld:
        document["withheld"] = sorted(set(withheld))
    with open(os.path.join(REPO_DIR, "manifest.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(document, f, indent=2)
    with open(os.path.join(REPO_DIR, "manifest.json"), "rb") as f:
        manifest_digest = _manifest_digest(f.read())
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
    _check_staged(files_dir, kinds, entry_of)
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
    new_sha = _git(REPO_DIR, "rev-parse", "HEAD").strip()
    _ledger_add(new_sha, manifest_digest)
    _maintain()
    return new_sha


#: The snapshot history is trimmed (and git's garbage collector run) at most
#: this often, when a snapshot is taken.
MAINTENANCE_EVERY = 24 * 3600
#: Without a saved policy (`prune --save`): keep this many snapshots of each
#: project, and everything younger than this many days.
AUTO_KEEP = 200
AUTO_DAYS = 90


def _auto_prune_on() -> bool:
    env = os.environ.get("SESSIONSTEWARD_AUTO_PRUNE", "").strip().lower()
    if env:
        return env not in ("0", "off", "false", "no")
    try:
        return load_config().get("auto_prune", True) is not False
    except StewardError:
        return True


def _maintain(now: float | None = None) -> None:
    """Once a day, after a snapshot: drop snapshots beyond the policy and run
    `git gc --auto`. Best effort - a failure here never fails the snapshot.
    Turn it off with SESSIONSTEWARD_AUTO_PRUNE=0 or "auto_prune": false in
    config.json."""
    stamp = os.path.join(os.path.dirname(REPO_DIR) or ".", "maintenance.stamp")
    now = time.time() if now is None else now
    try:
        if now - os.path.getmtime(stamp) < MAINTENANCE_EVERY:
            return
    except OSError:
        pass
    try:
        with open(stamp, "w", encoding="ascii") as f:
            f.write(str(int(now)))
    except OSError:
        return
    if not _auto_prune_on():
        return
    try:
        from . import retention     # (retention imports this module)
        saved = load_config().get("retention")
        keep, days = (retention.policy(None, None)
                      if isinstance(saved, dict) and saved
                      else (AUTO_KEEP, AUTO_DAYS))
        retention.prune(keep, days, apply=True, now=now)
        _run_git(REPO_DIR, ("-c", "gc.auto=6700", "gc", "--auto", "--quiet"))
    except (StewardError, OSError, ValueError):
        pass


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


def ledger_path() -> str:
    return os.path.join(os.path.dirname(REPO_DIR) or ".", "manifests.sha256")


def _manifest_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ledger_add(sha: str, digest: str) -> None:
    """Remember, outside the snapshots repository, the SHA-256 of the
    manifest each snapshot was made with. Never raises: a ledger that cannot
    be written only means that snapshot is checked by git alone."""
    path = ledger_path()
    try:
        _private_dir(os.path.dirname(path))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{sha} {digest}\n".encode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        pass


def ledger() -> dict[str, str]:
    """{snapshot id: manifest sha256} as recorded when the snapshots were
    taken (the last entry for an id wins)."""
    out: dict[str, str] = {}
    try:
        with open(ledger_path(), encoding="ascii", errors="replace") as f:
            for line in f:
                parts = line.split()
                if (len(parts) == 2 and len(parts[1]) == 64
                        and re.fullmatch(r"[0-9a-f]{40,64}", parts[0])):
                    out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


def ledger_forget_except(keep: set[str]) -> None:
    """Rewrite the ledger with only the ids in `keep` (after a prune)."""
    kept = {k: v for k, v in ledger().items() if k in keep}
    path = ledger_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="ascii") as f:
            f.writelines(f"{k} {v}\n" for k, v in kept.items())
        os.replace(tmp, path)
    except OSError:
        pass


def ledger_remap(mapping: dict[str, str]) -> None:
    """After a prune rebuilt the history: the same manifests under their new
    ids."""
    old = ledger()
    new = {mapping[k]: v for k, v in old.items() if k in mapping}
    path = ledger_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="ascii") as f:
            f.writelines(f"{k} {v}\n" for k, v in new.items())
        os.replace(tmp, path)
    except OSError:
        pass


def _manifest_checksum_problem(sha: str) -> str | None:
    """Why the manifest of snapshot `sha` is not the one recorded when it was
    taken, or None (it is, or nothing was recorded - older snapshots)."""
    full = _git(REPO_DIR, "rev-parse", "--verify", "--quiet",
                f"{sha}^{{commit}}").strip()
    want = ledger().get(full)
    if want is None:
        return None
    p = _run_git(REPO_DIR, ("show", f"{full}:manifest.json"))
    got = _manifest_digest(p.stdout) if p.returncode == 0 else None
    if got != want:
        return ("its manifest does not match the SHA-256 recorded when the "
                "snapshot was taken (the snapshots repository was changed "
                "afterwards)")
    return None


def _destination_problems(manifest: dict, root: str | None) -> list[str]:
    """Every destination the manifest says to write to must be one of the
    paths on your CURRENT watch list (the watch list is the one place that
    says where sessionsteward may write), spelled the way it spells them. A
    manifest that names anything else - ~/.ssh/authorized_keys, say - is
    refused before a live file is touched."""
    files = manifest.get("files") or {}
    if not files:
        return []
    allowed = []
    for entry in watch_list():
        p = _expand_watch_entry(entry, root or manifest.get("project_root") or "")
        if p:
            allowed.append(_fold(os.path.abspath(p)))
    problems = []
    for rel, ap in files.items():
        if not isinstance(ap, str) or not os.path.isabs(ap):
            problems.append(f"{display_rel(str(rel))} has no absolute "
                            f"destination")
            continue
        here = _fold(os.path.abspath(ap))
        if not any(here == a or _inside(here, a) for a in allowed):
            problems.append(f"{ap} is not on your watch list")
            continue
        if root:
            try:
                back = _rel(os.path.abspath(ap), root)
            except StewardError:
                back = None
            if back is None or back.casefold() != str(rel).casefold():
                problems.append(f"{ap} does not belong to the name "
                                f"{display_rel(str(rel))} in the manifest")
    return problems


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
    _require_snapshot_id(sha)
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
    why = _manifest_checksum_problem(sha)
    if why:
        raise SnapshotDamagedError(
            f"refusing to restore snapshot {sha[:7]}: {why}. Nothing was "
            f"changed")
    bad = _destination_problems(manifest, project_root
                                or manifest.get("project_root"))
    if bad:
        raise SnapshotDamagedError(
            f"refusing to restore snapshot {sha[:7]}: it would write outside "
            f"your watch list - " + "; ".join(bad[:5])
            + ". Nothing was changed (`sessionsteward watch --add` a path "
            "to allow it)")
    problems = _snapshot_problems(sha, manifest)
    if problems:
        raise SnapshotDamagedError(
            f"refusing to restore snapshot {sha[:7]}: it is incomplete - "
            + "; ".join(problems) + ". Nothing was changed; "
            "`sessionsteward log` lists the other snapshots")
    return manifest


# ---------------------------------------------------------------------------
# Walking a watched directory (the one walk behind both snapshots and the
# live-tree comparison)
# ---------------------------------------------------------------------------

MAX_SNAPSHOT_MB = 500.0   # total bytes one snapshot may walk (see _Budget)
MAX_WALK_DEPTH = 64       # directory levels below a watched directory


def max_snapshot_bytes() -> int | None:
    """The most one snapshot copies from watched directories, or None for no
    cap. SESSIONSTEWARD_MAX_SNAPSHOT_MB overrides MAX_SNAPSHOT_MB; 0 = no cap;
    anything that is not a finite number >= 0 is ignored."""
    raw = os.environ.get("SESSIONSTEWARD_MAX_SNAPSHOT_MB")
    try:
        megabytes = float(raw) if raw and raw.strip() else MAX_SNAPSHOT_MB
    except ValueError:
        megabytes = MAX_SNAPSHOT_MB
    if not 0 <= megabytes < float("inf"):
        megabytes = MAX_SNAPSHOT_MB
    try:
        return int(megabytes * 1024 * 1024) or None
    except OverflowError:
        return None


def follow_external_links() -> bool:
    """May a walk of a watched directory follow a link that leads OUTSIDE it
    (`.claude/out -> ~/.ssh`)? No, unless you opted in: the target's files
    would be copied into the snapshot store. SESSIONSTEWARD_FOLLOW_LINKS=1 or
    `sessionsteward watch --follow-links` (config key follow_links) opts in.
    A watched entry that is itself a link is always followed."""
    env = os.environ.get("SESSIONSTEWARD_FOLLOW_LINKS", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    try:
        return load_config().get("follow_links") is True
    except StewardError:
        return False


class _Budget:
    """Total bytes one snapshot (or one comparison) may walk. Shared by every
    watched directory, so a tree that is huge only through links cannot fill
    the disk; files refused for it are listed in `skipped`."""

    def __init__(self):
        self.cap = max_snapshot_bytes()
        self.used = 0
        self.skipped: list = []

    def take(self, path: str, size: int) -> bool:
        if self.cap is not None and self.used + size > self.cap:
            self.skipped.append((path, size))
            return False
        self.used += size
        return True


def _inside(real: str, root_real: str) -> bool:
    a, b = os.path.normcase(real), os.path.normcase(root_real)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


_REPARSE_MOUNT_POINT = 0xA0000003   # IO_REPARSE_TAG_MOUNT_POINT: a junction


def _is_link(path: str) -> bool:
    """A symlink, or - on Windows - an NTFS junction. os.path.islink() is
    False for a junction (Python before 3.12 cannot see one, and 3.12+ has
    os.path.isjunction for it), but os.walk follows it like a link, so a
    junction pointing at a parent loops and one pointing at ~/.ssh copies
    your keys: both must be treated exactly like symlinks."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            if isjunction(path):
                return True
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        try:
            st = os.lstat(path)
        except (OSError, ValueError):
            return False
        return getattr(st, "st_reparse_tag", 0) == _REPARSE_MOUNT_POINT
    return False


def _walk_files(ap: str, budget: "_Budget", too_big: list | None = None,
                on_dir=None, skips: dict | None = None):
    """Yield (path, size) for every regular file under the watched directory
    ap, in a stable order: IGNORE_PATTERNS, virtual environments and build
    output left out; pipes, sockets, devices and dangling links skipped;
    files over max_file_bytes() left out (appended to too_big when given).

    Links to directories are walked (the mirror holds the content behind
    them) unless they would loop or repeat: a link to the watched
    directory, one of its ancestors or a directory already visited is never
    followed, depth is capped, and so is the total (budget). A link
    (file or directory) that leads outside ap is skipped unless
    follow_external_links(). on_dir(path), when given, is called for ap and
    each directory walked, before its files. skips, when given, collects
    what the walk left out that the user did not ask to have left out:
    skips["outside"] (links leading outside ap), skips["deep"]
    (directories below MAX_WALK_DEPTH levels) and skips["secret"] (files
    that look like they hold a secret: see secretscan), as lists of paths."""
    limit = max_file_bytes()
    root_real = os.path.realpath(ap)
    outside_ok = follow_external_links()
    seen = {os.path.normcase(root_real)}
    scan_secrets = secretscan.enabled()
    base_depth = ap.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(ap, followlinks=True):
        if on_dir is not None:
            on_dir(root)
        keep = []
        too_deep = root.rstrip(os.sep).count(os.sep) - base_depth >= MAX_WALK_DEPTH
        for d in sorted(dirs):
            if _ignored_dir(root, d):
                continue
            full = os.path.join(root, d)
            if too_deep:
                if skips is not None:
                    skips.setdefault("deep", []).append(full)
                continue
            if _is_link(full):
                real = os.path.realpath(full)
                nreal = os.path.normcase(real)
                if nreal in seen or _inside(root_real, real):
                    continue   # a loop, or somewhere already walked
                if not outside_ok and not _inside(real, root_real):
                    if skips is not None:
                        skips.setdefault("outside", []).append(full)
                    continue
                seen.add(nreal)
            elif not outside_ok and not _inside(os.path.realpath(full),
                                                root_real):
                # Not a link as far as this Python can tell, yet it resolves
                # to somewhere else (a junction an old Python cannot see, or
                # a path with a link above it): the same rule applies.
                if skips is not None:
                    skips.setdefault("outside", []).append(full)
                continue
            keep.append(d)
        dirs[:] = keep
        for fn in sorted(files):
            if _ignored_name(fn):
                continue
            full = os.path.join(root, fn)
            if _is_transcript(full):
                continue
            size = _regular_size(full)
            if size is None:
                continue
            if (not outside_ok and _is_link(full)
                    and not _inside(os.path.realpath(full), root_real)):
                if skips is not None:
                    skips.setdefault("outside", []).append(full)
                continue
            if limit is not None and size > limit:
                if too_big is not None:
                    too_big.append((full, size))
                continue
            if scan_secrets and secretscan.file_kinds(full, size):
                # Left out like an ignored file (and listed, so the command
                # can say so): the text that matched is never kept.
                if skips is not None:
                    skips.setdefault("secret", []).append(full)
                continue
            if budget.take(full, size):
                yield full, size


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
    them, so the live tree must see the same files) under the rules of
    _walk_files: no loops or repeats, bounded depth and total size, and no
    links that lead outside the watched directory unless you opted in."""
    budget = _Budget()
    for ap in paths:
        if os.path.isdir(ap):
            for full, _size in _walk_files(ap, budget):
                yield full
        elif os.path.isfile(ap):
            if secretscan.enabled() and secretscan.file_kinds(ap):
                continue   # not in the snapshot, so not in the comparison
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
    # each watched path under both spellings: git on a Mac composes a
    # pathspec itself, a git elsewhere does not
    specs = sorted({f"files/{rel}" for rel in files}
                   | {f"files/{_composed(rel)}" for rel in files})
    listing = _git(REPO_DIR, "ls-tree", "-r", "-z", sha, "--", *specs)
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
        else:
            # git may spell the name of a watched path composed where the
            # manifest has it decomposed (macOS lists names decomposed);
            # _same_tree() takes it from there
            crel = _composed(rel)
            for key, ap in files.items():
                ckey = _composed(key)
                if crel == ckey:
                    out[ap] = parts[2]
                    break
                if crel.startswith(ckey + "/"):
                    sub = crel[len(ckey) + 1:].split("/")
                    out[os.path.join(ap, *sub)] = parts[2]
                    break
    return out


def _same_tree(live: dict[str, str], snap: dict[str, str]) -> bool:
    """Are these the same paths with the same content? {abspath: blob id}
    both. A path compares as git records it: macOS keeps a name like
    e-acute decomposed (e + a combining accent) and git, with
    core.precomposeunicode, stores it composed - the allowance _check_staged
    makes - so two paths that differ only in composition are one file here
    too. Never at the price of merging two files: if composing makes two
    paths of one side the same, the sides are not equal."""
    if live == snap:
        return True
    a = {_composed(path): blob for path, blob in live.items()}
    b = {_composed(path): blob for path, blob in snap.items()}
    return len(a) == len(live) and len(b) == len(snap) and a == b


def _blob_algo(tree: dict[str, str]) -> str:
    return "sha256" if any(len(v) == 64 for v in tree.values()) else "sha1"


def worktree_matches_snapshot(sha: str, project_root: str,
                              manifest: dict | None = None) -> bool:
    """True if the current watched files are byte-identical to the
    snapshot's recorded state (redo is possible without a pre-snap)."""
    manifest = _manifest_at(sha) if manifest is None else manifest
    if not manifest:
        return False
    manifest = dict(manifest, files=_on_disk_files(
        manifest, _norm_root(project_root)))
    current = _scoped_paths(project_root, manifest)
    if set(manifest.get("files", {}).values()) != set(current):
        return False
    snap = snapshot_tree(sha, manifest)
    live = live_tree(current, _blob_algo(snap))
    return live is not None and _same_tree(live, snap)


def current_position(snaps: list[dict], project_root: str) -> int | None:
    """Index of the newest snapshot the live files match, or None when
    the current state is not snapshotted (dirty). Each snapshot is
    compared within its own scope (the paths watched when it was taken)."""
    if not snaps:
        return None
    manifests = _manifests_at([s["sha"] for s in snaps])
    entries = _watched_entries(project_root)
    root = _norm_root(project_root)
    listings: dict[str, list[str]] = {}
    live_all = None
    for i, s in enumerate(snaps):
        m = manifests.get(s["sha"]) or {}
        if not m:
            continue
        m = dict(m, files=_on_disk_files(m, root, listings))
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
        if _same_tree(live, snap):
            return i
    return None


def _subtree(tree: dict[str, str], ap: str) -> dict[str, str]:
    return {k: v for k, v in tree.items() if _inside(k, ap)}


def _live_matches(ap: str, snap_tree: dict[str, str], algo: str) -> bool:
    """True when the file(s) at ap (a file, a dir, or a link to either)
    are byte-identical to what the snapshot recorded there."""
    live = live_tree([ap], algo)
    return live is not None and _same_tree(live, _subtree(snap_tree, ap))


def _links_under(top: str) -> list[str]:
    """Every symlink (to a file or a dir) inside the directory top."""
    links = []
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not _ignored_dir(root, d)]
        for name in dirs + files:
            full = os.path.join(root, name)
            if _is_link(full):
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


def unsnapshotted_items(top: str) -> list[str]:
    """The top-most paths under the watched folder ``top`` that a snapshot
    walk leaves out: ignored names (logs, caches, a virtual environment, a
    skill's own .git), links leading outside, folders below the depth limit,
    files over the size limit or the snapshot cap, and anything that is not a
    regular file. A snapshot cannot hold them, so a restore must not take
    them away. Links that ARE walked are not listed (_keep_links looks after
    those)."""
    walked: set[str] = set()
    yielded: set[str] = set()
    for full, _size in _walk_files(top, _Budget(), None,
                                   lambda d: walked.add(os.path.normcase(d))):
        yielded.add(os.path.normcase(full))
    out: list[str] = []

    def visit(directory: str) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            full = os.path.join(directory, name)
            key = os.path.normcase(full)
            if os.path.isdir(full):
                if key in walked:
                    if not os.path.islink(full):
                        visit(full)
                else:
                    out.append(full)
            elif key not in yielded:
                out.append(full)
    visit(top)
    return out


def _carry_unsnapshotted(live: str, trashed: str, items: list[str],
                         report: list[str]) -> None:
    """Put the items (listed under the live folder ``live``, whose old copy
    is now ``trashed``) back where they were, unless the restored folder
    already has something by that name. Reports each one."""
    for item in items:
        sub = os.path.relpath(item, live)
        src = os.path.join(trashed, sub)
        dst = os.path.join(live, sub)
        if not os.path.lexists(src) or os.path.lexists(dst):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            report.append(f"skipped {dst}: could not put it back from the "
                          f"trash ({e.strerror or e}); it is in {src}")
            continue
        report.append(f"kept {dst}: not part of any snapshot (left in "
                      f"place)")


def _unpack_folder(archive: bytes, prefix: str, dest: str) -> None:
    """Unpack the folder `prefix` of a `git archive` stream AS `dest`.

    The archive names everything by its whole mirror path
    (files/__home/.claude/projects/<project>/memory/...). Unpacking that as
    it stands, in a staging folder beside the live one, nested the long part
    of the path a second time: for a project with a long name that passed the
    260-character limit of a Windows without long-path support, and the
    restore failed with "could not extract". Here the leading path is
    dropped, so the staged copy is only a few characters deeper than the
    live folder. Raises tarfile.TarError or OSError when the folder could not
    be unpacked completely (nothing partial is ever swapped in).
    """
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        members = []
        for member in tf.getmembers():
            if member.name == prefix:
                member.name = "."
            elif member.name.startswith(prefix + "/"):
                member.name = member.name[len(prefix) + 1:]
            else:
                continue   # a folder around it, which the archive lists first
            parts = member.name.replace("\\", "/").split("/")
            if (member.islnk() or ".." in parts
                    or member.name.startswith(("/", "\\"))):
                raise tarfile.ExtractError(
                    f"unexpected entry {member.name!r} in the archive")
            members.append(member)
        if not members:
            raise tarfile.ReadError(f"nothing under {prefix} in the archive")
        if hasattr(tarfile, "tar_filter"):
            tf.extractall(dest, members=members, filter="tar")
        else:  # Python without extraction filters
            tf.extractall(dest, members=members)


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
    root = _norm_root(project_root if project_root is not None
                      else manifest.get("project_root"))
    # The live paths as the file system spells them now: a snapshot from an
    # older version holds the spelling of the watch list, and restoring
    # under that name would trash the file whose name differs in case only
    # and write it again under the other one.
    files = _on_disk_files(manifest, root)
    manifest = dict(manifest, files=files)
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
    bits = _manifest_modes(manifest)   # group/other bits (see _apply_modes)
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
                src = tmp   # the folder is unpacked AS tmp (_unpack_folder)
                arch = _run_git(REPO_DIR, ("archive", sha, f"files/{rel}"))
                why = "" if arch.returncode == 0 else " (git could not read it)"
                if not why:
                    try:
                        _unpack_folder(arch.stdout, f"files/{rel}", src)
                    except (tarfile.TarError, OSError) as e:
                        why = f" ({getattr(e, 'strerror', None) or e})"
                if why:
                    restored.append(f"skipped {ap}: could not extract "
                                    f"snapshot {sha[:7]}{why} (left untouched)")
                    continue
                leftovers = []
                if os.path.isdir(ap) and not os.path.islink(ap):
                    restored.extend(_keep_links(ap, src, snap_tree, algo))
                    leftovers = unsnapshotted_items(ap)
                _apply_modes(src, ap, rel, bits)
                # Never delete: the existing path (dir, file or link) goes
                # to trash before the snapshot copy takes its place - and
                # comes back if that fails.
                if _swap_via_trash(ap, lambda: os.rename(src, ap), trash_dir,
                                   restored, trashed):
                    restored.append(ap)
                    if leftovers and trashed:
                        _carry_unsnapshotted(ap, trashed[-1], leftovers, restored)
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
            _narrow_to_recorded(ap, rel, bits)
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
                _narrow_to_recorded(ap, rel, bits)
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
    # A file left out of the newer snapshot on purpose (it looked like it
    # held a secret) is not a deleted file.
    withheld = set(_manifest_at(sha).get("withheld") or [])
    hits = [(rel, old[rel], new[rel]) for rel in sorted(new)
            if rel in old and new[rel] < old[rel] * (1 - ratio)]
    hits += [(rel, old[rel], None) for rel in sorted(old)
             if rel not in new and rel not in withheld]
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


_SHA_RE = re.compile(r"(?:[0-9a-fA-F]{4,64}|HEAD)\Z")


def is_snapshot_id(sha) -> bool:
    """A snapshot is named by a hex id (4-64 digits) or HEAD, nothing else:
    an argument that starts with `-` must never reach git as an option."""
    return isinstance(sha, str) and bool(_SHA_RE.match(sha))


def _require_snapshot_id(sha) -> str:
    if not is_snapshot_id(sha):
        shown = " ".join(str(sha).split())[:40]
        raise NothingToRestoreError(
            f"no such snapshot: {shown!r} (a snapshot is named by its hex "
            f"id as `sessionsteward log` prints it, or HEAD)")
    return sha


def diff(sha1: str, sha2: str) -> str:
    _require_snapshot_id(sha1)
    _require_snapshot_id(sha2)
    # Plain unified diff whatever your git config says: no colour (escape
    # codes hid every line from the +/- filter), no external diff tool or
    # textconv filter (their output is not a diff), and no rename detection
    # (a renamed file would show as "similarity index 100%" and no lines).
    option_end = ("--end-of-options",) if _git_has_end_of_options() else ()
    return _git(REPO_DIR, "diff", "--no-color", "--no-ext-diff",
                "--no-textconv", "--no-renames", *option_end,
                f"{sha1}..{sha2}", "--", "files/")


#: git added --end-of-options in 2.24 (November 2019).
_GIT_END_OF_OPTIONS = (2, 24)


@functools.lru_cache(maxsize=1)
def _git_version() -> tuple[int, ...] | None:
    """The installed git's version as numbers, e.g. (2, 43, 0), or None when
    it cannot be read."""
    try:
        p = _run_git(REPO_DIR if os.path.isdir(REPO_DIR) else os.getcwd(),
                     ("--version",))
    except StewardError:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?",
                  p.stdout.decode("utf-8", "replace"))
    if p.returncode != 0 or not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _git_has_end_of_options() -> bool:
    """Does this git know --end-of-options (2.24+)? An older git gets the
    command without it: the ids are checked by _require_snapshot_id first, so
    nothing that looks like an option can reach it either way. A git whose
    version cannot be read is assumed to be recent."""
    version = _git_version()
    return version is None or version >= _GIT_END_OF_OPTIONS


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

HOOK_TIMEOUT_SECONDS = 30


def _console_script() -> str | None:
    """Absolute path of the installed `sessionsteward` console script, or
    None when it is not there (a run from a source checkout)."""
    name = "sessionsteward.exe" if os.name == "nt" else "sessionsteward"
    dirs = []
    try:
        dirs.append(sysconfig.get_path("scripts"))
        dirs.append(sysconfig.get_path(
            "scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError, AttributeError):
        pass
    for d in dirs:
        if not d:
            continue
        path = os.path.join(d, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    return None


def _self_cmd() -> str:
    """The command a Claude Code hook runs, quoted for hook shells.

    Hook shells don't load your aliases or activate your venv, so a bare
    `sessionsteward` fails silently there: the absolute path of the console
    script is written. NOT `python -m sessionsteward`: Claude Code runs hooks
    with the PROJECT as the working directory, and `python -m` puts that
    directory first on sys.path, so a cloned repository holding its own
    sessionsteward/__main__.py would run its code at every session start.
    Only when the script cannot be found does this fall back to
    `python -I -m sessionsteward` (-I: no working directory on sys.path).
    Windows paths with spaces need double quotes (cmd and Git Bash both
    accept them); POSIX gets shlex.
    """
    def quote(word: str) -> str:
        return f'"{word}"' if os.name == "nt" else shlex.quote(word)
    script = _console_script()
    if script:
        return quote(script)
    return f"{quote(sys.executable)} -I -m sessionsteward"


HOOK_PRECOMPACT_CMD = f"{_self_cmd()} precompact"
HOOK_SESSIONSTART_CMD = f'{_self_cmd()} snap -m "session start"'
HOOK_REMIND_CMD = f"{_self_cmd()} remind"
HOOK_GUARD_CMD = f"{_self_cmd()} guard hook"

#: Every hook sessionsteward installs: (event, matcher, arguments after the
#: command, the full command). The matchers are Claude Code's: SessionStart
#: "compact" is the session that continues after a compaction; the guard's
#: PreToolUse matcher names the file-editing tools; FileChanged takes the
#: literal file names to watch (see guardhook.py for what each can do).
HOOK_SPECS = (
    ("PreCompact", "", ["precompact"], HOOK_PRECOMPACT_CMD),
    ("SessionStart", "", ["snap", "-m", "session start"],
     HOOK_SESSIONSTART_CMD),
    ("SessionStart", "compact", ["remind"], HOOK_REMIND_CMD),
    ("PreToolUse", "Write|Edit|MultiEdit", ["guard", "hook"],
     HOOK_GUARD_CMD),
    ("FileChanged", "CLAUDE.md|CLAUDE.local.md|AGENTS.md", ["guard", "hook"],
     HOOK_GUARD_CMD),
)
# The arguments each event's first hook has when sessionsteward wrote it.
_HOOK_ARGS = {}
for _event, _matcher, _args, _cmd in HOOK_SPECS:
    _HOOK_ARGS.setdefault(_event, _args)


def _is_ours(event: str, cmd, args=None) -> bool:
    """Did sessionsteward write this hook command for this event? Exactly
    that, and nothing that merely mentions it: the bare `sessionsteward ...`
    the first version wrote, `<any python> [-I] -m sessionsteward ...` (the
    0.1.0-0.1.3 form, unsafe - see _self_cmd - and rewritten by
    `install-hooks --write`), or `<path>/sessionsteward[.exe] ...` (ours from
    another, moved or recreated, environment). merge_hooks rewrites these in
    place rather than appending a second, stale-plus-fresh pair; every other
    hook is the user's - `cd ~/code/sessionsteward && git pull`,
    `sessionsteward snap -m checkpoint` - and is never touched."""
    if not isinstance(cmd, str):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    args = _HOOK_ARGS[event] if args is None else args
    if words[-len(args):] != args:
        return False
    head = words[:-len(args)]
    if head == ["sessionsteward"]:
        return True
    if len(head) == 1:
        return re.split(r"[\\/]", head[0])[-1].lower() in (
            "sessionsteward", "sessionsteward.exe")
    return (len(head) in (3, 4) and head[-2:] == ["-m", "sessionsteward"]
            and (len(head) == 3 or head[1] == "-I"))


def build_hooks_json() -> dict:
    """The hooks payload for Claude Code's settings.json `hooks` section."""
    hooks: dict = {}
    for event, matcher, _args, cmd in HOOK_SPECS:
        hooks.setdefault(event, []).append(
            {"matcher": matcher,
             "hooks": [{"type": "command", "command": cmd,
                        "timeout": HOOK_TIMEOUT_SECONDS}]})
    return {"hooks": hooks}


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
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = settings["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise SettingsError('"hooks" in the settings file is not an object')
    for event, matcher, args, cmd in HOOK_SPECS:
        existing = hooks.get(event)
        if existing is None:
            existing = hooks[event] = []
        elif not isinstance(existing, list):
            raise SettingsError(f'"hooks.{event}" in the settings file is '
                                f'not a list')
        found = False
        for e in list(existing):
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            kept = []
            for h in e["hooks"]:
                if isinstance(h, dict) and _is_ours(event, h.get("command"),
                                                    args):
                    if found:
                        continue  # stale duplicate
                    h["command"] = cmd
                    h.setdefault("timeout", HOOK_TIMEOUT_SECONDS)
                    found = True
                kept.append(h)
            if kept:
                e["hooks"] = kept
            elif e["hooks"]:
                existing.remove(e)  # we emptied it
        if not found:
            existing.append({"matcher": matcher,
                             "hooks": [{"type": "command", "command": cmd,
                                        "timeout": HOOK_TIMEOUT_SECONDS}]})
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
