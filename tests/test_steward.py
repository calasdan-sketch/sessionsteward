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
from contextlib import redirect_stdout
from unittest import mock

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
        self._cli("init")
        self._cli("snap", "-m", "first")
        self._cli("snap", "-m", "second")  # no changes -> skipped
        snaps = core_mod.log()
        messages = [s["message"] for s in snaps]
        self.assertIn("first", messages)
        self.assertNotIn("second", messages)  # empty snap skipped

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
        # init only: no snapshots exist at all -> clean message, no crash
        self._cli("init")
        code, out = self._cli("undo")  # must not raise
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
        init_sha = core_mod.log()[0]["sha"]  # manifest-less init commit
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

        # log in A must not show B's snapshots
        _, out = self._cli_root(proj_a, "log")
        self.assertIn("A-v1", out)
        self.assertNotIn("B-v1", out)

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
        v1 = next(s["sha"] for s in core_mod.log(50, project_root=self.proj)
                  if s["message"] == "v1")
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
        v1 = next(s["sha"] for s in core_mod.log(50, project_root=self.proj)
                  if s["message"] == "v1")
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
        cmds = json.dumps(merged["hooks"])
        self.assertIn("-m sessionsteward precompact", cmds)
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
            if os.name == "nt":
                self.assertTrue(cmd.startswith(f'"{sys.executable}" '), cmd)
            else:
                self.assertEqual(shlex.split(cmd)[:3],
                                 [sys.executable, "-m", "sessionsteward"])

    def test_hook_cmd_quotes_paths_with_spaces(self):
        from unittest import mock
        exe = r"C:\Program Files\Python 3\python.exe"
        with mock.patch.object(core_mod.sys, "executable", exe), \
                mock.patch.object(core_mod.os, "name", "nt"):
            self.assertEqual(core_mod._self_cmd(),
                             f'"{exe}" -m sessionsteward')

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
        self._cli("snap", "-m", "good")
        claude = os.path.join(self.proj, "CLAUDE.md")
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
        self.assertEqual(messages, ["pre-compaction snapshot", "good"])
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
        self.assertEqual(messages[:2], ["v2", "v1"])
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
        self._cli("init")
        when = "2025-01-01T12:00:00+09:00"
        for var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
            old = os.environ.get(var)
            os.environ[var] = when
            self.addCleanup(_restore_var, var, old)
        self._cli("snap", "-m", "v1")
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


if __name__ == "__main__":
    unittest.main()
