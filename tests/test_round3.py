"""Expert round 3: a hostile-input pass.

Credentials are assembled at run time; none is stored in this file.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

try:
    from . import _isolate  # noqa: F401
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import core as core_mod
from sessionsteward import diet as diet_mod
from sessionsteward import secrets as secrets_mod

try:
    from .test_round5_fixes import Base, write
except ImportError:
    from test_round5_fixes import Base, write

NPM = "np" + "m_" + "a1B2" * 9


class TestOldPython(unittest.TestCase):
    """requires-python is 3.10: no f-string may hold a backslash in its
    braces (a SyntaxError before 3.12 - the 0.3.0 line had one in core.py)."""

    def test_no_backslash_inside_fstring_braces(self):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for base in ("sessionsteward", "tests"):
            for fn in sorted(os.listdir(os.path.join(root, base))):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, base, fn)
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                for node in ast.walk(ast.parse(src)):
                    if isinstance(node, ast.FormattedValue):
                        seg = ast.get_source_segment(src, node.value) or ""
                        self.assertNotIn("\\", seg, f"{fn}:{node.lineno}")

    def test_compiles_on_the_oldest_python_when_present(self):
        import shutil
        import subprocess
        import sys
        exe = shutil.which("python3.10")
        if not exe:
            self.skipTest("python3.10 not installed")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([exe, "-c",
                            "import sys,glob;[compile(open(f,encoding='utf-8')"
                            ".read(),f,'exec') for f in glob.glob('sessionste"
                            "ward/*.py')]"], cwd=root, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-300:])


class TestSecretsScan(unittest.TestCase):
    def test_no_quadratic_time_on_one_long_line(self):
        # 20,000 characters of `a.a.a.` took 47 s with the old pattern
        for line in ("a." * 10000, "x-" * 100000, "api_key" * 20000):
            t = time.time()
            secrets_mod.scan_line(line)
            self.assertLess(time.time() - t, 5.0)

    def test_secret_far_into_a_long_line_is_found_and_redacted(self):
        line = "x " * 30000 + NPM + " tail"
        self.assertEqual(len(secrets_mod.scan_text(line)), 1)
        red, n = secrets_mod.redact_text(line)
        self.assertEqual(n, 1)
        self.assertNotIn(NPM, red)
        self.assertTrue(red.endswith("tail"))

    def test_token_cut_by_a_piece_boundary_is_found(self):
        for pad in range(secrets_mod.CHUNK - 45, secrets_mod.CHUNK + 5):
            line = " " * pad + NPM
            self.assertEqual(len(secrets_mod.scan_line(line)), 1, pad)

    def test_new_provider_shapes(self):
        for tok in (NPM, "glpat" + "-" + "a1" * 12, "hf" + "_" + "aB3" * 12,
                    "ya29" + "." + "a1B2" * 8):
            self.assertTrue(secrets_mod.scan_line("k " + tok), tok)

    def test_prose_is_not_flagged(self):
        self.assertEqual(secrets_mod.scan_line(
            "The token budget is explained in the password policy doc."), [])


class TestSafeText(unittest.TestCase):
    def test_bidi_controls_are_shown_as_question_marks(self):
        s = "a\u202eb\u2066c\u200fd\x1b[2Jf"
        out = diet_mod.safe(s)
        for ch in "\u202e\u2066\u200f\x1b":
            self.assertNotIn(ch, out)
        self.assertEqual(diet_mod.safe("plain é text"), "plain é text")


class TestHostileMessages(Base):
    def test_log_and_status_escape_a_hostile_message(self):
        self.cli("init")
        self.cli("snap", "-m", "hi\x1b[2Jthere \u202ex")
        for cmd in ("log", "status"):
            _, out, err = self.cli(cmd)
            self.assertNotIn("\x1b", out + err)
            self.assertNotIn("\u202e", out + err)


class TestNotCompared(Base):
    def test_lost_names_a_file_it_could_not_read_and_check_fails(self):
        self.cli("init")
        big = os.path.join(self.proj, "CLAUDE.md")
        write(big, "# Rules\n\n- Never force-push to main\n")
        self.cli("snap", "-m", "a")
        with mock.patch("sessionsteward.extras.MAX_TEXT_BYTES", 5):
            code, out, _ = self.cli("lost", "--live", "--check")
            self.assertEqual(code, 3)
            self.assertIn("NOT COMPARED", out)
            self.assertIn("over 4 MiB", out)
            code, out, _ = self.cli("lost", "--live", "--json")
            self.assertTrue(json.loads(out)["not_compared"])
        code, out, _ = self.cli("lost", "--live", "--check")
        self.assertEqual(code, 0)
        self.assertNotIn("NOT COMPARED", out)

    def test_utf16_file_is_read_not_skipped(self):
        self.cli("init")
        p = os.path.join(self.proj, "CLAUDE.md")
        with open(p, "wb") as f:
            f.write("# Rules\n\n- Always run the tests first\n".encode("utf-16"))
        self.cli("snap", "-m", "u16")
        write(p, "# Rules\n")
        self.cli("snap", "-m", "gone")
        _, out, _ = self.cli("lost")
        self.assertIn("run the tests first", out)

    def test_binary_file_with_text_name_is_reported_by_scan(self):
        self.cli("init")
        with open(os.path.join(self.proj, "CLAUDE.md"), "wb") as f:
            f.write(b"\x00\x01\x02" * 100)
        code, out, _ = self.cli("scan")
        self.assertIn("NOT SCANNED", out)


class TestSnapshotBudget(Base):
    def test_too_many_files_refused_before_anything_is_copied(self):
        self.cli("init")
        for i in range(6):
            write(os.path.join(self.proj, ".claude", "rules", f"r{i}.md"), "x\n")
        with mock.patch.dict(os.environ,
                             {"SESSIONSTEWARD_MAX_SNAPSHOT_FILES": "3"}):
            code, out, err = self.cli("snap")
        self.assertEqual(code, 2)
        self.assertIn("more than a snapshot may", err)
        self.assertEqual(len(core_mod.log(50, self.proj)), 1)   # init only
        code, _, _ = self.cli("snap")      # default limits: fine
        self.assertEqual(code, 0)

    def test_reading_commands_and_undo_still_work_over_the_limit(self):
        self.cli("init")
        p = os.path.join(self.proj, "CLAUDE.md")
        write(p, "# one\n")
        self.cli("snap", "-m", "one")
        write(p, "# two\n")
        self.cli("snap", "-m", "two")
        for i in range(6):
            write(os.path.join(self.proj, ".claude", "rules", f"r{i}.md"), "x\n")
        # these new files are unsaved; the budget must not lock the user out
        # of `log`, `status`, `lost` and `restore` of a saved snapshot
        with mock.patch.dict(os.environ,
                             {"SESSIONSTEWARD_MAX_SNAPSHOT_FILES": "3"}):
            for cmd in (("log",), ("status",), ("lost", "--live"),
                        ("undo", "--dry-run")):
                code, out, err = self.cli(*cmd)
                self.assertNotIn("more than a snapshot may", err, cmd)
            sha = core_mod.log(50, self.proj)[1]["sha"]
            code, out, err = self.cli("restore", sha)
        # restore saves the unsaved state first (pre-restore): refused, loudly,
        # and nothing was changed
        self.assertEqual(code, 2)
        with open(p) as f:
            self.assertEqual(f.read(), "# two\n")

    def test_size_limit_and_lifting_it(self):
        self.cli("init")
        write(os.path.join(self.proj, ".claude", "a.md"), "x" * 3000)
        with mock.patch.dict(os.environ,
                             {"SESSIONSTEWARD_MAX_SNAPSHOT_MB": "0.001"}):
            self.assertEqual(self.cli("snap")[0], 2)
        with mock.patch.dict(os.environ,
                             {"SESSIONSTEWARD_MAX_SNAPSHOT_MB": "0"}):
            self.assertEqual(self.cli("snap")[0], 0)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_link_to_a_big_folder_is_refused(self):
        self.cli("init")
        big = os.path.join(self._tmp, "bigdir")
        for i in range(8):
            write(os.path.join(big, f"f{i}.md"), "x\n")
        os.symlink(big, os.path.join(self.proj, ".claude", "linked"))
        with mock.patch.dict(os.environ,
                             {"SESSIONSTEWARD_MAX_SNAPSHOT_FILES": "5"}):
            code, _, err = self.cli("snap")
        self.assertEqual(code, 2)
        self.assertIn("a link to a big folder", err)


class TestPrune(Base):
    def _snaps(self, n):
        for i in range(n):
            write(os.path.join(self.proj, "CLAUDE.md"), f"# v{i}\n" * (i + 1))
            self.assertEqual(self.cli("snap", "-m", f"m{i}")[0], 0)

    def test_preview_changes_nothing(self):
        self.cli("init")
        self._snaps(5)
        before = [s["sha"] for s in core_mod.log(50, self.proj)]
        code, out, _ = self.cli("prune", "--keep", "2")
        self.assertEqual(code, 0)
        self.assertIn("nothing was changed", out)
        self.assertEqual([s["sha"] for s in core_mod.log(50, self.proj)], before)

    def test_write_keeps_newest_and_undo_still_works(self):
        self.cli("init")
        self._snaps(5)
        newest = core_mod.log(50, self.proj)[0]
        code, out, _ = self.cli("prune", "--keep", "2", "--write")
        self.assertEqual(code, 0)
        self.assertIn("forgotten", out)
        snaps = core_mod.log(50, self.proj)
        self.assertEqual([s["message"] for s in snaps], ["m4", "m3"])
        self.assertEqual(snaps[0]["date"], newest["date"])
        core_mod._git(core_mod.REPO_DIR, "fsck", "--no-dangling")
        code, out, _ = self.cli("undo")
        self.assertEqual(code, 0)
        with open(os.path.join(self.proj, "CLAUDE.md")) as f:
            self.assertEqual(f.read().count("# v3"), 4)

    def test_forgotten_secret_is_gone_from_the_store(self):
        self.cli("init")
        secret = "np" + "m_" + "Zq9X" * 9
        write(os.path.join(self.proj, "CLAUDE.md"), f"key {secret}\n")
        self.cli("snap", "-m", "oops")
        self._snaps(3)
        self.cli("prune", "--keep", "2", "--write")
        p = core_mod._run_git(core_mod.REPO_DIR,
                              ("log", "--all", "-p", "-S", secret))
        self.assertEqual(p.stdout.strip(), b"")

    def test_other_projects_are_kept(self):
        self.cli("init")
        self._snaps(4)
        other = os.path.join(self._tmp, "other")
        write(os.path.join(other, "CLAUDE.md"), "# other\n")
        self.cli("snap", "-m", "other-1", root=other)
        self.cli("prune", "--keep", "1", "--write")
        self.assertEqual(len(core_mod.log(50, other)), 1)
        self.assertEqual(len(core_mod.log(50, self.proj)), 1)

    def test_a_snapshot_without_a_readable_manifest_is_kept(self):
        self.cli("init")
        self._snaps(3)
        other = os.path.join(self._tmp, "other")
        write(os.path.join(other, "CLAUDE.md"), "# o\n")
        self.cli("snap", "-m", "o", root=other)
        with mock.patch.object(core_mod, "_manifests_at",
                               side_effect=lambda shas, strict=False: {
                                   s: {} for s in shas}):
            r = core_mod.prune(1, write=False)
        # every commit but the first looks torn: none may be dropped
        self.assertEqual(r["removed"], 0)

    def test_bad_keep(self):
        self.cli("init")
        code, _, err = self.cli("prune", "--keep", "0")
        self.assertNotEqual(code, 0)
        self.assertIn("--keep", err)


class TestDryRun(Base):
    def test_undo_and_restore_dry_run_touch_nothing(self):
        self.cli("init")
        p = os.path.join(self.proj, "CLAUDE.md")
        write(p, "# one\n")
        self.cli("snap", "-m", "one")
        write(p, "# two\n")
        self.cli("snap", "-m", "two")
        n = len(core_mod.log(50, self.proj))
        code, out, _ = self.cli("undo", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would be rewritten", out)
        self.assertIn("nothing was changed", out)
        write(p, "# dirty\n")             # unsaved: still no pre-undo snapshot
        code, out, _ = self.cli("undo", "--dry-run")
        self.assertIn("pre-undo", out)
        sha = core_mod.log(50, self.proj)[1]["sha"]
        code, out, _ = self.cli("restore", "--dry-run", sha[:7])
        self.assertIn("would be rewritten", out)
        with open(p) as f:
            self.assertEqual(f.read(), "# dirty\n")
        self.assertEqual(len(core_mod.log(50, self.proj)), n)

    def test_dry_run_with_nothing_to_change(self):
        self.cli("init")
        self.cli("snap", "-m", "a")
        write(os.path.join(self.proj, "CLAUDE.md"), "# b\n")
        self.cli("snap", "-m", "b")
        sha = core_mod.log(50, self.proj)[0]["sha"]
        _, out, _ = self.cli("restore", "--dry-run", sha)
        self.assertIn("already are that snapshot", out)


if __name__ == "__main__":
    unittest.main()
