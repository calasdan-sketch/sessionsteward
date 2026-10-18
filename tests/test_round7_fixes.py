"""Round-7 regression tests (third verification panel).

Temp dirs only, and the same hermetic environment as the earlier rounds
(tests/_isolate.py for the process, Base in the round-5 tests for every
test): HOME and the git configuration point into a private directory, no
GIT_* variable and no PYTHONUNBUFFERED reaches a child process.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

import sessionsteward
from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod

try:
    from .test_round5_fixes import Base, REPO_ROOT, plain_git, slurp, write
    from .test_round6_fixes import case_insensitive, listed
except ImportError:
    from test_round5_fixes import Base, REPO_ROOT, plain_git, slurp, write
    from test_round6_fixes import case_insensitive, listed


class TestTheVersionIsWrittenOnce(unittest.TestCase):
    """LOW (round 3, #8): the version lived in pyproject.toml and in cli.py,
    kept by hand, and nothing noticed when the two drifted apart."""

    def pyproject_version(self) -> str:
        path = os.path.join(REPO_ROOT, "pyproject.toml")
        if not os.path.isfile(path):
            self.skipTest("no pyproject.toml beside the package")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        table = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text,
                          re.M | re.S)
        self.assertIsNotNone(table, "pyproject.toml has no [project] table")
        found = re.search(r'^version\s*=\s*"([^"]+)"\s*$', table.group(1),
                          re.M)
        self.assertIsNotNone(found, "[project] has no static version")
        return found.group(1)

    def flag_version(self) -> str:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli_mod.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        return out.getvalue().split()[-1]

    def test_the_package_has_a_version(self):
        self.assertRegex(sessionsteward.__version__, r"^\d+\.\d+\.\d+$")

    def test_pyproject_the_package_and_the_flag_agree(self):
        self.assertEqual(self.pyproject_version(), sessionsteward.__version__)
        self.assertEqual(self.flag_version(), sessionsteward.__version__)

    def test_a_real_process_reports_the_same_version(self):
        p = subprocess.run([sys.executable, "-m", "sessionsteward",
                            "--version"], capture_output=True, text=True,
                           cwd=REPO_ROOT, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.split()[-1], sessionsteward.__version__)


class TestHelpKeepsItsLines(unittest.TestCase):
    """LOW (round 3, #6): `sessionsteward --help` printed the module
    docstring with its newlines collapsed - every command example ran into
    one paragraph."""

    def help_lines(self) -> list[str]:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli_mod.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        return out.getvalue().splitlines()

    def test_every_example_is_on_a_line_of_its_own(self):
        lines = self.help_lines()
        examples = [ln for ln in lines if ln.startswith("  sessionsteward ")]
        self.assertGreaterEqual(len(examples), 10, "\n".join(lines))
        for ln in examples:
            self.assertEqual(ln.count("sessionsteward "), 1, ln)

    def test_the_commands_the_readme_teaches_are_in_the_help(self):
        text = "\n".join(self.help_lines())
        for command in ("init", "snap -m", "precompact", "undo",
                        "restore <sha>", "diff", "diet", "install-hooks",
                        "watch --add", "watch --remove"):
            self.assertIn(f"sessionsteward {command}", text)


class TestHandEditedConfigNeverTracebacks(Base):
    """LOW (round 3, #4): a config.json edited by hand ended in a traceback
    (`{"unwatch": 5}`, `{"watch": [1]}`, a UTF-16 file from PowerShell 5's
    `>`), was iterated per character (`{"watch": "NOTES.md"}`), or - with a
    UTF-8 byte-order mark - was refused although it is fine."""

    def put(self, data: bytes) -> bytes:
        write(core_mod.CONFIG_PATH, data, binary=True)
        return data

    def assert_refused(self, *argv, mentions=(), code=2):
        before = slurp(core_mod.CONFIG_PATH)
        got, out, err = self.cli(*argv)
        self.assertEqual(got, code, (argv, out, err))
        self.assertEqual(len(err.strip().splitlines()), 1, (argv, err))
        self.assertTrue(err.startswith("error: "), err)
        self.assertNotIn("Traceback", err)
        self.assertIn(core_mod.CONFIG_PATH, err)
        for word in mentions:
            self.assertIn(word, err)
        self.assertEqual(slurp(core_mod.CONFIG_PATH), before, "config changed")
        return err

    def test_a_non_list_unwatch_is_one_error_line(self):
        self.put(b'{"unwatch": 5}')
        err = self.assert_refused("snap", mentions=("unwatch", "list"))
        self.assertIn("left as is", err)

    def test_a_string_instead_of_a_list_is_not_iterated_per_character(self):
        self.put(b'{"watch": "NOTES.md"}')
        self.assert_refused("snap", mentions=('"watch"', "list"))

    def test_entries_that_are_not_text_are_refused(self):
        for bad in (b'{"watch": [1, null]}', b'{"unwatch": ["a", 5]}',
                    b'{"watch": {"a": 1}}', b'{"watch": [["x"]]}',
                    b'{"unwatch": true}'):
            self.put(bad)
            self.assert_refused("snap")

    def test_every_command_that_reads_the_list_refuses_alike(self):
        self.put(b'{"watch": 7}')
        self.assert_refused("init", mentions=('"watch"',))
        os.remove(core_mod.CONFIG_PATH)
        self.assertEqual(self.cli("init")[0], 0)   # undo and restore need one
        self.put(b'{"watch": 7}')
        for argv in (("snap",), ("precompact",), ("undo",),
                     ("restore", "HEAD"), ("watch", "--add", "X.md"),
                     ("watch", "--remove", "X.md")):
            self.assert_refused(*argv, mentions=('"watch"',))

    def test_utf16_from_powershell_names_the_cure(self):
        # PowerShell 5's `>` and Out-File write UTF-16 LE with a BOM
        for data in ('{"watch": ["NOTES.md"]}'.encode("utf-16"),
                     '{"watch": []}'.encode("utf-16-le"),
                     b"\xfe\xff\x00{\x00}"):
            self.put(data)
            err = self.assert_refused("snap", mentions=("UTF-16", "UTF-8"))

    def test_invalid_utf8_is_one_error_line(self):
        self.put(b'{"watch": ["caf\xe9.md"]}')
        self.assert_refused("snap", mentions=("UTF-8",))

    def test_a_utf8_byte_order_mark_is_accepted(self):
        # Set-Content -Encoding utf8 (PowerShell 5) and Notepad write one
        self.put(b'\xef\xbb\xbf{"watch": ["NOTES.md"]}')
        write(os.path.join(self.proj, "NOTES.md"), "notes\n")
        self.assertEqual(core_mod.load_config()["watch"], ["NOTES.md"])
        code, out, err = self.cli("snap")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("files/NOTES.md", core_mod._git(
            core_mod.REPO_DIR, "ls-tree", "-r", "--name-only", "HEAD"))
        # editing the list writes plain UTF-8 back
        self.assertEqual(self.cli("watch", "--add", "OTHER.md")[0], 0)
        self.assertFalse(slurp(core_mod.CONFIG_PATH).startswith(b"\xef\xbb"))
        self.assertEqual(core_mod.load_config()["watch"],
                         ["NOTES.md", "OTHER.md"])

    def test_null_means_not_set(self):
        self.put(b'{"watch": null, "unwatch": null, "other": 1}')
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(self.cli("watch", "--add", "X.md")[0], 0)
        self.assertEqual(self.cli("watch", "--remove", "AGENTS.md")[0], 0)
        cfg = core_mod.load_config()
        self.assertEqual((cfg["watch"], cfg["unwatch"], cfg["other"]),
                         (["X.md"], ["AGENTS.md"], 1))

    def test_an_entry_the_system_cannot_look_up_is_ignored_as_before(self):
        # a path cannot hold a NUL: os.lstat and os.path.realpath raise
        # ValueError for it, and snap said "unexpected ValueError" for an
        # entry that has always just been dropped
        write(os.path.join(self.proj, "NOTES.md"), "notes\n")
        self.put(json.dumps({"watch": ["a\x00b", "NOTES.md"]}).encode())
        for argv in (("init",), ("snap",), ("precompact",)):
            code, out, err = self.cli(*argv)
            self.assertEqual((code, err), (0, ""), (argv, out))
        self.assertIn(os.path.join(self.proj, "NOTES.md"),
                      core_mod.watched_paths(self.proj))
        self.assertEqual(core_mod.unwatchable_entries(self.proj), [])

    def test_a_valid_config_is_still_read(self):
        self.put(b'{"watch": ["NOTES.md"], "unwatch": ["AGENTS.md"]}')
        self.assertEqual(core_mod.watch_list(),
                         [p for p in core_mod.DEFAULT_WATCH
                          if p != "AGENTS.md"] + ["NOTES.md"])

    def test_a_real_process_never_prints_a_traceback(self):
        for data in (b'{"unwatch": 5}', b'{"watch": "NOTES.md"}',
                     b'{"watch": [1]}', "{}".encode("utf-16"),
                     b"\xff\xfe\x00"):
            self.put(data)
            for argv in (("snap",), ("watch", "--add", "X.md")):
                p = self.run_child(*argv)
                self.assertEqual(p.returncode, 2, (data, p.stderr))
                self.assertNotIn(b"Traceback", p.stderr)
                self.assertEqual(len(p.stderr.strip().splitlines()), 1,
                                 p.stderr)


class TestTheSizeLimitVariableNeverTracebacks(Base):
    """LOW (round 3, #4): SESSIONSTEWARD_MAX_FILE_MB=1e305 is a finite
    number, but its byte count overflows a float: OverflowError."""

    def test_a_limit_too_large_to_count_means_no_limit(self):
        for text in ("1e305", "1e308", "1.7e308"):
            os.environ["SESSIONSTEWARD_MAX_FILE_MB"] = text
            try:
                self.assertIsNone(core_mod.max_file_bytes(), text)
            except OverflowError as e:   # the bug
                self.fail(f"{text}: {e}")

    def test_junk_and_ordinary_values_are_unchanged(self):
        for text, want in (("2", 2 * 1048576), ("0", None), ("", 10485760),
                           ("nan", 10485760), ("inf", 10485760),
                           ("1e400", 10485760), ("9" * 400, 10485760),
                           ("-1", 10485760),
                           ("abc", 10485760), ("1e30", int(1e30 * 1048576))):
            os.environ["SESSIONSTEWARD_MAX_FILE_MB"] = text
            self.assertEqual(core_mod.max_file_bytes(), want, text)

    def test_a_snap_with_the_huge_limit_works(self):
        os.environ["SESSIONSTEWARD_MAX_FILE_MB"] = "1e305"
        code, out, err = self.cli("init")
        self.assertEqual((code, err), (0, ""), out)
        p = self.run_child("snap")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"Traceback", p.stderr)


class TestNoTracebackReachesTheUser(Base):
    """What no check foresaw still ends as one `error:` line, exit 1."""

    def test_an_unexpected_exception_is_one_line_and_exit_1(self):
        for exc in (TypeError("boom"), ValueError("bad value"),
                    OverflowError("too big"), UnicodeDecodeError(
                        "utf-8", b"\xff", 0, 1, "invalid start byte"),
                    KeyError("k"), AttributeError("no such attribute")):
            with mock.patch.object(core_mod, "snap_report",
                                   side_effect=exc):
                code, out, err = self.cli("snap")
            self.assertEqual(code, 1, (exc, out, err))
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("error: unexpected "), err)
            self.assertIn(type(exc).__name__, err)
            self.assertNotIn("Traceback", err)

    def test_known_errors_keep_their_exit_codes(self):
        with mock.patch.object(core_mod, "snap_report",
                               side_effect=core_mod.StewardError("known")):
            self.assertEqual(self.cli("snap")[0], 2)
        with mock.patch.object(core_mod, "snap_report",
                               side_effect=OSError(28, "No space left")):
            self.assertEqual(self.cli("snap")[0], 1)

    def test_ctrl_c_is_not_swallowed(self):
        with mock.patch.object(core_mod, "snap_report",
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("snap")


class TestWatchRefusesWhatCanNeverBeMirrored(Base):
    """MED (round 3, #3): `watch --add .` (or `~`) was accepted; afterwards
    snap, undo, restore and precompact all exited 2 with "cannot watch
    <root>" and no way out, and the installed SessionStart and PreCompact
    hooks failed on every run - no snapshot was taken at all."""

    def config(self) -> bytes | None:
        try:
            return slurp(core_mod.CONFIG_PATH)
        except OSError:
            return None

    def root_spellings(self):
        return (".", "./", "", self.proj, os.path.join(self.proj, "sub", ".."),
                self.proj + os.sep)

    def home_spellings(self):
        return ("~", "~/", "~\\", self._home, self._home + os.sep)

    def assert_refused(self, *argv, mentions=()):
        before = self.config()
        code, out, err = self.cli(*argv)
        self.assertEqual(code, 2, (argv, out, err))
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertTrue(err.startswith("error: cannot watch "), err)
        self.assertNotIn("Traceback", err)
        for word in mentions:
            self.assertIn(word, err)
        self.assertEqual(self.config(), before, "the config was touched")
        return err

    def test_the_project_root_is_refused_at_add(self):
        for entry in self.root_spellings():
            err = self.assert_refused("watch", "--add", entry,
                                      mentions=("project root", "watch --add"))
        self.assertFalse(os.path.exists(core_mod.REPO_DIR), "touched the repo")

    def test_the_home_directory_is_refused_at_add(self):
        for entry in self.home_spellings():
            self.assert_refused("watch", "--add", entry,
                                mentions=("home directory", "watch --add"))

    def test_the_root_of_a_drive_is_refused_at_add(self):
        self.assert_refused("watch", "--add", os.path.abspath(os.sep),
                            mentions=("root of a drive", "watch --add"))

    def test_the_snapshots_folder_is_refused_at_add(self):
        # it ends in a mirror of itself, and every snap after it fails
        store = os.path.dirname(core_mod.REPO_DIR)
        for entry in (store, core_mod.REPO_DIR,
                      os.path.join(core_mod.REPO_DIR, "files"),
                      "~/.sessionsteward"):
            self.assert_refused("watch", "--add", entry,
                                mentions=("keeps its snapshots",))

    def link(self, name, target):
        """A symlink called `name` in the project, to `target`."""
        try:
            os.symlink(target, os.path.join(self.proj, name),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not let the test make a symlink")
        return name

    def test_a_link_to_the_project_root_is_the_project_root(self):
        self.link("self", self.proj)
        self.assert_refused("watch", "--add", "self",
                            mentions=("project root", "watch --add"))

    def test_a_link_to_the_home_directory_is_the_home_directory(self):
        # a walk of it met the snapshots folder, and so its own mirror: snap
        # died with "maximum recursion depth exceeded"
        self.link("homelink", self._home)
        self.assert_refused("watch", "--add", "homelink",
                            mentions=("home directory", "watch --add"))

    def test_a_link_to_the_snapshots_folder_is_refused_at_add(self):
        os.makedirs(core_mod.REPO_DIR)
        self.link("store", os.path.dirname(core_mod.REPO_DIR))
        self.assert_refused("watch", "--add", "store",
                            mentions=("keeps its snapshots",))

    def test_the_store_is_the_store_whichever_way_it_is_spelled(self):
        # your home directory reached through a link (a symlinked /home, the
        # /var of macOS): the store is known by one spelling, the entry
        # arrives by the other
        real = os.path.join(self._tmp, "elsewhere")
        alias = os.path.join(self._tmp, "alias")
        os.makedirs(os.path.join(real, "snapshots"))
        try:
            os.symlink(real, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not let the test make a symlink")
        with mock.patch.object(core_mod, "REPO_DIR",
                               os.path.join(alias, "snapshots")):
            self.assert_refused("watch", "--add",
                                os.path.join(real, "snapshots"),
                                mentions=("keeps its snapshots",))
            self.assert_refused("watch", "--add", real,
                                mentions=("keeps its snapshots",))

    def test_a_link_already_in_the_config_is_skipped_with_the_cure(self):
        write(os.path.join(self.proj, "NOTES.md"), "notes\n")
        self.link("homelink", self._home)
        write(core_mod.CONFIG_PATH, json.dumps(
            {"watch": ["homelink", "NOTES.md"]}), binary=False)
        for argv in (("init",), ("snap",), ("precompact",)):
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 0, (argv, out, err))
            self.assertEqual(err.count("warning: not watching 'homelink'"), 1,
                             err)
            self.assertIn("watch --remove homelink", err)
        self.assertIn(os.path.join(self.proj, "NOTES.md"),
                      core_mod.watched_paths(self.proj))

    def test_a_link_to_an_ordinary_folder_is_still_accepted(self):
        write(os.path.join(self.proj, "docs", "plan.md"), "plan\n")
        self.link("docs-link", os.path.join(self.proj, "docs"))
        code, out, err = self.cli("watch", "--add", "docs-link")
        self.assertEqual((code, err), (0, ""), out)

    def test_one_bad_entry_among_good_ones_adds_nothing(self):
        write(os.path.join(self.proj, "NOTES.md"), "notes\n")
        self.assertEqual(self.cli("init")[0], 0)
        self.assert_refused("watch", "--add", "NOTES.md", ".")
        self.assertNotIn("NOTES.md", core_mod.watch_list())

    def test_the_project_root_is_refused_when_home_is_the_project(self):
        self.assertEqual(self.cli("init", root=self._home)[0], 0)
        before = self.config()
        for entry in (".", "~", self._home):
            code, out, err = self.cli("watch", "--add", entry, root=self._home)
            self.assertEqual(code, 2, (entry, out, err))
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("itself", err)
        self.assertEqual(self.config(), before)

    def test_remove_still_works_for_every_one_of_them(self):
        self.assertEqual(self.cli("init")[0], 0)
        for entry in (".", "~", "/"):
            write(core_mod.CONFIG_PATH, json.dumps(
                {"watch": [entry, "NOTES.md"]}), binary=False)
            code, out, err = self.cli("watch", "--remove", entry)
            self.assertEqual((code, err), (0, ""), out)
            self.assertEqual(core_mod.load_config()["watch"], ["NOTES.md"])

    def test_ordinary_entries_are_still_accepted(self):
        write(os.path.join(self.proj, "docs", "plan.md"), "plan\n")
        for entry in ("docs/plan.md", "docs", "NOTES.md", "~/.claude/settings.json",
                      "../elsewhere.md", self._home + "/x.md"):
            code, out, err = self.cli("watch", "--add", entry)
            self.assertEqual((code, err), (0, ""), (entry, out))
        self.assertIn("docs/plan.md", core_mod.load_config()["watch"])

    def test_the_old_message_names_the_way_out(self):
        with self.assertRaises(core_mod.StewardError) as cm:
            core_mod._rel(self.proj, self.proj)
        self.assertIn("watch --remove", str(cm.exception))


class TestAnEntryAlreadyInTheConfigIsSkippedNotFatal(Base):
    """The same entries, already in a config.json (from a version that
    accepted them, or typed by hand): the hooks must go on protecting
    everything else, and the one line on stderr names the cure."""

    def setUp(self):
        super().setUp()
        self.notes = os.path.join(self.proj, "NOTES.md")
        write(self.notes, "notes v1\n")
        write(core_mod.CONFIG_PATH, json.dumps(
            {"watch": [".", "~", "NOTES.md"]}))
        self.assertEqual(self.cli("init")[0], 0)

    def warnings(self, err: str) -> list[str]:
        return [ln for ln in err.splitlines() if ln.startswith("warning: ")]

    def test_snap_precompact_undo_and_restore_all_work(self):
        write(self.notes, "notes v2\n")
        code, out, err = self.cli("snap", "-m", "two")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.warnings(err)), 2, err)
        code, out, err = self.cli("precompact")
        self.assertEqual(code, 0, out + err)
        write(self.notes, "clobbered\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(self.notes), b"notes v2\n")
        first = self.log()[-1]["sha"]
        write(self.notes, "clobbered again\n")
        code, out, err = self.cli("restore", first)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(self.notes), b"notes v1\n")
        self.assertEqual(len(self.warnings(err)), 2, err)

    def test_the_warning_names_the_entry_and_the_cure(self):
        code, out, err = self.cli("snap")
        self.assertEqual(code, 0, out + err)
        text = "\n".join(self.warnings(err))
        self.assertIn("not watching '.'", text)
        self.assertIn("the project root itself", text)
        self.assertIn("watch --remove .", text)
        self.assertIn("not watching '~'", text)
        self.assertIn("your home directory itself", text)
        # '~' is quoted: an unquoted ~ is replaced by the shell with the path
        # of the home directory, which is not the entry that was stored
        if os.name != "nt":
            self.assertIn("watch --remove '~'", text)

    def test_removing_the_entries_ends_the_warnings(self):
        code, out, err = self.cli("watch", "--remove", ".", "~")
        self.assertEqual((code, err), (0, ""), out)
        write(self.notes, "notes v2\n")
        code, out, err = self.cli("snap")
        self.assertEqual((code, err), (0, ""), out)

    def test_the_bad_entries_are_not_watched_paths(self):
        paths = core_mod.watched_paths(self.proj)
        self.assertNotIn(self.proj, paths)
        self.assertNotIn(self._home, paths)
        self.assertIn(self.notes, paths)
        self.assertEqual([e for e, _why in core_mod.unwatchable_entries(
            self.proj)], [".", "~"])

    def test_a_real_process_is_not_a_traceback(self):
        write(self.notes, "notes v3\n")
        for argv in (("snap",), ("precompact",), ("undo",)):
            p = self.run_child(*argv)
            self.assertEqual(p.returncode, 0, (argv, p.stderr))
            self.assertNotIn(b"Traceback", p.stderr)
            self.assertEqual(p.stderr.decode().count("warning: not watching"),
                             2, p.stderr)

    def test_the_daemon_warns_once_and_watches_the_rest(self):
        stop = mock.Mock(sleep=mock.Mock(side_effect=KeyboardInterrupt))
        with mock.patch.object(cli_mod, "time", stop):
            code, out, err = self.cli("watch", "--interval", "0.1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.warnings(err)), 2, err)


DECOMPOSED = "cafe" + chr(0x301)      # e + combining acute: what macOS lists
COMPOSED = "caf" + chr(0xE9)          # e-acute: what git records there


class CaseInsensitiveLookups:
    """Names are looked up without regard to case (and to Unicode
    composition) under `bases`, as on Windows and macOS - on any file system. Only the LOOKUP of an existing
    path is emulated (stat and lstat, so os.path.exists, isfile, isdir and
    lexists answer as they do there): a file is still created and listed in
    the case it was created in. That is the half of a case-insensitive file
    system that makes a watch-list entry in the wrong case resolve to a file
    and a mirror path collide with a differently spelled one; git's half is
    covered by the round-6 tests, the whole of it by the real-file-system
    tests below, which run wherever the temp directory is case-insensitive.
    The real call goes first, so nothing that already works is touched."""

    def __init__(self, *bases: str):
        self.bases = [os.path.abspath(b) for b in bases]
        self._real = {"stat": os.stat, "lstat": os.lstat}

    @staticmethod
    def fold(name):
        """What macOS treats as one name: case, and composition"""
        return unicodedata.normalize("NFC", name).casefold()

    def resolve(self, path):
        if not isinstance(path, (str, os.PathLike)):
            return path
        text = os.path.abspath(os.fspath(path))
        for base in self.bases:
            if text == base or text.startswith(base + os.sep):
                break
        else:
            return path
        cur = base
        for part in text[len(base):].split(os.sep):
            if not part:
                continue
            nxt = os.path.join(cur, part)
            try:
                self._real["lstat"](nxt)
            except OSError:
                try:
                    names = os.listdir(cur)
                except OSError:
                    return path
                same = [n for n in names if self.fold(n) == self.fold(part)]
                if not same:
                    return path
                nxt = os.path.join(cur, sorted(same)[0])
            cur = nxt
        return cur

    def __enter__(self):
        self._patches = []
        for name, func in self._real.items():
            def wrapper(path, *args, _func=func, **kwargs):
                try:
                    return _func(path, *args, **kwargs)
                except FileNotFoundError:
                    other = self.resolve(path)
                    if other == path:
                        raise
                    return _func(other, *args, **kwargs)
            patch = mock.patch.object(os, name, wrapper)
            patch.start()
            self._patches.append(patch)
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()


class EntrySpelling:
    """MED (round 3, #1): a watch-list entry spelled in another case than the
    file on disk, that also lies inside a watched folder, made init, snap,
    undo and restore exit 2 "snapshot incomplete ... (first: files/...)".
    The mirror of the folder holds the file in its real case, the entry's
    mirror path (spelled as typed) is "there already" on a case-insensitive
    file system, so the copy is skipped - and the check then misses the
    entry's own spelling. The cure is in the record: every entry is stored
    under the spelling the file system gives it. Run twice: with the lookups
    emulated (any file system) and with nothing emulated (wherever the temp
    directory is case-insensitive)."""

    def ci(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, "CLAUDE.md")
        self.dot = os.path.join(self.proj, ".claude")
        self.rules = os.path.join(self.dot, "rules")

    def run_ci(self, *argv, root=None):
        with self.ci():
            return self.cli(*argv, root=root)

    def ok(self, *argv, root=None):
        code, out, err = self.run_ci(*argv, root=root)
        self.assertEqual(code, 0, f"{argv}: {out}{err}")
        return out, err

    def head_names(self):
        return listed("HEAD")

    def assert_recovers(self, path, good: bytes, root=None):
        """clobber `path`, undo, and the good content is back"""
        write(path, "- clobbered\n")
        self.ok("undo", root=root)
        self.assertEqual(slurp(path), good)

    # (a) project == HOME: the default entry ~/.claude/CLAUDE.md, the file
    # is .claude/claude.md
    def test_a_default_entry_in_the_other_case_in_the_home_project(self):
        lower = os.path.join(self._home, ".claude", "claude.md")
        write(lower, "user memory\n")
        write(os.path.join(self._home, ".claude", "rules", "r.md"), "rule\n")
        self.ok("init", root=self._home)                      # init
        first = self.log(root=self._home)[0]["sha"]
        write(lower, "user memory v2\n")
        self.ok("snap", "-m", "v2", root=self._home)          # snap
        self.assertIn("files/.claude/claude.md", self.head_names())
        self.assertNotIn("files/.claude/CLAUDE.md", self.head_names())
        manifest = core_mod._manifest_at("HEAD")
        self.assertEqual(manifest["kinds"][".claude/claude.md"], "file")
        self.assertNotIn(".claude/CLAUDE.md", manifest["files"])
        self.assert_recovers(lower, b"user memory v2\n", root=self._home)  # undo
        write(lower, "- clobbered\n")
        self.ok("restore", first, root=self._home)            # restore
        self.assertEqual(slurp(lower), b"user memory\n")

    # (b) the file is Style.md, the entry says style.md
    def test_an_added_entry_in_the_other_case_than_the_file(self):
        style = os.path.join(self.rules, "Style.md")
        write(style, "style v1\n")
        self.ok("init")
        first = self.log()[0]["sha"]
        self.ok("watch", "--add", ".claude/rules/style.md")
        write(style, "style v2\n")
        self.ok("snap", "-m", "v2")
        self.assertIn("files/.claude/rules/Style.md", self.head_names())
        self.assertNotIn("files/.claude/rules/style.md", self.head_names())
        self.assert_recovers(self.claude, b"# Project\n\nBe helpful.\n")
        write(self.claude, "- clobbered\n")
        self.ok("restore", first)
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")
        self.assertEqual(slurp(style), b"style v1\n")

    # (c) the entry says Style.md, and the file is renamed to style.md
    def test_a_file_renamed_by_case_under_an_entry_that_names_it(self):
        style = os.path.join(self.rules, "Style.md")
        write(style, "style v1\n")
        self.ok("init")
        self.ok("watch", "--add", ".claude/rules/Style.md")
        self.ok("snap", "-m", "named")
        named = self.log()[0]["sha"]
        os.rename(style, os.path.join(self.rules, "tmp.md"))
        os.rename(os.path.join(self.rules, "tmp.md"),
                  os.path.join(self.rules, "style.md"))
        write(self.claude, "- clobbered\n")      # the point: this comes back
        self.ok("snap", "-m", "renamed")                      # snap
        renamed = self.log()[0]["sha"]
        names = self.head_names()
        self.assertIn("files/.claude/rules/style.md", names)
        self.assertNotIn("files/.claude/rules/Style.md", names)
        self.ok("undo")                                       # undo
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")
        write(self.claude, "- clobbered again\n")
        self.ok("restore", named)                             # restore
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")
        self.ok("restore", renamed)
        self.assertEqual(slurp(self.claude), b"- clobbered\n")
        # the file kept the name it has now: nothing was trashed and put
        # back under the old one
        self.assertEqual(os.listdir(self.rules), ["style.md"])
        self.assertEqual(self.trash_entries(), [])

    def test_undo_straight_after_the_rename_before_any_snap(self):
        # the old version refused here too: undo snapshots the dirty state
        # first, and that snapshot is the one that was refused
        style = os.path.join(self.rules, "Style.md")
        write(style, "style v1\n")
        self.ok("init")
        self.ok("watch", "--add", ".claude/rules/Style.md")
        self.ok("snap", "-m", "named")
        os.rename(style, os.path.join(self.rules, "tmp.md"))
        os.rename(os.path.join(self.rules, "tmp.md"),
                  os.path.join(self.rules, "style.md"))
        self.assert_recovers(self.claude, b"# Project\n\nBe helpful.\n")

    # (d) beyond the panel's list: a DIRECTORY entry in the other case,
    # inside a watched folder. The snapshot was fine; the restore found no
    # files/.claude/Rules in the tree, took it for an empty folder and
    # swapped the folder it had just restored for an empty one.
    def test_a_folder_entry_in_the_other_case_inside_a_watched_folder(self):
        style = os.path.join(self.rules, "style.md")
        write(style, "style v1\n")
        self.ok("init")
        self.ok("watch", "--add", ".claude/Rules")
        write(style, "style v2\n")
        self.ok("snap", "-m", "v2")
        self.assertIn("files/.claude/rules/style.md", self.head_names())
        write(style, "- clobbered\n")
        write(self.claude, "- clobbered\n")
        out, _ = self.ok("undo")
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")
        self.assertEqual(slurp(style), b"style v2\n")
        self.assertEqual(os.listdir(self.rules), ["style.md"])
        self.assertNotIn("Rules", " ".join(self.trash_entries()))

    def test_the_same_file_named_twice_in_two_cases_is_one_entry(self):
        write(os.path.join(self.rules, "Style.md"), "style\n")
        self.ok("init")
        self.ok("watch", "--add", ".claude/rules/style.md",
                ".claude/RULES/STYLE.MD")
        self.ok("snap", "-m", "twins")
        manifest = core_mod._manifest_at("HEAD")
        self.assertEqual(sorted(manifest["files"]),
                         [".claude", ".claude/rules/Style.md", "CLAUDE.md"])
        self.assert_recovers(self.claude, b"# Project\n\nBe helpful.\n")

    def test_an_exact_spelling_is_recorded_as_it_always_was(self):
        write(os.path.join(self.rules, "Style.md"), "style\n")
        self.ok("init")
        self.ok("watch", "--add", ".claude/rules/Style.md")
        self.ok("snap", "-m", "exact")
        self.assertEqual(sorted(core_mod._manifest_at("HEAD")["files"]),
                         [".claude", ".claude/rules/Style.md", "CLAUDE.md"])

    def test_an_entry_outside_the_project_is_recorded_as_spelled_on_disk(self):
        outside = os.path.join(self._tmp, "Outside", "Notes.md")
        write(outside, "outside\n")
        self.ok("watch", "--add", outside.replace("Outside", "outside")
                .replace("Notes.md", "notes.md"))
        self.ok("init")
        write(outside, "outside v2\n")
        self.ok("snap", "-m", "outside")
        files = core_mod._manifest_at("HEAD")["files"]
        self.assertIn(outside, files.values())
        self.assertEqual([p for p in files.values()
                          if p.casefold() == outside.casefold()], [outside])
        self.assert_recovers(outside, b"outside v2\n")


    # What is left of the wedge: the spelling the watch list has is not the
    # one the file system gives (the old behaviour, put back by hand).
    @contextlib.contextmanager
    def spelled_as_typed(self):
        with mock.patch.object(
                core_mod, "_on_disk_spelling",
                lambda path, project_root, listings=None:
                    os.path.abspath(path)):
            yield

    def wedge(self):
        """Case (b) under the old behaviour; returns the entry and the
        error text of the refused snap."""
        write(os.path.join(self.rules, "Style.md"), "style v1\n")
        self.ok("init")
        self.ok("watch", "--add", ".claude/rules/style.md")
        with self.spelled_as_typed():
            code, out, err = self.run_ci("snap", "-m", "refused")
        self.assertEqual(code, 2, out + err)
        return ".claude/rules/style.md", err

    def test_the_refusal_that_remains_names_the_entry_to_drop(self):
        entry, err = self.wedge()
        self.assertIn("snapshot incomplete", err)
        self.assertIn("files/.claude/rules/style.md", err)
        self.assertIn(f"`sessionsteward watch --remove {entry}`", err)
        # and the remedy works, whatever the spelling of the others
        with self.spelled_as_typed():
            self.ok("watch", "--remove", entry)
            self.ok("snap", "-m", "after the remedy")

    def test_the_next_snap_heals_what_the_refused_ones_left_behind(self):
        _entry, _err = self.wedge()
        head = core_mod._git(core_mod.REPO_DIR, "rev-parse", "HEAD").strip()
        with self.spelled_as_typed():
            self.assertEqual(self.run_ci("snap")[0], 2)   # still refused
        self.assertEqual(core_mod._git(core_mod.REPO_DIR, "rev-parse",
                                       "HEAD").strip(), head)
        self.ok("snap", "-m", "healed")                    # the new version
        self.assertIn("files/.claude/rules/Style.md", self.head_names())
        self.assertNotEqual(core_mod._git(core_mod.REPO_DIR, "rev-parse",
                                          "HEAD").strip(), head)

    def test_a_snapshot_spelled_as_the_watch_list_had_it_restores_in_place(self):
        # an older version recorded the entry as typed (notes.md); the file
        # is called Notes.md now. Nothing may be moved to the trash and
        # written again under the other name.
        notes = os.path.join(self.proj, "notes.md")
        write(notes, "notes v1\n")
        self.ok("watch", "--add", "notes.md")
        self.ok("init")
        typed = self.log()[0]["sha"]
        self.assertIn("notes.md", core_mod._manifest_at(typed)["files"])
        os.rename(notes, os.path.join(self.proj, "Notes.md"))
        notes = os.path.join(self.proj, "Notes.md")
        with self.ci():
            # the live files are that snapshot, as far as content goes
            self.assertEqual(core_mod.current_position(self.log(), self.proj),
                             0)
        write(notes, "- clobbered\n")
        self.ok("restore", typed)
        self.assertEqual(slurp(notes), b"notes v1\n")
        self.assertIn("Notes.md", os.listdir(self.proj))
        self.assertNotIn("notes.md", os.listdir(self.proj))
        self.assertEqual(self.trash_entries(), [])

    def test_on_disk_spelling_changes_the_names_below_the_anchor_only(self):
        write(os.path.join(self.proj, "Docs", "Plan.TXT"), "plan\n")
        spell = core_mod._on_disk_spelling
        with self.ci():
            self.assertEqual(
                spell(os.path.join(self.proj, "docs", "plan.txt"), self.proj),
                os.path.join(self.proj, "Docs", "Plan.TXT"))
            # a name that is not there, and what follows it, stay as typed
            self.assertEqual(
                spell(os.path.join(self.proj, "docs", "gone", "X.md"),
                      self.proj),
                os.path.join(self.proj, "Docs", "gone", "X.md"))
            # the anchor is how the project is identified: never respelled
            self.assertEqual(spell(self.proj, self.proj), self.proj)
            # the same names, asked for twice, are listed once
            listings = {}
            spell(os.path.join(self.proj, "docs", "plan.txt"), self.proj,
                  listings)
            self.assertEqual(sorted(listings),
                             sorted([self.proj,
                                     os.path.join(self.proj, "Docs")]))


class TestEntrySpellingEmulated(EntrySpelling, Base):
    def ci(self):
        return CaseInsensitiveLookups(self._tmp)


class TestEntryComposition(Base):
    """The same wedge with a name spelled composed in the watch list and
    decomposed on disk (macOS lists names decomposed and finds either). Only
    the lookup is emulated, with hand-made names - never run on a Mac."""

    def test_an_entry_typed_composed_names_the_file_listed_decomposed(self):
        rules = os.path.join(self.proj, ".claude", "rules")
        write(os.path.join(rules, DECOMPOSED + ".md"), "cafe\n")
        with CaseInsensitiveLookups(self._tmp):
            self.assertEqual(self.cli("init")[0], 0)
            code, out, err = self.cli("watch", "--add",
                                      f".claude/rules/{COMPOSED}.md")
            self.assertEqual(code, 0, out + err)
            code, out, err = self.cli("snap", "-m", "typed composed")
            self.assertEqual(code, 0, out + err)
        files = core_mod._manifest_at("HEAD")["files"]
        self.assertEqual(
            sorted(rel for rel in files if rel.endswith(".md")
                   and "rules" in rel),
            [f".claude/rules/{DECOMPOSED}.md"])
        write(os.path.join(rules, DECOMPOSED + ".md"), "- clobbered\n")
        with CaseInsensitiveLookups(self._tmp):
            self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(slurp(os.path.join(rules, DECOMPOSED + ".md")),
                         b"cafe\n")


@unittest.skipUnless(case_insensitive(tempfile.gettempdir()),
                     "needs a case-insensitive file system (Windows, macOS; "
                     "on Linux point TMPDIR at e.g. a ciopfs mount)")
class TestEntrySpellingOnARealCaseInsensitiveFileSystem(EntrySpelling, Base):
    def ci(self):
        return contextlib.nullcontext()


class TestAFileYouNamedIsNotReportedAsLeftOut(Base):
    """LOW (round 3, #5): `watch --add .claude/big.bin` (over the size limit,
    inside the watched .claude) is mirrored, restored by undo - and was still
    reported on every snap as "not snapshotted, over the limit". The limit
    applies to what is FOUND in a watched folder; a file named in the watch
    list is never left out."""

    def setUp(self):
        super().setUp()
        os.environ["SESSIONSTEWARD_MAX_FILE_MB"] = "0.001"    # 1048 bytes
        self.addCleanup(os.environ.pop, "SESSIONSTEWARD_MAX_FILE_MB", None)
        self.claude = os.path.join(self.proj, "CLAUDE.md")
        self.dot = os.path.join(self.proj, ".claude")
        self.big = os.path.join(self.dot, "big.bin")
        write(self.big, "x" * 3000)

    def warnings(self, err):
        return [line for line in err.splitlines() if line.strip()]

    def test_the_file_is_in_the_snapshot_and_nothing_is_said(self):
        self.assertEqual(self.cli("watch", "--add", ".claude/big.bin")[0], 0)
        for argv in (("init",), ("snap", "-m", "again"), ("precompact",)):
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(err, "", argv)
        self.assertIn("files/.claude/big.bin", listed())
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0)
        write(self.big, "clobbered")
        write(self.claude, "clobbered")
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(slurp(self.big), b"x" * 3000)

    def test_a_file_you_did_not_name_is_still_reported(self):
        other = os.path.join(self.dot, "other.bin")
        write(other, "y" * 4000)
        self.cli("watch", "--add", ".claude/big.bin")
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        lines = self.warnings(err)
        self.assertEqual(len(lines), 1, err)
        self.assertIn("other.bin", lines[0])
        self.assertNotIn("big.bin", lines[0])
        self.assertNotIn("files/.claude/other.bin", listed())
        self.assertIn("files/.claude/big.bin", listed())

    def test_a_file_in_a_folder_you_named_is_still_reported(self):
        # naming the folder is not naming the file inside it
        os.remove(self.big)
        self.cli("watch", "--add", "docs")
        write(os.path.join(self.proj, "docs", "huge.bin"), "z" * 5000)
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        self.assertIn("huge.bin", err)
        self.assertNotIn("files/docs/huge.bin", listed())


@unittest.skipIf(os.name == "nt", "POSIX permission bits")
class TestARestoreNeverWidensPermissions(Base):
    """MED (round 3, #2): restoring a folder (git archive + tar) wrote every
    file 0644 and every folder 0755: `.claude` 0700 holding secret.json 0600
    came back 755 and 644 (the trash copy kept the old modes). The group and
    other bits a path had are now recorded in the manifest (where they are
    narrower than git's own 0644 / 0755), and a restore gives a path those
    bits - minus any the live path has lost since, so it is never more open
    than either."""

    PATHS = (".claude", ".claude/secret.json", ".claude/skills",
             ".claude/skills/x.md", "CLAUDE.local.md")

    def setUp(self):
        super().setUp()
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.dot = os.path.join(self.proj, ".claude")
        self.secret = os.path.join(self.dot, "secret.json")
        self.skills = os.path.join(self.dot, "skills")
        self.skill = os.path.join(self.skills, "x.md")
        self.local = os.path.join(self.proj, "CLAUDE.local.md")
        write(self.secret, '{"token": "abc"}\n')
        write(self.skill, "skill\n")
        write(self.local, "private\n")
        self.expected = {".claude": 0o700, ".claude/secret.json": 0o600,
                         ".claude/skills": 0o750,
                         ".claude/skills/x.md": 0o640,
                         "CLAUDE.local.md": 0o600}
        self.set_modes(self.expected)

    def set_modes(self, modes):
        for rel in sorted(modes, key=lambda r: -r.count("/")):  # inner first
            os.chmod(os.path.join(self.proj, *rel.split("/")), modes[rel])

    def modes(self, rels=None):
        return {rel: stat.S_IMODE(os.stat(
                    os.path.join(self.proj, *rel.split("/"))).st_mode)
                for rel in (rels or self.PATHS)}

    def ok(self, *argv):
        code, out, err = self.cli(*argv)
        self.assertEqual(code, 0, f"{argv}: {out}{err}")
        return out

    def test_a_folder_comes_back_as_private_as_it_was(self):
        self.ok("init")
        write(self.secret, '{"token": "clobbered"}\n')   # in place
        self.ok("snap", "-m", "clobbered")
        self.ok("undo")
        self.assertEqual(slurp(self.secret), b'{"token": "abc"}\n')
        self.assertEqual(self.modes(), self.expected)

    def test_files_the_agent_wrote_again_are_private_again(self):
        self.ok("init")
        os.remove(self.secret)
        write(self.secret, "new\n")                  # 0644 under this umask
        os.remove(self.local)                        # gone altogether
        self.assertEqual(self.modes([".claude/secret.json"]),
                         {".claude/secret.json": 0o644})
        self.ok("undo")
        self.assertEqual(slurp(self.secret), b'{"token": "abc"}\n')
        self.assertEqual(slurp(self.local), b"private\n")
        self.assertEqual(self.modes(), self.expected)

    def test_a_file_written_in_place_is_narrowed_to_what_was_recorded(self):
        self.ok("init")
        write(self.local, "clobbered\n")
        os.chmod(self.local, 0o644)
        self.ok("undo")
        self.assertEqual(slurp(self.local), b"private\n")
        self.assertEqual(self.modes(), self.expected)

    def test_a_path_you_tightened_since_is_not_loosened(self):
        notes = os.path.join(self.dot, "notes.md")
        write(notes, "notes\n")                      # 0644: nothing recorded
        self.ok("init")
        os.chmod(notes, 0o600)
        write(notes, "clobbered\n")
        self.ok("undo")
        self.assertEqual(slurp(notes), b"notes\n")
        self.assertEqual(stat.S_IMODE(os.stat(notes).st_mode), 0o600)

    def test_group_and_other_write_bits_are_not_added(self):
        os.chmod(self.skills, 0o775)
        os.chmod(self.skill, 0o664)
        self.ok("init")
        write(self.skill, "clobbered\n")
        self.ok("undo")
        self.assertEqual(slurp(self.skill), b"skill\n")
        self.assertEqual(self.modes([".claude/skills", ".claude/skills/x.md"]),
                         {".claude/skills": 0o755,
                          ".claude/skills/x.md": 0o644})

    def test_an_executable_stays_executable_and_no_more_open(self):
        hook = os.path.join(self.dot, "hook.sh")
        write(hook, "#!/bin/sh\necho hi\n")
        os.chmod(hook, 0o750)
        self.ok("init")
        write(hook, "#!/bin/sh\necho clobbered\n")
        self.ok("undo")
        self.assertEqual(slurp(hook), b"#!/bin/sh\necho hi\n")
        self.assertEqual(stat.S_IMODE(os.stat(hook).st_mode), 0o750)

    def test_an_empty_folder_comes_back_as_private_as_it_was(self):
        private = os.path.join(self.proj, "private")
        os.makedirs(private)
        os.chmod(private, 0o700)
        self.ok("watch", "--add", "private")
        self.ok("init")
        write(os.path.join(private, "stray.txt"), "x\n")
        self.ok("undo")
        self.assertEqual(os.listdir(private), [])
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o700)

    def test_the_manifest_records_only_what_is_narrower_than_the_default(self):
        self.ok("init")
        manifest = core_mod._manifest_at("HEAD")
        self.assertEqual(
            manifest["modes"],
            {".claude": "0700", ".claude/secret.json": "0600",
             ".claude/skills": "0750", ".claude/skills/x.md": "0640",
             "CLAUDE.local.md": "0600"})
        self.assertEqual(list(manifest["modes"]), sorted(manifest["modes"]))
        # an ordinary tree adds nothing to the manifest
        self.set_modes({".claude": 0o755, ".claude/secret.json": 0o644,
                        ".claude/skills": 0o755, ".claude/skills/x.md": 0o644,
                        "CLAUDE.local.md": 0o644})
        self.ok("snap", "-m", "ordinary")
        self.assertNotIn("modes", core_mod._manifest_at("HEAD"))

    def test_a_snapshot_without_recorded_modes_still_never_widens(self):
        # what a snapshot from before 0.1.3 looks like
        self.ok("init")
        old = self.forge_commit("old", edit_manifest=lambda m: m.pop("modes"))
        write(self.secret, '{"token": "clobbered"}\n')
        self.ok("restore", old)
        self.assertEqual(slurp(self.secret), b'{"token": "abc"}\n')
        self.assertEqual(self.modes(), self.expected)

    def test_unreadable_entries_in_the_manifest_are_ignored(self):
        self.ok("init")
        bad = self.forge_commit("hand-edited", edit_manifest=lambda m:
                                m.update(modes={".claude": "banana",
                                                ".claude/secret.json": 5,
                                                "CLAUDE.local.md": None,
                                                ".claude/skills": "0o9"}))
        write(self.secret, '{"token": "clobbered"}\n')
        self.ok("restore", bad)
        self.assertEqual(slurp(self.secret), b'{"token": "abc"}\n')
        # nothing recorded that can be read: the live modes stand
        self.assertEqual(self.modes(), self.expected)


class TestADecomposedNameIsTheFileGitRecordedComposed(Base):
    """LOW (round 3, #7; macOS only, reproduced here with hand-made names,
    never on a Mac): macOS keeps a name like e-acute decomposed on disk and
    git (core.precomposeunicode) records it composed. _check_staged allowed
    for that, but the comparison of the live files with a snapshot - what
    `undo`, `restore` and `log` use to tell where you are - did not, so a
    file named that way looked changed for ever. The tests build the
    situation by hand: the file is decomposed on disk and the snapshot's tree
    holds the composed name."""

    def setUp(self):
        super().setUp()
        self.dot = os.path.join(self.proj, ".claude")
        self.live = os.path.join(self.dot, DECOMPOSED + ".md")
        write(self.live, "cafe\n")
        self.assertNotIn(COMPOSED + ".md", os.listdir(self.dot))

    def compose_the_snapshot(self):
        """What git does on a Mac: the newest snapshot, its names composed
        (replaced in place, so the history keeps one commit per state)."""
        repo = core_mod.REPO_DIR
        for root, dirs, names in os.walk(os.path.join(repo, "files"),
                                         topdown=False):
            for name in names + dirs:
                composed = unicodedata.normalize("NFC", name)
                if composed != name:
                    os.rename(os.path.join(root, name),
                              os.path.join(root, composed))
        core_mod._git(repo, "add", "-A", "-f")
        core_mod._git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
                      "commit", "-q", "--amend", "--no-edit", "--allow-empty")
        return core_mod._git(repo, "rev-parse", "HEAD").strip()

    def snapped(self):
        self.assertEqual(self.cli("init")[0], 0)
        return self.compose_the_snapshot()

    def test_the_live_files_still_are_that_snapshot(self):
        self.snapped()
        names = core_mod._git(core_mod.REPO_DIR, "ls-tree", "-r",
                              "--name-only", "HEAD")
        self.assertIn(COMPOSED + ".md", names)
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0)
        self.assertTrue(core_mod.worktree_matches_snapshot(
            self.log()[0]["sha"], self.proj))

    def test_undo_steps_back_from_there_instead_of_snapshotting_again(self):
        self.snapped()
        write(os.path.join(self.proj, "CLAUDE.md"), "v2\n")
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)
        self.compose_the_snapshot()
        before = [s["sha"] for s in self.log()]
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        # no "pre-undo" snapshot: the files were that snapshot all along
        self.assertEqual([s["sha"] for s in self.log()], before)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"# Project\n\nBe helpful.\n")

    def test_a_restore_finds_the_folder_unchanged(self):
        sha = self.snapped()
        code, out, err = self.cli("restore", sha)
        self.assertEqual(code, 0, out + err)
        self.assertIn("unchanged", out)
        self.assertEqual(self.trash_entries(), [])
        self.assertEqual(slurp(self.live), b"cafe\n")

    def test_a_watched_folder_with_a_decomposed_name(self):
        folder = os.path.join(self.proj, DECOMPOSED)
        write(os.path.join(folder, "x.md"), "x\n")
        self.assertEqual(self.cli("watch", "--add", DECOMPOSED)[0], 0)
        self.assertEqual(self.cli("init")[0], 0)
        self.compose_the_snapshot()
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0)

    def test_a_real_change_is_still_a_change(self):
        self.snapped()
        write(self.live, "changed\n")
        self.assertIsNone(core_mod.current_position(self.log(), self.proj))
        os.rename(self.live, os.path.join(self.dot, "other.md"))
        write(self.live, "cafe\n")
        self.assertIsNone(core_mod.current_position(self.log(), self.proj))

    def test_two_paths_are_never_merged_into_one(self):
        blob = "a" * 40
        composed, decomposed = "/p/" + COMPOSED, "/p/" + DECOMPOSED
        same = core_mod._same_tree
        self.assertTrue(same({}, {}))
        self.assertTrue(same({decomposed: blob}, {composed: blob}))
        self.assertTrue(same({composed: blob}, {composed: blob}))
        self.assertFalse(same({decomposed: blob}, {composed: "b" * 40}))
        self.assertFalse(same({decomposed: blob}, {"/p/other": blob}))
        # both spellings on one side are two files; the other side has one
        self.assertFalse(same({decomposed: blob, composed: blob},
                              {composed: blob}))
        self.assertFalse(same({composed: blob},
                              {decomposed: blob, composed: blob}))


if __name__ == "__main__":
    unittest.main()
