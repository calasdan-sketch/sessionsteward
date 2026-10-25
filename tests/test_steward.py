"""Session steward tests. Uses temp dirs; never touches ~/.sessionsteward."""

import datetime
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

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod
from sessionsteward import diet as diet_mod

# Permission bits do not bind root (common in containers/CI), so the
# read-only regressions are only observable as an ordinary user.
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def patch_env(test: unittest.TestCase):
    tmp = tempfile.mkdtemp()
    test._old_repo = core_mod.REPO_DIR
    test._old_cfg = core_mod.CONFIG_PATH
    test._old_trash = core_mod.TRASH_DIR
    core_mod.REPO_DIR = os.path.join(tmp, "snapshots")
    core_mod.CONFIG_PATH = os.path.join(tmp, "config.json")
    core_mod.TRASH_DIR = os.path.join(tmp, "trash")
    # DEFAULT_WATCH includes ~/.claude/CLAUDE.md: point ~ at a temp home
    # so tests never read or restore the real user-level memory file.
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    for var in ("HOME", "USERPROFILE"):
        old = os.environ.get(var)
        os.environ[var] = home
        test.addCleanup(_restore_var, var, old)
    test._home = home
    test._tmp = tmp
    test.addCleanup(restore_env, test)


def _restore_var(var, old):
    if old is None:
        os.environ.pop(var, None)
    else:
        os.environ[var] = old


def restore_env(test: unittest.TestCase):
    core_mod.REPO_DIR = test._old_repo
    core_mod.CONFIG_PATH = test._old_cfg
    core_mod.TRASH_DIR = test._old_trash


def make_project() -> str:
    proj = tempfile.mkdtemp()
    with open(os.path.join(proj, "CLAUDE.md"), "w") as f:
        f.write("# Project\n\nBe helpful.\n")
    os.makedirs(os.path.join(proj, ".claude"))
    with open(os.path.join(proj, ".claude", "settings.json"), "w") as f:
        f.write('{"x": 1}\n')
    return proj


def run_cli(*argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_mod.main(["--root", argv[0], *argv[1:]] if argv and
                            not argv[0].startswith("-") else list(argv))
    return code, buf.getvalue()


class TestSteward(unittest.TestCase):
    def setUp(self):
        patch_env(self)
        self.proj = make_project()

    def _cli(self, *args):
        return self._cli_root(self.proj, *args)

    def _cli_root(self, root, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_mod.main(["--root", root, *args])
        return code, buf.getvalue()

    def test_init(self):
        code, out = self._cli("init")
        self.assertEqual(code, 0)
        self.assertTrue(core_mod.repo_ready())
        self.assertIn("snapshots repo", out)

    def test_snap_log(self):
        self._cli("init")  # takes the initial snapshot
        self._cli("snap", "-m", "first")  # identical content -> skipped
        with open(os.path.join(self.proj, "CLAUDE.md"), "a") as f:
            f.write("more\n")
        self._cli("snap", "-m", "second")
        self._cli("snap", "-m", "third")  # no changes -> skipped
        snaps = core_mod.log()
        messages = [s["message"] for s in snaps]
        self.assertIn("initial snapshot", messages)
        self.assertIn("second", messages)
        self.assertNotIn("first", messages)  # empty snaps skipped
        self.assertNotIn("third", messages)

    def test_undo(self):
        self._cli("init")
        self._cli("snap", "-m", "v1")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("# Project\n\nA single bullet.\n")  # the horror story
        self._cli("snap", "-m", "v2")
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())

    def test_restore_specific(self):
        self._cli("init")
        self._cli("snap", "-m", "v1")
        sha1 = core_mod.log()[0]["sha"]
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("ruined\n")
        self._cli("snap", "-m", "v2")
        self._cli("restore", sha1[:7])
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())

    def test_diff(self):
        self._cli("init")
        self._cli("snap", "-m", "v1")
        with open(os.path.join(self.proj, "CLAUDE.md"), "a") as f:
            f.write("Extra line.\n")
        self._cli("snap", "-m", "v2")
        code, out = self._cli("diff")
        self.assertEqual(code, 0)
        self.assertIn("Extra line.", out)

    def test_precompact(self):
        self._cli("init")
        with open(os.path.join(self.proj, "CLAUDE.md"), "a") as f:
            f.write("more\n")
        code, out = self._cli("precompact")
        self.assertEqual(code, 0)
        self.assertIn("pre-compaction", core_mod.log()[0]["message"])

    def test_watch_add(self):
        self._cli("init")
        code, _ = self._cli("watch", "--add", "NOTES.md", "~/extra.md")
        self.assertEqual(code, 0)
        cfg = core_mod.load_config()
        self.assertIn("NOTES.md", cfg["watch"])
        self.assertIn("~/extra.md", cfg["watch"])

    def test_trash_on_undo(self):
        self._cli("init")
        self._cli("snap", "-m", "v1")
        newfile = os.path.join(self.proj, "SCRATCH.md")
        with open(newfile, "w") as f:
            f.write("scratch\n")
        # not watched (not in watch list) -> undo shouldn't touch it;
        # make it watched first
        self._cli("watch", "--add", "SCRATCH.md")
        self._cli("snap", "-m", "v2")
        os.remove(newfile)
        with open(os.path.join(self.proj, "CLAUDE.md"), "w") as f:
            f.write("changed\n")
        self._cli("snap", "-m", "v3")
        # undo to v2: SCRATCH.md didn't exist in v1... restore v2 sha
        snaps = core_mod.log()
        v2 = next(s for s in snaps if s["message"] == "v2")["sha"]
        restored = core_mod.restore(
            v2, trash_dir=os.path.join(self._tmp, "trash"))
        self.assertTrue(any("SCRATCH.md" in r for r in restored))
        self.assertTrue(os.path.exists(newfile))

    def test_diet(self):
        self._cli("init")
        dup = "This is a long duplicated instruction line for testing purposes.\n"
        with open(os.path.join(self.proj, "CLAUDE.md"), "a") as f:
            f.write(dup)
        with open(os.path.join(self.proj, "AGENTS.md"), "w") as f:
            f.write("# Agents\n\n" + dup)
        report = diet_mod.analyze(self.proj)
        self.assertGreater(report["total_chars"], 0)
        self.assertGreater(report["n_duplicates"], 0)
        rendered = diet_mod.render(report)
        self.assertIn("Duplicated content", rendered)
        self.assertIn("tokens", rendered)

    def test_default_watch_user_and_local_memory(self):
        # ~/.claude/CLAUDE.md (user memory) and CLAUDE.local.md were not
        # watched by default, so compaction/overwrites there had no undo.
        user_mem = os.path.join(self._home, ".claude", "CLAUDE.md")
        os.makedirs(os.path.dirname(user_mem))
        with open(user_mem, "w") as f:
            f.write("user memory\n")
        local_mem = os.path.join(self.proj, "CLAUDE.local.md")
        with open(local_mem, "w") as f:
            f.write("local memory\n")
        paths = core_mod.watched_paths(self.proj)
        self.assertIn(os.path.abspath(user_mem), paths)
        self.assertIn(os.path.abspath(local_mem), paths)

    def test_mtimes(self):
        m = core_mod.file_mtimes(core_mod.watched_paths(self.proj))
        claude = os.path.join(self.proj, "CLAUDE.md")
        self.assertIn(claude, m)
        # dir watch includes nested files
        self.assertIn(os.path.join(self.proj, ".claude", "settings.json"), m)

    # ---- v1-critical regression tests ----

    def test_first_undo_no_crash(self):
        # init writes a manifest-less empty commit; the first undo used to
        # hit it and crash with a traceback.
        self._cli("init")
        self._cli("snap", "-m", "v1")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("changed after v1\n")
        code, out = self._cli("undo")  # must not raise
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())

    def test_undo_no_snapshots(self):
        # init in a project without any watched file: no snapshots exist
        # at all -> clean message, no crash
        empty = tempfile.mkdtemp()
        code, out = self._cli_root(empty, "init")
        self.assertEqual(code, 0)
        self.assertIn("nothing to snapshot yet", out)
        code, out = self._cli_root(empty, "undo")  # must not raise
        self.assertNotEqual(code, 0)
        self.assertIn("nothing to restore", out)

    def test_undo_single_snapshot_clean(self):
        # one snapshot, worktree clean -> nothing to undo to, no pre-undo
        # noise commit
        self._cli("init")
        self._cli("snap", "-m", "v1")
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        self.assertIn("only one snapshot", out)
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertNotIn("pre-undo", messages)

    def test_restore_init_commit_clean_message(self):
        self._cli("init")
        init_sha = core_mod.log()[-1]["sha"]  # manifest-less init commit
        self.assertEqual(core_mod.log()[-1]["message"], "init")
        code, out = self._cli("restore", init_sha[:7])  # must not raise
        self.assertNotEqual(code, 0)
        self.assertIn("nothing to restore", out)

    def test_undo_scoped_to_project(self):
        proj_a = self.proj
        proj_b = make_project()
        claude_a = os.path.join(proj_a, "CLAUDE.md")
        claude_b = os.path.join(proj_b, "CLAUDE.md")

        self._cli_root(proj_a, "init")
        self._cli_root(proj_a, "snap", "-m", "A-v1")
        with open(claude_a, "w") as f:
            f.write("A edited\n")
        self._cli_root(proj_a, "snap", "-m", "A-v2")

        self._cli_root(proj_b, "snap", "-m", "B-v1")
        with open(claude_b, "w") as f:
            f.write("B edited\n")
        self._cli_root(proj_b, "snap", "-m", "B-v2")

        # log in A must not show B's snapshots (A-v1 is a no-op: init
        # already snapshotted that content)
        _, out = self._cli_root(proj_a, "log")
        self.assertIn("A-v2", out)
        self.assertIn("initial snapshot", out)
        self.assertNotIn("B-v", out)

        # undo in A restores A's file only; B is untouched
        code, _ = self._cli_root(proj_a, "undo")
        self.assertEqual(code, 0)
        with open(claude_a) as f:
            self.assertIn("Be helpful.", f.read())
        with open(claude_b) as f:
            self.assertIn("B edited", f.read())
        # clean-state undo must not leave a spurious pre-undo commit
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=proj_a)]
        self.assertNotIn("pre-undo", messages)

    def test_cross_project_restore_fails_clean(self):
        proj_a = self.proj
        proj_b = make_project()
        self._cli_root(proj_a, "init")
        self._cli_root(proj_a, "snap", "-m", "A-v1")
        self._cli_root(proj_b, "snap", "-m", "B-v1")
        b_sha = next(s["sha"] for s in
                     core_mod.log(50, project_root=proj_b)
                     if s["message"] == "B-v1")

        claude_a = os.path.join(proj_a, "CLAUDE.md")
        claude_b = os.path.join(proj_b, "CLAUDE.md")
        with open(claude_a, "w") as f:
            f.write("A sentinel\n")

        code, out = self._cli_root(proj_a, "restore", b_sha[:7])
        self.assertNotEqual(code, 0)  # clean failure, no traceback
        self.assertIn("refusing cross-project restore", out)
        # nothing clobbered on either side
        with open(claude_a) as f:
            self.assertIn("A sentinel", f.read())
        with open(claude_b) as f:
            self.assertIn("Be helpful.", f.read())

    def test_undo_trash_sweep_scoped_to_project(self):
        # Regression: restore() swept extra files against the GLOBAL HEAD
        # manifest, so undo in A trashed B's watched files when B was the
        # last project snapshotted. The sweep must be scoped to the
        # current project.
        proj_a = self.proj
        proj_b = make_project()
        # B has an extra watched file A doesn't: AGENTS.md is in
        # DEFAULT_WATCH, so it lands in B's manifest but not A's.
        agents_b = os.path.join(proj_b, "AGENTS.md")
        with open(agents_b, "w") as f:
            f.write("# B agents\n")
        # A watches files only: the dir-restore path legitimately moves
        # A's own .claude dir through trash, which would make the strict
        # trash-empty assertion below vacuous.
        shutil.rmtree(os.path.join(proj_a, ".claude"))
        claude_a = os.path.join(proj_a, "CLAUDE.md")

        self._cli_root(proj_a, "init")
        self._cli_root(proj_a, "snap", "-m", "A-v1")
        with open(claude_a, "w") as f:
            f.write("A edited\n")
        self._cli_root(proj_a, "snap", "-m", "A-v2")
        self._cli_root(proj_b, "snap", "-m", "B-v1")  # HEAD now belongs to B

        code, _ = self._cli_root(proj_a, "undo")
        self.assertEqual(code, 0)
        # A's restore still correct ...
        with open(claude_a) as f:
            self.assertIn("Be helpful.", f.read())
        # ... and B is completely untouched: file still on disk, nothing
        # of B's in trash.
        self.assertTrue(os.path.exists(agents_b),
                        "B's watched file was trashed by A's undo")
        with open(agents_b) as f:
            self.assertIn("# B agents", f.read())
        trashed = []
        for root, _, files in os.walk(core_mod.TRASH_DIR):
            trashed.extend(os.path.join(root, fn) for fn in files)
        self.assertEqual(trashed, [], f"A's undo trashed files: {trashed}")

    def test_version_flag(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                cli_mod.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("sessionsteward", buf.getvalue())

    def test_dir_restore_uses_trash(self):
        self._cli("init")
        self._cli("snap", "-m", "v1")
        newfile = os.path.join(self.proj, ".claude", "newfile.txt")
        with open(newfile, "w") as f:
            f.write("new\n")
        self._cli("snap", "-m", "v2")
        v1 = core_mod.log(50, project_root=self.proj)[-1]["sha"]  # init snap
        trash = os.path.join(self._tmp, "trash")
        restored = core_mod.restore(v1, trash_dir=trash,
                                    project_root=self.proj)
        # snapshot state restored ...
        self.assertFalse(os.path.exists(newfile))
        self.assertTrue(os.path.exists(
            os.path.join(self.proj, ".claude", "settings.json")))
        # ... and the old directory landed in trash, not rmtree'd
        trashed = []
        for root, _, files in os.walk(trash):
            trashed.extend(os.path.join(root, fn) for fn in files)
        self.assertTrue(any("newfile.txt" in t for t in trashed),
                        f"old dir not found in trash: {trashed}")
        self.assertTrue(any("moved" in r for r in restored))

    def test_dir_restore_failed_extract_leaves_live_dir(self):
        # Regression: the live dir was moved to trash BEFORE checking the
        # extract worked; a bad/failed extract then lost it entirely.
        from unittest import mock
        self._cli("init")
        self._cli("snap", "-m", "v1")
        live = os.path.join(self.proj, ".claude")
        newfile = os.path.join(live, "newfile.txt")
        with open(newfile, "w") as f:
            f.write("live work\n")
        self._cli("snap", "-m", "v2")
        v1 = core_mod.log(50, project_root=self.proj)[-1]["sha"]  # init snap
        real_run = core_mod.subprocess.run

        def broken_archive(cmd, *a, **kw):
            if "archive" in cmd:
                return core_mod.subprocess.CompletedProcess(
                    cmd, 0, b"not a tar stream", b"")
            return real_run(cmd, *a, **kw)

        with mock.patch.object(core_mod.subprocess, "run", broken_archive):
            restored = core_mod.restore(
                v1, trash_dir=os.path.join(self._tmp, "trash"),
                project_root=self.proj)
        with open(newfile) as f:
            self.assertEqual(f.read(), "live work\n")
        self.assertTrue(any(r.startswith("skipped") for r in restored))
        self.assertFalse(os.path.exists(os.path.join(self._tmp, "trash")))
        # no staging dir left behind in the project
        self.assertEqual([n for n in os.listdir(self.proj)
                          if n.startswith(".steward-restore-")], [])

    def test_crlf_restores_byte_identical(self):
        # Windows users commonly have core.autocrlf=true globally; the
        # snapshots repo must still store and restore bytes verbatim.
        gitcfg = os.path.join(self._tmp, "gitconfig")
        with open(gitcfg, "w") as f:
            f.write("[core]\n\tautocrlf = true\n")
        old = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = gitcfg
        self.addCleanup(_restore_var, "GIT_CONFIG_GLOBAL", old)
        claude = os.path.join(self.proj, "CLAUDE.md")
        nested = os.path.join(self.proj, ".claude", "notes.md")
        want = {claude: b"# Project\r\n\r\nBe helpful.\r\n",
                nested: b"mixed\r\nendings\nhere\r\n"}
        for p, data in want.items():
            with open(p, "wb") as f:
                f.write(data)
        self._cli("init")
        self._cli("snap", "-m", "v1")
        for p in want:
            with open(p, "wb") as f:
                f.write(b"clobbered\n")
        self._cli("snap", "-m", "v2")
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        for p, data in want.items():
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data, p)

    def test_guard_warns_on_big_shrink(self):
        # One snapshot that drops >50% of a watched file (the compaction
        # / "remember this" overwrite) must be flagged in snap and log.
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("rule\n" * 30)
        self._cli("init")
        self._cli("snap", "-m", "v1")
        with open(claude, "a") as f:
            f.write("one more rule\n")  # growth: no warning
        _, out = self._cli("snap", "-m", "v2")
        self.assertNotIn("guard", out)
        with open(claude, "w") as f:
            f.write("- a single bullet\n")
        _, out = self._cli("snap", "-m", "v3")
        self.assertIn("guard: CLAUDE.md shrank", out)
        _, out = self._cli("log")
        lines = out.splitlines()
        self.assertIn("v3", lines[0])
        self.assertIn("guard: CLAUDE.md shrank", lines[1])
        self.assertEqual(sum("guard" in ln for ln in lines), 1)

    def test_redo_after_undo(self):
        # dirty worktree -> undo auto-snaps "pre-undo" first, so the undo
        # is reversible: restoring the pre-undo snapshot redoes it.
        self._cli("init")
        claude = os.path.join(self.proj, "CLAUDE.md")
        self._cli("snap", "-m", "v1")
        with open(claude, "w") as f:
            f.write("unsaved work\n")  # dirty, never snapshotted
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())  # back to v1
        pre_undo = next(s["sha"] for s in
                        core_mod.log(50, project_root=self.proj)
                        if s["message"] == "pre-undo")
        code, _ = self._cli("restore", pre_undo[:7])
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("unsaved work", f.read())  # redo worked

    def test_redo_after_clean_undo(self):
        # clean worktree -> no pre-undo needed; redo is restore of the
        # snapshot that was HEAD before the undo.
        self._cli("init")
        claude = os.path.join(self.proj, "CLAUDE.md")
        self._cli("snap", "-m", "v1")
        with open(claude, "w") as f:
            f.write("v2 content\n")
        self._cli("snap", "-m", "v2")
        v2 = next(s["sha"] for s in
                  core_mod.log(50, project_root=self.proj)
                  if s["message"] == "v2")
        self._cli("undo")
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())  # back to v1
        code, _ = self._cli("restore", v2[:7])
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("v2 content", f.read())  # redo worked

    def test_install_hooks_print(self):
        code, out = self._cli("install-hooks")
        self.assertEqual(code, 0)
        payload = json.loads(out.split("\n\n")[0])
        precompact = payload["hooks"]["PreCompact"][0]["hooks"][0]["command"]
        sessstart = payload["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(precompact, core_mod.HOOK_PRECOMPACT_CMD)
        self.assertIn("session start", sessstart)
        self.assertIn("~/.claude/settings.json", out)

    def test_install_hooks_write_merges(self):
        settings_path = os.path.join(self._tmp, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"theme": "dark",
                       "hooks": {"Stop": [{"matcher": "",
                                           "hooks": [{"type": "command",
                                                      "command": "echo stop"}]}]}},
                      f)
        core_mod.write_hooks_file(settings_path)
        with open(settings_path) as f:
            merged = json.load(f)
        # existing settings preserved
        self.assertEqual(merged["theme"], "dark")
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["command"],
                         "echo stop")
        # our hooks added
        # (whatever form the hook command has: the installed console script
        # when there is one next to this Python, `python -I -m ...` if not)
        cmds = json.dumps(merged["hooks"])
        self.assertIn(json.dumps(core_mod.HOOK_PRECOMPACT_CMD)[1:-1], cmds)
        self.assertIn(json.dumps(core_mod.HOOK_SESSIONSTART_CMD)[1:-1], cmds)
        self.assertIn("session start", cmds)
        # idempotent: second run adds no duplicates
        before = json.dumps(merged["hooks"], sort_keys=True)
        core_mod.write_hooks_file(settings_path)
        with open(settings_path) as f:
            after = json.dumps(json.load(f)["hooks"], sort_keys=True)
        self.assertEqual(before, after)

    def test_hook_cmd_uses_absolute_interpreter(self):
        # A bare `sessionsteward` fails silently in hook shells (no alias,
        # no venv). Hooks must run this interpreter by absolute path.
        import shlex
        import sys
        for cmd in (core_mod.HOOK_PRECOMPACT_CMD,
                    core_mod.HOOK_SESSIONSTART_CMD):
            self.assertFalse(cmd.startswith("sessionsteward"), cmd)
            first = shlex.split(cmd)[0]
            self.assertTrue(os.path.isabs(first), cmd)
            script = core_mod._console_script()
            if script:   # installed: the console script, never `python -m`
                self.assertEqual(first, script)
                self.assertNotIn("-m sessionsteward", cmd)
            else:        # run from a checkout: isolated mode, no cwd on path
                self.assertEqual(shlex.split(cmd)[:4],
                                 [sys.executable, "-I", "-m", "sessionsteward"])

    def test_hook_cmd_quotes_paths_with_spaces(self):
        from unittest import mock
        exe = r"C:\Program Files\Python 3\python.exe"
        with mock.patch.object(core_mod.sys, "executable", exe), \
                mock.patch.object(core_mod.os, "name", "nt"), \
                mock.patch.object(core_mod, "_console_script",
                                  return_value=None):
            self.assertEqual(core_mod._self_cmd(),
                             f'"{exe}" -I -m sessionsteward')
        script = r"C:\Program Files\Python 3\Scripts\sessionsteward.exe"
        with mock.patch.object(core_mod.os, "name", "nt"), \
                mock.patch.object(core_mod, "_console_script",
                                  return_value=script):
            self.assertEqual(core_mod._self_cmd(), f'"{script}"')

    def test_install_hooks_upgrades_legacy_bare_command(self):
        settings_path = os.path.join(self._tmp, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"hooks": {"PreCompact": [{"matcher": "", "hooks": [
                {"type": "command",
                 "command": "sessionsteward precompact"}]}]}}, f)
        core_mod.write_hooks_file(settings_path)
        with open(settings_path) as f:
            pre = json.load(f)["hooks"]["PreCompact"]
        self.assertEqual(core_mod._hook_commands(pre),
                         [core_mod.HOOK_PRECOMPACT_CMD])

    # ---- round-2 release-panel regressions ----

    def test_identical_resnap_later_makes_no_commit(self):
        # The manifest carried a timestamp, so snapping identical content
        # seconds apart still committed. With hooks installed: good state
        # -> agent nukes CLAUDE.md -> PreCompact snap -> SessionStart snap
        # (same nuked content, new commit) -> `undo` stepped back one to
        # the PreCompact snap, i.e. restored the NUKED state.
        self._cli("init")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "a") as f:
            f.write("Another rule.\n")
        self._cli("snap", "-m", "good")
        with open(claude, "w") as f:
            f.write("- one bullet\n")
        clock = [time.time()]

        def seconds_later():
            clock[0] += 30
            return clock[0]

        with mock.patch.object(core_mod.time, "time", seconds_later):
            self._cli("precompact")
            self._cli("snap", "-m", "session start")
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertEqual(messages, ["pre-compaction snapshot", "good",
                                    "initial snapshot"])
        self.assertNotIn("at", core_mod._manifest_at("HEAD"))
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())

    def test_non_ascii_filename_matches_snapshot(self):
        # git quotes non-ASCII paths ("\316\232...") unless
        # core.quotePath=false, so a .claude/commands/Καλάς.md made every
        # snapshot look "dirty": undo took a pre-undo snap of the current
        # state and then restored it — a permanent no-op.
        cmds = os.path.join(self.proj, ".claude", "commands")
        os.makedirs(cmds)
        greek = os.path.join(cmds, "Καλάς.md")
        with open(greek, "w", encoding="utf-8") as f:
            f.write("rule\n" * 30)
        self._cli("init")
        self._cli("snap", "-m", "v1")
        head = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertTrue(core_mod.worktree_matches_snapshot(head, self.proj))
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("nuked\n")
        with open(greek, "w", encoding="utf-8") as f:
            f.write("- bullet\n")
        _, out = self._cli("snap", "-m", "v2")
        # guard names the file verbatim, not octal-escaped
        self.assertIn("guard: .claude/commands/Καλάς.md shrank", out)
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())
        with open(greek, encoding="utf-8") as f:
            self.assertEqual(f.read(), "rule\n" * 30)
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertNotIn("pre-undo", messages)

    def test_watch_add_keeps_defaults(self):
        # `watch --add NOTES.md` REPLACED the default list: CLAUDE.md and
        # the user memory file were silently unprotected.
        with open(os.path.join(self.proj, "NOTES.md"), "w") as f:
            f.write("notes\n")
        user_mem = os.path.join(self._home, ".claude", "CLAUDE.md")
        os.makedirs(os.path.dirname(user_mem))
        with open(user_mem, "w") as f:
            f.write("user memory\n")
        self._cli("init")
        code, out = self._cli("watch", "--add", "NOTES.md")
        self.assertEqual(code, 0)
        self.assertIn("CLAUDE.md", out)
        names = {os.path.basename(p) for p in core_mod.watched_paths(self.proj)}
        self.assertEqual(names, {"CLAUDE.md", ".claude", "NOTES.md"})
        self.assertIn(os.path.abspath(user_mem),
                      core_mod.watched_paths(self.proj))
        self.assertEqual(core_mod.watch_list(),
                         core_mod.DEFAULT_WATCH + ["NOTES.md"])
        # adding a path twice, or a default, never duplicates
        self._cli("watch", "--add", "NOTES.md", "CLAUDE.md")
        self.assertEqual(core_mod.watch_list(),
                         core_mod.DEFAULT_WATCH + ["NOTES.md"])

    def test_watch_remove_drops_default_or_extra(self):
        self._cli("init")
        self._cli("watch", "--add", "NOTES.md")
        code, out = self._cli("watch", "--remove", "AGENTS.md", "NOTES.md")
        self.assertEqual(code, 0)
        self.assertNotIn("AGENTS.md", out)
        self.assertNotIn("NOTES.md", out)
        self.assertEqual(core_mod.watch_list(),
                         [p for p in core_mod.DEFAULT_WATCH if p != "AGENTS.md"])
        with open(os.path.join(self.proj, "AGENTS.md"), "w") as f:
            f.write("agents\n")
        self.assertNotIn(os.path.join(self.proj, "AGENTS.md"),
                         core_mod.watched_paths(self.proj))
        # and a removed default can be re-added
        self._cli("watch", "--add", "AGENTS.md")
        self.assertEqual(core_mod.watch_list(), core_mod.DEFAULT_WATCH)
        self.assertNotIn("unwatch", core_mod.load_config())

    def test_restore_skips_unwritable_target_and_exits_1(self):
        # A target that cannot be written (here: a directory now sits
        # where the file was; on a 0444 file it was a PermissionError)
        # tracebacked out of restore half-way. It must be reported as
        # skipped, the other files restored, and the CLI exit 1.
        notes = os.path.join(self.proj, "NOTES.md")
        with open(notes, "w") as f:
            f.write("notes v1\n")
        self._cli("init")
        self._cli("watch", "--add", "NOTES.md")
        self._cli("snap", "-m", "v1")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("nuked\n")
        os.remove(notes)
        os.makedirs(notes)
        with open(os.path.join(notes, "inner.txt"), "w") as f:
            f.write("now a dir\n")
        self._cli("snap", "-m", "v2")
        code, out = self._cli("undo")  # must not raise
        self.assertEqual(code, 1)
        self.assertRegex(out, r"skipped .*NOTES\.md: .*\(left untouched\)")
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())  # the rest was restored
        self.assertTrue(os.path.isfile(os.path.join(notes, "inner.txt")))

    @unittest.skipIf(IS_ROOT, "root ignores permission bits")
    def test_read_only_file_restore_skipped(self):
        agents = os.path.join(self.proj, "AGENTS.md")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(agents, "w") as f:
            f.write("agents v1\n")
        self._cli("init")
        self._cli("snap", "-m", "v1")
        for p in (claude, agents):
            with open(p, "w") as f:
                f.write("nuked\n")
        self._cli("snap", "-m", "v2")
        os.chmod(agents, 0o444)
        self.addCleanup(os.chmod, agents, 0o644)
        code, out = self._cli("undo")  # used to traceback (PermissionError)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"skipped .*AGENTS\.md: .*\(left untouched\)")
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())
        with open(agents) as f:
            self.assertEqual(f.read(), "nuked\n")

    @unittest.skipIf(IS_ROOT, "root ignores permission bits")
    def test_read_only_dir_in_watched_dir_resnaps(self):
        # copytree mirrored a 0555 dir / 0444 file with its modes, so the
        # next snap's rmtree of the mirror crashed with PermissionError.
        ro = os.path.join(self.proj, ".claude", "ro")
        os.makedirs(ro)
        with open(os.path.join(ro, "f.txt"), "w") as f:
            f.write("x\n")
        os.chmod(os.path.join(ro, "f.txt"), 0o444)
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        self._cli("init")
        self._cli("snap", "-m", "v1")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("nuked\n")
        code, out = self._cli("snap", "-m", "v2")  # must not raise
        self.assertEqual(code, 0)
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        # init already took the first snapshot, so "v1" was a no-op
        self.assertEqual(messages[:2], ["v2", "initial snapshot"])
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())
        with open(os.path.join(ro, "f.txt")) as f:
            self.assertEqual(f.read(), "x\n")

    def test_crlf_survives_gitattributes(self):
        # `-c core.autocrlf=false` was not enough: a .gitattributes inside
        # the watched dir (mirrored into the repo) or a global
        # core.attributesFile saying `* text=auto` still normalised CRLF
        # to LF on add, so undo handed back LF files.
        attrs = os.path.join(self._tmp, "attributes")
        with open(attrs, "w") as f:
            f.write("* text=auto\n")
        gitcfg = os.path.join(self._tmp, "gitconfig")
        with open(gitcfg, "w") as f:
            # Backslashes are escapes in git config values: write the
            # path with forward slashes (git accepts them on Windows).
            f.write(f"[core]\n\tattributesFile = {attrs.replace(os.sep, '/')}\n")
        old = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = gitcfg
        self.addCleanup(_restore_var, "GIT_CONFIG_GLOBAL", old)
        with open(os.path.join(self.proj, ".claude", ".gitattributes"),
                  "w") as f:
            f.write("* text=auto\n")
        claude = os.path.join(self.proj, "CLAUDE.md")
        nested = os.path.join(self.proj, ".claude", "notes.md")
        want = {claude: b"# Project\r\n\r\nBe helpful.\r\n",
                nested: b"mixed\r\nendings\nhere\r\n"}
        for p, data in want.items():
            with open(p, "wb") as f:
                f.write(data)
        self._cli("init")
        with open(os.path.join(core_mod.REPO_DIR, ".git", "info",
                               "attributes")) as f:
            self.assertEqual(f.read(), core_mod.ATTRIBUTES)
        self._cli("snap", "-m", "v1")
        for p in want:
            with open(p, "wb") as f:
                f.write(b"clobbered\n")
        self._cli("snap", "-m", "v2")
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        for p, data in want.items():
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data, p)

    def test_guard_scoped_to_project_and_reports_deletion(self):
        # The guard sized the whole files/ tree: project A's snap flagged
        # project B's shrunken AGENTS.md, and a watched file deleted
        # outright was never mentioned (its stale mirror stayed in files/).
        proj_a, proj_b = self.proj, make_project()
        agents_b = os.path.join(proj_b, "AGENTS.md")
        with open(agents_b, "w") as f:
            f.write("rule\n" * 40)
        self._cli_root(proj_a, "init")
        self._cli_root(proj_b, "snap", "-m", "B1")
        self._cli_root(proj_a, "snap", "-m", "A1")
        with open(agents_b, "w") as f:
            f.write("x\n")
        _, out = self._cli_root(proj_b, "snap", "-m", "B2")
        self.assertIn("guard: AGENTS.md shrank", out)  # B's own guard
        claude_a = os.path.join(proj_a, "CLAUDE.md")
        with open(claude_a, "a") as f:
            f.write("more\n")
        _, out = self._cli_root(proj_a, "snap", "-m", "A2")
        self.assertNotIn("guard", out)  # not B's business
        os.remove(claude_a)
        _, out = self._cli_root(proj_a, "snap", "-m", "A3")
        self.assertRegex(out, r"guard: CLAUDE\.md deleted \(\d+ bytes\)")
        _, out = self._cli_root(proj_a, "log")
        self.assertIn("CLAUDE.md deleted", out.splitlines()[1])
        self.assertNotIn("AGENTS.md", out)

    def test_strict_console_never_tracebacks(self):
        # Hook output is a pipe: on Windows that is cp1252/strict, so a
        # guard line naming .claude/commands/Καλάς.md raised
        # UnicodeEncodeError inside the hook. Run the real CLI with an
        # ASCII-only stdout: no traceback, exit 0, the name is replaced.
        cmds = os.path.join(self.proj, ".claude", "commands")
        os.makedirs(cmds)
        greek = os.path.join(cmds, "Καλάς.md")
        with open(greek, "w", encoding="utf-8") as f:
            f.write("rule\n" * 30)
        env = {**os.environ, "HOME": self._home, "USERPROFILE": self._home,
               "PYTHONIOENCODING": "ascii"}
        env.pop("PYTHONUTF8", None)
        repo_root = os.path.dirname(os.path.dirname(
            os.path.abspath(cli_mod.__file__)))

        def run(*argv):
            return subprocess.run(
                [sys.executable, "-m", "sessionsteward", "--root", self.proj,
                 *argv], capture_output=True, cwd=repo_root, env=env,
                timeout=60)

        self.assertEqual(run("init").returncode, 0)
        self.assertEqual(run("snap", "-m", "v1").returncode, 0)
        with open(greek, "w", encoding="utf-8") as f:
            f.write("- bullet\n")
        p = run("snap", "-m", "v2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"Traceback", p.stderr)
        self.assertIn(b"guard: .claude/commands/?????.md shrank", p.stdout)
        p = run("log")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"Traceback", p.stderr)
        self.assertIn(b"?????.md shrank", p.stdout)

    def test_install_hooks_replaces_stale_interpreter_entry(self):
        # Re-running install-hooks after moving/recreating the venv
        # appended a second entry next to the stale one (whose python no
        # longer exists); any hook that invokes sessionsteward must be
        # rewritten in place instead.
        stale_pre = "'/old/venv/bin/python' -m sessionsteward precompact"
        stale_start = ('"C:\\old venv\\Scripts\\python.exe" -m '
                       'sessionsteward snap -m "session start"')
        settings = {"hooks": {
            "PreCompact": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "echo other"},
                    {"type": "command", "command": stale_pre}]},
                {"matcher": "", "hooks": [
                    {"type": "command", "command": stale_pre}]}],
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": stale_start}]}],
        }}
        core_mod.merge_hooks(settings)
        self.assertEqual(core_mod._hook_commands(settings["hooks"]["PreCompact"]),
                         ["echo other", core_mod.HOOK_PRECOMPACT_CMD])
        self.assertEqual(
            core_mod._hook_commands(settings["hooks"]["SessionStart"]),
            [core_mod.HOOK_SESSIONSTART_CMD])
        before = json.dumps(settings, sort_keys=True)
        core_mod.merge_hooks(settings)
        self.assertEqual(json.dumps(settings, sort_keys=True), before)

    def test_log_dates_are_local_time(self):
        # `--date=iso` printed the recorded offset; the log now renders
        # snapshot times in the user's local zone (--date=iso-local).
        when = "2025-01-01T12:00:00+09:00"
        self._cli("init")  # takes the initial snapshot
        # Give it a known time. (GIT_AUTHOR_DATE in the environment used to
        # do this; it is deliberately not passed on to our git calls now.)
        core_mod._git(core_mod.REPO_DIR, "commit", "-q", "--amend",
                      "--no-edit", "--date", when)
        epoch = datetime.datetime.fromisoformat(when).timestamp()
        want = time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
        self.assertEqual(core_mod.log(1, project_root=self.proj)[0]["date"][:16],
                         want)
        _, out = self._cli("log")
        self.assertIn(want, out)

    def test_transcripts_not_snapshotted(self):
        tdir = os.path.join(self.proj, ".claude", "transcripts")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "x.jsonl"), "w") as f:
            f.write('{"big": "transcript"}\n')
        with open(os.path.join(self.proj, ".claude", "debug.log"), "w") as f:
            f.write("log noise\n")
        self._cli("init")
        self._cli("snap", "-m", "v1")
        names = core_mod._git(core_mod.REPO_DIR, "ls-tree", "-r", "HEAD",
                              "--name-only").splitlines()
        self.assertFalse(any("transcript" in n for n in names), names)
        self.assertFalse(any(n.endswith(".log") for n in names), names)
        self.assertTrue(any(n.endswith(".claude/settings.json") for n in names),
                        names)
        # watch daemon must not see them either
        mtimes = core_mod.file_mtimes(core_mod.watched_paths(self.proj))
        self.assertFalse(any("transcript" in p or p.endswith(".log")
                             for p in mtimes), list(mtimes))

    # ---- round-3 release-panel regressions ----

    def _cli_err(self, root, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.main(["--root", root, *args])
        return code, out.getvalue(), err.getvalue()

    def test_watched_path_outside_home_never_escapes_repo(self):
        # HIGH: _rel() built the mirror path from relpath(), so a watched
        # file outside both home and the project became
        # files/../../../x/notes.md. At one directory depth that resolved
        # to the watched file ITSELF: snap removed it (not via trash) and
        # then crashed copying it. At other depths the mirror silently
        # landed outside the repo and the file was never protected.
        tmp = self._tmp
        proj = os.path.join(self._home, "a", "b", "c")  # depth that hit
        os.makedirs(proj)
        with open(os.path.join(proj, "CLAUDE.md"), "w") as f:
            f.write("# proj\n")
        outside = os.path.join(tmp, "outside2", "notes.md")
        os.makedirs(os.path.dirname(outside))
        with open(outside, "w") as f:
            f.write("IMPORTANT NOTES\n")
        self._cli_root(proj, "init")
        self._cli_root(proj, "watch", "--add", outside)
        code, out, err = self._cli_err(proj, "snap", "-m", "one")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(outside), "watched file was deleted")
        with open(outside) as f:
            self.assertEqual(f.read(), "IMPORTANT NOTES\n")
        rel = core_mod._rel(outside, proj)
        # POSIX: __abs/root/...; Windows: __abs/<drive letter>/...
        self.assertRegex(rel, r"^__abs/(root|[A-Za-z])/")
        self.assertNotIn("..", rel)
        names = core_mod._git(core_mod.REPO_DIR, "ls-tree", "-r",
                              "--name-only", "HEAD").splitlines()
        self.assertIn(f"files/{rel}", names)
        files_dir = os.path.realpath(os.path.join(core_mod.REPO_DIR, "files"))
        for n in names:
            real = os.path.realpath(os.path.join(core_mod.REPO_DIR, n))
            self.assertTrue(real.startswith(os.path.join(core_mod.REPO_DIR, "")),
                            n)
        self.assertFalse(os.path.exists(os.path.join(
            os.path.dirname(tmp), "outside2")))  # no stray mirror either
        # the file is actually protected: clobber -> undo brings it back
        with open(outside, "w") as f:
            f.write("x\n")
        self._cli_root(proj, "snap", "-m", "two")
        code, _, _ = self._cli_err(proj, "undo")
        self.assertEqual(code, 0)
        with open(outside) as f:
            self.assertEqual(f.read(), "IMPORTANT NOTES\n")
        # and the containment check itself refuses an escaping path
        for bad in ("../x", "a/../../x", "/abs", "", "."):
            with self.assertRaises(core_mod.StewardError):
                core_mod._mirror_path(files_dir, bad)

    def test_other_drive_path_maps_to_valid_mirror_name(self):
        # Windows: a watched file on another drive has no relpath, so the
        # fallback produced "D:_notes_x.md" — a colon is not a valid file
        # name character there. It must become __abs/D/notes/x.md.
        def splitdrive(p):
            return (p[:2], p[2:]) if p[1:2] == ":" else ("", p)
        with mock.patch.object(core_mod.os.path, "splitdrive", splitdrive):
            rel = core_mod._abs_rel("D:\\notes\\x.md")
            unc = core_mod._abs_rel("\\\\server\\share\\x.md")
        self.assertEqual(rel, "__abs/D/notes/x.md")
        self.assertNotIn(":", rel)
        self.assertEqual(core_mod.display_rel(rel), "D:/notes/x.md")
        with mock.patch.object(core_mod.os.path, "splitdrive",
                               lambda p: ("\\\\server\\share", p[14:])):
            unc = core_mod._abs_rel("\\\\server\\share\\x.md")
        self.assertEqual(unc, "__abs/UNC/server/share/x.md")
        self.assertEqual(core_mod.display_rel("__home/.claude/CLAUDE.md"),
                         "~/.claude/CLAUDE.md")
        self.assertEqual(core_mod.display_rel("__abs/root/x/notes.md"),
                         "/x/notes.md")

    def test_undo_twice_does_not_bring_the_clobber_back(self):
        # HIGH: `undo` assumed the live files match snaps[0]. After one
        # undo they match snaps[1], so the next undo saw a "dirty" tree,
        # snapshotted it as pre-undo and restored snaps[1] — the clobber.
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("rule\n" * 30)
        self._cli("init")  # initial snapshot = good
        with open(claude, "w") as f:
            f.write("- one bullet\n")
        self._cli("snap", "-m", "clobber")
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        self.assertIn("initial snapshot", out)
        with open(claude) as f:
            self.assertEqual(f.read(), "rule\n" * 30)
        code, out = self._cli("undo")  # must NOT restore the clobber
        self.assertEqual(code, 0)
        self.assertIn("oldest snapshot", out)
        with open(claude) as f:
            self.assertEqual(f.read(), "rule\n" * 30)
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertEqual(messages, ["clobber", "initial snapshot"])
        # redo = restore the clobber snapshot: the live tree matches an
        # existing snapshot, so no pre-restore noise commit either
        clobber = core_mod.log(50, project_root=self.proj)[0]["sha"]
        code, _ = self._cli("restore", clobber[:7])
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertEqual(f.read(), "- one bullet\n")
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertEqual(messages, ["clobber", "initial snapshot"])

    def test_undo_steps_from_current_position_and_reports_clamp(self):
        claude = os.path.join(self.proj, "CLAUDE.md")
        self._cli("init")
        for v in ("v2", "v3", "v4"):
            with open(claude, "w") as f:
                f.write(f"{v}\n")
            self._cli("snap", "-m", v)
        self._cli("undo")  # at v3
        with open(claude) as f:
            self.assertEqual(f.read(), "v3\n")
        code, out = self._cli("undo", "--steps", "5")  # only 2 left
        self.assertEqual(code, 0)
        self.assertIn("undone 2 step(s)", out)
        self.assertIn("asked for 5 step(s)", out)
        with open(claude) as f:
            self.assertEqual(f.read(), "# Project\n\nBe helpful.\n")
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertNotIn("pre-undo", messages)

    def test_log_after_restore_drops_stale_undo_hint(self):
        # After `restore <good>` the log's top entry (pre-restore, the
        # clobbered state) still said "`sessionsteward undo` to recover";
        # following it re-clobbered. The hint only holds while the live
        # files ARE that top snapshot.
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("rule\n" * 30)
        self._cli("init")
        good = core_mod.log(1, project_root=self.proj)[0]["sha"]
        with open(claude, "w") as f:
            f.write("- one bullet\n")
        self._cli("snap", "-m", "clobber")
        _, out = self._cli("log")
        self.assertIn("`sessionsteward undo` to recover", out)  # live = clobber
        self._cli("restore", good[:7])
        _, out = self._cli("log")
        self.assertIn("guard: CLAUDE.md shrank", out)  # history still shown
        self.assertNotIn("`sessionsteward undo` to recover", out)
        with open(claude, "w") as f:
            f.write("dirty\n" * 40)
        self._cli("snap", "-m", "later")
        _, out = self._cli("log")
        # older entries point at the snapshot holding the previous version
        self.assertIn(f"`sessionsteward restore {good[:7]}` has the previous "
                      f"version", out)
        self.assertNotIn("`sessionsteward undo` to recover", out)

    def test_diff_defaults_to_this_projects_snapshots(self):
        # `diff` diffed against global HEAD, i.e. whichever project
        # snapshotted last: project A's diff showed project B's changes.
        proj_a, proj_b = self.proj, make_project()
        self._cli_root(proj_a, "init")
        with open(os.path.join(proj_a, "CLAUDE.md"), "a") as f:
            f.write("A extra line.\n")
        self._cli_root(proj_a, "snap", "-m", "A2")
        with open(os.path.join(proj_b, "CLAUDE.md"), "a") as f:
            f.write("B secret line.\n")
        self._cli_root(proj_b, "snap", "-m", "B1")
        code, out = self._cli_root(proj_a, "diff")
        self.assertEqual(code, 0)
        self.assertIn("A extra line.", out)
        self.assertNotIn("B secret line.", out)

    def test_git_missing_is_a_clean_error(self):
        # git not on PATH raised FileNotFoundError out of subprocess (a
        # traceback inside the SessionStart hook). One line, exit 2.
        real_run = core_mod.subprocess.run

        def no_git(cmd, *a, **kw):
            if cmd and cmd[0] == "git":
                raise FileNotFoundError(2, "No such file or directory", "git")
            return real_run(cmd, *a, **kw)

        with mock.patch.object(core_mod.subprocess, "run", no_git):
            code, out, err = self._cli_err(self.proj, "init")
        self.assertEqual(code, 2)
        self.assertIn("error: git not found", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)

    def test_git_failure_is_a_clean_error(self):
        # A stale .git/index.lock made `git add` fail -> RuntimeError
        # traceback. Must be `error: git <command> failed: ...`, exit 2 (the
        # first git command to meet the lock is the one that is named).
        self._cli("init")
        lock = os.path.join(core_mod.REPO_DIR, ".git", "index.lock")
        with open(lock, "w") as f:
            f.write("")
        with open(os.path.join(self.proj, "CLAUDE.md"), "a") as f:
            f.write("more\n")
        code, out, err = self._cli_err(self.proj, "snap", "-m", "x")
        self.assertEqual(code, 2)
        self.assertRegex(err, r"^error: git \S+.* failed: .*index\.lock")
        self.assertNotIn("Traceback", err)

    def test_identical_dir_not_swapped_and_trash_pruned(self):
        # Every undo/restore moved a watched directory to trash and
        # re-extracted it even when byte-identical, and the trash grew by
        # one copy per undo, forever.
        claude = os.path.join(self.proj, "CLAUDE.md")
        self._cli("init")
        for i in range(3):
            with open(claude, "w") as f:
                f.write(f"v{i}\n")
            self._cli("snap", "-m", f"v{i}")
            code, out = self._cli("undo")
            self.assertEqual(code, 0)
            self.assertIn("unchanged .claude", out)
            self.assertNotIn("moved", out)
        self.assertFalse(os.path.exists(core_mod.TRASH_DIR))
        # pruning: only the newest TRASH_KEEP entries survive
        trash = os.path.join(self._tmp, "trash2")
        os.makedirs(trash)
        for i in range(core_mod.TRASH_KEEP + 5):
            p = os.path.join(trash, f"old.{i}")
            with open(p, "w") as f:
                f.write("x")
            os.utime(p, (1_600_000_000 + i, 1_600_000_000 + i))
        removed = core_mod.prune_trash(trash)
        self.assertEqual(len(removed), 5)
        left = sorted(os.listdir(trash), key=lambda n: int(n.split(".")[1]))
        self.assertEqual(len(left), core_mod.TRASH_KEEP)
        self.assertEqual(left[0], "old.5")  # the oldest five went
        # and restore() prunes after it moved something
        with mock.patch.object(core_mod, "TRASH_KEEP", 1):
            with open(os.path.join(self.proj, ".claude", "new.txt"), "w") as f:
                f.write("new\n")
            self._cli("snap", "-m", "with new")
            self._cli("undo")  # .claude differs now -> moved to trash
            self.assertEqual(len(os.listdir(core_mod.TRASH_DIR)), 1)
            with open(os.path.join(self.proj, ".claude", "new2.txt"), "w") as f:
                f.write("new2\n")
            self._cli("snap", "-m", "with new2")
            self._cli("undo")
            self.assertEqual(len(os.listdir(core_mod.TRASH_DIR)), 1)

    def test_guard_names_project_relative_file_under_home(self):
        # For a project under $HOME every rel was __home/code/projH/...,
        # so the guard said "__home/code/projH/CLAUDE.md shrank", not
        # "CLAUDE.md" as the README shows.
        proj = os.path.join(self._home, "code", "projH")
        os.makedirs(proj)
        claude = os.path.join(proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("rule\n" * 30)
        user_mem = os.path.join(self._home, ".claude", "CLAUDE.md")
        os.makedirs(os.path.dirname(user_mem))
        with open(user_mem, "w") as f:
            f.write("memory\n" * 20)
        self._cli_root(proj, "init")
        self.assertEqual(core_mod._rel(claude, proj), "CLAUDE.md")
        self.assertEqual(core_mod._rel(user_mem, proj), "__home/.claude/CLAUDE.md")
        with open(claude, "w") as f:
            f.write("- bullet\n")
        with open(user_mem, "w") as f:
            f.write("- gone\n")
        _, out = self._cli_root(proj, "snap", "-m", "v2")
        self.assertIn("guard: CLAUDE.md shrank", out)
        self.assertIn("guard: ~/.claude/CLAUDE.md shrank", out)
        self.assertNotIn("__home", out)

    def test_corrupt_config_is_kept_and_reported(self):
        self._cli("init")
        self._cli("watch", "--add", "NOTES.md")
        with open(core_mod.CONFIG_PATH, "a") as f:
            f.write("{{{ not json")
        with open(core_mod.CONFIG_PATH) as f:
            broken = f.read()
        code, out, err = self._cli_err(self.proj, "snap", "-m", "x")
        self.assertEqual(code, 2)
        self.assertIn("error: watch config", err)
        self.assertIn(core_mod.CONFIG_PATH, err)
        self.assertNotIn("Traceback", err)
        with open(core_mod.CONFIG_PATH) as f:
            self.assertEqual(f.read(), broken)  # not replaced
        self.assertIn("NOTES.md", broken)  # the watch list is still there

    def test_corrupt_manifest_is_skipped_by_log_and_refused_by_restore(self):
        # One snapshot with a broken manifest.json must not take `log` down
        # for every project (it used to: "corrupt manifest.json", exit 2,
        # forever); it is skipped there, and restoring IT is a clean error.
        self._cli("init")
        with open(os.path.join(core_mod.REPO_DIR, "manifest.json"), "w") as f:
            f.write("not json")
        core_mod._git(core_mod.REPO_DIR, "add", "-A")
        core_mod._git(core_mod.REPO_DIR, "commit", "-q", "-m", "tampered")
        bad = core_mod._git(core_mod.REPO_DIR, "rev-parse", "HEAD").strip()
        code, out, err = self._cli_err(self.proj, "log")
        self.assertEqual(code, 0, err)
        self.assertIn("initial snapshot", out)
        self.assertNotIn("tampered", out)
        self.assertNotIn("Traceback", err)
        code, out, err = self._cli_err(self.proj, "restore", bad[:7])
        self.assertEqual(code, 2)
        self.assertIn("corrupt manifest.json", err)
        self.assertNotIn("Traceback", err)

    def test_diet_counts_only_files_loaded_into_context(self):
        # diet counted .claude/settings.json and .gitattributes as
        # context-eaters and ignored IGNORE_PATTERNS (transcripts, logs).
        rules = os.path.join(self.proj, ".claude", "rules")
        os.makedirs(rules)
        with open(os.path.join(rules, "testing.md"), "w") as f:
            f.write("Always run the tests before you claim success.\n")
        with open(os.path.join(self.proj, ".claude", ".gitattributes"), "w") as f:
            f.write("* text=auto\n")
        with open(os.path.join(self.proj, ".claude", "debug.log"), "w") as f:
            f.write("x" * 5000)
        tdir = os.path.join(self.proj, ".claude", "transcripts")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "s.jsonl"), "w") as f:
            f.write("y" * 5000)
        cmds = os.path.join(self.proj, ".claude", "commands")
        os.makedirs(cmds)
        with open(os.path.join(cmds, "deploy.md"), "w") as f:
            f.write("on-demand skill text\n")
        report = diet_mod.analyze(self.proj)
        names = {os.path.relpath(f["path"], self.proj).replace(os.sep, "/")
                 for f in report["files"]}
        self.assertEqual(names, {"CLAUDE.md", ".claude/rules/testing.md"})
        self.assertLess(report["total_chars"], 200)
        self.assertEqual(report["n_not_context"], 3)  # settings, attrs, cmd
        rendered = diet_mod.render(report)
        self.assertIn("Files loaded into context: 2", rendered)
        self.assertIn("3 other watched file(s)", rendered)
        self.assertNotIn("settings.json", rendered)

    def test_log_finds_this_projects_snapshots_behind_many_others(self):
        # log() fetched a fixed window (max(limit*5, 100) commits) and
        # filtered by project afterwards, so a project whose snapshots
        # sat behind 100+ of another project's was reported as having
        # none — and undo said "nothing to restore".
        proj_a, proj_b = self.proj, make_project()
        self._cli_root(proj_a, "init")
        a_sha = core_mod.log(1, project_root=proj_a)[0]["sha"]
        self._cli_root(proj_b, "snap", "-m", "B")
        for i in range(105):  # B's manifest stays in the tree
            core_mod._git(core_mod.REPO_DIR, "commit", "-q", "--allow-empty",
                          "-m", f"B noise {i}")
        snaps = core_mod.log(1, project_root=proj_a)
        self.assertEqual([s["sha"] for s in snaps], [a_sha])
        _, out = self._cli_root(proj_a, "log")
        self.assertIn(a_sha[:7], out)
        self.assertNotIn("noise", out)

    def test_tilde_backslash_watch_entry_expands(self):
        # `~\x.md` (Windows spelling) never matched startswith("~/") and
        # was looked up relative to the project.
        extra = os.path.join(self._home, "extra.md")
        with open(extra, "w") as f:
            f.write("extra\n")
        self._cli("init")
        self._cli("watch", "--add", "~\\extra.md")
        self.assertIn(os.path.abspath(extra), core_mod.watched_paths(self.proj))
        self.assertEqual(core_mod._rel(extra, self.proj), "__home/extra.md")

    def test_init_takes_first_snapshot_so_undo_works_at_once(self):
        # Right after the quickstart, the first clobber had nothing to
        # undo to ("nothing to restore").
        code, out = self._cli("init")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"snapshot [0-9a-f]{7} \(initial\)")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(claude, "w") as f:
            f.write("- one bullet\n")
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())
        # second init: no second initial snapshot
        self._cli("init")
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertEqual(messages.count("initial snapshot"), 1)

    def test_log_empty_repo_says_no_snapshots(self):
        empty = tempfile.mkdtemp()
        self._cli_root(empty, "init")
        code, out = self._cli_root(empty, "log")
        self.assertEqual(code, 0)
        self.assertIn("no snapshots yet", out)
        code, out = self._cli_root(empty, "diff")
        self.assertEqual(code, 2)
        self.assertIn("no snapshots yet", out)

    def test_install_hooks_write_does_not_say_rerun_with_write(self):
        settings = os.path.join(self._tmp, "settings.json")
        with mock.patch.object(cli_mod, "CLAUDE_SETTINGS", settings):
            code, out = self._cli("install-hooks", "--write")
        self.assertEqual(code, 0)
        self.assertIn(f"merged hooks into {settings}", out)
        self.assertNotIn("re-run with --write", out)
        self.assertNotIn("Copy the JSON", out)
        self.assertTrue(os.path.isfile(settings))

    def test_undo_report_is_short(self):
        self._cli("init")
        claude = os.path.join(self.proj, "CLAUDE.md")
        with open(os.path.join(self.proj, ".claude", "new.txt"), "w") as f:
            f.write("new\n")
        with open(claude, "w") as f:
            f.write("nuked\n")
        self._cli("snap", "-m", "v2")
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^undone 1 step\(s\) -> [0-9a-f]{7}  initial snapshot$")
        self.assertIn("  restored CLAUDE.md", lines)
        moved = [ln for ln in lines if ln.startswith("  moved ")]
        self.assertEqual(len(moved), 1)
        # (the test trash dir is not under the fake home; in real use the
        # destination reads ~/.sessionsteward/trash/.claude.<time>)
        self.assertRegex(moved[0], r"^  moved \.claude -> \S+trash[/\\]\.claude\.\d+$")
        self.assertNotIn(self.proj, out)  # no absolute project paths
        self.assertEqual(cli_mod._short(
            os.path.join(self._home, ".sessionsteward", "trash", "x.1"),
            self.proj), os.path.join("~", ".sessionsteward", "trash", "x.1"))

    def test_auto_memory_dir_watched_when_present(self):
        # Claude Code's auto memory lives in
        # ~/.claude/projects/<project>/memory/ (<project> = the repo or
        # project path with non-alphanumerics replaced by '-'). It is
        # watched by default when it exists.
        slug = core_mod._project_slug(os.path.abspath(self.proj))
        self.assertRegex(slug, r"^[A-Za-z0-9-]+$")
        mem = os.path.join(self._home, ".claude", "projects", slug, "memory")
        os.makedirs(mem)
        index = os.path.join(mem, "MEMORY.md")
        with open(index, "w") as f:
            f.write("- prefers short answers\n" * 10)
        self.assertEqual(core_mod.auto_memory_dir(self.proj), mem)
        self.assertIn(os.path.abspath(mem), core_mod.watched_paths(self.proj))
        self._cli("init")
        with open(index, "w") as f:
            f.write("- gone\n")
        _, out = self._cli("snap", "-m", "v2")
        self.assertIn("guard: ~/.claude/projects/", out)
        self.assertIn("memory/MEMORY.md shrank", out)
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        with open(index) as f:
            self.assertEqual(f.read(), "- prefers short answers\n" * 10)
        # a project without one simply has nothing to watch there
        self.assertIsNone(core_mod.auto_memory_dir(tempfile.mkdtemp()))
        # the memory index counts as context in `diet`, topic files don't
        with open(os.path.join(mem, "topic.md"), "w") as f:
            f.write("details\n")
        report = diet_mod.analyze(self.proj)
        paths = [f["path"] for f in report["files"]]
        self.assertIn(index, paths)
        self.assertNotIn(os.path.join(mem, "topic.md"), paths)

    def test_blob_ids_match_git(self):
        # worktree_matches_snapshot compares locally computed git blob ids
        # against ls-tree, instead of one `git show` per file.
        self._cli("init")
        head = core_mod.log(1, project_root=self.proj)[0]["sha"]
        tree = core_mod.snapshot_tree(head)
        claude = os.path.join(self.proj, "CLAUDE.md")
        self.assertIn(claude, tree)
        self.assertIn(os.path.join(self.proj, ".claude", "settings.json"), tree)
        live = core_mod.live_tree(core_mod.watched_paths(self.proj))
        self.assertEqual(live, tree)
        self.assertEqual(tree[claude], core_mod._git(
            core_mod.REPO_DIR, "hash-object", claude).strip())
        with open(claude, "a") as f:
            f.write("x")
        self.assertFalse(core_mod.worktree_matches_snapshot(head, self.proj))
        self.assertIsNone(core_mod.current_position(
            core_mod.log(50, project_root=self.proj), self.proj))

    # ---- round-4 release-panel regressions ----

    def _position(self, root=None):
        root = root or self.proj
        return core_mod.current_position(
            core_mod.log(50, project_root=root), root)

    def _trashed(self):
        out = []
        for root, _, files in os.walk(core_mod.TRASH_DIR):
            out.extend(os.path.join(root, fn) for fn in files)
        return out

    def test_restore_older_then_newer_sweeps_against_live_tree(self):
        # HIGH (repro A): the extra-file sweep compared against this
        # project's NEWEST snapshot instead of the live worktree. After
        # `undo` (-> A) and `restore B`, AGENTS.md (gone in B) survived,
        # the worktree matched no snapshot, and the next `undo` snapped
        # it as pre-undo and stepped FORWARD onto B: "undone -> B",
        # AGENTS.md trashed, CLAUDE.md = the clobber.
        shutil.rmtree(os.path.join(self.proj, ".claude"))
        claude = os.path.join(self.proj, "CLAUDE.md")
        agents = os.path.join(self.proj, "AGENTS.md")
        with open(agents, "w") as f:
            f.write("# agents\n")
        self._cli("init")  # A: CLAUDE.md + AGENTS.md
        os.remove(agents)
        with open(claude, "w") as f:
            f.write("clobber\n")
        self._cli("snap", "-m", "B")
        b_sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        code, _ = self._cli("undo")
        self.assertEqual(code, 0)
        self.assertEqual(self._position(), 1)
        self.assertTrue(os.path.exists(agents))
        code, out = self._cli("restore", b_sha[:7])
        self.assertEqual(code, 0)
        # the worktree IS B now: AGENTS.md went to trash (not deleted)
        self.assertFalse(os.path.exists(agents))
        self.assertIn("moved AGENTS.md", out)
        self.assertEqual(self._position(), 0)
        code, out = self._cli("undo")  # back to A, not "forward" to B
        self.assertEqual(code, 0)
        self.assertIn("initial snapshot", out)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())
        with open(agents) as f:
            self.assertEqual(f.read(), "# agents\n")
        self.assertEqual(self._position(), 1)
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=self.proj)]
        self.assertEqual(messages, ["B", "initial snapshot"])

    def test_undo_twice_past_an_added_file_then_stops(self):
        # HIGH (repro B): A(v1) -> B(v2 + AGENTS.md) -> C(v3, AGENTS.md
        # deleted). undo -> B fine; undo -> A left AGENTS.md behind (B,
        # the newest snapshot, is not where it came from); the third undo
        # then went forward to C.
        shutil.rmtree(os.path.join(self.proj, ".claude"))
        claude = os.path.join(self.proj, "CLAUDE.md")
        agents = os.path.join(self.proj, "AGENTS.md")
        self._cli("init")  # A
        with open(claude, "w") as f:
            f.write("v2\n")
        with open(agents, "w") as f:
            f.write("agents\n")
        self._cli("snap", "-m", "B")
        with open(claude, "w") as f:
            f.write("v3\n")
        os.remove(agents)
        self._cli("snap", "-m", "C")
        self._cli("undo")
        self.assertEqual(self._position(), 1)
        self.assertTrue(os.path.exists(agents))
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        self.assertIn("moved AGENTS.md", out)
        self.assertFalse(os.path.exists(agents))
        self.assertEqual(self._position(), 2)
        code, out = self._cli("undo")  # oldest: must not jump to C
        self.assertEqual(code, 0)
        self.assertIn("already at the oldest snapshot", out)
        with open(claude) as f:
            self.assertEqual(f.read(), "# Project\n\nBe helpful.\n")
        self.assertEqual(len(self._trashed()), 1)  # AGENTS.md, once

    def test_restore_of_effectively_empty_watched_dir(self):
        # HIGH (repro C): a watched dir holding only ignored files has a
        # manifest entry but no tree entry (git tracks no empty dirs), so
        # restore skipped it: settings.json added later stayed, the
        # worktree matched nothing, and the next undo went forward.
        dot = os.path.join(self.proj, ".claude")
        os.remove(os.path.join(dot, "settings.json"))
        with open(os.path.join(dot, "debug.log"), "w") as f:
            f.write("noise\n")
        claude = os.path.join(self.proj, "CLAUDE.md")
        self._cli("init")  # A: .claude is effectively empty
        a_sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertTrue(core_mod.worktree_matches_snapshot(a_sha, self.proj))
        settings = os.path.join(dot, "settings.json")
        with open(settings, "w") as f:
            f.write("{}\n")
        with open(claude, "w") as f:
            f.write("v2\n")
        self._cli("snap", "-m", "B")
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(settings))
        self.assertTrue(os.path.isdir(dot))
        self.assertIn("moved .claude", out)
        self.assertEqual(self._position(), 1)
        code, out = self._cli("undo")
        self.assertIn("already at the oldest snapshot", out)
        with open(claude) as f:
            self.assertIn("Be helpful.", f.read())
        # and when the dir is still (effectively) empty, it is unchanged
        with open(claude, "w") as f:
            f.write("v3\n")
        self._cli("snap", "-m", "C")  # .claude empty here too
        code, out = self._cli("undo", "--steps", "2")  # C -> A
        self.assertEqual(code, 0)
        self.assertIn("unchanged .claude", out)
        self.assertNotIn("moved", out)
        self.assertEqual(self._position(), 2)

    def test_identical_resnap_skipped_across_projects(self):
        # MED: "nothing changed" was `git status` against the GLOBAL
        # HEAD. With two projects snapping in turn (A, B, A) A's identical
        # state was re-committed, and `undo` stepped onto that duplicate
        # — a no-op that buried the good snapshot one step further.
        proj_a, proj_b = self.proj, make_project()
        claude_a = os.path.join(proj_a, "CLAUDE.md")
        self._cli_root(proj_a, "init")
        with open(claude_a, "w") as f:
            f.write("clobber\n")
        self._cli_root(proj_a, "snap", "-m", "A-clobber")
        self._cli_root(proj_b, "snap", "-m", "B1")  # HEAD is B's now
        code, out = self._cli_root(proj_a, "snap", "-m", "A-again")
        self.assertEqual(code, 0)
        messages = [s["message"]
                    for s in core_mod.log(50, project_root=proj_a)]
        self.assertEqual(messages, ["A-clobber", "initial snapshot"])
        a_head = core_mod.log(1, project_root=proj_a)[0]["sha"]
        self.assertIn(a_head[:7], out)  # reports the existing snapshot
        code, _ = self._cli_root(proj_a, "undo")
        self.assertEqual(code, 0)
        with open(claude_a) as f:
            self.assertIn("Be helpful.", f.read())
        # a real change in A is still committed
        with open(claude_a, "w") as f:
            f.write("new\n")
        self._cli_root(proj_a, "snap", "-m", "A-new")
        self.assertEqual(core_mod.log(1, project_root=proj_a)[0]["message"],
                         "A-new")

    def test_undo_past_watch_add_keeps_the_newer_file(self):
        # LOW: undoing to a snapshot taken before `watch --add NOTES.md`
        # trashed NOTES.md — a file that snapshot knew nothing about. The
        # manifest now records the watch list; out-of-scope paths are
        # left alone and reported, and do not make the snapshot "dirty".
        claude = os.path.join(self.proj, "CLAUDE.md")
        notes = os.path.join(self.proj, "NOTES.md")
        self._cli("init")  # A: NOTES.md not watched
        a_sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        with open(notes, "w") as f:
            f.write("my notes\n")
        self._cli("watch", "--add", "NOTES.md")
        with open(claude, "w") as f:
            f.write("v2\n")
        self._cli("snap", "-m", "B")
        self.assertEqual(core_mod._manifest_at("HEAD")["watch"],
                         core_mod.DEFAULT_WATCH + ["NOTES.md"])
        with open(claude, "w") as f:
            f.write("v3\n")
        self._cli("snap", "-m", "C")
        self._cli("undo")
        code, out = self._cli("undo")
        self.assertEqual(code, 0)
        self.assertIn(f"  kept NOTES.md: not watched when snapshot "
                      f"{a_sha[:7]} was taken", out)
        self.assertNotIn("moved", out)
        with open(notes) as f:
            self.assertEqual(f.read(), "my notes\n")
        self.assertEqual(self._trashed(), [])
        self.assertEqual(self._position(), 2)  # A, NOTES.md notwithstanding
        code, out = self._cli("undo")
        self.assertIn("already at the oldest snapshot", out)
        # a snapshot taken AFTER the add does sweep a deleted-in-it file
        c_sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self._cli("restore", c_sha[:7])
        self.assertEqual(self._position(), 0)
        with open(notes) as f:
            self.assertEqual(f.read(), "my notes\n")

    @unittest.skipIf(not hasattr(os, "symlink") or os.name == "nt",
                     "POSIX symlinks")
    def test_restore_never_writes_through_symlinks(self):
        # LOW: a top-level watched symlink was restored THROUGH the link
        # (overwriting the never-watched target), and a symlinked dir
        # inside .claude came back as a real directory.
        shared = os.path.join(self._tmp, "shared")
        os.makedirs(os.path.join(shared, "cmds"))
        shared_claude = os.path.join(shared, "CLAUDE.md")
        shared_cmd = os.path.join(shared, "cmds", "deploy.md")
        with open(shared_claude, "w") as f:
            f.write("shared v1\n")
        with open(shared_cmd, "w") as f:
            f.write("deploy v1\n")
        claude = os.path.join(self.proj, "CLAUDE.md")
        os.remove(claude)
        os.symlink(shared_claude, claude)
        commands = os.path.join(self.proj, ".claude", "commands")
        os.symlink(os.path.join(shared, "cmds"), commands)
        settings = os.path.join(self.proj, ".claude", "settings.json")
        # a link out of the watched folder is only followed on request
        # (SESSIONSTEWARD_FOLLOW_LINKS, see TestRound9LinksAndHooks)
        patcher = mock.patch.dict(os.environ,
                                  {"SESSIONSTEWARD_FOLLOW_LINKS": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._cli("init")
        a_sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        # what is behind the links is seen, so the live tree matches
        self.assertTrue(core_mod.worktree_matches_snapshot(a_sha, self.proj))
        for p, text in ((shared_claude, "shared v2\n"),
                        (shared_cmd, "deploy v2\n"), (settings, "{}\n")):
            with open(p, "w") as f:
                f.write(text)
        self._cli("snap", "-m", "B")
        code, out = self._cli("undo")
        self.assertEqual(code, 1)  # something was skipped
        # named as its folder lists it (ciopfs, the case-insensitive mount the
        # tests can run on, keeps no case for the name of a symlink)
        listed = [n for n in os.listdir(self.proj) if n.lower() == "claude.md"]
        self.assertEqual(len(listed), 1, listed)
        self.assertIn("  skipped %s: is a symlink" % listed[0], out)
        self.assertIn("  skipped .claude/commands: is a symlink", out)
        self.assertIn("  restored .claude", out)
        # links intact, targets untouched, the rest restored
        self.assertTrue(os.path.islink(claude))
        self.assertTrue(os.path.islink(commands))
        self.assertEqual(os.readlink(commands), os.path.join(shared, "cmds"))
        with open(shared_claude) as f:
            self.assertEqual(f.read(), "shared v2\n")
        with open(shared_cmd) as f:
            self.assertEqual(f.read(), "deploy v2\n")
        with open(settings) as f:
            self.assertEqual(f.read(), '{"x": 1}\n')
        # a link whose content already matches is just "unchanged"
        with open(shared_claude, "w") as f:
            f.write("shared v1\n")
        with open(shared_cmd, "w") as f:
            f.write("deploy v1\n")
        self.assertEqual(self._position(), 1)
        code, out = self._cli("restore", a_sha[:7])
        self.assertEqual(code, 0)
        self.assertIn("  unchanged " + listed[0], out)
        self.assertIn("  unchanged .claude", out)

    def test_broken_pipe_exits_quietly(self):
        # LOW: `sessionsteward install-hooks | head -1` ended in a
        # BrokenPipeError traceback. Run the real CLI with its stdout
        # closed by the reader: no traceback, exit 0.
        env = {**os.environ, "HOME": self._home, "USERPROFILE": self._home}
        repo_root = os.path.dirname(os.path.dirname(
            os.path.abspath(cli_mod.__file__)))
        p = subprocess.Popen(
            [sys.executable, "-m", "sessionsteward", "--root", self.proj,
             "install-hooks"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=repo_root, env=env)
        p.stdout.close()
        err = p.stderr.read()
        p.stderr.close()
        self.assertEqual(p.wait(timeout=60), 0, err)
        self.assertNotIn(b"Traceback", err)
        self.assertNotIn(b"BrokenPipe", err)
        # and in-process: a stdout whose write raises is swallowed too
        class Gone(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")
        with redirect_stdout(Gone()):
            self.assertEqual(cli_mod.main(["--root", self.proj,
                                           "install-hooks"]), 0)


if __name__ == "__main__":
    unittest.main()
