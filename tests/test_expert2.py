"""Expert round 2: `lost`, `diet --lint / --fix / --history`, `scan`, `pin`,
`show --redact`, and the committed offline benchmark.

Credentials are assembled at run time; none is stored in this file.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

try:
    from . import _isolate  # noqa: F401
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import core as core_mod
from sessionsteward import diet as diet_mod
from sessionsteward import dietlint, lost, textlines as tl
from sessionsteward import secrets as secrets_mod

try:
    from .test_round5_fixes import Base, write, slurp
except ImportError:
    from test_round5_fixes import Base, write, slurp

AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
GH = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class TestTextLines(unittest.TestCase):
    def test_normalise_ignores_markers_case_and_emphasis(self):
        a = tl.normalise("- **Always** run `pytest`  before commit.")
        b = tl.normalise("1. always run pytest before commit")
        self.assertEqual(a, b)

    def test_units_skip_fences_rules_and_short_lines(self):
        t = "# Title here ok\n---\n```\ncode line is long enough\n```\nhi\n"
        self.assertEqual([u.raw for u in tl.units(t)],
                         ["# Title here ok", "code line is long enough"])
        self.assertEqual([u.raw for u in tl.units(t, keep_code=False)],
                         ["# Title here ok"])

    def test_polarity(self):
        self.assertEqual(tl.polarity("never force push to main"), -1)
        self.assertEqual(tl.polarity("always run the linter"), 1)
        self.assertEqual(tl.polarity("the build takes a while"), 0)


class TestLost(unittest.TestCase):
    def test_reorder_is_not_a_loss(self):
        old = {"a.md": "- Always run the linter before you push code\n"
                       "- Keep pull requests small and focused please\n"}
        new = {"a.md": "- keep pull requests small and focused please\n"
                       "- Always run the linter before you push code\n"}
        r = lost.compare(old, new)
        self.assertEqual((len(r["dropped"]), len(r["reworded"])), (0, 0))
        self.assertEqual(r["kept"], 2)

    def test_dropped_reworded_moved_reversed(self):
        old = {"a.md": "- Delete me forever and ever amen\n"
                       "- Always run the linter before you push code\n"
                       "- Keep pull requests small and focused please\n"
                       "- Always use the staging database for experiments\n"}
        new = {"a.md": "- Always run the linter before pushing code to remote\n"
                       "- Never use the staging database for experiments\n",
               "b.md": "- Keep pull requests small and focused please\n"}
        r = lost.compare(old, new)
        self.assertEqual([x["text"] for x in r["dropped"]],
                         ["- Delete me forever and ever amen"])
        self.assertEqual(len(r["reworded"]), 1)
        self.assertEqual(r["reworded"][0]["to_text"][:8], "- Always")
        self.assertEqual(len(r["moved"]), 1)
        self.assertEqual(r["moved"][0]["to"], "b.md")
        self.assertEqual(len(r["reversed"]), 1)
        self.assertGreater(r["tokens_dropped"], 0)

    def test_unrelated_lines_are_dropped_not_reworded(self):
        r = lost.compare({"a.md": "- Rotate the signing keys every quarter\n"},
                         {"a.md": "- Pet the office dog on Fridays happily\n"})
        self.assertEqual(len(r["dropped"]), 1)
        self.assertEqual(r["reworded"], [])

    def test_file_gone(self):
        r = lost.compare({"a.md": "- Rotate the signing keys every quarter\n"}, {})
        self.assertEqual(r["files_gone"], ["a.md"])
        self.assertEqual(len(r["dropped"]), 1)

    def test_render_redacts_secrets_and_controls(self):
        old = {"a.md": f"- the deploy key is {AWS} ok\x1b[2J\n"}
        r = lost.compare(old, {})
        text = lost.render(r, lambda p: p)
        self.assertNotIn(AWS, text)
        self.assertNotIn("\x1b", text)
        self.assertIn("REDACTED", text)

    def test_crlf_and_empty(self):
        r = lost.compare({"a.md": "- Run the tests before every commit\r\n"},
                         {"a.md": "- Run the tests before every commit\n"})
        self.assertEqual(r["kept"], 1)
        self.assertEqual(lost.compare({}, {})["kept"], 0)

    def test_huge_side_is_capped_and_says_so(self):
        text = "".join(f"- unique instruction number {i} about thing\n"
                       for i in range(lost.MAX_LINES + 10))
        r = lost.compare({"a.md": text}, {"a.md": text})
        self.assertTrue(r["truncated"])


class TestSecrets(unittest.TestCase):
    def test_finds_known_shapes(self):
        for kind, v in (("aws-access-key-id", AWS), ("github-token", GH)):
            f = secrets_mod.scan_text(f"token: {v}\n")
            self.assertIn(kind, {x.kind for x in f}, v)

    def test_assignment_needs_a_random_looking_value(self):
        self.assertTrue(secrets_mod.scan_text("db_password = aZ3kQ9xLm2PqR7sT1vWb"))
        for ok in ("password = changeme-please-now", "api_key = <your key here>",
                   "The password policy requires 12 characters.",
                   "token = correct-horse-battery", "secret = $SECRET_FROM_ENV"):
            self.assertEqual(secrets_mod.scan_text(ok), [], ok)

    def test_hashes_and_uuids_are_not_secrets(self):
        t = ("commit " + "0123456789abcdef" * 2 + "01234567\n"
             "id 123e4567-e89b-12d3-a456-426614174000\n")
        self.assertEqual(secrets_mod.scan_text(t), [])

    def test_redact_text(self):
        out, n = secrets_mod.redact_text(f"a {AWS} b\nplain\n")
        self.assertEqual(n, 1)
        self.assertNotIn(AWS, out)
        self.assertIn("plain", out)

    def test_preview_is_four_chars(self):
        (f,) = secrets_mod.scan_text(f"x {AWS}")
        self.assertEqual(f.preview, AWS[:4] + "...")

    def test_very_long_line_does_not_hang(self):
        secrets_mod.scan_text("a" * 5_000_000)


class TestLint(Base):
    def lint(self):
        rep = diet_mod.analyze(self.proj)
        return dietlint.lint(rep, self.proj)

    def test_finds_each_kind(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "# P\n"
              "- Always run pytest before committing any change\n"
              "- Never run pytest before committing any change\n"
              "- Always run pytest before committing any change\n"
              "- Format the Python modules with the black formatter tool\n"
              "- Please format the python module with the black formatter tool\n"
              "- See `src/ghost/gone.py` for details\n"
              "- See `CLAUDE.md` for details\n")
        r = self.lint()
        self.assertEqual(len(r["exact_in_file"]), 1)
        self.assertEqual(r["exact_in_file"][0]["line"], 4)
        self.assertEqual(len(r["near_duplicates"]), 1)
        self.assertEqual(len(r["contradictions"]), 1)
        self.assertEqual([d["ref"] for d in r["dead_references"]],
                         ["src/ghost/gone.py"])
        self.assertGreater(r["tokens_saved_if_deduped"], 0)

    def test_dead_reference_exclusions(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "- Build output goes to `dist/app.js` and `build/x.py`\n"
              "- Copy `path/to/file.py` and `your-repo/src/a.py`\n"
              "- URL `https://example.com/a/b.html` and `~/x/y.md`\n"
              "- Pattern `src/**/*.py` and `${HOME}/a/b.sh`\n"
              "- Env `.env.local` and `config/.env`\n"
              "```\nsrc/in/a/fence.py\n```\n")
        self.assertEqual(self.lint()["dead_references"], [])

    def test_existing_path_is_alive_relative_to_root_or_file(self):
        write(os.path.join(self.proj, "src", "a.py"), "x\n")
        write(os.path.join(self.proj, "CLAUDE.md"), "- Run `src/a.py` now\n")
        self.assertEqual(self.lint()["dead_references"], [])

    def test_templated_lines_group_into_one(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "".join(f"- rule {i}: format all python modules with black\n"
                      for i in range(6)))
        g = self.lint()["near_duplicates"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["count"], 6)

    def test_polarity_flip_is_not_a_near_duplicate(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "- Always cache the search indexes in staging\n"
              "- Never cache the search indexes in staging\n")
        r = self.lint()
        self.assertEqual(r["near_duplicates"], [])
        self.assertEqual(len(r["contradictions"]), 1)

    def test_clean_file_has_no_findings(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "# Project\n- Run the unit tests before each commit\n"
              "- Keep functions short and well named\n")
        r = self.lint()
        self.assertEqual(sum(len(r[k]) for k in
                             ("exact_in_file", "near_duplicates",
                              "contradictions", "dead_references")), 0)

    def test_cli_lint_check_and_json(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "- Run the unit tests before each commit\n"
              "- Run the unit tests before each commit\n")
        code, out, err = self.cli("diet", "--lint", "--check")
        self.assertEqual(code, 3)
        self.assertIn("Lint", out)
        self.assertIn("same line as line 1", err)
        code, out, _ = self.cli("diet", "--lint", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["lint"]["exact_in_file"]), 1)
        code, out, _ = self.cli("diet")      # without --lint: unchanged
        self.assertNotIn("Lint", out)

    def test_secret_in_a_duplicated_line_is_not_printed(self):
        line = f"- The deploy key is {AWS} for production use\n"
        write(os.path.join(self.proj, "CLAUDE.md"), line * 2)
        _, out, err = self.cli("diet", "--lint", "--check")
        self.assertNotIn(AWS, out + err)


class TestFix(Base):
    def test_preview_changes_nothing_write_snapshots_first(self):
        path = os.path.join(self.proj, "CLAUDE.md")
        original = (b"# P\r\n- Run the unit tests before each commit\r\n"
                    b"- Keep functions short and well named\r\n"
                    b"- Run the unit tests before each commit\r\n\r\n\r\n\r\n"
                    b"- Tail line stays\r\n")
        with open(path, "wb") as f:
            f.write(original)
        self.cli("init")
        code, out, _ = self.cli("diet", "--fix")
        self.assertEqual(code, 0)
        self.assertIn("line 4", out)
        self.assertEqual(slurp(path), original)
        n = len(self.log())
        code, out, err = self.cli("diet", "--fix", "--write")
        self.assertEqual(code, 0, err)
        new = slurp(path)
        self.assertEqual(new.count(b"Run the unit tests"), 1)
        self.assertNotIn(b"\r\n\r\n\r\n", new)
        self.assertTrue(new.endswith(b"- Tail line stays\r\n"))   # CRLF kept
        self.assertGreaterEqual(len(self.log()), n + 1)
        # one undo brings the original back, byte for byte
        self.cli("undo")
        self.assertEqual(slurp(path), original)

    def test_nothing_to_fix(self):
        self.cli("init")
        _, out, _ = self.cli("diet", "--fix")
        self.assertIn("nothing it can safely remove", out)

    def test_fence_headings_and_short_lines_are_kept(self):
        path = os.path.join(self.proj, "CLAUDE.md")
        text = ("## Run the tests every time\n## Run the tests every time\n"
                "```\nrun the tests every single time\n"
                "run the tests every single time\n```\n- ok\n- ok\n")
        write(path, text)
        self.cli("init")
        self.cli("diet", "--fix", "--write")
        self.assertEqual(slurp(path).decode(), text)

    def test_symlink_and_changed_file_are_left_alone(self):
        path = os.path.join(self.proj, "CLAUDE.md")
        write(path, "- Run the unit tests before each commit\n" * 2)
        self.cli("init")
        rep = diet_mod.analyze(self.proj)
        plan = dietlint.fix_plan(rep, self.proj)
        write(path, "- someone edited this file meanwhile ok\n")
        written, skipped = dietlint.apply_plan(plan)
        self.assertEqual(written, [])
        self.assertIn("changed since", skipped[0])
        self.assertEqual(slurp(path), b"- someone edited this file meanwhile ok\n")

    def test_unwatched_file_is_not_touched(self):
        extra = os.path.join(self.proj, "docs", "x.md")
        write(os.path.join(self.proj, "CLAUDE.md"), "See @docs/x.md\n")
        dup = "- Run the unit tests before each commit\n" * 2
        write(extra, dup)
        self.cli("init")
        self.cli("diet", "--fix", "--write")
        self.assertEqual(slurp(extra).decode(), dup)


class TestScanPinHistoryLost(Base):
    def test_scan_and_snap_warning(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              f"- key {AWS}\n")
        code, out, _ = self.cli("scan")
        self.assertEqual(code, 3)
        self.assertNotIn(AWS, out)
        self.assertIn("aws-access-key-id", out)
        self.cli("init")
        code, out, err = self.cli("snap", "-m", "x")
        self.assertIn("possible secret", err)
        self.assertNotIn(AWS, out + err)
        sha = self.log()[0]["sha"]
        code, out, _ = self.cli("scan", "--sha", sha[:7])
        self.assertEqual(code, 3)

    def test_scan_clean(self):
        code, out, _ = self.cli("scan")
        self.assertEqual(code, 0)
        self.assertIn("no secrets", out)

    def test_show_redact(self):
        write(os.path.join(self.proj, "CLAUDE.md"), f"- key {AWS}\n- fine line\n")
        self.cli("init")
        sha = self.log()[0]["sha"][:7]
        code, out, _ = self.cli("show", sha, "CLAUDE.md", "--redact")
        self.assertEqual(code, 0)
        self.assertNotIn(AWS, out)
        self.assertIn("fine line", out)
        _, raw, _ = self.cli("show", sha, "CLAUDE.md")
        self.assertIn(AWS, raw)         # plain show stays byte-exact

    def test_pin_lifecycle(self):
        rule = "Never force-push to the main branch"
        write(os.path.join(self.proj, "CLAUDE.md"), f"# P\n- {rule}.\n")
        self.cli("init")
        code, out, _ = self.cli("pin", rule)
        self.assertEqual(code, 0)
        self.assertEqual(self.cli("pin", "--check")[0], 0)
        self.assertIn("ok", self.cli("pin")[1])
        write(os.path.join(self.proj, "CLAUDE.md"), "# P\n- nothing left\n")
        code, out, _ = self.cli("pin", "--check")
        self.assertEqual(code, 3)
        self.assertIn("missing", out)
        _, out, _ = self.cli("snap", "-m", "after")
        self.assertIn("pinned line missing", out)
        _, out, _ = self.cli("status")
        self.assertIn("pinned line missing", out)
        self.assertEqual(self.cli("pin", "--remove", rule)[0], 0)
        self.assertEqual(self.cli("pin", "--check")[0], 0)

    def test_pin_refuses_secrets_short_and_multiline(self):
        self.assertEqual(self.cli("pin", f"key {AWS}")[0], 2)
        self.assertEqual(self.cli("pin", "short")[0], 2)
        self.assertEqual(self.cli("pin", "line one is long\nline two")[0], 2)
        self.assertEqual(self.cli("pin", "--remove", "no such pin at all")[0], 1)

    def test_bad_pins_config_is_one_line(self):
        os.makedirs(os.path.dirname(core_mod.CONFIG_PATH), exist_ok=True)
        write(core_mod.CONFIG_PATH, '{"pins": "nope"}')
        code, _, err = self.cli("pin")
        self.assertEqual(code, 2)
        self.assertIn('"pins" must be a list', err)

    def test_history_shows_a_jump(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "# P\n- small file here ok\n")
        self.cli("init")
        write(os.path.join(self.proj, "CLAUDE.md"),
              "# P\n" + "- a big pasted paragraph of rules " * 400 + "\n")
        self.cli("snap", "-m", "pasted")
        code, out, _ = self.cli("diet", "--history")
        self.assertEqual(code, 0)
        self.assertIn("<- jump", out)
        code, out, _ = self.cli("diet", "--history", "1", "--json")
        self.assertEqual(len(json.loads(out)), 1)
        self.assertEqual(self.cli("diet", "--history", "0")[0], 2)

    def test_lost_between_snapshots_and_live(self):
        path = os.path.join(self.proj, "CLAUDE.md")
        write(path, "# P\n- Keep pull requests small and focused please\n"
                    "- Rotate the signing keys every quarter\n")
        self.cli("init")
        write(path, "# P\n- Keep pull requests small and focused please\n")
        self.cli("snap", "-m", "compacted")
        code, out, _ = self.cli("lost")
        self.assertEqual(code, 0)
        self.assertIn("Rotate the signing keys", out)
        self.assertNotIn("small and focused", out)
        self.assertEqual(self.cli("lost", "--check")[0], 3)
        code, out, _ = self.cli("lost", "--json")
        self.assertEqual(len(json.loads(out)["dropped"]), 1)
        # live: the file is edited again, newest snapshot vs now
        write(path, "# P\n")
        code, out, _ = self.cli("lost", "--live")
        self.assertIn("Keep pull requests", out)

    def test_lost_input_errors(self):
        self.assertEqual(self.cli("lost")[0], 2)       # no repo yet
        self.cli("init")
        self.assertEqual(self.cli("lost", "HEAD~1")[0], 1)
        self.assertEqual(self.cli("lost", "zz")[0], 1)
        code, out, _ = self.cli("lost")
        self.assertIn("only one snapshot", out)


class TestBenchmark(unittest.TestCase):
    def test_committed_benchmark_meets_its_floors(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isfile(os.path.join(root, "bench", "run.py")):
            self.skipTest("bench/ is not part of this source tree")
        p = subprocess.run([sys.executable, os.path.join(root, "bench", "run.py"),
                            "--check"], capture_output=True, text=True,
                           timeout=300, env={**os.environ})
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])


if __name__ == "__main__":
    unittest.main()


class TestRedactionInJson(Base):
    def test_lint_and_duplicates_json_never_hold_the_secret(self):
        line = f"- The deploy key is {AWS} for production use\n"
        write(os.path.join(self.proj, "CLAUDE.md"), line * 2)
        write(os.path.join(self.proj, "AGENTS.md"), line)
        _, out, _ = self.cli("diet", "--lint", "--json")
        self.assertNotIn(AWS, out)
        _, out, _ = self.cli("diet")
        self.assertNotIn(AWS, out)
