"""Round-10 regression tests.

R10-1 The release workflow's version check was a folded plain scalar (see
      test_release_gate.py).
R10-2 Hook tests failed when the console script sat next to the interpreter.
R10-3 A snapshot silently left out links that lead outside a watched folder
      and folders more than 64 levels deep; `restore` then moved the whole
      watched folder to the trash and brought back only what a snapshot
      holds, so what the snapshot could not hold was lost from the folder.
R10-4 NTFS junctions were not treated like symlinks.
R10-5 install-hooks did not say that the `python -I` fallback needs an
      installed package; git older than 2.24 has no --end-of-options.
"""
from __future__ import annotations

import io
import os
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

try:
    from . import _isolate  # noqa: F401
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod

try:
    from .test_round5_fixes import REPO_ROOT, Base, slurp, write
except ImportError:
    from test_round5_fixes import REPO_ROOT, Base, slurp, write

POSIX_LINKS = unittest.skipIf(os.name == "nt" or not hasattr(os, "symlink"),
                              "POSIX symlinks")


class LinkBase(Base):
    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, ".claude")
        self.secret = os.path.join(self._tmp, "ssh")
        write(os.path.join(self.secret, "id_rsa"), "PRIVATE\n")

    def link_out(self, name="out"):
        path = os.path.join(self.claude, name)
        os.symlink(self.secret, path)
        return path


@POSIX_LINKS
class TestSnapWarnsAboutWhatItLeavesOut(LinkBase):
    def test_a_link_leading_outside_is_named_with_a_count(self):
        self.link_out("out")
        self.link_out("out2")
        code, _out, err = self.cli("init")
        self.assertEqual(code, 0)
        self.assertIn("2 link(s) leading outside the watched folders", err)
        self.assertIn(os.path.join(".claude", "out"), err)
        self.assertIn("--follow-links", err)
        self.assertNotIn("PRIVATE", err)

    def test_snap_and_precompact_warn_too(self):
        self.cli("init")
        self.link_out()
        for cmd in (("snap", "-m", "x"), ("precompact",)):
            code, _out, err = self.cli(*cmd)
            self.assertEqual(code, 0)
            self.assertIn("1 link(s) leading outside", err, cmd)

    def test_no_warning_without_such_links(self):
        code, _out, err = self.cli("init")
        self.assertEqual(code, 0)
        self.assertNotIn("leading outside", err)
        self.assertNotIn("levels deep", err)

    def test_opting_in_removes_the_warning(self):
        self.link_out()
        os.environ["SESSIONSTEWARD_FOLLOW_LINKS"] = "1"
        _code, _out, err = self.cli("init")
        self.assertNotIn("leading outside", err)

    def test_a_file_link_leading_outside_is_counted(self):
        outside = os.path.join(self._tmp, "outside.md")
        write(outside, "o\n")
        os.makedirs(self.claude, exist_ok=True)
        os.symlink(outside, os.path.join(self.claude, "note.md"))
        _code, _out, err = self.cli("init")
        self.assertIn("1 link(s) leading outside", err)

    def test_a_link_to_a_folder_already_walked_is_not_a_warning(self):
        write(os.path.join(self.claude, "rules", "a.md"), "a\n")
        os.symlink("rules", os.path.join(self.claude, "alias"))
        _code, _out, err = self.cli("init")
        self.assertNotIn("leading outside", err)

    def test_deep_folders_are_counted(self):
        deep = os.path.join(self.claude, *["d"] * (core_mod.MAX_WALK_DEPTH + 3))
        write(os.path.join(deep, "x.md"), "x\n")
        code, _out, err = self.cli("init")
        self.assertEqual(code, 0)
        self.assertIn(f"more than {core_mod.MAX_WALK_DEPTH} levels deep", err)

    def test_ignored_names_stay_quiet(self):
        write(os.path.join(self.claude, "node_modules", "x.js"), "x\n")
        write(os.path.join(self.claude, "run.log"), "x\n")
        _code, _out, err = self.cli("init")
        self.assertEqual(err.strip(), "")

    def test_snap_report_fills_the_dict(self):
        self.link_out()
        skipped = {}
        core_mod.snap_report(self.proj, "x", skipped)
        self.assertEqual(skipped["outside"], [os.path.join(self.claude, "out")])


@POSIX_LINKS
class TestRestoreKeepsWhatASnapshotCannotHold(LinkBase):
    def snapshot_then_edit(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.claude, "settings.json"), '{"x": 2}\n')

    def test_an_outside_link_stays_in_place(self):
        link = self.link_out()
        self.snapshot_then_edit()
        code, out, _err = self.cli("restore", core_mod.log(
            1, project_root=self.proj)[0]["sha"])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(slurp(os.path.join(link, "id_rsa")), b"PRIVATE\n")
        self.assertEqual(slurp(os.path.join(self.claude, "settings.json")),
                         b'{"x": 1}\n')
        self.assertIn("kept", out)

    def test_an_ignored_folder_stays_in_place(self):
        write(os.path.join(self.claude, "node_modules", "pkg", "i.js"), "js\n")
        write(os.path.join(self.claude, "skills", "s", ".git", "HEAD"), "ref\n")
        self.snapshot_then_edit()
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertEqual(self.cli("restore", sha)[0], 0)
        self.assertEqual(slurp(os.path.join(
            self.claude, "node_modules", "pkg", "i.js")), b"js\n")
        self.assertEqual(slurp(os.path.join(
            self.claude, "skills", "s", ".git", "HEAD")), b"ref\n")

    def test_an_oversize_file_stays_in_place(self):
        os.environ["SESSIONSTEWARD_MAX_FILE_MB"] = "0.001"
        write(os.path.join(self.claude, "big.bin"), "x" * 5000)
        self.snapshot_then_edit()
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertEqual(self.cli("restore", sha)[0], 0)
        self.assertEqual(len(slurp(os.path.join(self.claude, "big.bin"))), 5000)

    def test_a_skipped_item_inside_a_subfolder_comes_back_to_it(self):
        write(os.path.join(self.claude, "rules", "a.md"), "a\n")
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.claude, "rules", "__pycache__", "a.pyc"), "c\n")
        write(os.path.join(self.claude, "rules", "a.md"), "a2\n")
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertEqual(self.cli("restore", sha)[0], 0)
        self.assertEqual(slurp(os.path.join(self.claude, "rules", "a.md")),
                         b"a\n")
        self.assertEqual(slurp(os.path.join(
            self.claude, "rules", "__pycache__", "a.pyc")), b"c\n")

    def test_an_ordinary_new_file_still_goes_to_the_trash(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.claude, "new.md"), "new\n")
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        self.assertEqual(self.cli("restore", sha)[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "new.md")))
        self.assertTrue(self.trash_entries())

    def test_undo_keeps_it_too(self):
        self.link_out()
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.claude, "settings.json"), '{"x": 2}\n')
        self.assertEqual(self.cli("snap", "-m", "two")[0], 0)
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertTrue(os.path.islink(os.path.join(self.claude, "out")))

    def test_the_snapshot_wins_a_name_clash(self):
        write(os.path.join(self.claude, "keep.log"), "old\n")  # ignored name
        self.assertEqual(self.cli("init")[0], 0)
        items = core_mod.unsnapshotted_items(self.claude)
        self.assertEqual(items, [os.path.join(self.claude, "keep.log")])

    def test_unsnapshotted_items_lists_only_the_top_most(self):
        write(os.path.join(self.claude, "node_modules", "a", "b.js"), "j\n")
        write(os.path.join(self.claude, "rules", "ok.md"), "ok\n")
        self.link_out()
        items = core_mod.unsnapshotted_items(self.claude)
        self.assertEqual(sorted(items), sorted([
            os.path.join(self.claude, "node_modules"),
            os.path.join(self.claude, "out")]))

    def test_a_failed_carry_is_reported_not_lost(self):
        write(os.path.join(self.claude, "node_modules", "i.js"), "js\n")
        self.snapshot_then_edit()
        sha = core_mod.log(1, project_root=self.proj)[0]["sha"]
        real_rename = os.rename

        def rename(src, dst, *a, **k):
            if (os.path.basename(dst) == "node_modules"
                    and core_mod.TRASH_DIR in src):
                raise OSError(13, "denied")
            return real_rename(src, dst, *a, **k)
        with mock.patch.object(core_mod.os, "rename", rename):
            restored = core_mod.restore(sha, project_root=self.proj)
        text = "\n".join(restored)
        self.assertIn("could not put it back", text)
        self.assertTrue(self.trash_entries())   # the copy is still there


@POSIX_LINKS
class TestJunctions(LinkBase):
    def test_a_junction_is_treated_like_a_link(self):
        link = self.link_out()
        real_islink = os.path.islink
        with mock.patch.object(core_mod.os.path, "islink",
                               lambda p: False if p == link
                               else real_islink(p)), \
                mock.patch.object(core_mod.os.path, "isjunction",
                                  lambda p: p == link, create=True):
            self.assertTrue(core_mod._is_link(link))
            skipped = {}
            files = [f for f, _ in core_mod._walk_files(
                self.claude, core_mod._Budget(), None, None, skipped)]
        self.assertFalse([f for f in files if "id_rsa" in f], files)
        self.assertEqual(skipped["outside"], [link])

    def test_a_link_nobody_can_see_is_still_caught_by_where_it_leads(self):
        link = self.link_out()
        with mock.patch.object(core_mod, "_is_link", lambda p: False):
            skipped = {}
            files = [f for f, _ in core_mod._walk_files(
                self.claude, core_mod._Budget(), None, None, skipped)]
        self.assertFalse([f for f in files if "id_rsa" in f], files)
        self.assertEqual(skipped["outside"], [link])

    def test_a_windows_mount_point_is_a_link(self):
        st = SimpleNamespace(st_reparse_tag=core_mod._REPARSE_MOUNT_POINT)
        with mock.patch.object(core_mod.os, "name", "nt"), \
                mock.patch.object(core_mod.os, "lstat", return_value=st), \
                mock.patch.object(core_mod.os.path, "islink",
                                  return_value=False):
            self.assertTrue(core_mod._is_link("C:\\x\\junction"))
        plain = SimpleNamespace(st_reparse_tag=0)
        with mock.patch.object(core_mod.os, "name", "nt"), \
                mock.patch.object(core_mod.os, "lstat", return_value=plain), \
                mock.patch.object(core_mod.os.path, "islink",
                                  return_value=False):
            self.assertFalse(core_mod._is_link("C:\\x\\folder"))

    def test_an_ordinary_folder_is_not_a_link(self):
        self.assertFalse(core_mod._is_link(self.proj))
        self.assertFalse(core_mod._is_link(os.path.join(self.proj, "nope")))


class TestInstallHooksFallbackWarning(Base):
    def run_install(self, script):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core_mod, "_console_script",
                               return_value=script), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.cmd_install_hooks(SimpleNamespace(write=False))
        return code, out.getvalue(), err.getvalue()

    def test_the_fallback_is_explained(self):
        code, out, err = self.run_install(None)
        self.assertEqual(code, 0)
        self.assertIn("python -I -m sessionsteward", err)
        self.assertIn("pip install", err)
        self.assertIn('"hooks"', out)    # the JSON is still printed

    def test_no_warning_with_an_installed_command(self):
        _code, _out, err = self.run_install(sys.executable)
        self.assertEqual(err, "")

    def test_write_warns_too(self):
        settings = os.path.join(self._tmp, "settings.json")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core_mod, "_console_script",
                               return_value=None), \
                mock.patch.object(cli_mod, "CLAUDE_SETTINGS", settings), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.cmd_install_hooks(SimpleNamespace(write=True))
        self.assertEqual(code, 0)
        self.assertIn("-I -m sessionsteward", err.getvalue())
        self.assertTrue(os.path.isfile(settings))


class TestHookCommandForms(unittest.TestCase):
    """The hook command in both situations, whatever this machine has."""

    def test_with_a_console_script(self):
        script = os.path.join(os.sep, "venv", "bin", "sessionsteward")
        with mock.patch.object(core_mod, "_console_script",
                               return_value=script):
            cmd = core_mod._self_cmd()
        self.assertEqual(shlex.split(cmd), [script])

    def test_without_one(self):
        with mock.patch.object(core_mod, "_console_script",
                               return_value=None):
            cmd = core_mod._self_cmd()
        self.assertEqual(shlex.split(cmd)[:4],
                         [sys.executable, "-I", "-m", "sessionsteward"])

    def test_the_hook_that_install_wrote_is_recognised_either_way(self):
        for script in (None, os.path.join(os.sep, "v", "bin", "sessionsteward")):
            with mock.patch.object(core_mod, "_console_script",
                                   return_value=script):
                cmd = f'{core_mod._self_cmd()} precompact'
            self.assertTrue(core_mod._is_ours("PreCompact", cmd), cmd)


class TestGitWithoutEndOfOptions(Base):
    def setUp(self):
        super().setUp()
        core_mod._git_version.cache_clear()
        self.addCleanup(core_mod._git_version.cache_clear)

    def diff_args(self, version):
        seen = []
        with mock.patch.object(core_mod, "_git_version",
                               return_value=version), \
                mock.patch.object(core_mod, "_git",
                                  side_effect=lambda repo, *a: seen.append(a)
                                  or ""):
            core_mod.diff("abcd1234", "ef567890")
        return seen[0]

    def test_old_git_gets_no_end_of_options(self):
        self.assertNotIn("--end-of-options", self.diff_args((2, 23, 1)))

    def test_new_git_gets_it(self):
        self.assertIn("--end-of-options", self.diff_args((2, 24, 0)))
        self.assertIn("--end-of-options", self.diff_args((2, 43, 0)))
        self.assertIn("--end-of-options", self.diff_args((3, 0)))

    def test_unknown_version_is_assumed_recent(self):
        self.assertIn("--end-of-options", self.diff_args(None))

    def test_an_option_like_id_is_still_refused_on_old_git(self):
        with mock.patch.object(core_mod, "_git_version",
                               return_value=(2, 20, 0)):
            with self.assertRaises(core_mod.NothingToRestoreError):
                core_mod.diff("--output=/tmp/x", "HEAD")

    def parse(self, text):
        core_mod._git_version.cache_clear()
        done = SimpleNamespace(returncode=0, stdout=text.encode())
        with mock.patch.object(core_mod, "_run_git", return_value=done):
            return core_mod._git_version()

    def test_version_strings(self):
        self.assertEqual(self.parse("git version 2.43.0\n"), (2, 43, 0))
        self.assertEqual(self.parse("git version 2.39.3 (Apple Git-146)\n"),
                         (2, 39, 3))
        self.assertEqual(self.parse("git version 2.43.0.windows.1\n"),
                         (2, 43, 0))
        self.assertEqual(self.parse("git version 2.24\n"), (2, 24))
        self.assertIsNone(self.parse("not git"))

    def test_real_git_diff_works(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "# Project\n\nMore.\n")
        self.assertEqual(self.cli("snap", "-m", "two")[0], 0)
        code, out, _err = self.cli("diff")
        self.assertEqual(code, 0)
        self.assertIn("+More.", out)


class TestReadmeIsAccurate(unittest.TestCase):
    def setUp(self):
        path = os.path.join(REPO_ROOT, "README.md")
        if not os.path.isfile(path):
            self.skipTest("no README next to the package")
        with open(path, encoding="utf-8") as f:
            self.text = f.read()

    def test_git_version_is_stated(self):
        self.assertTrue("2.24" in self.text)

    def test_links_section_matches_the_behaviour(self):
        links = self.text.split("## Links", 1)[1].split("\n## ", 1)[0]
        for needle in ("junction", "warning", "restore"):
            self.assertTrue(needle in links, needle)

    def test_the_fallback_hooks_are_described(self):
        self.assertTrue("python -I -m sessionsteward" in self.text)
        self.assertTrue("pip install ." in self.text)


if __name__ == "__main__":
    unittest.main()
