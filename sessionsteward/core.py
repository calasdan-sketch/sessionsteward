"""Snapshot backend: git-versions watched files. Every snap is a commit.

Layout inside the snapshots repo:
  files/<rel>          mirrored watched files
  manifest.json        {rel: abspath} for the latest snap

rel = path relative to project root, or __home/... for home files.
"""

from __future__ import annotations

import fnmatch
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

# ~/.claude/CLAUDE.md is Claude Code's user-level memory; CLAUDE.local.md
# is the per-project private memory file.
DEFAULT_WATCH = ["CLAUDE.md", "CLAUDE.local.md", ".claude", "AGENTS.md",
                 "~/.claude/CLAUDE.md"]

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


def _git(repo: str, *args: str) -> str:
    # git prints paths as UTF-8 bytes on every platform; decode them the
    # way os.walk() names files so the two always compare equal.
    p = subprocess.run([*GIT, "-C", repo, *args], capture_output=True,
                       encoding="utf-8", errors="surrogateescape",
                       timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{p.stderr.strip()[:200]}")
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
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"watch": []}


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
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


def watched_paths(project_root: str) -> list[str]:
    """Absolute paths of watched files that currently exist."""
    out = []
    for rel in watch_list():
        if rel.startswith("~/"):
            p = os.path.expanduser(rel)
        elif os.path.isabs(rel):
            p = rel
        else:
            p = os.path.join(project_root, rel)
        if os.path.exists(p):
            out.append(os.path.abspath(p))
    return out


def _rel(abspath: str, project_root: str) -> str:
    """Path of abspath inside files/, always with forward slashes (git's
    tree paths are '/'-separated on every platform)."""
    home = os.path.expanduser("~")
    if abspath == home or abspath.startswith(home + os.sep):
        rel = os.path.join("__home", os.path.relpath(abspath, home))
    else:
        try:
            rel = os.path.relpath(abspath, project_root)
        except ValueError:
            rel = abspath.lstrip(os.sep).replace(os.sep, "_")
    return rel.replace(os.sep, "/")


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
    manifest = {}
    files_dir = os.path.join(REPO_DIR, "files")
    for ap in paths:
        rel = _rel(ap, project_root)
        manifest[rel] = ap
        dest = os.path.join(files_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest):
            _remove_mirror(dest)
        # copyfile, not copy2/copytree's default: the mirror must never
        # inherit read-only modes, or the next snap cannot replace it.
        if os.path.isdir(ap):
            shutil.copytree(ap, dest, copy_function=shutil.copyfile,
                            ignore=shutil.ignore_patterns(*IGNORE_PATTERNS))
        else:
            shutil.copyfile(ap, dest)
    # No timestamp in the manifest: identical content must produce an
    # identical tree so the re-snap below is skipped. (It used to carry
    # "at"; two hooks snapping the same clobbered state seconds apart
    # made two commits, and `undo` then stepped back to the clobbered one.)
    with open(os.path.join(REPO_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump({"project_root": _norm_root(project_root),
                   "files": manifest}, f, indent=2)
    _git(REPO_DIR, "add", "-A")
    # skip empty commits (nothing changed)
    status = _git(REPO_DIR, "status", "--porcelain")
    if not status.strip():
        return _git(REPO_DIR, "rev-parse", "HEAD").strip()
    _git(REPO_DIR, "commit", "-q", "-m", message)
    return _git(REPO_DIR, "rev-parse", "HEAD").strip()


def log(limit: int = 20, project_root: str | None = None) -> list[dict]:
    """List snapshots, newest first.

    When project_root is given, only snapshots whose manifest records
    that project root are returned (manifest-less commits like the
    initial `init` commit belong to no project and are excluded).
    """
    # When filtering by project we must scan past other projects'
    # commits; fetch a wider window (documented bound for v1).
    fetch = max(limit * 5, 100) if project_root else limit
    out = _git(REPO_DIR, "log", f"-{fetch}",
               "--pretty=format:%H|%ad|%s", "--date=iso-local")
    want = _norm_root(project_root)
    snaps = []
    for line in out.splitlines():
        sha, date, msg = line.split("|", 2)
        entry = {"sha": sha, "date": date, "message": msg}
        if want is not None:
            m = _manifest_at(sha)
            if not m or _norm_root(m.get("project_root")) != want:
                continue
            entry["project_root"] = m.get("project_root")
        snaps.append(entry)
        if len(snaps) >= limit:
            break
    return snaps


def _manifest_at(sha: str) -> dict:
    try:
        raw = _git(REPO_DIR, "show", f"{sha}:manifest.json")
    except RuntimeError:
        return {}
    return json.loads(raw)


def _read_snapshot_file(sha: str, rel: str) -> tuple[bytes | None, bool]:
    """(content, is_dir) for files/<rel> at sha."""
    ls = _git(REPO_DIR, "ls-tree", sha, f"files/{rel}").strip()
    if not ls:
        return None, False
    if ls.startswith("040000"):
        return None, True
    p = subprocess.run([*GIT, "-C", REPO_DIR, "show", f"{sha}:files/{rel}"],
                       capture_output=True, timeout=60)
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


def check_restore(sha: str, project_root: str | None = None) -> dict:
    """Validate that sha is restorable for project_root.

    Returns the snapshot's manifest. Raises NothingToRestoreError for a
    manifest-less snapshot (e.g. the `init` commit) or an unknown sha,
    and CrossProjectError when project_root is given and the snapshot
    belongs to a different project.
    """
    manifest = _manifest_at(sha)
    if not manifest:
        p = subprocess.run(
            [*GIT, "-C", REPO_DIR, "rev-parse", "--verify", "--quiet",
             f"{sha}^{{commit}}"], capture_output=True, timeout=60)
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


def _dir_matches(sha: str, rel: str, ap: str) -> bool:
    """True if the on-disk dir matches the snapshot's recorded tree."""
    if not os.path.isdir(ap) or os.path.islink(ap):
        return False
    prefix = f"files/{rel}/"
    snap_names = set()
    for line in _git(REPO_DIR, "ls-tree", "-r", "--name-only",
                     sha, f"files/{rel}").splitlines():
        if line.startswith(prefix):
            snap_names.add(line[len(prefix):])
    disk_names = set()
    for root, dirs, files in os.walk(ap):
        dirs[:] = [d for d in dirs if not _ignored_name(d)]
        for fn in files:
            if _ignored_name(fn):
                continue
            fp = os.path.join(root, fn)
            name = os.path.relpath(fp, ap).replace(os.sep, "/")
            disk_names.add(name)
            p = subprocess.run(
                [*GIT, "-C", REPO_DIR, "show", f"{sha}:{prefix}{name}"],
                capture_output=True, timeout=60)
            if p.returncode != 0:
                return False
            try:
                with open(fp, "rb") as f:
                    if f.read() != p.stdout:
                        return False
            except OSError:
                return False
    return snap_names == disk_names


def worktree_matches_snapshot(sha: str, project_root: str) -> bool:
    """True if the current watched files are byte-identical to the
    snapshot's recorded state (redo is possible without a pre-snap)."""
    manifest = _manifest_at(sha)
    if not manifest:
        return False
    snap_files = manifest.get("files", {})
    current = {os.path.abspath(p) for p in watched_paths(project_root)}
    if set(snap_files.values()) != current:
        return False
    for rel, ap in snap_files.items():
        data, is_dir = _read_snapshot_file(sha, rel)
        if is_dir:
            if not _dir_matches(sha, rel, ap):
                return False
        elif data is None:
            return False
        else:
            try:
                with open(ap, "rb") as f:
                    if f.read() != data:
                        return False
            except OSError:
                return False
    return True


def _latest_manifest_for_project(project_root: str | None) -> dict:
    """Manifest of the newest snapshot, scoped to project_root.

    The snapshots repo is shared between projects, so global HEAD may
    belong to a different project. The trash sweep in restore() must
    compare extra files against the current project's latest state,
    never another project's — otherwise undo in project A would yank
    project B's watched files into trash. Returns {} when no snapshot
    exists for the project; when project_root is None (unscoped API
    use) falls back to the global HEAD manifest (legacy behavior).
    """
    if project_root is None:
        return _manifest_at("HEAD")
    snaps = log(1, project_root=project_root)
    if not snaps:
        return {}
    return _manifest_at(snaps[0]["sha"])


def restore(sha: str, trash_dir: str | None = None,
            project_root: str | None = None) -> list[str]:
    """Restore watched files from a snapshot. Files that exist now but
    didn't in the snapshot are moved to trash instead of deleted —
    directories included (never `rmtree`'d).

    Raises NothingToRestoreError for a manifest-less snapshot (e.g. the
    `init` commit) and CrossProjectError when project_root is given and
    the snapshot belongs to a different project.
    """
    manifest = check_restore(sha, project_root)
    _ensure_attributes()  # `git archive` below honours attributes
    restored = []
    # Scope the extra-file sweep to the current project: global HEAD may
    # belong to another project, and sweeping against it would trash
    # that project's watched files (cross-project clobbering).
    latest = _latest_manifest_for_project(project_root)
    trash_dir = trash_dir or TRASH_DIR
    for rel, ap in latest.get("files", {}).items():
        if rel not in manifest["files"] and os.path.lexists(ap):
            dest = _move_to_trash(ap, trash_dir)
            restored.append(f"moved {ap} -> {dest}")
    for rel, ap in manifest["files"].items():
        data, is_dir = _read_snapshot_file(sha, rel)
        if is_dir:
            # Extract next to the live dir and verify BEFORE touching it,
            # so a failed extract leaves the live dir as it was and the
            # final swap is a same-filesystem rename. tarfile, not `tar`:
            # works on Windows too.
            parent = os.path.dirname(ap)
            os.makedirs(parent, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".steward-restore-", dir=parent)
            try:
                src = os.path.join(tmp, "files", rel)
                arch = subprocess.run(
                    [*GIT, "-C", REPO_DIR, "archive", sha, f"files/{rel}"],
                    capture_output=True, timeout=60)
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
                if os.path.lexists(ap):
                    # Never delete: move the existing path (dir, file or
                    # link) to trash before restoring the snapshot copy.
                    dest = _move_to_trash(ap, trash_dir)
                    restored.append(f"moved {ap} -> {dest}")
                os.rename(src, ap)
                restored.append(ap)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        elif data is not None:
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
    return restored


def skipped(restored: list[str]) -> list[str]:
    """The 'skipped ...' lines of a restore() result (nothing was written
    for those paths; the CLI exits 1 when any exist)."""
    return [r for r in restored if r.startswith("skipped ")]


def _sizes(sha: str) -> dict[str, int]:
    """{path under files/: blob size} at sha, limited to the paths the
    snapshot's own manifest records. The repo is shared between projects
    (and files/ keeps stale mirrors), so the whole files/ tree would
    report other projects' files and hide this project's deletions."""
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


def guard_lines(sha: str, prev_sha: str) -> list[str]:
    lines = []
    for rel, o, n in shrunk(sha, prev_sha):
        if n is None:
            lines.append(f"  ! guard: {rel} deleted ({o} bytes) — "
                         f"`sessionsteward undo` to recover")
        else:
            lines.append(f"  ! guard: {rel} shrank {o} -> {n} bytes "
                         f"(-{100 - round(n * 100 / o)}%) — "
                         f"`sessionsteward undo` to recover")
    return lines


def diff(sha1: str, sha2: str = "HEAD") -> str:
    return _git(REPO_DIR, "diff", f"{sha1}..{sha2}", "--", "files/")


def file_mtimes(paths: list[str]) -> dict[str, float]:
    mtimes = {}
    for ap in paths:
        if os.path.isdir(ap):
            for root, dirs, files in os.walk(ap):
                dirs[:] = [d for d in dirs if not _ignored_name(d)]
                for fn in files:
                    if _ignored_name(fn):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        mtimes[fp] = os.path.getmtime(fp)
                    except OSError:
                        pass
        elif os.path.exists(ap):
            mtimes[ap] = os.path.getmtime(ap)
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
        with open(settings_path) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    merge_hooks(settings)
    os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    return settings_path
