"""Round-9 regression tests: hooks that cannot run a cloned repo's code, a
snapshot that cannot be filled by link loops, links that leave the watched
folder, option-like snapshot ids."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import core as core_mod

try:
    from .test_round5_fixes import REPO_ROOT, Base, slurp, write
except ImportError:
    from test_round5_fixes import REPO_ROOT, Base, slurp, write

POSIX_LINKS = unittest.skipIf(os.name == "nt" or not hasattr(os, "symlink"),
                              "POSIX symlinks")


def store_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(root, fn)).st_size
            except OSError:
                pass
    return total


class TestHooksDoNotRunCodeFromTheProject(Base):
    """HIGH: hooks were `python -m sessionsteward ...`; Claude Code runs a
    hook with the project as the working directory and `python -m` puts it
    first on sys.path, so a cloned repo with its own sessionsteward/__main__.py
    ran at session start."""

    def plant(self):
        """A project holding a fake package that leaves a marker if run."""
        evil = os.path.join(self._tmp, "evil")
        self.marker = os.path.join(evil, "PWNED")
        write(os.path.join(evil, "sessionsteward", "__init__.py"), "")
        write(os.path.join(evil, "sessionsteward", "__main__.py"),
              "open(%r, 'w').write('x')\n" % self.marker)
        return evil

    def run_hook(self, cmd, cwd):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run(cmd, shell=True, cwd=cwd, env=env,
                              capture_output=True, timeout=60)

    @POSIX_LINKS
    def test_the_fallback_ignores_the_working_directory(self):
        evil = self.plant()
        with mock.patch.object(core_mod, "_console_script",
                               return_value=None):
            cmd = core_mod._self_cmd()
        self.assertIn(" -I -m sessionsteward", cmd)
        self.run_hook(cmd + " --version", evil)
        self.assertFalse(os.path.exists(self.marker), "ran the project's code")

    @POSIX_LINKS
    def test_the_console_script_is_written_and_ignores_the_directory(self):
        evil = self.plant()
        bindir = os.path.join(self._tmp, "venv", "bin")
        script = os.path.join(bindir, "sessionsteward")
        write(script, "#!%s\nimport sys\nsys.path.insert(0, %r)\n"
                      "from sessionsteward.cli import main\n"
                      "sys.exit(main())\n" % (sys.executable, REPO_ROOT))
        os.chmod(script, 0o755)
        real = core_mod.sysconfig.get_path

        def fake(name, *a, **kw):
            return bindir if name == "scripts" and not a and not kw \
                else real(name, *a, **kw)
        with mock.patch.object(core_mod.sysconfig, "get_path", fake):
            cmd = core_mod._self_cmd()
        self.assertEqual(shlex.split(cmd), [script])
        self.assertNotIn("-m", shlex.split(cmd))
        done = self.run_hook(cmd + " --version", evil)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"sessionsteward", done.stdout)
        self.assertFalse(os.path.exists(self.marker), "ran the project's code")

    def test_both_hooks_carry_a_timeout(self):
        hooks = core_mod.build_hooks_json()["hooks"]
        for event in ("PreCompact", "SessionStart"):
            self.assertEqual(hooks[event][0]["hooks"][0]["timeout"], 30)

    def test_install_hooks_rewrites_an_old_dash_m_hook_in_place(self):
        settings = os.path.join(self._tmp, "settings.json")
        old = {"hooks": {
            "PreCompact": [{"matcher": "", "hooks": [
                {"type": "command",
                 "command": "'/old/venv/bin/python' -m sessionsteward "
                            "precompact"}]}],
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command",
                 "command": '/usr/bin/python3 -m sessionsteward snap -m '
                            '"session start"'}]}]}}
        write(settings, json.dumps(old))
        core_mod.write_hooks_file(settings)
        merged = json.loads(slurp(settings).decode())
        for event, want in (("PreCompact", core_mod.HOOK_PRECOMPACT_CMD),
                            ("SessionStart", core_mod.HOOK_SESSIONSTART_CMD)):
            entries = merged["hooks"][event]
            # not appended (SessionStart also gets the 0.2.0 "compact" hook)
            self.assertEqual(len(entries), 2 if event == "SessionStart" else 1,
                             entries)
            hook = entries[0]["hooks"][0]
            self.assertEqual(hook["command"], want)
            self.assertEqual(hook["timeout"], 30)
        # idempotent
        before = slurp(settings)
        core_mod.write_hooks_file(settings)
        self.assertEqual(slurp(settings), before)

    def test_every_form_we_ever_wrote_is_recognised(self):
        for cmd in ("sessionsteward precompact",
                    "/usr/bin/python3 -m sessionsteward precompact",
                    "/usr/bin/python3 -I -m sessionsteward precompact",
                    "/home/me/venv/bin/sessionsteward precompact",
                    '"C:\\Py\\Scripts\\sessionsteward.exe" precompact',
                    core_mod.HOOK_PRECOMPACT_CMD):
            self.assertTrue(core_mod._is_ours("PreCompact", cmd), cmd)
        for cmd in ("sessionsteward log", "echo sessionsteward precompact",
                    "/bin/other precompact",
                    "python -m sessionsteward diff"):
            self.assertFalse(core_mod._is_ours("PreCompact", cmd), cmd)


@POSIX_LINKS
class TestLinkLoopsAndLinksOutside(Base):
    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, ".claude")

    def mirrored(self):
        out = []
        files = os.path.join(core_mod.REPO_DIR, "files")
        for root, _d, names in os.walk(files):
            out += [os.path.relpath(os.path.join(root, n), files)
                    .replace(os.sep, "/") for n in names]
        return sorted(out)

    def test_self_links_do_not_multiply(self):
        """HIGH: three links to '.' filled 355 MB in 20 s."""
        for name in "abc":
            os.symlink(".", os.path.join(self.claude, name))
        started = time.time()
        self.assertEqual(self.cli("init")[0], 0)
        self.assertLess(time.time() - started, 20)
        self.assertLess(store_bytes(core_mod.REPO_DIR), 1_000_000)
        self.assertIn(".claude/settings.json", self.mirrored())
        self.assertFalse([m for m in self.mirrored() if "/a/" in m])

    def test_a_link_to_an_ancestor_or_a_repeat_is_skipped(self):
        write(os.path.join(self.claude, "real", "x.md"), "x\n")
        os.symlink("real", os.path.join(self.claude, "again"))
        os.symlink("..", os.path.join(self.claude, "real", "up"))
        self.assertEqual(self.cli("init")[0], 0)
        mirrored = self.mirrored()
        self.assertIn(".claude/real/x.md", mirrored)
        self.assertFalse([m for m in mirrored if "/up/" in m], mirrored)
        # the live tree and the snapshot agree (so nothing looks changed)
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertTrue(core_mod.worktree_matches_snapshot(sha, self.proj))

    def test_depth_is_capped(self):
        deep = self.claude
        for i in range(6):
            deep = os.path.join(deep, "d%d" % i)
        write(os.path.join(deep, "bottom.md"), "b\n")
        write(os.path.join(self.claude, "d0", "top.md"), "t\n")
        with mock.patch.object(core_mod, "MAX_WALK_DEPTH", 3):
            self.assertEqual(self.cli("init")[0], 0)
        mirrored = self.mirrored()
        self.assertIn(".claude/d0/top.md", mirrored)
        self.assertNotIn(".claude/d0/d1/d2/d3/d4/d5/bottom.md", mirrored)

    def test_the_total_is_capped_with_a_warning(self):
        for i in range(5):
            write(os.path.join(self.claude, "big%d.md" % i), "x" * 400_000)
        os.environ["SESSIONSTEWARD_MAX_SNAPSHOT_MB"] = "1"
        code, out, err = self.cli("init")
        self.assertEqual(code, 0)
        self.assertIn("at most 1 MB", err)
        got = [m for m in self.mirrored() if "/big" in m]
        self.assertEqual(len(got), 2, got)
        # 0 means no cap
        os.environ["SESSIONSTEWARD_MAX_SNAPSHOT_MB"] = "0"
        self.assertEqual(self.cli("snap", "-m", "all")[0], 0)
        self.assertEqual(len([m for m in self.mirrored() if "/big" in m]), 5)

    def test_links_outside_the_watched_folder_are_not_copied(self):
        secret = os.path.join(self._tmp, "ssh")
        write(os.path.join(secret, "id_rsa"), "PRIVATE\n")
        outside = os.path.join(self._tmp, "outside.md")
        write(outside, "outside\n")
        os.symlink(secret, os.path.join(self.claude, "out"))
        os.symlink(outside, os.path.join(self.claude, "note.md"))
        write(os.path.join(self.claude, "inside.md"), "in\n")
        os.symlink("inside.md", os.path.join(self.claude, "alias.md"))
        self.assertEqual(self.cli("init")[0], 0)
        mirrored = self.mirrored()
        self.assertFalse([m for m in mirrored if "id_rsa" in m], mirrored)
        self.assertNotIn(".claude/note.md", mirrored)
        self.assertIn(".claude/alias.md", mirrored)   # a link inside is fine
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertTrue(core_mod.worktree_matches_snapshot(sha, self.proj))

    def test_opting_in_copies_them(self):
        secret = os.path.join(self._tmp, "ssh")
        write(os.path.join(secret, "id_rsa"), "PRIVATE\n")
        os.symlink(secret, os.path.join(self.claude, "out"))
        os.environ["SESSIONSTEWARD_FOLLOW_LINKS"] = "1"
        self.assertEqual(self.cli("init")[0], 0)
        self.assertIn(".claude/out/id_rsa", self.mirrored())

    def test_watch_follow_links_sets_the_config_key(self):
        secret = os.path.join(self._tmp, "ssh")
        write(os.path.join(secret, "id_rsa"), "PRIVATE\n")
        os.symlink(secret, os.path.join(self.claude, "out"))
        self.assertEqual(self.cli("watch", "--follow-links")[0], 0)
        self.assertIs(core_mod.load_config().get("follow_links"), True)
        self.assertEqual(self.cli("init")[0], 0)
        self.assertIn(".claude/out/id_rsa", self.mirrored())
        self.assertEqual(self.cli("watch", "--no-follow-links")[0], 0)
        self.assertEqual(self.cli("snap", "-m", "off")[0], 0)
        self.assertNotIn(".claude/out/id_rsa", self.mirrored())


class TestSnapshotIdsCannotBeOptions(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "changed\n")
        self.assertEqual(self.cli("snap", "-m", "two")[0], 0)

    def test_is_snapshot_id(self):
        for good in ("abcd", "HEAD", "0123456789abcdef" * 4, "ABCDEF1"):
            self.assertTrue(core_mod.is_snapshot_id(good), good)
        for bad in ("--output=/tmp/x", "-p", "abc", "HEAD~1", "main",
                    "a..b", "abcd ", "", None, "g" * 8, "0" * 65):
            self.assertFalse(core_mod.is_snapshot_id(bad), bad)

    def test_diff_refuses_option_like_arguments(self):
        target = os.path.join(self._tmp, "x")
        code, out, err = self.cli("diff", "--", "--output=" + target, "HEAD")
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(target))
        with self.assertRaises(SystemExit):   # argparse refuses it itself
            self.cli("diff", "HEAD", "--output=" + target)
        self.assertFalse(os.path.exists(target))

    def test_diff_still_works_with_shas_and_head(self):
        snaps = core_mod.log(5, project_root=self.proj)
        code, out, err = self.cli("diff", snaps[1]["sha"][:7],
                                  snaps[0]["sha"])
        self.assertEqual(code, 0, err)
        self.assertIn("+changed", out)
        self.assertEqual(self.cli("diff", snaps[1]["sha"][:7], "HEAD")[0], 0)

    def test_restore_refuses_option_like_arguments(self):
        with self.assertRaises(SystemExit):   # argparse refuses it itself
            self.cli("restore", "--hard")
        code, out, err = self.cli("restore", "--", "--hard")
        self.assertNotEqual(code, 0)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"changed\n")

    def test_undo_steps_below_one_are_refused(self):
        for bad in ("0", "-1"):
            code, out, err = self.cli("undo", "--steps=" + bad)
            self.assertEqual(code, 2, (bad, out, err))
            self.assertIn("--steps must be 1 or more", err)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"changed\n")


if __name__ == "__main__":
    unittest.main()
