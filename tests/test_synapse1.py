"""Synapse round 1: diet that follows Claude Code's documented loading rules
(@imports, MEMORY.md's 200-line cut, path-scoped rules), `diet --check` /
`--json` / `--budget`, `status` and `show`.

Temp dirs only (tests/_isolate.py, Base from the round-5 tests).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

try:
    from . import _isolate  # noqa: F401
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod
from sessionsteward import diet as diet_mod

try:
    from .test_round5_fixes import Base, write
except ImportError:
    from test_round5_fixes import Base, write


def rel(report_paths, root):
    return {os.path.relpath(p, root).replace(os.sep, "/") for p in report_paths}


class TestFindImports(unittest.TestCase):
    def test_documented_syntax(self):
        text = ("See @README for the overview and @docs/a.md.\n"
                "- @~/.claude/mine.md\n"
                "mail me@example.com, not an import\n"
                "literal `@kept.md` in a code span\n"
                "```\n@fenced.md\n```\n"
                "~~~md\n@tilde-fenced.md\n~~~\n"
                "@Design\\ Docs/api.md\n"
                "@\"quoted.md\"\n")
        self.assertEqual(diet_mod.find_imports(text),
                         ["README", "docs/a.md.", "~/.claude/mine.md",
                          "Design Docs/api.md"])

    def test_unclosed_fence_hides_the_rest(self):
        self.assertEqual(diet_mod.find_imports("```\n@a.md\n"), [])

    def test_front_matter(self):
        self.assertTrue(diet_mod.is_path_scoped(
            "---\npaths:\n  - src/**\n---\nbody\n"))
        self.assertTrue(diet_mod.is_path_scoped(
            "---\r\npaths: [\"x\"]\r\n---\r\nbody\r\n"))
        self.assertFalse(diet_mod.is_path_scoped("---\ndescription: x\n---\n"))
        self.assertFalse(diet_mod.is_path_scoped("no front matter\npaths: x\n"))
        self.assertFalse(diet_mod.is_path_scoped("---\npaths: x\n"))  # unclosed


class TestDietLoadingRules(Base):
    def report(self):
        return diet_mod.analyze(self.proj)

    def test_imports_are_counted_and_flagged_unprotected(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "# P\nSee @docs/arch.md and @AGENTS.md.\n")
        write(os.path.join(self.proj, "AGENTS.md"), "agents\n")
        write(os.path.join(self.proj, "docs", "arch.md"),
              "arch\n@../docs/deeper.md\n")
        write(os.path.join(self.proj, "docs", "deeper.md"), "deep\n")
        r = self.report()
        self.assertEqual(rel([f["path"] for f in r["files"]], self.proj),
                         {"CLAUDE.md", "AGENTS.md", "docs/arch.md",
                          "docs/deeper.md"})   # AGENTS.md counted once
        arch = [f for f in r["files"] if f["path"].endswith("arch.md")][0]
        self.assertTrue(arch["imported_by"].endswith("CLAUDE.md"))
        self.assertEqual(rel(r["unprotected"], self.proj),
                         {"docs/arch.md", "docs/deeper.md"})
        out = diet_mod.render(r, self.proj)
        self.assertIn("sessionsteward watch --add docs/arch.md", out)
        self.assertIn("(imported by `CLAUDE.md`)", out)
        self.assertEqual(r["warnings"], [])

    def test_watched_import_is_protected(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "@.claude/notes.md\n")
        write(os.path.join(self.proj, ".claude", "notes.md"), "notes\n")
        r = self.report()
        self.assertIn(os.path.join(self.proj, ".claude", "notes.md"),
                      [f["path"] for f in r["files"]])
        self.assertEqual(r["unprotected"], [])

    def test_cycles_and_depth_limit(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "@a.md\n")
        for a, b in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"),
                     ("e", "a")):
            write(os.path.join(self.proj, f"{a}.md"), f"{a}\n@{b}.md\n")
        r = self.report()
        names = rel([f["path"] for f in r["files"]], self.proj)
        self.assertEqual(names, {"CLAUDE.md", "a.md", "b.md", "c.md", "d.md"})
        self.assertEqual([w["code"] for w in r["warnings"]],
                         ["import-too-deep"])

    def test_missing_md_import_warns_but_prose_mentions_do_not(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "@docs/gone.md, ask @alice, use @types/node\n")
        r = self.report()
        self.assertEqual([w["code"] for w in r["warnings"]], ["missing-import"])
        self.assertIn("imports @docs/gone.md, which", r["warnings"][0]["message"])

    def test_memory_index_only_first_200_lines(self):
        cfg = os.path.join(self._home, ".claude")
        mem = os.path.join(cfg, "projects",
                           core_mod._project_slug(self.proj), "memory")
        write(os.path.join(mem, "MEMORY.md"),
              "".join(f"- note {i}\n" for i in range(260)))
        r = self.report()
        idx = [f for f in r["files"] if f["path"].endswith("MEMORY.md")]
        self.assertEqual(len(idx), 1, r["files"])
        self.assertEqual(idx[0]["loaded_lines"], 200)
        self.assertEqual(idx[0]["lines"], 260)
        self.assertEqual([w["code"] for w in r["warnings"]],
                         ["memory-index-truncated"])
        self.assertIn("other 60", r["warnings"][0]["message"])

    def test_memory_index_25kb_cut(self):
        mem = os.path.join(self._home, ".claude", "projects",
                           core_mod._project_slug(self.proj), "memory")
        write(os.path.join(mem, "MEMORY.md"), ("x" * 999 + "\n") * 30)
        r = self.report()
        idx = [f for f in r["files"] if f["path"].endswith("MEMORY.md")][0]
        self.assertEqual(idx["loaded_lines"], 25)
        self.assertLessEqual(idx["chars"], 25 * 1024)

    def test_path_scoped_rules_are_on_demand(self):
        rules = os.path.join(self.proj, ".claude", "rules")
        write(os.path.join(rules, "scoped.md"),
              "---\npaths:\n  - \"src/**\"\n---\n" + "x" * 5000 + "\n")
        write(os.path.join(rules, "always.md"),
              "---\ndescription: hi\n---\nalways\n")
        r = self.report()
        self.assertEqual(rel([f["path"] for f in r["on_demand"]], self.proj),
                         {".claude/rules/scoped.md"})
        always = [f for f in r["files"] if f["path"].endswith("always.md")][0]
        self.assertEqual(always["chars"], len("always\n"))  # front matter cut
        self.assertLess(r["total_chars"], 1000)
        self.assertIn("Loaded on demand", diet_mod.render(r))

    def test_long_file_and_huge_file(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "".join(f"rule {i}\n" for i in range(250)) + "@big.md\n")
        with open(os.path.join(self.proj, "big.md"), "wb") as f:
            f.truncate(diet_mod.SKIPPED_BYTES + 10)
        r = self.report()
        self.assertEqual(sorted(w["code"] for w in r["warnings"]),
                         ["long-file", "skipped"])
        self.assertNotIn(os.path.join(self.proj, "big.md"),
                         [f["path"] for f in r["files"]])

    def test_external_import_lines_are_never_quoted(self):
        secret = "SECRET-TOKEN-" + "z" * 50
        outside = os.path.join(self._tmp, "elsewhere", "keys.md")
        write(outside, secret + "\n")
        write(os.path.join(self.proj, "CLAUDE.md"),
              f"@{outside}\n{secret}\n")
        r = self.report()
        ext = [f for f in r["files"] if f["path"] == outside][0]
        self.assertTrue(ext["external"])
        self.assertEqual(r["duplicates"], [])
        self.assertNotIn(secret, json.dumps(r["duplicates"]))

    def test_unreadable_and_odd_imports_never_crash(self):
        write(os.path.join(self.proj, "CLAUDE.md"),
              "@\n@ \n@.\n@..\n@/\n@~\n@" + "a" * 5000 + ".md\n@nul\x00x.md\n")
        r = self.report()   # no exception
        self.assertEqual(len(r["files"]), 1)


class TestDietCli(Base):
    def test_check_exit_codes_and_json(self):
        code, out, _ = self.cli("diet", "--check")
        self.assertEqual(code, 0)
        self.assertIn("check: ok", out)
        write(os.path.join(self.proj, "CLAUDE.md"), "@docs/gone.md\n")
        code, out, err = self.cli("diet", "--check")
        self.assertEqual(code, 1)
        self.assertIn("check: CLAUDE.md: imports @docs/gone.md", err)
        code, out, _ = self.cli("diet", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["warnings"][0]["code"], "missing-import")
        self.assertIn("total_tokens", data)

    def test_budget(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "x" * 4000 + "\n")
        self.assertEqual(self.cli("diet", "--budget", "2000")[0], 0)
        code, _, err = self.cli("diet", "--budget", "500")
        self.assertEqual(code, 1)
        self.assertIn("over the budget of 500", err)
        code, _, err = self.cli("diet", "--budget", "-1")
        self.assertEqual(code, 2)
        self.assertIn("--budget", err)


class TestStatus(Base):
    def setUp(self):
        super().setUp()
        self.settings = os.path.join(self._home, ".claude", "settings.json")
        p = mock.patch.object(cli_mod, "CLAUDE_SETTINGS", self.settings)
        p.start()
        self.addCleanup(p.stop)

    def test_before_init(self):
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("snapshots: none", out)
        self.assertIn("install-hooks --write", out)

    def test_clean_dirty_and_after_undo(self):
        self.cli("init")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("nothing unsaved", out)
        write(os.path.join(self.proj, "CLAUDE.md"), "clobbered\n")
        write(os.path.join(self.proj, ".claude", "rules", "new.md"), "n\n")
        os.remove(os.path.join(self.proj, ".claude", "settings.json"))
        _, out, _ = self.cli("status")
        self.assertIn("3 unsaved change(s)", out)
        self.assertIn("modified CLAUDE.md", out)
        self.assertIn(f"added {os.path.join('.claude', 'rules', 'new.md')}",
                      out)
        self.assertIn(f"removed {os.path.join('.claude', 'settings.json')}",
                      out)
        self.cli("snap", "-m", "two")
        self.cli("undo")
        _, out, _ = self.cli("status")
        self.assertIn("back, after an undo or restore", out)
        self.assertNotIn("unsaved", out)

    def test_shrink_guard_and_hooks(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "x" * 2000)
        self.cli("init")
        write(os.path.join(self.proj, "CLAUDE.md"), "x")
        self.cli("snap")
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        # install-hooks wrote cli_mod.CLAUDE_SETTINGS (patched in setUp)
        _, out, _ = self.cli("status")
        self.assertIn("hooks: installed", out)
        self.assertIn("guard: CLAUDE.md shrank", out)

    def test_unreadable_settings(self):
        write(self.settings, "{not json")
        _, out, _ = self.cli("status")
        self.assertIn("cannot be read", out)


class TestShow(Base):
    def test_list_print_and_errors(self):
        write(os.path.join(self.proj, "CLAUDE.md"), "line one\r\nline two\r\n")
        self.cli("init")
        sha = self.log()[0]["sha"]
        write(os.path.join(self.proj, "CLAUDE.md"), "gone\n")
        code, out, _ = self.cli("show", sha[:7])
        self.assertEqual(code, 0)
        self.assertIn("CLAUDE.md", out)
        self.assertIn("2 file(s)", out)
        code, out, _ = self.cli("show", sha[:7], "CLAUDE.md")
        self.assertEqual(code, 0)
        self.assertEqual(out, "line one\r\nline two\r\n")
        code, out, _ = self.cli("show", sha[:7], ".claude")
        self.assertIn("settings.json", out)
        code, _, err = self.cli("show", sha[:7], "nope.md")
        self.assertEqual(code, 1)
        self.assertIn("has no file nope.md", err)
        for bad in ("-x", "--all", "HEAD^{tree}"):
            with self.assertRaises(core_mod.NothingToRestoreError):
                core_mod.resolve_snapshot(bad)
        for bad in ("HEAD", "HEAD~1", "zzzz", "abc", "0" * 40):
            code, _, err = self.cli("show", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("no such snapshot", err)
        # the live file was never touched
        with open(os.path.join(self.proj, "CLAUDE.md")) as f:
            self.assertEqual(f.read(), "gone\n")

    def test_byte_exact_in_a_child_process(self):
        data = bytes(range(256)) * 4
        write(os.path.join(self.proj, ".claude", "blob.bin"), data, binary=True)
        self.cli("init")
        sha = self.log()[0]["sha"]
        p = self.run_child("show", sha, ".claude/blob.bin")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, data)

    def test_other_projects_snapshot_is_refused(self):
        self.cli("init")
        sha = self.log()[0]["sha"]
        other = os.path.join(self._tmp, "other")
        write(os.path.join(other, "CLAUDE.md"), "other\n")
        code, _, err = self.cli("show", sha, "CLAUDE.md", root=other)
        self.assertEqual(code, 1)
        self.assertIn("belongs to project", err)

    def test_home_path(self):
        write(os.path.join(self._home, ".claude", "CLAUDE.md"), "user mem\n")
        self.cli("init")
        sha = self.log()[0]["sha"]
        code, out, _ = self.cli("show", sha, "~/.claude/CLAUDE.md")
        self.assertEqual((code, out), (0, "user mem\n"))


if __name__ == "__main__":
    unittest.main()
