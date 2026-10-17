"""Snapshot backend: git-versions watched files. Every snap is a commit.

Layout inside the snapshots repo:
  files/<rel>          mirrored watched files (only this snapshot's project)
  manifest.json        {"project_root": ..., "files": {rel: abspath},
                        "watch": [the watch list in force]} for the snap

rel is the path relative to the project root; files outside the project
are mirrored under __home/... (inside your home directory) or
__abs/<drive-or-root>/... (anywhere else). A mirror path can therefore
never climb out of files/ — and snap() verifies that before touching
anything.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

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

# Snapshots must be byte-exact: never let a user's global core.autocrlf
# (common on Windows) rewrite CRLF/LF on add, show or archive. Paths must
# be byte-exact too: with core.quotePath (git's default) a non-ASCII name
# such as commands/Καλάς.md is listed as "\316\232..." and never matches
# the name on disk.
GIT = ["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false"]

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


def _ignored_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in IGNORE_PATTERNS)


def _norm_root(root: str | None) -> str | None:
    if root is None:
        return None
    return os.path.abspath(os.path.expanduser(root))


def _run_git(repo: str, args: tuple[str, ...], **kw) -> subprocess.CompletedProcess:
    """subprocess.run of a git command with the failure modes that are not
    git's own exit code turned into GitError: no git on PATH, a hang."""
    try:
        return subprocess.run([*GIT, "-C", repo, *args], capture_output=True,
                              timeout=60, **kw)
    except FileNotFoundError:
        raise GitError("git not found — sessionsteward needs git on PATH "
                       "(https://git-scm.com/downloads)") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} did not finish within 60s") \
            from None


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
    return os.path.isdir(os.path.join(REPO_DIR, ".git"))


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
    os.makedirs(REPO_DIR, exist_ok=True)
    if not repo_ready():
        _git(REPO_DIR, "init", "-qb", "main")
        _git(REPO_DIR, "config", "user.email", "sessionsteward@local")
        _git(REPO_DIR, "config", "user.name", "sessionsteward")
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
            common, top = (p.stdout.splitlines() + ["", ""])[:2]
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


def _watched_entries(project_root: str) -> list[tuple[str, str]]:
    """[(watch-list entry, absolute path)] for the watched paths that
    exist now, deduped by path (the first entry naming a path wins)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel in watch_list():
        p = _expand_watch_entry(rel, project_root)
        if p and os.path.exists(p):
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
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


def snap(project_root: str, message: str) -> str:
    """Copy watched files into the repo and commit. Returns commit sha."""
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
    # files/ holds exactly this snapshot's files: stale mirrors of other
    # projects (the repo is shared) would otherwise ride along in every
    # commit and show up in `diff`.
    if os.path.lexists(files_dir):
        _remove_mirror(files_dir)
    os.makedirs(files_dir)
    for rel, ap, dest in plan:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # copyfile, not copy2/copytree's default: the mirror must never
        # inherit read-only modes, or the next snap cannot replace it.
        # (dirs_exist_ok / the lexists check: a watched file inside a
        # watched dir, e.g. ~/.claude/CLAUDE.md with ~ as the project.)
        if os.path.isdir(ap):
            # Symlinks are followed: the mirror holds the content behind
            # them (restore() never writes it back through the link).
            shutil.copytree(ap, dest, copy_function=shutil.copyfile,
                            ignore=shutil.ignore_patterns(*IGNORE_PATTERNS),
                            ignore_dangling_symlinks=True, dirs_exist_ok=True)
        elif not os.path.lexists(dest):
            shutil.copyfile(ap, dest)
    # No timestamp in the manifest: identical content must produce an
    # identical tree so the re-snap below is skipped. (It used to carry
    # "at"; two hooks snapping the same clobbered state seconds apart
    # made two commits, and `undo` then stepped back to the clobbered one.)
    # "watch" records the effective watch list, so a restore knows which
    # paths were in this snapshot's scope (see _in_scope).
    with open(os.path.join(REPO_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump({"project_root": _norm_root(project_root),
                   "files": manifest, "watch": watch_list()}, f, indent=2)
    _git(REPO_DIR, "add", "-A")
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


def _manifests_at(shas: list[str]) -> dict[str, dict]:
    """{sha: manifest} for many snapshots in one git call (cat-file
    --batch); a commit without manifest.json maps to {}."""
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
            raise StewardError(f"snapshot {sha[:7]} has a corrupt "
                               f"manifest.json ({e})")
    return out


def _manifest_at(sha: str) -> dict:
    try:
        full = _git(REPO_DIR, "rev-parse", "--verify", "--quiet",
                    f"{sha}^{{commit}}").strip()
    except GitError:
        return {}
    return _manifests_at([full]).get(full, {})


def log(limit: int = 20, project_root: str | None = None) -> list[dict]:
    """List snapshots, newest first.

    When project_root is given, only snapshots whose manifest records
    that project root are returned (manifest-less commits like the
    initial `init` commit belong to no project and are excluded). The
    whole history is scanned — the repo is shared between projects, so
    this project's snapshots may sit behind any number of others'.
    """
    args = ["log", "--pretty=format:%H|%ad|%s", "--date=iso-local"]
    if project_root is None:
        args.insert(1, f"-{limit}")
    out = _git(REPO_DIR, *args)
    want = _norm_root(project_root)
    rows = [line.split("|", 2) for line in out.splitlines() if line]
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


def prune_trash(trash_dir: str, keep: int | None = None) -> list[str]:
    """Delete all but the newest `keep` (default TRASH_KEEP) trash
    entries. Returns the removed entries."""
    keep = TRASH_KEEP if keep is None else keep
    try:
        names = os.listdir(trash_dir)
    except OSError:
        return []
    entries = []
    for name in names:
        p = os.path.join(trash_dir, name)
        try:
            entries.append((os.lstat(p).st_mtime, p))
        except OSError:
            pass
    entries.sort(reverse=True)
    removed = []
    for _, p in entries[keep:]:
        try:
            _remove_mirror(p)
            removed.append(p)
        except OSError:
            pass
    return removed


def check_restore(sha: str, project_root: str | None = None) -> dict:
    """Validate that sha is restorable for project_root.

    Returns the snapshot's manifest. Raises NothingToRestoreError for a
    manifest-less snapshot (e.g. the `init` commit) or an unknown sha,
    and CrossProjectError when project_root is given and the snapshot
    belongs to a different project.
    """
    manifest = _manifest_at(sha)
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
    return manifest


# ---------------------------------------------------------------------------
# Tree comparison: git blob ids, computed locally
# ---------------------------------------------------------------------------

def iter_files(paths: list[str]):
    """Yield every file under the watched paths (directories walked,
    IGNORE_PATTERNS applied), in a stable order.

    Symlinked directories are walked (snap() mirrors what is behind
    them, so the live tree must see the same files), with a guard so a
    link back into the tree cannot loop forever."""
    for ap in paths:
        if os.path.isdir(ap):
            seen: set[str] = set()
            for root, dirs, files in os.walk(ap, followlinks=True):
                seen.add(os.path.realpath(root))
                keep = []
                for d in sorted(dirs):
                    if _ignored_name(d):
                        continue
                    full = os.path.join(root, d)
                    if os.path.islink(full) and os.path.realpath(full) in seen:
                        continue
                    keep.append(d)
                dirs[:] = keep
                for fn in sorted(files):
                    if not _ignored_name(fn):
                        yield os.path.join(root, fn)
        elif os.path.exists(ap):
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
    listing = _git(REPO_DIR, "ls-tree", "-r", sha, "--",
                   *(f"files/{rel}" for rel in files))
    for line in listing.splitlines():
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
        dirs[:] = [d for d in dirs if not _ignored_name(d)]
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
    `init` commit) and CrossProjectError when project_root is given and
    the snapshot belongs to a different project.
    """
    manifest = check_restore(sha, project_root)
    _ensure_attributes()  # `git archive` below honours attributes
    restored = []
    trash_dir = trash_dir or TRASH_DIR
    moved = False
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
        dest = _move_to_trash(ap, trash_dir)
        restored.append(f"moved {ap} -> {dest}")
        moved = True
    snap_tree = snapshot_tree(sha, manifest)
    algo = _blob_algo(snap_tree)
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
                if os.path.lexists(ap):
                    # Never delete: move the existing path (dir, file or
                    # link) to trash before restoring the snapshot copy.
                    dest = _move_to_trash(ap, trash_dir)
                    restored.append(f"moved {ap} -> {dest}")
                    moved = True
                os.rename(src, ap)
                restored.append(ap)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        elif data is not None:
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
            try:
                if os.path.lexists(ap):
                    dest = _move_to_trash(ap, trash_dir)
                    restored.append(f"moved {ap} -> {dest}")
                    moved = True
                os.makedirs(ap)
            except OSError as e:
                restored.append(f"skipped {ap}: {e.strerror or e} "
                                f"(left untouched)")
                continue
            restored.append(ap)
    if moved:
        prune_trash(trash_dir)
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
    for line in _git(REPO_DIR, "ls-tree", "-r", "-l", sha, "--",
                     *(f"files/{rel}" for rel in rels)).splitlines():
        meta, _, path = line.partition("\t")
        size = meta.split()[-1]
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
    return _git(REPO_DIR, "diff", f"{sha1}..{sha2}", "--", "files/")


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
# Any hook command that runs sessionsteward — ours from another (moved,
# recreated) venv, or a bare `sessionsteward ...` written by an older
# version. merge_hooks rewrites these in place rather than appending a
# second, stale-plus-fresh pair.
_STEWARD_CMD_RE = re.compile(
    r"(?:^|[\s/\\\"'])sessionsteward(?:\.exe)?(?:\s|$)")


def _invokes_steward(cmd) -> bool:
    return isinstance(cmd, str) and bool(_STEWARD_CMD_RE.search(cmd))


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
    Per event, the first existing hook that already runs sessionsteward
    (from any interpreter path, or an old bare command) is rewritten to
    the current command and further copies are dropped; our entry is
    appended only when none exists. Re-running is idempotent.
    """
    ours = build_hooks_json()["hooks"]
    hooks = settings.setdefault("hooks", {})
    for event, entries in ours.items():
        existing = hooks.setdefault(event, [])
        cmd = entries[0]["hooks"][0]["command"]
        found = False
        for e in list(existing):
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            kept = []
            for h in e["hooks"]:
                if isinstance(h, dict) and _invokes_steward(h.get("command")):
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


def write_hooks_file(settings_path: str) -> str:
    """Merge hooks into the settings file at settings_path. Returns it."""
    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    merge_hooks(settings)
    os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return settings_path
