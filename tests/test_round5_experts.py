"""Round-5 (expert round) tests: monorepo ancestors, hook behaviour, a damaged
or half-written snapshot store, links, an old store, and seeded property and
golden-shape tests.

Temp dirs only, same hermetic environment as the earlier rounds.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
import unittest

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

import sessionsteward
from sessionsteward import core as core_mod

try:
    from .test_round5_fixes import REPO_ROOT, Base, slurp, write
except ImportError:
    from test_round5_fixes import REPO_ROOT, Base, slurp, write

AUTHOR = "Calas Automations Inc."


class Author(unittest.TestCase):
    def test_author_string_is_pinned_everywhere(self):
        self.assertEqual(sessionsteward.__author__, AUTHOR)
        with open(os.path.join(REPO_ROOT, "pyproject.toml"),
                  encoding="utf-8") as f:
            self.assertIn(f'authors = [{{name = "{AUTHOR}"}}]', f.read())
        with open(os.path.join(REPO_ROOT, "LICENSE"), encoding="utf-8") as f:
            self.assertIn(f"Copyright (c) 2026 {AUTHOR}", f.read())

    def test_no_personal_name_or_handle_as_author(self):
        for name in ("pyproject.toml", "LICENSE"):
            with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as f:
                text = f.read()
            for bad in ("Dan Calas", "DC Enterprises"):
                self.assertNotIn(bad, text)


class Ancestors(Base):
    """Claude Code started in packages/web also loads the CLAUDE.md of every
    folder above it."""

    def setUp(self):
        super().setUp()
        self.mono = os.path.join(self._tmp, "mono")
        self.web = os.path.join(self.mono, "packages", "web")
        os.makedirs(self.web)
        write(os.path.join(self.mono, "CLAUDE.md"), "- repo rule one\n- two\n")
        write(os.path.join(self.mono, "packages", "CLAUDE.local.md"), "mine\n")

    def test_files_above_are_found_nearest_first(self):
        got = core_mod.ancestor_memory_files(self.web)
        self.assertEqual(got[:2], [
            os.path.join(self.mono, "packages", "CLAUDE.local.md"),
            os.path.join(self.mono, "CLAUDE.md")])

    def test_snapshot_and_undo_cover_the_parent_file(self):
        self.assertEqual(self.cli("snap", "-m", "a", root=self.web)[0], 0)
        parent = os.path.join(self.mono, "CLAUDE.md")
        write(parent, "x\n")
        self.assertEqual(self.cli("snap", "-m", "b", root=self.web)[0], 0)
        code, out, err = self.cli("undo", root=self.web)
        self.assertEqual(code, 0, err)
        self.assertEqual(slurp(parent), b"- repo rule one\n- two\n")

    def test_diet_counts_the_parent_file(self):
        code, out, _ = self.cli("diet", "--json", root=self.web)
        self.assertEqual(code, 0)
        self.assertIn("mono", out)

    def test_restoring_an_older_snapshot_leaves_the_parent_alone(self):
        # a snapshot taken before the entry was watched is out of its scope
        self.assertEqual(self.cli("watch", "--remove",
                                  core_mod.ANCESTOR_MEMORY, root=self.web)[0],
                         0)
        self.cli("snap", "-m", "old", root=self.web)
        self.cli("watch", "--add", core_mod.ANCESTOR_MEMORY, root=self.web)
        parent = os.path.join(self.mono, "CLAUDE.md")
        write(parent, "changed\n")
        write(os.path.join(self.web, "CLAUDE.md"), "web\n")
        self.cli("snap", "-m", "new", root=self.web)
        code, out, err = self.cli("undo", root=self.web)
        self.assertEqual(slurp(parent), b"changed\n", out + err)

    def test_project_without_parents_files_is_unchanged(self):
        self.assertEqual(core_mod.ancestor_memory_files(self.proj)[:0], [])


class HookMode(Base):
    def test_hook_snap_is_silent_when_nothing_is_wrong(self):
        code, out, err = self.cli("snap", "--hook", "-m", "session start")
        self.assertEqual((code, out), (0, ""))

    def test_hook_snap_still_reports_a_shrunk_file(self):
        self.cli("snap", "-m", "one")
        write(os.path.join(self.proj, "CLAUDE.md"), "x\n" )
        code, out, _ = self.cli("snap", "--hook", "-m", "two")
        self.assertEqual(code, 0)
        self.assertIn("guard", out)
        self.assertNotIn("snapshot ", out.split("guard")[0])

    def test_precompact_hook_never_exits_2(self):
        write(core_mod.CONFIG_PATH, "{ not json")
        code, _, err = self.cli("precompact", "--hook")
        self.assertEqual(code, 1, err)
        self.assertTrue(err.startswith("error:"))
        code, _, err = self.cli("precompact")
        self.assertEqual(code, 2, err)

    def test_hook_commands_carry_the_flag_and_old_ones_still_count(self):
        self.assertIn("precompact --hook", core_mod.HOOK_PRECOMPACT_CMD)
        self.assertIn("snap --hook", core_mod.HOOK_SESSIONSTART_CMD)
        old = {"hooks": {
            "PreCompact": [{"matcher": "", "hooks": [
                {"type": "command", "command": "sessionsteward precompact"}]}],
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command",
                 "command": 'sessionsteward snap -m "session start"'}]}]}}
        self.assertTrue(core_mod._is_ours("PreCompact",
                                          "sessionsteward precompact"))
        merged = core_mod.merge_hooks(old)
        cmds = [h["command"] for e in merged["hooks"]["PreCompact"]
                for h in e["hooks"]]
        self.assertEqual(len(cmds), 1)
        self.assertIn("--hook", cmds[0])

    def test_a_users_own_hook_is_not_taken_for_ours(self):
        self.assertFalse(core_mod._is_ours(
            "SessionStart", "sessionsteward snap --hook -m other"))


class StoreDamage(Base):
    def test_old_index_lock_is_cleared_and_fresh_one_is_not(self):
        self.cli("snap", "-m", "one")
        lock = os.path.join(core_mod.REPO_DIR, ".git", "index.lock")
        write(lock, "")
        write(os.path.join(self.proj, "CLAUDE.md"), "two\n")
        code, _, err = self.cli("snap", "-m", "fresh lock")
        self.assertEqual(code, 2, err)       # a live git may own it
        old = time.time() - 3600
        os.utime(lock, (old, old))
        code, _, err = self.cli("snap", "-m", "stale lock")
        self.assertEqual(code, 0, err)
        self.assertIn("leftover git lock", err)
        self.assertFalse(os.path.exists(lock))

    def test_damaged_blob_is_not_reported_as_a_deleted_file(self):
        self.cli("snap", "-m", "one")
        write(os.path.join(self.proj, "CLAUDE.md"), "# Project\n\nmore\n")
        self.cli("snap", "-m", "two")
        top = core_mod.log(2, project_root=self.proj)[0]["sha"]
        blob = subprocess.run(
            ["git", "-C", core_mod.REPO_DIR, "rev-parse",
             f"{top}:files/CLAUDE.md"], capture_output=True, text=True,
            env=self.child_env()).stdout.strip()
        path = os.path.join(core_mod.REPO_DIR, ".git", "objects", blob[:2],
                            blob[2:])
        os.chmod(path, 0o644)
        write(path, "junk")
        code, out, _ = self.cli("log")
        self.assertEqual(code, 0)
        self.assertNotIn("deleted", out)
        self.assertIn("doctor", out)
        code, out, _ = self.cli("doctor")
        self.assertEqual(code, 3)
        self.assertIn("problem", out)

    def test_doctor_on_a_healthy_store(self):
        self.cli("snap", "-m", "one")
        code, out, err = self.cli("doctor", "--json")
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertTrue(all(set(r) == {"level", "text"} for r in rows))
        self.assertNotIn("problem", {r["level"] for r in rows})

    def test_doctor_before_any_snapshot(self):
        code, out, _ = self.cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("no snapshots repo yet", out)


@unittest.skipIf(os.name == "nt", "symlinks need a privilege on Windows")
class LinkedMemory(Base):
    def test_claude_md_linked_to_agents_md_undoes_cleanly(self):
        os.remove(os.path.join(self.proj, "CLAUDE.md"))
        write(os.path.join(self.proj, "AGENTS.md"), "one\ntwo\nthree\n")
        os.symlink("AGENTS.md", os.path.join(self.proj, "CLAUDE.md"))
        self.cli("snap", "-m", "good")
        write(os.path.join(self.proj, "AGENTS.md"), "x\n")
        self.cli("snap", "-m", "clobbered")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("skipped", out)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"one\ntwo\nthree\n")
        self.assertTrue(os.path.islink(os.path.join(self.proj, "CLAUDE.md")))


class OldStore(Base):
    """A store written by 0.1.0 (manifest with project_root, files, at; no
    modes, kinds or watch) must keep working."""

    def git(self, *args, env_extra=None):
        env = self.child_env()
        env.update({"GIT_AUTHOR_NAME": "s", "GIT_AUTHOR_EMAIL": "s@local",
                    "GIT_COMMITTER_NAME": "s", "GIT_COMMITTER_EMAIL": "s@local"})
        return subprocess.run(["git", "-C", core_mod.REPO_DIR, *args],
                              capture_output=True, text=True, env=env,
                              check=True).stdout

    def old_snapshot(self, text: str, msg: str) -> str:
        files = os.path.join(core_mod.REPO_DIR, "files")
        write(os.path.join(files, "CLAUDE.md"), text)
        write(os.path.join(core_mod.REPO_DIR, "manifest.json"), json.dumps(
            {"project_root": self.proj,
             "files": {"CLAUDE.md": os.path.join(self.proj, "CLAUDE.md")},
             "at": 1794099530}, indent=2))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD").strip()

    def test_log_undo_lost_status_and_a_new_snapshot_work_on_it(self):
        import shutil
        shutil.rmtree(os.path.join(self.proj, ".claude"))
        os.makedirs(core_mod.REPO_DIR)
        self.git("init", "-q", ".")
        write(core_mod.CONFIG_PATH, '{"watch": []}')
        a = self.old_snapshot("- Never run migrations on prod\n- Use pytest\n",
                              "old-one")
        self.old_snapshot("- Use pytest\n", "old-two")
        write(os.path.join(self.proj, "CLAUDE.md"), "- Use pytest\n")
        code, out, err = self.cli("log")
        self.assertEqual(code, 0, err)
        self.assertIn("old-two", out)
        code, out, _ = self.cli("lost", a[:7])
        self.assertIn("Never run migrations", out)
        self.assertEqual(self.cli("status")[0], 0)
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Never run migrations on prod",
                      slurp(os.path.join(self.proj, "CLAUDE.md")).decode())
        write(os.path.join(self.proj, "CLAUDE.md"), "new\n")
        self.assertEqual(self.cli("snap", "-m", "new")[0], 0)
        self.assertEqual(self.cli("undo")[0], 0)


class SeededProperties(Base):
    """No hypothesis dependency: a seeded generator, so a failure names its
    seed and can be replayed."""

    @staticmethod
    def text(rng: random.Random) -> bytes:
        words = ["alpha", "Béta", "γάμμα", "rule:", "- never", "- always",
                 "`x`", "日本", "", " ", "\t", "#", "@a.md"]
        eol = rng.choice(["\n", "\r\n"])
        lines = [" ".join(rng.choice(words) for _ in range(rng.randint(0, 6)))
                 for _ in range(rng.randint(0, 25))]
        data = eol.join(lines) + (eol if rng.random() < .7 else "")
        return data.encode("utf-8")

    def test_undo_restores_the_exact_bytes(self):
        path = os.path.join(self.proj, "CLAUDE.md")
        for seed in range(25):
            rng = random.Random(seed)
            before = self.text(rng)
            with open(path, "wb") as f:
                f.write(before)
            self.cli("snap", "-m", f"seed {seed} a")
            with open(path, "wb") as f:
                f.write(self.text(rng) + b"changed")
            self.cli("snap", "-m", f"seed {seed} b")
            code, out, err = self.cli("undo")
            self.assertEqual(code, 0, f"seed {seed}: {out}{err}")
            self.assertEqual(slurp(path), before, f"seed {seed}")

    def test_reordering_lines_never_counts_as_a_loss(self):
        a = os.path.join(self.proj, "a.md")
        write(os.path.join(self.proj, "CLAUDE.md"), "x\n")
        self.cli("watch", "--add", "a.md")
        for seed in range(15):
            rng = random.Random(1000 + seed)
            lines = [f"- rule {i} {rng.choice(['alpha', 'beta', 'gamma'])} "
                     f"item{rng.randint(0, 999)}" for i in range(20)]
            write(a, "\n".join(lines) + "\n")
            self.cli("snap", "-m", f"s{seed}a")
            rng.shuffle(lines)
            write(a, "\n".join(lines) + "\n")
            self.cli("snap", "-m", f"s{seed}b")
            code, out, err = self.cli("lost", "--json")
            self.assertEqual(code, 0, err)
            data = json.loads(out)
            self.assertEqual(data["dropped"], [], f"seed {seed}")
            self.assertEqual(data["reworded"], [], f"seed {seed}")
            self.assertEqual(data["reversed"], [], f"seed {seed}")


class GoldenShapes(Base):
    """The keys machine-readable output promises."""

    def test_json_outputs_keep_their_keys(self):
        self.cli("snap", "-m", "one")
        write(os.path.join(self.proj, "CLAUDE.md"), "# Project\n\nx\n")
        self.cli("snap", "-m", "two")
        rows = json.loads(self.cli("log", "--json")[1])
        self.assertEqual(set(rows[0]), {"sha", "date", "message", "guard"})
        st = json.loads(self.cli("status", "--json")[1])
        for key in ("project", "hooks", "newest", "state"):
            self.assertIn(key, st)
        lost = json.loads(self.cli("lost", "--json")[1])
        for key in ("dropped", "not_compared"):
            self.assertIn(key, lost)
        scan = json.loads(self.cli("scan", "--json")[1])
        self.assertIsInstance(scan, (dict, list))
        diet = json.loads(self.cli("diet", "--json")[1])
        self.assertIsInstance(diet, dict)

    def test_version_line(self):
        out = subprocess.run([sys.executable, "-m", "sessionsteward",
                              "--version"], capture_output=True, text=True,
                             cwd=REPO_ROOT, env=self.child_env()).stdout
        self.assertRegex(out, rf"^sessionsteward {re.escape(sessionsteward.__version__)}$")


import sys  # noqa: E402  (used by GoldenShapes only)

if __name__ == "__main__":
    unittest.main()
