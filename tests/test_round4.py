"""Round-4 tests: what a stranger who types `pip install` gets.

Temp dirs only; same hermetic environment as the earlier rounds.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod

try:
    from .test_round5_fixes import REPO_ROOT, Base
except ImportError:
    from test_round5_fixes import REPO_ROOT, Base


def read_pyproject() -> str:
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), encoding="utf-8") as f:
        return f.read()


class PackagingMetadata(unittest.TestCase):
    def test_only_the_package_is_built(self):
        # Without this line setuptools sees tests/, bench/ and the package
        # as three top-level packages and `python -m build` fails.
        self.assertRegex(read_pyproject(),
                         r'(?m)^packages = \["sessionsteward"\]')

    def test_author_and_urls_are_declared(self):
        text = read_pyproject()
        self.assertIn('authors = [{name = "Calas Automations Inc."}]', text)
        for key in ("Homepage", "Source", "Issues", "Changelog"):
            self.assertRegex(text, rf"(?m)^{key} = \"https://github.com/")

    def test_license_holder_matches_the_author(self):
        with open(os.path.join(REPO_ROOT, "LICENSE"), encoding="utf-8") as f:
            self.assertIn("Calas Automations Inc.", f.read())

    def test_changelog_names_the_current_version(self):
        with open(os.path.join(REPO_ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            head = f.read(400)
        m = re.search(r"(?m)^## (\d+\.\d+\.\d+)", head)
        self.assertTrue(m)
        from sessionsteward import __version__
        self.assertEqual(m.group(1), __version__)

    def test_sdist_manifest_ships_the_tests(self):
        # CI runs the tests from the unpacked sdist.
        with open(os.path.join(REPO_ROOT, "MANIFEST.in"), encoding="utf-8") as f:
            self.assertIn("tests", f.read())


class NumbersAreChecked(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli_mod.main(["--root", self.proj, *args])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_undo_steps_zero_or_negative_is_an_error_not_a_step(self):
        self.cli("init")
        for bad in ("0", "-3", "x"):
            code, out, err = self.run_cli("undo", "--steps", bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("--steps", err)
            self.assertNotIn("undone", out)

    def test_log_limit_zero_is_an_error(self):
        code, _, err = self.run_cli("log", "--limit", "0")
        self.assertEqual(code, 2)
        self.assertIn("1 or more", err)

    def test_root_that_does_not_exist_is_an_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            cli_mod.main(["--root", os.path.join(self._tmp, "nope"), "status"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("not a folder that exists", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class MachineReadable(Base):
    def test_log_json_is_valid_and_escapes_control_characters(self):
        import json
        self.cli("init")
        core_mod.snap(self.proj, "evil \x1b[31m\u202e message")
        code, out, err = self.cli("log", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        rows = json.loads(out)
        self.assertEqual(len(rows[0]["sha"]), 40)
        self.assertEqual(set(rows[0]), {"sha", "date", "message", "guard"})

    def test_log_json_without_a_repo_is_an_empty_list(self):
        code, out, _ = self.cli("log", "--json")
        self.assertEqual((code, out.strip()), (0, "[]"))

    def test_status_json_reports_unsaved_changes(self):
        import json
        self.cli("init")
        code, out, _ = self.cli("status", "--json")
        self.assertEqual(json.loads(out)["state"], "saved")
        with open(os.path.join(self.proj, "CLAUDE.md"), "a", encoding="utf-8") as f:
            f.write("more\n")
        data = json.loads(self.cli("status", "--json")[1])
        self.assertEqual(data["state"], "unsaved")
        self.assertEqual(data["unsaved"][0]["change"], "modified")
        self.assertIsInstance(data["snapshots"], int)

    def test_diff_with_an_unknown_snapshot_is_one_plain_line(self):
        self.cli("init")
        code, out, err = self.cli("diff", "nosuch")
        self.assertEqual(code, 1)
        self.assertIn("no such snapshot", err)
        self.assertNotIn("git diff", err)


class ConfigWriteIsAtomic(Base):
    def test_save_config_leaves_no_temp_file_and_reads_back(self):
        core_mod.save_config({"watch": ["NOTES.md"]})
        folder = os.path.dirname(core_mod.CONFIG_PATH)
        self.assertEqual(os.listdir(folder), ["config.json"])
        self.assertEqual(core_mod.load_config()["watch"], ["NOTES.md"])

    def test_a_failed_write_keeps_the_old_config(self):
        core_mod.save_config({"watch": ["OLD.md"]})

        class Boom:
            pass   # not JSON serialisable: json.dump raises TypeError

        with contextlib.suppress(TypeError):
            core_mod.save_config({"watch": [Boom()]})
        self.assertEqual(core_mod.load_config()["watch"], ["OLD.md"])
        self.assertEqual(os.listdir(os.path.dirname(core_mod.CONFIG_PATH)),
                         ["config.json"])


if __name__ == "__main__":
    unittest.main()
