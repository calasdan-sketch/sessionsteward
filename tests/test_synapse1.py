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
        self.assertNotIn(outside, [f["path"] for f in r["files"]])
        self.assertEqual([f["path"] for f in r["external"]], [outside])
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
        self.assertEqual(code, cli_mod.CHECK_FAILED)
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
        self.assertEqual(code, cli_mod.CHECK_FAILED)
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



@unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
class TestPanelSymlinkedRules(Base):
    """Panel #1: a rule that is a symlink out of the project leaked its lines
    into the duplicate report and was counted as loaded."""

    def test_symlinked_rule_out_of_the_project_is_external(self):
        secret = "AWS_SECRET_ACCESS_KEY=" + "q" * 40
        outside = os.path.join(self._tmp, "secret")
        write(os.path.join(outside, "creds.md"), secret + "\n")
        write(os.path.join(outside, "creds2.md"), secret + "\n")
        rules = os.path.join(self.proj, ".claude", "rules")
        os.makedirs(rules)
        os.symlink(os.path.join(outside, "creds.md"),
                   os.path.join(rules, "a.md"))
        os.symlink(os.path.join(outside, "creds2.md"),
                   os.path.join(rules, "b.md"))
        r = diet_mod.analyze(self.proj)
        self.assertEqual(r["duplicates"], [])
        self.assertEqual(len(r["external"]), 2)
        names = [f["path"] for f in r["files"]]
        self.assertFalse(any(n.endswith(("a.md", "b.md")) for n in names))
        code, out, err = self.cli("diet", "--check")
        self.assertNotIn(secret, out + err)
        self.assertIn("approve external imports", out)

    def test_symlink_inside_the_project_still_counts(self):
        write(os.path.join(self.proj, "docs", "real.md"), "real rule\n")
        rules = os.path.join(self.proj, ".claude", "rules")
        os.makedirs(rules)
        os.symlink(os.path.join(self.proj, "docs", "real.md"),
                   os.path.join(rules, "r.md"))
        r = diet_mod.analyze(self.proj)
        self.assertEqual(r["external"], [])
        self.assertEqual(len(r["files"]), 2)


class TestPanelControlCharacters(Base):
    """Panel #2: escape sequences from a memory file reached the terminal."""

    def test_duplicates_warnings_and_check_lines_are_neutralised(self):
        evil = "\x1b]52;c;ZXZpbA==\x07\x1b[2J long duplicated line " + "x" * 30
        write(os.path.join(self.proj, "CLAUDE.md"),
              evil + "\n@x\x1b[31mred.md\n\x9b31m\n")
        write(os.path.join(self.proj, "AGENTS.md"), evil + "\n")
        code, out, err = self.cli("diet", "--check")
        self.assertEqual(code, cli_mod.CHECK_FAILED)
        for text in (out, err):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
            self.assertNotIn("\x9b", text)
        self.assertIn("?]52;c;", out)
        self.assertIn("@x?[31mred.md", err)
        _, out, _ = self.cli("diet", "--json")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x9b", out)
        self.assertEqual(json.loads(out)["n_duplicates"], 1)

    def test_safe_keeps_tabs_and_text(self):
        self.assertEqual(diet_mod.safe("a\tb Καλάς\x00\x85\r\n"),
                         "a\tb Καλάς????")


class TestPanelNoSecretSuggestions(Base):
    """Panel #3: `watch --add` was suggested for any imported file."""

    def test_user_level_import_outside_claude_dir_is_not_suggested(self):
        creds = os.path.join(self._home, ".aws", "credentials.md")
        write(creds, "key\n")
        mine = os.path.join(self._home, ".claude", "mine.md")
        write(mine, "mine\n")
        write(os.path.join(self._home, ".claude", "CLAUDE.md"),
              "@~/.aws/credentials.md\n@~/.claude/mine.md\n")
        write(os.path.join(self.proj, "CLAUDE.md"), "@docs/a.md\n")
        write(os.path.join(self.proj, "docs", "a.md"), "a\n")
        r = diet_mod.analyze(self.proj)
        self.assertEqual(sorted(r["unprotected"]),
                         sorted([mine, os.path.join(self.proj, "docs", "a.md")]))
        self.assertEqual(r["n_unprotected_elsewhere"], 1)
        out = diet_mod.render(r, self.proj)
        self.assertNotIn("credentials", out.split("Imported but not")[1]
                         .split("##")[0].replace("1 imported file(s)", ""))
        self.assertIn("may hold secrets", out)


class TestPanelRulesFolder(Base):
    """Panel #4: any folder called `rules` above the project made its
    commands count as context."""

    def test_project_under_a_rules_folder(self):
        proj = os.path.join(self._tmp, "rules", "p")
        write(os.path.join(proj, "CLAUDE.md"), "hi\n")
        write(os.path.join(proj, ".claude", "commands", "deploy.md"), "d\n")
        write(os.path.join(proj, ".claude", "rules", "x.md"), "x\n")
        r = diet_mod.analyze(proj)
        names = rel([f["path"] for f in r["files"]], proj)
        self.assertEqual(names, {"CLAUDE.md", ".claude/rules/x.md"})

    def test_rules_detection(self):
        j = os.path.join
        self.assertTrue(diet_mod._is_rules_file(j("/a", ".claude", "rules",
                                                  "sub", "x.md")))
        self.assertFalse(diet_mod._is_rules_file(j("/rules", "p", ".claude",
                                                   "commands", "x.md")))
        self.assertFalse(diet_mod._is_rules_file(j("/a", "rules", "x.md")))
        self.assertTrue(diet_mod._is_rules_file(
            j(core_mod._claude_config_dir(), "rules", "x.md")))


class TestPanelMissingImportFlood(Base):
    """Panel #5: one warning per repeated missing import (400,000 lines)."""

    def test_deduplicated_and_capped(self):
        many = "".join(f"@gone{i}.md\n" for i in range(100))
        write(os.path.join(self.proj, "CLAUDE.md"), "@nope.md " * 5000 + "\n"
              + many)
        r = diet_mod.analyze(self.proj)
        msgs = [w["message"] for w in r["warnings"]
                if w["code"] == "missing-import"]
        self.assertEqual(len(msgs), diet_mod.MAX_MISSING_PER_FILE + 1)
        self.assertEqual(sum("@nope.md," in m for m in msgs), 1)
        self.assertIn("... and 81 more missing imports", msgs[-1])


class TestPanelParser(unittest.TestCase):
    """Panel #6: list-item and indented fences, indented code blocks, bad
    front matter, block HTML comments."""

    def test_fences_behind_list_markers_and_quotes(self):
        text = ("- ```\n  @a.md\n  ```\n"
                "1. ~~~\n@b.md\n~~~\n"
                "> ```\n> @c.md\n> ```\n"
                "      ```\n@d.md\n```\n"
                "@ok.md\n")
        self.assertEqual(diet_mod.find_imports(text), ["ok.md"])

    def test_indented_code_block(self):
        text = "para\n\n    @code.md\n\tcode @tab.md\n\n@ok.md\n"
        self.assertEqual(diet_mod.find_imports(text), ["ok.md"])

    def test_indented_list_continuation_is_not_code(self):
        text = "- item\n\n    @nested.md\n- other\n    - @deep.md\n"
        self.assertEqual(diet_mod.find_imports(text),
                         ["nested.md", "deep.md"])

    def test_indent_inside_paragraph_is_not_code(self):
        self.assertEqual(diet_mod.find_imports("text\n    @lazy.md\n"),
                         ["lazy.md"])

    def test_html_comments(self):
        text = ("<!-- maintainer note @hidden.md -->\n"
                "<!--\nmulti line\n@also-hidden.md\n-->\n"
                "kept @ok.md\n"
                "```\n<!-- kept in code -->\n```\n"
                "inline <!-- not block --> @inline.md\n")
        body, imports = diet_mod._scan(text)
        self.assertEqual(imports, ["ok.md", "inline.md"])
        self.assertNotIn("maintainer", body)
        self.assertNotIn("multi line", body)
        self.assertIn("<!-- kept in code -->", body)
        self.assertIn("<!-- not block -->", body)

    def test_unclosed_comment_drops_the_rest(self):
        body, imports = diet_mod._scan("a\n<!-- open\n@x.md\n")
        self.assertEqual((body, imports), ("a", []))

    def test_front_matter_that_does_not_parse_is_not_scoped(self):
        for fm in ("paths: [unclosed", "paths:\n\t- src/**",
                   "paths: \"open", "not yaml at all {", "paths: {a: [}"):
            self.assertFalse(diet_mod.is_path_scoped(f"---\n{fm}\n---\nb\n"),
                             fm)
        for fm in ("paths: [\"src/**\", 'lib/**']", "paths:\n  - src/**",
                   "paths: src/**  # comment", "paths: [don't]",
                   "description: it's fine\npaths:\n  - \"a\"",
                   "paths:\n  - [a, b]"):
            self.assertTrue(diet_mod.is_path_scoped(f"---\n{fm}\n---\nb\n"),
                            fm)


class TestPanelExitCodesAndWarnings(Base):
    """Panel #7: a failed check shared exit 1 with unexpected errors,
    unreadable files passed the check, imports got the 200-line warning."""

    def test_check_failure_code_is_distinct(self):
        self.assertNotIn(cli_mod.CHECK_FAILED, (0, 1, 2))
        write(os.path.join(self.proj, "CLAUDE.md"), "@gone.md\n")
        self.assertEqual(self.cli("diet", "--check")[0], cli_mod.CHECK_FAILED)
        with mock.patch.object(diet_mod, "analyze",
                               side_effect=KeyError("boom")):
            code, _, err = self.cli("diet", "--check")
        self.assertEqual(code, 1)
        self.assertIn("unexpected", err)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid")
                     and os.geteuid() == 0), "permission bits do not bind")
    def test_unreadable_file_fails_the_check(self):
        rule = os.path.join(self.proj, ".claude", "rules", "locked.md")
        write(rule, "secret rule\n")
        os.chmod(rule, 0)
        self.addCleanup(os.chmod, rule, 0o600)
        r = diet_mod.analyze(self.proj)
        self.assertEqual([w["code"] for w in r["warnings"]], ["unreadable"])

    def test_unreadable_file_reported_when_open_fails(self):
        real = diet_mod._read_text

        def fake(path):
            if path.endswith("CLAUDE.md"):
                return "Permission denied"
            return real(path)
        with mock.patch.object(diet_mod, "_read_text", fake):
            r = diet_mod.analyze(self.proj)
        self.assertEqual([w["code"] for w in r["warnings"]], ["unreadable"])
        self.assertIn("Permission denied", r["warnings"][0]["message"])

    def test_long_file_warning_only_for_claude_md_files(self):
        long = "".join(f"line {i}\n" for i in range(300))
        write(os.path.join(self.proj, "CLAUDE.md"), "@README.md\n")
        write(os.path.join(self.proj, "README.md"), long)
        write(os.path.join(self.proj, ".claude", "rules", "big.md"), long)
        r = diet_mod.analyze(self.proj)
        self.assertEqual(r["warnings"], [])
        write(os.path.join(self.proj, "CLAUDE.md"), long)
        r = diet_mod.analyze(self.proj)
        self.assertEqual([w["code"] for w in r["warnings"]], ["long-file"])


class TestPanelReadmeNames(unittest.TestCase):
    """Panel #8: the README examples carried the owner's name."""

    def test_no_personal_names_in_examples(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
            text = f.read()
        for name in ("/home/dan", "Users\\Dan", "-home-dan-", "Users\\dan"):
            self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
