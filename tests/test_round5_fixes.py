"""Round-5 regression tests (final verification panel): the data-loss paths.

Temp dirs only. The environment is hermetic: HOME and the git config
locations point into a temp dir, every GIT_* variable is removed, and the
child processes of the real-subprocess tests get PYTHONUNBUFFERED removed
(it hides buffered-flush failures).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(cli_mod.__file__)))


def _fwd(path: str) -> str:
    """A path as git config wants it (backslashes are escapes there)."""
    return path.replace("\\", "/")


def write(path: str, text: str = "", *, binary: bool = False) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if binary:
        with open(path, "wb") as f:
            f.write(text)
    else:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)


def slurp(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def plain_git(repo: str, *args: str) -> str:
    """git as the TEST looks at a repo: no location or identity variables
    (the tests set some on purpose), only the hermetic config ones."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_")
           or k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")
    return p.stdout


def script(path: str, body: str) -> str:
    """An executable shell script (POSIX tests only)."""
    write(path, "#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


class Base(unittest.TestCase):
    """A private snapshots repo, config, trash, HOME and git environment."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="ss5-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self._tmp = tmp
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        self._home = home
        # the same locations a child process derives from HOME, so
        # in-process calls and `python -m sessionsteward` children share
        # one snapshots repo (and one lock)
        dot = os.path.join(home, ".sessionsteward")
        for name, value in (("REPO_DIR", os.path.join(dot, "snapshots")),
                            ("CONFIG_PATH", os.path.join(dot, "config.json")),
                            ("TRASH_DIR", os.path.join(dot, "trash"))):
            p = mock.patch.object(core_mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self._gitconfig = os.path.join(tmp, "gitconfig")
        write(self._gitconfig, "")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update({
            "HOME": home, "USERPROFILE": home,
            "XDG_CONFIG_HOME": os.path.join(tmp, "xdg"),
            "GIT_CONFIG_GLOBAL": self._gitconfig, "GIT_CONFIG_NOSYSTEM": "1",
        })
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.proj = os.path.join(tmp, "proj")
        write(os.path.join(self.proj, "CLAUDE.md"), "# Project\n\nBe helpful.\n")
        write(os.path.join(self.proj, ".claude", "settings.json"), '{"x": 1}\n')

    # -- helpers --------------------------------------------------------
    def gitconfig(self, text: str) -> None:
        write(self._gitconfig, text)

    def cli(self, *args, root=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.main(["--root", root or self.proj, *args])
        return code, out.getvalue(), err.getvalue()

    def child_env(self) -> dict:
        env = dict(os.environ)
        env.pop("PYTHONUNBUFFERED", None)
        return env

    def run_child(self, *args, root=None, timeout=60, **kw):
        return subprocess.run(
            [sys.executable, "-m", "sessionsteward",
             "--root", root or self.proj, *args],
            capture_output=True, cwd=REPO_ROOT, env=self.child_env(),
            timeout=timeout, **kw)

    def log(self, root=None):
        return core_mod.log(50, project_root=root or self.proj)

    def trash_entries(self):
        try:
            return sorted(os.listdir(core_mod.TRASH_DIR))
        except OSError:
            return []

    def forge_commit(self, message="forged", *, drop_from_tree=(),
                     edit_manifest=None):
        """Commit a hand-made snapshot on top of HEAD: files removed from
        the tree and/or the manifest edited, the way a torn or an old
        snapshot looks. Returns the new sha."""
        repo = core_mod.REPO_DIR
        for rel in drop_from_tree:
            core_mod._git(repo, "rm", "-q", "-r", "--cached", "--",
                          f"files/{rel}")
        if edit_manifest is not None:
            mpath = os.path.join(repo, "manifest.json")
            m = json.loads(slurp(mpath).decode("utf-8"))
            edit_manifest(m)
            write(mpath, json.dumps(m, indent=2))
            core_mod._git(repo, "add", "-f", "manifest.json")
        core_mod._git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-q", "--allow-empty", "-m", message)
        return core_mod._git(repo, "rev-parse", "HEAD").strip()


class TestIgnoredFilesAreSnapshotted(Base):
    """HIGH 1: `git add -A` obeyed ignore rules, so a watched file matching
    one (CLAUDE.local.md in a global ignore) was in the manifest but not in
    the tree, and `undo` turned the live file into an empty directory."""

    def _ignore_globally(self, *patterns):
        ignore = os.path.join(self._tmp, "global-ignore")
        write(ignore, "".join(p + "\n" for p in patterns))
        self.gitconfig(f"[core]\n\texcludesFile = {_fwd(ignore)}\n")

    def test_globally_ignored_watched_file_survives_snap_and_undo(self):
        self._ignore_globally("CLAUDE.local.md")
        local = os.path.join(self.proj, "CLAUDE.local.md")
        write(local, "a\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(local, "b\n")
        write(os.path.join(self.proj, "CLAUDE.md"), "x\n")
        self.assertEqual(self.cli("snap")[0], 0)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.isfile(local), "live file became a directory")
        self.assertEqual(slurp(local), b"a\n")
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"# Project\n\nBe helpful.\n")

    def test_the_default_xdg_ignore_file_does_not_hide_a_watched_file(self):
        # the panel's repro: ~/.config/git/ignore, no core.excludesFile set
        write(os.path.join(self._tmp, "xdg", "git", "ignore"),
              "CLAUDE.local.md\n")
        local = os.path.join(self.proj, "CLAUDE.local.md")
        write(local, "a\n")
        self.assertEqual(self.cli("init")[0], 0)
        listed = core_mod._git(core_mod.REPO_DIR, "ls-tree", "-r",
                               "--name-only", "HEAD")
        self.assertIn("files/CLAUDE.local.md", listed)
        write(local, "b\n")
        write(os.path.join(self.proj, "CLAUDE.md"), "x\n")
        self.assertEqual(self.cli("snap")[0], 0)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.isfile(local), "live file became a directory")
        self.assertEqual(slurp(local), b"a\n")

    def test_gitignore_inside_a_watched_dir_does_not_hide_files(self):
        # a .gitignore under .claude is mirrored into the snapshots repo,
        # where it would hide its siblings from `git add`
        notes = os.path.join(self.proj, ".claude", "notes.md")
        write(os.path.join(self.proj, ".claude", ".gitignore"), "*\n")
        write(notes, "notes v1\n")
        self.assertEqual(self.cli("init")[0], 0)
        listed = core_mod._git(core_mod.REPO_DIR, "ls-tree", "-r",
                               "--name-only", "HEAD")
        self.assertIn("files/.claude/notes.md", listed)
        self.assertIn("files/.claude/settings.json", listed)
        write(notes, "clobbered\n")
        self.assertEqual(self.cli("snap")[0], 0)
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(slurp(notes), b"notes v1\n")

    def test_snapshot_records_what_each_watched_path_is(self):
        self.assertEqual(self.cli("init")[0], 0)
        m = core_mod._manifest_at("HEAD")
        self.assertEqual(m["kinds"], {"CLAUDE.md": "file", ".claude": "dir"})

    def test_snap_refuses_to_commit_what_git_did_not_record(self):
        # belt and braces: if git skips a mirrored file for any reason the
        # snapshot must not be committed as if it were complete
        self.assertEqual(self.cli("init")[0], 0)
        real_git = core_mod._git

        def lossy(repo, *args):
            if args[:1] == ("add",):
                real_git(repo, "add", "-A")        # honours ignore rules
                real_git(repo, "rm", "-q", "--cached", "--",
                         "files/CLAUDE.md")
                return ""
            return real_git(repo, *args)

        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        head = real_git(core_mod.REPO_DIR, "rev-parse", "HEAD")
        with mock.patch.object(core_mod, "_git", lossy):
            code, out, err = self.cli("snap")
        self.assertEqual(code, 2)
        self.assertIn("incomplete", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(real_git(core_mod.REPO_DIR, "rev-parse", "HEAD"), head)


class TestRestoreVerifiesTheSnapshotFirst(Base):
    """HIGH 1/2: restore must never replace a live file with a directory (or
    drop it) unless the snapshot provably holds what it replaces; the check
    runs before the working tree is touched."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, "CLAUDE.md")
        self.dot = os.path.join(self.proj, ".claude")
        self.assertEqual(self.cli("init")[0], 0)
        write(self.claude, "v2 live\n")
        write(os.path.join(self.dot, "settings.json"), '{"v": 2}\n')
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)

    def _live_state(self):
        state = {}
        for root, dirs, files in os.walk(self.proj):
            for n in files:
                p = os.path.join(root, n)
                state[p] = slurp(p)
            for n in dirs:
                state[os.path.join(root, n)] = None
        return state

    def test_torn_snapshot_is_refused_and_nothing_is_touched(self):
        # a snapshot whose manifest says CLAUDE.md + .claude but whose tree
        # has neither (what two racing snaps used to commit)
        torn = self.forge_commit("torn", drop_from_tree=["CLAUDE.md", ".claude"])
        before = self._live_state()
        code, out, err = self.cli("restore", torn[:7])
        self.assertEqual(code, 1, out + err)
        self.assertIn("refusing to restore", out)
        self.assertIn("CLAUDE.md", out)
        self.assertEqual(self._live_state(), before)
        self.assertTrue(os.path.isfile(self.claude))
        self.assertEqual(self.trash_entries(), [])

    def test_one_missing_file_refuses_the_whole_restore(self):
        torn = self.forge_commit("half", drop_from_tree=["CLAUDE.md"])
        before = self._live_state()
        code, out, err = self.cli("restore", torn[:7])
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self._live_state(), before)   # .claude not swapped
        self.assertEqual(self.trash_entries(), [])

    def test_undo_onto_a_torn_snapshot_is_refused_too(self):
        torn = self.forge_commit("torn", drop_from_tree=["CLAUDE.md"])
        write(self.claude, "dirty\n")
        code, out, err = self.cli("undo")
        # undo steps from the pre-undo snapshot onto the torn one
        self.assertEqual(code, 1, out + err)
        self.assertTrue(os.path.isfile(self.claude))
        self.assertEqual(slurp(self.claude), b"dirty\n")
        self.assertEqual(self.trash_entries(), [])

    def test_type_mismatch_is_refused(self):
        def kinds_swapped(m):
            m["kinds"] = {"CLAUDE.md": "dir", ".claude": "file"}
        bad = self.forge_commit("mismatch", edit_manifest=kinds_swapped)
        before = self._live_state()
        code, out, err = self.cli("restore", bad[:7])
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self._live_state(), before)

    def test_old_snapshot_without_kinds_never_empties_a_live_file(self):
        # snapshots taken before "kinds" existed (and before ignored files
        # were stored) cannot say whether a path with no content was an
        # empty directory or an ignored file: with live content at stake the
        # restore is refused instead of guessing.
        def legacy(m):
            m.pop("kinds", None)
        old = self.forge_commit("legacy", drop_from_tree=["CLAUDE.md"],
                                edit_manifest=legacy)
        before = self._live_state()
        code, out, err = self.cli("restore", old[:7])
        self.assertEqual(code, 1, out + err)
        self.assertIn("CLAUDE.md", out)
        self.assertEqual(self._live_state(), before)
        self.assertEqual(self.trash_entries(), [])

    def test_old_snapshot_without_kinds_still_restores_when_complete(self):
        def legacy(m):
            m.pop("kinds", None)
        good = self.forge_commit("legacy-ok", edit_manifest=legacy)
        write(self.claude, "later\n")
        code, out, err = self.cli("restore", good[:7])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(self.claude), b"v2 live\n")

    def test_empty_directory_snapshot_is_still_restorable(self):
        # a watched dir that is (effectively) empty has a manifest entry
        # and no tree entry: that is legitimate when the manifest says dir
        shutil.rmtree(self.dot)
        os.makedirs(self.dot)
        write(os.path.join(self.dot, "debug.log"), "noise\n")   # ignored name
        self.assertEqual(self.cli("snap", "-m", "empty-dot")[0], 0)
        empty = self.log()[0]["sha"]
        self.assertEqual(core_mod._manifest_at(empty)["kinds"][".claude"], "dir")
        write(os.path.join(self.dot, "settings.json"), "{}\n")
        code, out, err = self.cli("restore", empty[:7])
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.isdir(self.dot))
        self.assertFalse(os.path.exists(os.path.join(self.dot, "settings.json")))


class TestSnapshotsAreSerialised(Base):
    """HIGH 2: sessionsteward processes (hooks of several projects, the watch
    daemon, you) share one snapshots repo and used to race on it: index.lock
    failures, tracebacks, and torn snapshots whose tree lacked files."""

    def _snap_child(self, *, root=None, message="waiter", timeout="30"):
        env = self.child_env()
        env["SESSIONSTEWARD_LOCK_TIMEOUT"] = timeout
        return subprocess.Popen(
            [sys.executable, "-m", "sessionsteward", "--root",
             root or self.proj, "snap", "-m", message],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT,
            env=env)

    def _finish(self, p, timeout=60):
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            self.fail("child did not finish: " + err.decode("utf-8", "replace"))
        return p.returncode, out.decode("utf-8", "replace"), \
            err.decode("utf-8", "replace")

    def test_a_second_snap_waits_for_the_lock(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        with core_mod.locked():
            p = self._snap_child()
            self.addCleanup(lambda: p.poll() is None and p.kill())
            time.sleep(1.5)
            self.assertIsNone(p.poll(), "snap ran while the lock was held")
        rc, out, err = self._finish(p)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.log()[0]["message"], "waiter")

    def test_lock_timeout_is_a_clean_error(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        with core_mod.locked():
            rc, out, err = self._finish(self._snap_child(timeout="0.5"))
        self.assertEqual(rc, 2, err)
        self.assertIn("error:", err)
        self.assertIn("another sessionsteward", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual([s["message"] for s in self.log()],
                         ["initial snapshot"])

    def test_lock_is_reentrant_and_released_afterwards(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        self.assertEqual(self.cli("init")[0], 0)
        with core_mod.locked():
            with core_mod.locked():     # nested calls (undo -> snap) must not deadlock
                pass
            rc, _, err = self._finish(self._snap_child(timeout="0.3"))
            self.assertEqual(rc, 2, "the outer lock must still be held")
        with self.assertRaises(ZeroDivisionError):
            with core_mod.locked():
                1 / 0                   # an error inside must release the lock
        rc, _, err = self._finish(self._snap_child(timeout="10"))
        self.assertEqual(rc, 0, err)

    def test_lock_file_is_not_inside_the_snapshots_work_tree(self):
        # `git add -A` would commit a lock file that lives next to files/
        self.assertEqual(self.cli("init")[0], 0)
        with core_mod.locked():
            path = core_mod.lock_path()
        self.assertTrue(os.path.exists(path))
        self.assertNotEqual(os.path.dirname(path),
                            os.path.realpath(core_mod.REPO_DIR))
        self.assertNotIn(os.path.basename(path), os.listdir(core_mod.REPO_DIR))

    def test_concurrent_snaps_never_fail_or_tear(self):
        roots = []
        for i in range(3):
            root = os.path.join(self._tmp, f"p{i}")
            write(os.path.join(root, "CLAUDE.md"), f"base {i}\n")
            write(os.path.join(root, ".claude", "settings.json"), "{}\n")
            roots.append(root)
            rc = self.run_child("init", root=root).returncode
            self.assertEqual(rc, 0)
        rounds = 6
        for n in range(rounds):
            procs = []
            for root in roots:
                write(os.path.join(root, "CLAUDE.md"), f"edit {n}\n" * (n + 1))
                procs.append(self._snap_child(root=root, message=f"r{n}"))
            for p in procs:
                rc, out, err = self._finish(p, timeout=120)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
        for root in roots:
            snaps = self.log(root)
            self.assertEqual(len(snaps), rounds + 1, root)
            for s in snaps:
                manifest = core_mod._manifest_at(s["sha"])
                self.assertTrue(manifest, s)
                self.assertEqual(core_mod._snapshot_problems(
                    s["sha"], manifest), [], s)

    def test_windows_locking_path(self):
        # msvcrt.locking is what Windows has: a non-blocking attempt that
        # raises EACCES while the region is held, then LK_UNLCK on release
        import errno
        calls = []

        class FakeMsvcrt:
            LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

            def locking(self, fd, mode, nbytes):
                calls.append((mode, nbytes))
                if mode == self.LK_NBLCK and \
                        [c for c in calls if c[0] == self.LK_NBLCK][2:] == []:
                    raise OSError(errno.EACCES, "Permission denied")

        with mock.patch.object(core_mod, "fcntl", None), \
                mock.patch.object(core_mod, "msvcrt", FakeMsvcrt()):
            with core_mod.locked(timeout=10):
                pass
        self.assertEqual(calls, [(2, 1), (2, 1), (2, 1), (0, 1)])

    def test_every_command_that_writes_holds_the_lock_for_its_whole_run(self):
        # `undo` reads the log, snapshots a dirty state and restores: another
        # process must not get in between those steps. The same goes for the
        # read-modify-write of config.json in `watch --add`.
        import contextlib
        writers = [["init"], ["snap"], ["precompact"], ["undo"],
                   ["restore", "abc1234"], ["watch", "--add", "NOTES.md"],
                   ["watch", "--remove", "AGENTS.md"]]
        readers = [["log"], ["diff"], ["diet"], ["install-hooks"]]
        for argv, must_lock in ([(a, True) for a in writers]
                                + [(a, False) for a in readers]):
            events = []

            @contextlib.contextmanager
            def recording_lock(timeout=None):
                events.append("lock")
                try:
                    yield
                finally:
                    events.append("unlock")

            def command(_args):
                events.append("command")
                return 0

            names = {"watch": "cmd_watch_edit"}
            attr = names.get(argv[0], "cmd_" + argv[0].replace("-", "_"))
            with mock.patch.object(core_mod, "locked", recording_lock), \
                    mock.patch.object(cli_mod, attr, command):
                self.cli(*argv)
            expected = (["lock", "command", "unlock"] if must_lock
                        else ["command"])
            self.assertEqual(events, expected, argv)

    def test_unusable_locking_is_reported_not_ignored(self):
        import errno

        class BrokenFcntl:
            LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

            def flock(self, fd, op):
                raise OSError(errno.ENOLCK, "No locks available")

        with mock.patch.object(core_mod, "fcntl", BrokenFcntl()):
            with self.assertRaises(core_mod.StewardError) as cm:
                with core_mod.locked(timeout=1):
                    self.fail("must not run unlocked")
        self.assertIn("lock", str(cm.exception))


class TestWatchSurvivesContention(Base):
    """MED 6: one index.lock collision made the `watch` daemon exit 2."""

    def test_watch_retries_after_a_failed_snapshot(self):
        self.assertEqual(self.cli("init")[0], 0)
        ticks = {"n": 0, "snaps": 0, "sleeps": 0}

        def changing(_paths):
            ticks["n"] += 1
            return {"x": ticks["n"]}

        def flaky_snap(root, message):
            ticks["snaps"] += 1
            if ticks["snaps"] == 1:
                raise core_mod.GitError("git add -A -f failed: index.lock")
            return "a" * 40

        def stop_after_a_few(_seconds):
            ticks["sleeps"] += 1
            if ticks["sleeps"] > 3:
                raise KeyboardInterrupt

        with mock.patch.object(core_mod, "file_mtimes", changing), \
                mock.patch.object(core_mod, "snap", flaky_snap), \
                mock.patch.object(cli_mod, "time",
                                  mock.Mock(sleep=stop_after_a_few)):
            code, out, err = self.cli("watch", "--interval", "0.01")
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(ticks["snaps"], 2)
        self.assertIn("will retry", err)
        self.assertIn("change detected -> snapshot aaaaaaa", out)
        self.assertNotIn("Traceback", err)


class TestCorruptManifestsAreSkipped(Base):
    """HIGH 2: a snapshot with a 0-byte manifest.json (what the race
    committed) made log/undo/snap fail for EVERY project."""

    def _commit_empty_manifest(self):
        repo = core_mod.REPO_DIR
        write(os.path.join(repo, "manifest.json"), "")
        core_mod._git(repo, "add", "-f", "manifest.json")
        core_mod._git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-q", "--allow-empty", "-m", "zero-byte")
        return core_mod._git(repo, "rev-parse", "HEAD").strip()

    def test_other_projects_keep_working(self):
        other = os.path.join(self._tmp, "other")
        write(os.path.join(other, "CLAUDE.md"), "other\n")
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(self.cli("init", root=other)[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "v2\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        bad = self._commit_empty_manifest()
        for root in (self.proj, other):
            code, out, err = self.cli("log", root=root)
            self.assertEqual(code, 0, err)
            self.assertNotIn("zero-byte", out)
        write(os.path.join(self.proj, "CLAUDE.md"), "v3\n")
        code, out, err = self.cli("snap", "-m", "v3")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")), b"v2\n")
        # restoring the broken snapshot itself is refused, cleanly
        code, out, err = self.cli("restore", bad[:7])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("manifest", out + err)


class TestInstallHooksWriteNeverClobbers(Base):
    """HIGH 3: `install-hooks --write` on a settings.json that is not strict
    JSON (BOM, comment, trailing comma) fell back to {} and rewrote the file
    with only `hooks`, then said "existing settings preserved"."""

    def setUp(self):
        super().setUp()
        self.settings = os.path.join(self._tmp, "claude", "settings.json")
        os.makedirs(os.path.dirname(self.settings))
        p = mock.patch.object(cli_mod, "CLAUDE_SETTINGS", self.settings)
        p.start()
        self.addCleanup(p.stop)

    def put(self, raw: bytes) -> None:
        write(self.settings, raw, binary=True)

    def listing(self):
        return sorted(os.listdir(os.path.dirname(self.settings)))

    def assert_refused(self, raw: bytes):
        self.put(raw)
        code, out, err = self.cli("install-hooks", "--write")
        self.assertEqual(code, 2, out + err)
        self.assertEqual(slurp(self.settings), raw, "file must be untouched")
        self.assertEqual(self.listing(), ["settings.json"])  # no .bak / temp
        self.assertIn("error:", err)
        self.assertIn("settings.json", err)
        self.assertIn("not modified", err)
        self.assertNotIn("preserved", out + err)
        self.assertNotIn("Traceback", err)
        # what to add by hand is printed
        payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertIn("PreCompact", payload["hooks"])
        self.assertIn("SessionStart", payload["hooks"])

    def test_trailing_comma_is_refused(self):
        self.assert_refused(b'{"model":"opus",}')

    def test_comment_is_refused(self):
        self.assert_refused(b'{\n  // keep this\n  "model": "opus"\n}\n')

    def test_truncated_file_is_refused(self):
        self.assert_refused(b'{"model": "opus", "permissions": {')

    def test_invalid_utf8_is_refused(self):
        self.assert_refused(b'{"note": "\xff\xfe"}')

    def test_not_an_object_is_refused(self):
        self.assert_refused(b'["model", "opus"]')

    def test_hooks_of_an_unexpected_shape_are_refused(self):
        self.assert_refused(b'{"model": "opus", "hooks": ["x"]}')
        self.assert_refused(b'{"hooks": {"PreCompact": {"a": 1}}}')

    def test_bom_is_read_and_all_settings_survive(self):
        self.put(b'\xef\xbb\xbf{"model": "opus", "note": "Kal\xc3\xa1s"}')
        code, out, err = self.cli("install-hooks", "--write")
        self.assertEqual(code, 0, out + err)
        raw = slurp(self.settings)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        merged = json.loads(raw.decode("utf-8"))
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["note"], "Kal\u00e1s")
        self.assertIn("Kal\u00e1s".encode("utf-8"), raw)  # raw UTF-8, not escapes
        self.assertIn("PreCompact", merged["hooks"])

    def test_previous_file_is_kept_as_bak_and_nothing_else_is_left(self):
        original = b'{\n    "model": "opus",\n    "theme": "dark"\n}\n'
        self.put(original)
        code, out, err = self.cli("install-hooks", "--write")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.listing(), ["settings.json", "settings.json.bak"])
        self.assertEqual(slurp(self.settings + ".bak"), original)
        merged = json.loads(slurp(self.settings).decode("utf-8"))
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["theme"], "dark")
        # the user's own indentation is kept
        self.assertIn(b'\n    "model": "opus"', slurp(self.settings))

    def test_rerun_changes_nothing(self):
        self.put(b'{"model": "opus"}')
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        first, bak = slurp(self.settings), slurp(self.settings + ".bak")
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertEqual(slurp(self.settings), first)
        self.assertEqual(slurp(self.settings + ".bak"), bak)   # still the original
        self.assertEqual(bak, b'{"model": "opus"}')

    def test_crlf_line_endings_are_kept(self):
        self.put(b'{\r\n  "model": "opus"\r\n}\r\n')
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        raw = slurp(self.settings)
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_missing_and_empty_files_are_created(self):
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertIn("PreCompact", json.loads(slurp(self.settings))["hooks"])
        self.put(b"  \n")
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertIn("PreCompact", json.loads(slurp(self.settings))["hooks"])

    @unittest.skipIf(not hasattr(os, "symlink") or os.name == "nt",
                     "POSIX symlinks")
    def test_symlinked_settings_stays_a_symlink(self):
        # dotfile managers link ~/.claude/settings.json into a repo
        real = os.path.join(self._tmp, "dotfiles", "settings.json")
        write(real, '{"model": "opus"}')
        os.remove(self.settings) if os.path.exists(self.settings) else None
        os.symlink(real, self.settings)
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(os.path.realpath(self.settings),
                         os.path.realpath(real))
        merged = json.loads(slurp(real))
        self.assertEqual(merged["model"], "opus")
        self.assertIn("PreCompact", merged["hooks"])

    def test_other_tools_hooks_are_untouched(self):
        mine = {"matcher": "Bash", "hooks": [{"type": "command",
                                              "command": "echo mine"}]}
        self.put(json.dumps({"hooks": {"PreToolUse": [mine]}}).encode())
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        merged = json.loads(slurp(self.settings))
        self.assertEqual(merged["hooks"]["PreToolUse"], [mine])


class TestGitEnvironmentCannotRedirectOrBreakUs(Base):
    """MED 4: the user's git environment and configuration leaked into the
    snapshots repo. An absolute GIT_DIR made `init` commit into THEIR
    repository; commit.gpgsign=true or a hook that rejects the commit left a
    repo without a commit that repo_ready() accepted, so every later command
    failed."""

    def _user_repo(self):
        user = os.path.join(self._tmp, "userrepo")
        write(os.path.join(user, "notes.txt"), "mine\n")
        plain_git(user, "init", "-q")
        plain_git(user, "add", "-A")
        plain_git(user, "-c", "user.name=u", "-c", "user.email=u@u",
                  "commit", "-q", "-m", "mine")
        return user

    def test_an_absolute_git_dir_does_not_make_init_commit_into_your_repo(self):
        user = self._user_repo()
        before = plain_git(user, "rev-parse", "HEAD").strip()
        os.environ["GIT_DIR"] = os.path.join(user, ".git")
        os.environ["GIT_WORK_TREE"] = user
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(plain_git(user, "rev-parse", "HEAD").strip(), before)
        self.assertEqual(plain_git(user, "rev-list", "--count", "--all").strip(),
                         "1")
        self.assertNotIn("sessionsteward", plain_git(user, "config", "--local",
                                                     "--list"))
        self.assertTrue(os.path.isdir(os.path.join(core_mod.REPO_DIR, ".git")))
        self.assertEqual([s["message"] for s in self.log()],
                         ["initial snapshot"])

    def test_the_index_file_variable_is_not_written(self):
        # a git pre-commit hook runs with GIT_INDEX_FILE pointing at the
        # index of the commit in progress
        index = os.path.join(self._tmp, "their-index")
        os.environ["GIT_INDEX_FILE"] = index
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        self.assertEqual(self.cli("snap")[0], 0)
        self.assertFalse(os.path.exists(index), "their index was used")

    def test_author_committer_and_date_variables_do_not_leak_in(self):
        os.environ.update({
            "GIT_AUTHOR_NAME": "Mallory", "GIT_AUTHOR_EMAIL": "m@example.org",
            "GIT_COMMITTER_NAME": "Mallory",
            "GIT_COMMITTER_EMAIL": "m@example.org",
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
        self.assertEqual(self.cli("init")[0], 0)
        who = plain_git(core_mod.REPO_DIR, "log", "-1", "--date=short",
                        "--format=%an|%ae|%cn|%ce|%ad|%cd").strip()
        self.assertTrue(who.startswith(
            "sessionsteward|sessionsteward@local|sessionsteward|"
            "sessionsteward@local|"), who)
        self.assertNotIn("2001-01-01", who)

    def test_config_injected_through_the_environment_is_ignored(self):
        # what `git -c k=v` or GIT_CONFIG_COUNT hand down to a child
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "commit.gpgsign"
        os.environ["GIT_CONFIG_VALUE_0"] = "true"
        os.environ["GIT_CONFIG_PARAMETERS"] = "'core.hooksPath=/nonexistent'"
        self.gitconfig("[gpg]\n\tprogram = "
                       + _fwd(os.path.join(self._tmp, "no-such-gpg")) + "\n")
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(core_mod.repo_ready())

    def test_commit_signing_in_your_config_does_not_brick_the_repo(self):
        self.gitconfig("[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n"
                       "[gpg]\n\tprogram = "
                       + _fwd(os.path.join(self._tmp, "no-such-gpg")) + "\n")
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(core_mod.repo_ready())
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        code, out, err = self.cli("snap", "-m", "second")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([s["message"] for s in self.log()],
                         ["second", "initial snapshot"])

    def _rejecting_hooks(self, directory):
        marker = os.path.join(self._tmp, "hook-ran")
        for name in ("pre-commit", "commit-msg", "post-commit",
                     "reference-transaction", "pre-auto-gc"):
            script(os.path.join(directory, name),
                   f"echo {name} >> '{marker}'\nexit 1\n")
        return marker

    def _init_and_snap_with_hooks_around(self, marker):
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        code, out, err = self.cli("snap", "-m", "second")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.exists(marker), "a git hook of yours ran")
        self.assertTrue(core_mod.repo_ready())

    @unittest.skipIf(os.name == "nt", "shell-script hooks")
    def test_a_hooks_path_in_your_config_is_never_run(self):
        hooks = os.path.join(self._tmp, "hooks")
        marker = self._rejecting_hooks(hooks)
        self.gitconfig(f"[core]\n\thooksPath = {_fwd(hooks)}\n")
        self._init_and_snap_with_hooks_around(marker)

    @unittest.skipIf(os.name == "nt", "shell-script hooks")
    def test_hooks_copied_from_a_template_dir_are_never_run(self):
        template = os.path.join(self._tmp, "template")
        marker = self._rejecting_hooks(os.path.join(template, "hooks"))
        self.gitconfig(f"[init]\n\ttemplateDir = {_fwd(template)}\n")
        self._init_and_snap_with_hooks_around(marker)
        copied = os.path.join(core_mod.REPO_DIR, ".git", "hooks", "pre-commit")
        self.assertTrue(os.path.exists(copied), "the template was not used")

    @unittest.skipIf(os.name == "nt", "shell-script hook")
    def test_an_fsmonitor_in_your_config_is_not_started(self):
        marker = os.path.join(self._tmp, "fsmonitor-ran")
        mon = script(os.path.join(self._tmp, "fsmon"),
                     f"echo ran >> '{marker}'\nprintf '\\0'\n")
        self.gitconfig(f"[core]\n\tfsmonitor = {_fwd(mon)}\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        self.assertEqual(self.cli("snap")[0], 0)
        self.assertFalse(os.path.exists(marker), "your fsmonitor ran")

    def test_a_repo_that_never_got_its_first_commit_is_finished(self):
        # what a rejected or failed first commit leaves behind
        os.makedirs(core_mod.REPO_DIR)
        plain_git(core_mod.REPO_DIR, "init", "-q")
        self.assertFalse(core_mod.repo_ready())
        code, out, err = self.cli("log")
        self.assertEqual(code, 0, out + err)
        self.assertIn("no snapshots yet", out)
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(core_mod.repo_ready())
        self.assertEqual([s["message"] for s in self.log()],
                         ["initial snapshot"])

    def test_snap_finishes_a_repo_without_its_first_commit_by_itself(self):
        os.makedirs(core_mod.REPO_DIR)
        plain_git(core_mod.REPO_DIR, "init", "-q")
        code, out, err = self.cli("snap", "-m", "straight in")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([s["message"] for s in self.log()], ["straight in"])

    def test_a_dot_git_that_is_not_a_repository_is_reported_not_skipped(self):
        # HOME is itself a repository (dotfiles) and the snapshots dir inside
        # it has a broken .git: git must not wander up and use HOME's repo
        dotfiles = self._home
        write(os.path.join(dotfiles, ".bashrc"), "# mine\n")
        plain_git(dotfiles, "init", "-q")
        plain_git(dotfiles, "add", ".bashrc")
        plain_git(dotfiles, "-c", "user.name=u", "-c", "user.email=u@u",
                  "commit", "-q", "-m", "dotfiles")
        os.makedirs(os.path.join(core_mod.REPO_DIR, ".git"))   # empty
        for argv in (("snap",), ("init",)):
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 2, out + err)
            self.assertIn("error:", err)
            self.assertIn("snapshots", err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(plain_git(dotfiles, "rev-list", "--count",
                                   "--all").strip(), "1")
        self.assertEqual(plain_git(dotfiles, "ls-files").split(), [".bashrc"])
        self.assertEqual(plain_git(dotfiles, "diff", "--cached",
                                   "--name-only").strip(), "")

    def test_your_diff_settings_do_not_change_what_diff_prints(self):
        self.gitconfig("[color]\n\tui = always\n[diff]\n\tnoprefix = true\n"
                       "\tcontext = 0\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "# Project\n\nshort\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        code, out, err = self.cli("diff")
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertIn("-Be helpful.", out.splitlines())
        self.assertIn("+short", out.splitlines())

    @unittest.skipIf(os.name == "nt", "shell-script diff driver")
    def test_an_external_diff_tool_is_not_used(self):
        marker = os.path.join(self._tmp, "extdiff-ran")
        ext = script(os.path.join(self._tmp, "extdiff"),
                     f"echo ran >> '{marker}'\necho EXTERNAL\n")
        self.gitconfig(f"[diff]\n\texternal = {_fwd(ext)}\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "# Project\n\nshort\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        code, out, err = self.cli("diff")
        self.assertEqual(code, 0, err)
        self.assertNotIn("EXTERNAL", out)
        self.assertIn("+short", out)
        self.assertFalse(os.path.exists(marker), "your diff tool ran")


@unittest.skipIf(os.name == "nt", "POSIX permission bits")
class TestRestoreKeepsTheExecutableBit(Base):
    """MED 5: a hook script lost its execute bit in the snapshot (the mirror
    was a plain content copy), so `undo` brought it back mode 644."""

    def _exec(self, path):
        return os.stat(path).st_mode & 0o111

    def test_a_script_inside_a_watched_dir_keeps_its_bit_through_undo(self):
        hook = os.path.join(self.proj, ".claude", "hooks", "pre.sh")
        write(hook, "#!/bin/sh\necho hi\n")
        os.chmod(hook, 0o755)
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(
            core_mod._tree_blobs("HEAD")[".claude/hooks/pre.sh"], "100755")
        write(os.path.join(self.proj, ".claude", "x.md"), "b\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".claude",
                                                     "x.md")))
        self.assertTrue(self._exec(hook), "undo dropped the execute bit")
        self.assertEqual(slurp(hook), b"#!/bin/sh\necho hi\n")

    def test_a_plain_file_is_not_made_executable(self):
        plain = os.path.join(self.proj, ".claude", "notes.md")
        write(plain, "notes\n")
        os.chmod(plain, 0o644)
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(
            core_mod._tree_blobs("HEAD")[".claude/notes.md"], "100644")
        write(os.path.join(self.proj, ".claude", "x.md"), "b\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertFalse(self._exec(plain))

    def test_a_deleted_watched_script_comes_back_executable(self):
        run = os.path.join(self.proj, "run.sh")
        write(run, "#!/bin/sh\n")
        os.chmod(run, 0o755)
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(self.cli("watch", "--add", "run.sh")[0], 0)
        self.assertEqual(self.cli("snap", "-m", "with run.sh")[0], 0)
        with_run = self.log()[0]["sha"]
        os.remove(run)
        code, out, err = self.cli("restore", with_run[:7])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(run), b"#!/bin/sh\n")
        self.assertTrue(self._exec(run), "the restored script is not executable")

    def test_the_mirror_stays_writable(self):
        # the bit is copied, nothing else: a read-only source must not make
        # a read-only mirror the next snap cannot replace
        ro = os.path.join(self.proj, ".claude", "ro.sh")
        write(ro, "x\n")
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        self.assertEqual(self.cli("init")[0], 0)
        mirror = os.path.join(core_mod.REPO_DIR, "files", ".claude", "ro.sh")
        self.assertTrue(os.stat(mirror).st_mode & 0o200)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        self.assertEqual(self.cli("snap")[0], 0)


class TestTrashPruning(Base):
    """MED 5: prune_trash ordered entries by mtime, but a moved path keeps
    the mtime it had, so the directory a restore had just trashed could be
    the one it deleted again - the user's only copy."""

    def _entry(self, name, mtime):
        p = os.path.join(core_mod.TRASH_DIR, name)
        write(p, name)
        os.utime(p, (mtime, mtime))
        return p

    def test_entries_are_ordered_by_when_they_were_trashed(self):
        now = int(time.time())
        made = []
        for i in range(25):
            # the newest names carry the OLDEST mtimes
            made.append(self._entry(f"f.{now - 1000 + i}", 1_500_000_000 - i))
        removed = core_mod.prune_trash(core_mod.TRASH_DIR)
        self.assertEqual(len(removed), 5)
        self.assertEqual(sorted(removed), sorted(made[:5]))
        self.assertEqual(len(os.listdir(core_mod.TRASH_DIR)), core_mod.TRASH_KEEP)

    def test_collision_counters_and_dotted_names_order_correctly(self):
        now = int(time.time())
        newer = self._entry(f"CLAUDE.md.{now}.3", 1_000_000_000)
        mid = self._entry(f"CLAUDE.md.{now}.2", 1_000_000_001)
        dotted = self._entry(f"v1.2.{now - 5}", 2_000_000_000)
        older = self._entry(f"CLAUDE.md.{now - 50}", 2_000_000_001)
        removed = core_mod.prune_trash(core_mod.TRASH_DIR, keep=2)
        self.assertEqual(sorted(removed), sorted([dotted, older]))
        self.assertTrue(os.path.exists(newer) and os.path.exists(mid))

    def test_protected_entries_are_never_removed(self):
        now = int(time.time())
        mine = self._entry(f"mine.{now - 9999}", 1_000_000_000)
        self._entry(f"a.{now - 1}", 1_000_000_000)
        self._entry(f"b.{now - 2}", 1_000_000_000)
        removed = core_mod.prune_trash(core_mod.TRASH_DIR, keep=1,
                                       protect=[mine])
        self.assertTrue(os.path.exists(mine))
        self.assertEqual(len(removed), 1)

    def test_a_restore_never_deletes_the_directory_it_just_trashed(self):
        self.assertEqual(self.cli("init")[0], 0)
        dot = os.path.join(self.proj, ".claude")
        write(os.path.join(dot, "precious.md"), "only copy\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        # twenty entries already in the trash, all trashed earlier than now
        # but with fresh mtimes; the directory about to be trashed is old
        now = int(time.time())
        for i in range(core_mod.TRASH_KEEP):
            self._entry(f"junk.{now - 100 - i}", now)
        os.utime(dot, (1_500_000_000, 1_500_000_000))
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        kept = [n for n in self.trash_entries() if n.startswith(".claude.")]
        self.assertEqual(len(kept), 1, self.trash_entries())
        self.assertEqual(slurp(os.path.join(core_mod.TRASH_DIR, kept[0],
                                            "precious.md")), b"only copy\n")
        self.assertEqual(len(self.trash_entries()), core_mod.TRASH_KEEP)


class TestRestoreSurvivesAMoveThatFails(Base):
    """MED 5: a trash move that raised OSError aborted the restore half-way
    (or, after the live directory was already moved, left the path empty)."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, "CLAUDE.md")
        self.dot = os.path.join(self.proj, ".claude")
        self.assertEqual(self.cli("init")[0], 0)

    def _failing_move_for(self, victim):
        real = core_mod._move_to_trash

        def move(ap, trash_dir):
            if ap == victim:
                raise PermissionError(13, "Permission denied", ap)
            return real(ap, trash_dir)
        return mock.patch.object(core_mod, "_move_to_trash", move)

    def test_a_file_that_cannot_be_swept_is_reported_and_the_rest_restored(self):
        agents = os.path.join(self.proj, "AGENTS.md")
        write(agents, "agents\n")
        write(self.claude, "v2\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        with self._failing_move_for(agents):
            code, out, err = self.cli("undo")
        self.assertEqual(code, 1, out + err)
        self.assertIn("skipped AGENTS.md", out)
        self.assertIn("trash", out)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(slurp(agents), b"agents\n")          # untouched
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")

    def test_a_directory_that_cannot_be_trashed_is_left_as_it_was(self):
        write(os.path.join(self.dot, "extra.md"), "extra\n")
        write(self.claude, "v2\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        with self._failing_move_for(self.dot):
            code, out, err = self.cli("undo")
        self.assertEqual(code, 1, out + err)
        self.assertIn("skipped .claude", out)
        self.assertEqual(slurp(os.path.join(self.dot, "extra.md")), b"extra\n")
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")
        self.assertEqual([n for n in os.listdir(self.proj)
                          if n.startswith(".steward-restore")], [])

    def test_a_swap_that_fails_after_the_move_puts_the_directory_back(self):
        write(os.path.join(self.dot, "extra.md"), "extra\n")
        write(self.claude, "v2\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        real_rename, calls = os.rename, []

        def flaky(src, dst, *a, **kw):
            if dst == self.dot and not calls:
                calls.append(src)
                raise PermissionError(13, "Permission denied", dst)
            return real_rename(src, dst, *a, **kw)

        with mock.patch.object(core_mod.os, "rename", flaky):
            code, out, err = self.cli("undo")
        self.assertEqual(code, 1, out + err)
        self.assertTrue(calls, "the swap was never attempted")
        self.assertIn("skipped .claude", out)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(slurp(os.path.join(self.dot, "extra.md")), b"extra\n")
        self.assertEqual([n for n in self.trash_entries()
                          if n.startswith(".claude.")], [],
                         "the old directory must not be left in the trash")


class TestOddCharactersDoNotBreakTheSnapshotLists(Base):
    """MED 6: git's output was split with str.splitlines(), which also breaks
    on U+2028, U+2029, U+0085 and the C0 separators. A snapshot message with
    one of them made `log`, `undo` and `snap` raise ValueError for ever; a
    file name with one (or a quote, a backslash, a newline) mis-parsed the
    tree listings."""

    SEPARATORS = ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d",
                  "\x1e")

    def test_a_message_with_a_line_separator_does_not_break_log_or_undo(self):
        self.assertEqual(self.cli("init")[0], 0)
        for i, sep in enumerate(self.SEPARATORS):
            write(os.path.join(self.proj, "CLAUDE.md"), f"v{i}\n")
            code, out, err = self.cli("snap", "-m", f"a{sep}b")
            self.assertEqual(code, 0, repr((out, err)))
            self.assertEqual(self.log()[0]["message"], f"a{sep}b")
        code, out, err = self.cli("log")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.log()), len(self.SEPARATORS) + 1)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         f"v{len(self.SEPARATORS) - 2}\n".encode())

    def _odd_names(self):
        names = ["a\u2028b.md", "c\u2029d.md", "e\x85f.md"]
        if os.name != "nt":      # not valid in Windows file names
            names += ['q"uote.md', "back\\slash.md", "new\nline.md"]
        return names

    def test_odd_file_names_are_listed_exactly(self):
        names = self._odd_names()
        for n in names:
            write(os.path.join(self.proj, ".claude", n), "x" * 100)
        self.assertEqual(self.cli("init")[0], 0)
        head = self.log()[0]["sha"]
        tree = core_mod.snapshot_tree(head)
        for n in names:
            self.assertIn(os.path.join(self.proj, ".claude", n), tree, n)
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0)
        for n in names:     # the shrink guard names them exactly, too
            write(os.path.join(self.proj, ".claude", n), "x")
        code, out, err = self.cli("snap", "-m", "shrunk")
        self.assertEqual(code, 0, err)
        for n in names:
            self.assertIn(f"guard: .claude/{n} shrank 100 -> 1 bytes", out, n)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        for n in names:
            self.assertEqual(slurp(os.path.join(self.proj, ".claude", n)),
                             b"x" * 100, n)

    def test_the_memory_dir_of_a_repo_whose_path_has_a_separator_is_found(self):
        repo = os.path.join(self._tmp, "re\u2028po")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        plain_git(repo, "init", "-q")
        mem = os.path.join(self._home, ".claude", "projects",
                           core_mod._project_slug(repo), "memory")
        os.makedirs(mem)
        self.assertEqual(core_mod.auto_memory_dir(sub), mem)


class TestDiffShowsEveryChangedLine(Base):
    """MED 6: `diff` dropped every line that merely STARTED with `--` or
    `++` (a removed markdown rule or front-matter fence, "---"), and any
    line after a U+2028; it also cut off at 200 lines without a word."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, "CLAUDE.md")

    def diff_lines(self):
        code, out, err = self.cli("diff")
        self.assertEqual(code, 0, err)
        return out.split("\n")

    def test_removed_and_added_lines_that_look_like_headers_are_shown(self):
        write(self.claude, "---\nname: x\n---\n--flag\nkeep\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(self.claude, "keep\n++added\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        lines = self.diff_lines()
        for want in ("----", "-name: x", "---flag", "+++added"):
            self.assertIn(want, lines)
        self.assertEqual(lines.count("----"), 2)
        self.assertNotIn("--- a/files/CLAUDE.md", lines)
        self.assertFalse([ln for ln in lines if ln.startswith("+++ b/")])

    def test_every_changed_file_is_shown_and_no_header_is(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(self.claude, "# Project\n\nrewritten\n")
        write(os.path.join(self.proj, ".claude", "settings.json"), '{"x": 2}\n')
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        lines = self.diff_lines()
        self.assertIn("+rewritten", lines)
        self.assertIn('+{"x": 2}', lines)
        self.assertIn('-{"x": 1}', lines)
        self.assertEqual([ln for ln in lines if ln[:1] in "+-"
                          and ln.startswith(("--- ", "+++ "))], [])

    def test_a_line_separator_inside_a_line_does_not_split_it(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(self.claude, "# Project\n\nleft\u2028right\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        self.assertIn("+left\u2028right", self.diff_lines())

    def test_crlf_files_print_without_a_stray_carriage_return(self):
        write(self.claude, "one\r\ntwo\r\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(self.claude, "one\r\nTWO\r\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        code, out, err = self.cli("diff")
        self.assertNotIn("\r", out)
        self.assertIn("-two", out.split("\n"))
        self.assertIn("+TWO", out.split("\n"))

    def test_a_change_with_no_lines_to_show_is_not_called_no_changes(self):
        binary = os.path.join(self.proj, ".claude", "logo.bin")
        write(binary, b"\x00\x01\x02" * 10, binary=True)
        self.assertEqual(self.cli("init")[0], 0)
        write(binary, b"\x00\x09\x08" * 10, binary=True)
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        code, out, err = self.cli("diff")
        self.assertEqual(code, 0, err)
        self.assertIn("no line changes", out)
        self.assertNotIn("(no changes)", out)
        head, older = [s["sha"] for s in self.log()[:2]]
        code, out, err = self.cli("diff", head, head)
        self.assertEqual(out, "(no changes)\n")

    def test_a_long_diff_says_it_was_cut_off(self):
        write(self.claude, "".join(f"old {i}\n" for i in range(150)))
        self.assertEqual(self.cli("init")[0], 0)
        write(self.claude, "".join(f"new {i}\n" for i in range(150)))
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        lines = [ln for ln in self.diff_lines() if ln]
        self.assertEqual(len(lines), 201)
        self.assertEqual(lines[-1],
                         "... 100 more changed line(s) not shown")


class TestNoTracebackForWhatTheSystemRefuses(Base):
    """MED 7: the README promised "never a traceback", but an unwritable
    home directory (or a disk that is full, a file where a directory
    belongs, git that cannot be started) raised OSError out of main()."""

    def _block_state_dir(self):
        # ~/.sessionsteward is a FILE: nothing can be created under it, and
        # (unlike chmod) that binds root, too
        dot = os.path.dirname(core_mod.REPO_DIR)
        os.makedirs(os.path.dirname(dot), exist_ok=True)
        write(dot, "in the way")
        return dot

    def test_commands_that_write_report_a_one_line_error(self):
        self._block_state_dir()
        for argv in (("init",), ("snap",), ("precompact",), ("undo",),
                     ("restore", "abc1234"), ("watch", "--add", "NOTES.md")):
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 1, (argv, out, err))
            self.assertTrue(err.startswith("error: "), (argv, err))
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", out + err)

    def test_the_real_command_line_too(self):
        self._block_state_dir()
        p = self.run_child("snap")
        self.assertEqual(p.returncode, 1, p.stderr)
        err = p.stderr.decode("utf-8", "replace")
        self.assertTrue(err.startswith("error: "), err)
        self.assertNotIn("Traceback", err)

    def test_read_only_commands_still_work_without_a_state_dir(self):
        self._block_state_dir()
        code, out, err = self.cli("log")
        self.assertEqual(code, 0, err)
        self.assertIn("no snapshots yet", out)

    def test_git_that_cannot_be_started_is_a_git_error(self):
        def refuse(cmd, *a, **kw):
            raise PermissionError(13, "Permission denied", "git")

        with mock.patch.object(core_mod.subprocess, "run", refuse):
            code, out, err = self.cli("init")
        self.assertEqual(code, 2, out + err)
        self.assertIn("error: cannot run git", err)
        self.assertNotIn("Traceback", err)

    def test_a_subprocess_error_is_a_one_line_error(self):
        def odd(cmd, *a, **kw):
            raise subprocess.SubprocessError("something odd")

        with mock.patch.object(core_mod.subprocess, "run", odd):
            code, out, err = self.cli("init")
        self.assertEqual(code, 1, out + err)
        self.assertIn("error: something odd", err)
        self.assertNotIn("Traceback", err)

    def test_a_reader_that_went_away_is_still_quiet(self):
        self.assertEqual(self.cli("init")[0], 0)

        class Gone(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        with mock.patch.object(sys, "stdout", Gone()), \
                redirect_stderr(err):
            code = cli_mod.main(["--root", self.proj, "log"])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")


@unittest.skipIf(not hasattr(os, "mkfifo"), "needs named pipes (POSIX)")
class TestSpecialFilesAreSkippedNotRead(Base):
    """MED 7: a named pipe inside a watched directory blocked the first
    open() for ever (`log`, `undo`, the watch daemon) or raised
    SpecialFileError (`snap`)."""

    def setUp(self):
        super().setUp()
        self.dot = os.path.join(self.proj, ".claude")
        self.claude = os.path.join(self.proj, "CLAUDE.md")

    def child(self, *args):
        try:
            return self.run_child(*args, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail(f"`{' '.join(args)}` hung on a named pipe")

    def test_a_pipe_in_a_watched_dir_does_not_hang_or_break_anything(self):
        self.assertEqual(self.cli("init")[0], 0)
        os.mkfifo(os.path.join(self.dot, "events.fifo"))
        sock = os.path.join(self.dot, "agent.sock")
        import socket
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        try:
            s.bind(sock)
        except OSError:
            sock = None        # path too long for AF_UNIX on this system
        write(os.path.join(self.dot, "notes.md"), "notes\n")
        for argv in (("log",), ("snap", "-m", "with a pipe"), ("diet",),
                     ("diff",)):
            p = self.child(*argv)
            self.assertEqual(p.returncode, 0, (argv, p.stderr))
            self.assertNotIn(b"Traceback", p.stderr)
        listed = core_mod._git(core_mod.REPO_DIR, "ls-tree", "-r",
                               "--name-only", "HEAD")
        self.assertIn("files/.claude/notes.md", listed)
        self.assertNotIn("fifo", listed)
        self.assertNotIn("sock", listed)
        # and the position check, which reads every live file, terminates
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0)
        write(os.path.join(self.dot, "notes.md"), "changed\n")
        p = self.child("undo")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(slurp(os.path.join(self.dot, "notes.md")), b"notes\n")
        # a snapshot cannot hold a pipe, so the swapped-out directory (with
        # the pipe in it) is what the trash keeps
        kept = [n for n in os.listdir(core_mod.TRASH_DIR)
                if n.startswith(".claude.")]
        self.assertEqual(len(kept), 1, kept)
        self.assertTrue(os.path.exists(os.path.join(core_mod.TRASH_DIR, kept[0],
                                                    "events.fifo")))

    def test_a_pipe_where_a_watched_file_belongs_is_never_written_to(self):
        self.assertEqual(self.cli("init")[0], 0)
        first = self.log()[0]["sha"]
        os.remove(self.claude)
        os.mkfifo(self.claude)
        p = self.child("restore", first[:7])
        self.assertEqual(p.returncode, 1, p.stderr)
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("skipped CLAUDE.md", out)
        self.assertIn("not a regular file", out)
        import stat as stat_mod
        self.assertTrue(stat_mod.S_ISFIFO(os.lstat(self.claude).st_mode))
