"""Round 10c: restore trust (destinations, manifest checksum), the secret
scan over the whole file, auto-prune, the non-interactive guard, the hook
lock wait and skip log, doctor, config show, transcripts, the compaction
note."""
from __future__ import annotations

import io
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
from sessionsteward import doctor as doctor_mod
from sessionsteward import guardhook as guard_mod
from sessionsteward import retention as retention_mod
from sessionsteward import secretscan

try:
    from .test_round5_fixes import Base, slurp, write
except ImportError:
    from test_round5_fixes import Base, slurp, write

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
BIG = "".join(f"- Rule {i}: always do thing {i} carefully\n" for i in range(40))


def tamper_manifest(sha, change):
    """Make a NEW commit whose manifest is changed (what someone with write
    access to the snapshots repository could do) and return its id."""
    manifest = core_mod._manifest_at(sha)
    change(manifest)
    repo = core_mod.REPO_DIR
    blob = core_mod._run_git(repo, ("hash-object", "-w", "--stdin"),
                             input=json.dumps(manifest, indent=2).encode()
                             ).stdout.decode().strip()
    core_mod._git(repo, "read-tree", sha)
    core_mod._git(repo, "update-index", "--add", "--cacheinfo",
                  f"100644,{blob},manifest.json")
    tree = core_mod._git(repo, "write-tree").strip()
    return core_mod._git(repo, "commit-tree", tree, "-p", sha, "-m",
                         "evil").strip()


class TestRestoreTrust(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("init")[0], 0)

    def test_a_manifest_that_names_a_file_outside_the_watch_list_is_refused(self):
        sha = core_mod.snap(self.proj, "good")
        victim = os.path.join(self._home, ".ssh", "authorized_keys")
        write(victim, "mine\n")

        def evil(m):
            m["files"]["CLAUDE.md"] = victim
        bad = tamper_manifest(sha, evil)
        with self.assertRaises(core_mod.SnapshotDamagedError) as cm:
            core_mod.restore(bad, project_root=self.proj)
        self.assertIn("not on your watch list", str(cm.exception))
        self.assertEqual(slurp(victim), b"mine\n")

    def test_a_name_that_does_not_belong_to_its_destination_is_refused(self):
        sha = core_mod.snap(self.proj, "good")

        def evil(m):
            m["files"]["AGENTS.md"] = m["files"].pop("CLAUDE.md")
        bad = tamper_manifest(sha, evil)
        with self.assertRaises(core_mod.SnapshotDamagedError):
            core_mod.restore(bad, project_root=self.proj)

    def test_a_normal_restore_still_works(self):
        sha = core_mod.snap(self.proj, "good")
        write(os.path.join(self.proj, "CLAUDE.md"), "clobbered\n")
        core_mod.restore(sha, project_root=self.proj)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"# Project\n\nBe helpful.\n")

    def test_the_manifest_checksum_is_recorded_and_checked(self):
        sha = core_mod.snap(self.proj, "good")
        self.assertIn(sha, core_mod.ledger())
        # same id, different recorded checksum: the repository was changed
        # after the snapshot was taken
        with open(core_mod.ledger_path(), "a", encoding="ascii") as f:
            f.write(f"{sha} {'0' * 64}\n")
        with self.assertRaises(core_mod.SnapshotDamagedError) as cm:
            core_mod.check_restore(sha, self.proj)
        self.assertIn("SHA-256", str(cm.exception))

    def test_a_snapshot_with_no_recorded_checksum_is_still_restorable(self):
        sha = core_mod.snap(self.proj, "good")
        os.unlink(core_mod.ledger_path())
        core_mod.check_restore(sha, self.proj)

    def test_prune_keeps_the_checksums_under_the_new_ids(self):
        for i in range(4):
            write(os.path.join(self.proj, "CLAUDE.md"), f"v{i}\n")
            core_mod.snap(self.proj, f"s{i}")
        result = retention_mod.prune(1, 0, apply=True)
        self.assertTrue(result["applied"])
        for snap in core_mod.log(10, project_root=self.proj):
            self.assertIn(snap["sha"], core_mod.ledger())
            core_mod.check_restore(snap["sha"], self.proj)


class TestSecretScan(unittest.TestCase):
    def test_the_whole_file_is_scanned_not_the_first_two_megabytes(self):
        filler = b"x" * (3 * 1024 * 1024) + b"\n"
        self.assertEqual(secretscan.scan(filler + AWS.encode() + b"\n"),
                         ["an AWS access key id"])

    def test_a_secret_cut_by_a_chunk_boundary_is_found(self):
        data = b"a" * (secretscan.CHUNK - 7) + b"\n" + AWS.encode() + b"\n"
        import io as _io
        self.assertEqual(secretscan.scan_stream(_io.BytesIO(data)),
                         ["an AWS access key id"])

    def test_a_long_pure_alpha_passphrase_is_flagged(self):
        pw = b"password = correcthorsebatterystaple\n"
        self.assertEqual(secretscan.scan(pw), ["a password or key assignment"])

    def test_short_words_and_prose_are_not(self):
        self.assertEqual(secretscan.scan(b"password = hunter\n"), [])
        self.assertEqual(secretscan.scan(b"token: use the cache\n"), [])
        self.assertEqual(secretscan.scan(b"api_key = YOUR_KEY_HERE_PLEASE\n"), [])

    def test_the_size_comes_from_the_open_handle(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "n.md")
        with open(p, "wb") as f:
            f.write(b"hello\n")
        real = os.stat
        with mock.patch("sessionsteward.secretscan.os.stat",
                        side_effect=AssertionError("path stat")):
            self.assertEqual(secretscan.file_kinds(p), [])
        self.assertTrue(real(p))


class TestTranscriptsAndMaintenance(Base):
    def test_a_session_transcript_in_a_watched_projects_folder_is_left_out(self):
        projects = os.path.join(self._home, ".claude", "projects")
        write(os.path.join(projects, "p1", "abc.jsonl"), '{"x": 1}\n')
        write(os.path.join(projects, "p1", "memory", "m.md"), "# memo\n")
        write(os.path.join(projects, "p1", "notes.txt"), "keep\n")
        self.assertTrue(core_mod._is_transcript(os.path.join(projects, "p1", "abc.jsonl")))
        self.assertFalse(core_mod._is_transcript(os.path.join(self.proj, "data.jsonl")))
        self.assertFalse(core_mod._is_transcript(os.path.join(projects, "p1", "memory", "x.jsonl")))
        got = [os.path.basename(p) for p, _ in core_mod._walk_files(
            projects, core_mod._Budget())]
        self.assertEqual(sorted(got), ["m.md", "notes.txt"])

    def test_auto_prune_runs_once_a_day_with_the_default_policy(self):
        self.assertEqual(self.cli("init")[0], 0)
        for i in range(3):
            write(os.path.join(self.proj, "CLAUDE.md"), f"v{i}\n")
            core_mod.snap(self.proj, f"s{i}")
        calls = []
        real = retention_mod.prune

        def spy(keep, days, apply, now=None):
            calls.append((keep, days, apply))
            return real(keep, days, apply, now)
        stamp = os.path.join(os.path.dirname(core_mod.REPO_DIR), "maintenance.stamp")
        if os.path.exists(stamp):
            os.unlink(stamp)
        with mock.patch.object(retention_mod, "prune", spy):
            core_mod._maintain(now=time.time())
            core_mod._maintain(now=time.time() + 60)         # same day: nothing
            core_mod._maintain(now=time.time() + 2 * 86400)   # next day
        self.assertEqual(calls, [(200, 90, True), (200, 90, True)])

    def test_auto_prune_can_be_turned_off(self):
        stamp = os.path.join(os.path.dirname(core_mod.REPO_DIR), "maintenance.stamp")
        if os.path.exists(stamp):
            os.unlink(stamp)
        os.environ["SESSIONSTEWARD_AUTO_PRUNE"] = "0"
        with mock.patch.object(retention_mod, "prune") as p:
            core_mod._maintain(now=time.time())
        p.assert_not_called()


class TestGuardWithoutATerminal(Base):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.proj, "CLAUDE.md"), BIG)
        core_mod.init_repo()
        core_mod.snap(self.proj, "s")

    def pre(self):
        out = io.StringIO()
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                   "cwd": self.proj, "tool_input": {
                       "file_path": os.path.join(self.proj, "CLAUDE.md"),
                       "content": "x\n"}}
        for k in ("SESSIONSTEWARD_GUARD_MODE",):
            os.environ.pop(k, None)
        guard_mod.run_hook(io.StringIO(json.dumps(payload)), out)
        return json.loads(out.getvalue())["hookSpecificOutput"]

    def test_the_default_ask_becomes_warn_when_nobody_can_answer(self):
        with mock.patch.object(guard_mod, "has_terminal", return_value=False):
            out = self.pre()
        self.assertNotIn("permissionDecision", out)
        self.assertIn("no terminal to ask on", out["additionalContext"])

    def test_with_a_terminal_it_asks(self):
        with mock.patch.object(guard_mod, "has_terminal", return_value=True):
            self.assertEqual(self.pre()["permissionDecision"], "ask")

    def test_a_mode_you_set_is_honoured_even_without_a_terminal(self):
        guard_mod.set_settings(mode="ask")
        with mock.patch.object(guard_mod, "has_terminal", return_value=False):
            self.assertEqual(self.pre()["permissionDecision"], "ask")
        self.assertEqual(guard_mod.mode_source(), "config")


class TestHookLock(Base):
    def test_a_hook_waits_at_most_ten_seconds_and_logs_the_skip(self):
        self.assertLess(core_mod.HOOK_LOCK_WAIT, core_mod.HOOK_TIMEOUT_SECONDS)
        os.environ["SESSIONSTEWARD_LOCK_TIMEOUT"] = "999"
        core_mod.hook_mode(True)
        self.addCleanup(core_mod.hook_mode, False)
        self.assertEqual(core_mod._lock_timeout(), core_mod.HOOK_LOCK_WAIT)
        core_mod.hook_mode(False)
        self.assertEqual(core_mod._lock_timeout(), 999)

    def test_the_busy_lock_is_written_to_hook_log(self):
        os.environ["SESSIONSTEWARD_LOCK_TIMEOUT"] = "0"
        core_mod.hook_mode(True)
        self.addCleanup(core_mod.hook_mode, False)
        import threading
        held, release = threading.Event(), threading.Event()

        def hold():
            with core_mod.locked():
                held.set()
                release.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        # a second PROCESS-level holder is needed; the lock is re-entrant per
        # thread, so use a fresh thread-local state
        err = []

        def other():
            try:
                with core_mod.locked():
                    pass
            except core_mod.LockTimeoutError as e:
                err.append(e)
        t2 = threading.Thread(target=other)
        t2.start()
        t2.join(10)
        release.set()
        t.join(10)
        self.assertTrue(err)
        with open(core_mod.hook_log_path(), encoding="utf-8") as f:
            self.assertIn("skipped: another sessionsteward held the lock", f.read())

    def test_the_cli_marks_only_the_hook_commands(self):
        seen = []
        real = core_mod.hook_mode
        with mock.patch.object(core_mod, "hook_mode",
                               side_effect=lambda on=True: seen.append(on) or real(on)):
            self.cli("init")
            self.cli("snap", "-m", "session start")
            self.cli("snap", "-m", "by hand")
            self.cli("remind")
        self.assertEqual(seen, [False, True, False, True])

    def test_log_skip_is_bounded(self):
        for i in range(2000):
            core_mod.log_skip("x" * 100)
        self.assertLess(os.path.getsize(core_mod.hook_log_path()), 70 * 1024)


class TestDoctorAndConfig(Base):
    def test_doctor_flags_a_hook_that_points_at_a_missing_command(self):
        self.assertEqual(self.cli("init")[0], 0)
        settings = os.path.join(self._tmp, "settings.json")
        hooks = core_mod.merge_hooks({})
        hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"] = (
            os.path.join(self._tmp, "gone", "sessionsteward") + " precompact")
        write(settings, json.dumps(hooks))
        findings = doctor_mod.run_doctor(settings)
        text = doctor_mod.render_doctor(findings)
        self.assertIn("does not exist any more", text)
        self.assertTrue(any(lvl == doctor_mod.FAIL for lvl, _ in findings))

    def test_doctor_reads_quoted_paths_with_spaces(self):
        self.assertEqual(self.cli("init")[0], 0)
        settings = os.path.join(self._tmp, "settings.json")
        hooks = core_mod.merge_hooks({})
        gone = os.path.join(self._tmp, "gone dir", "sessionsteward.exe")
        hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"] = (
            f'"{gone}" precompact')
        write(settings, json.dumps(hooks))
        text = doctor_mod.render_doctor(doctor_mod.run_doctor(settings))
        self.assertIn("does not exist any more", text)
        self.assertNotIn("hook not installed: PreCompact", text)

    def test_split_command_keeps_windows_backslashes(self):
        with mock.patch.object(core_mod.os, "name", "nt"):
            self.assertEqual(
                core_mod.split_command(r'C:\Py Dir\ss.exe snap -m "a b"')[-3:],
                ["snap", "-m", "a b"])
            self.assertEqual(
                core_mod.split_command(r'"C:\Py Dir\ss.exe" precompact'),
                [r"C:\Py Dir\ss.exe", "precompact"])
            self.assertEqual(
                core_mod.split_command(r"C:\Users\me\ss.exe precompact"),
                [r"C:\Users\me\ss.exe", "precompact"])

    def test_doctor_is_happy_with_the_hooks_it_wrote(self):
        self.assertEqual(self.cli("init")[0], 0)
        settings = os.path.join(self._tmp, "settings.json")
        write(settings, json.dumps(core_mod.merge_hooks({})))
        text = doctor_mod.render_doctor(doctor_mod.run_doctor(settings))
        self.assertIn("points at this install", text)
        self.assertNotIn("FAIL", text)

    def test_doctor_says_when_no_hooks_are_installed(self):
        text = doctor_mod.render_doctor(
            doctor_mod.run_doctor(os.path.join(self._tmp, "none.json")))
        self.assertIn("no hooks are installed", text)

    def test_config_show_names_the_source_of_each_value(self):
        os.environ["SESSIONSTEWARD_SHRINK_PERCENT"] = "30"
        guard_mod.set_settings(mode="block")
        code, out, _ = self.cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("guard.percent", out)
        self.assertRegex(out, r"guard\.percent\s+30\s+\[environment\]")
        self.assertRegex(out, r"guard\.mode\s+block\s+\[config\.json\]")
        self.assertRegex(out, r"auto_prune\s+on\s+\[default\]")

    def test_the_doctor_command_exits_nonzero_on_a_failure(self):
        with mock.patch.object(cli_settings(), "CLAUDE_SETTINGS",
                               os.path.join(self._tmp, "s.json")):
            write(os.path.join(self._tmp, "s.json"), "{not json")
            code, out, _ = self.cli("doctor")
        self.assertEqual(code, 1)


def cli_settings():
    from sessionsteward import cli
    return cli


class TestCompactionNote(Base):
    def test_lines_lost_since_the_pre_compaction_snapshot_are_named(self):
        self.assertEqual(self.cli("init")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), BIG)
        pre = core_mod.snap(self.proj, "pre-compaction snapshot")
        write(os.path.join(self.proj, "CLAUDE.md"),
              BIG.replace("- Rule 3: always do thing 3 carefully\n", ""))
        core_mod.snap(self.proj, "session start")
        text = guard_mod.reminder(self.proj)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(pre[:7], text)
        self.assertIn("Rule 3: always do thing 3 carefully", text)

    def test_no_note_when_nothing_was_lost(self):
        self.assertEqual(self.cli("init")[0], 0)
        core_mod.snap(self.proj, "pre-compaction snapshot")
        text = guard_mod.reminder(self.proj)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("no longer in their files", text)


if __name__ == "__main__":
    unittest.main()
