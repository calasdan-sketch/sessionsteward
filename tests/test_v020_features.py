"""0.2.0: secret scan before snapshots, the shrink guard hooks, the
compaction reminder, `why`, and `prune`.

Everything here runs on a private snapshots repo (see test_round5_fixes.Base).
The hook payloads are the shapes https://code.claude.com/docs/en/hooks
documents; no real Claude Code was involved.
"""
from __future__ import annotations

import io
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
from sessionsteward import guardhook as guard_mod
from sessionsteward import retention as retention_mod
from sessionsteward import secretscan
from sessionsteward import why as why_mod

try:
    from .test_round5_fixes import Base, slurp, write
except ImportError:
    from test_round5_fixes import Base, slurp, write

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"          # the shape of a key, not a key
BIG = "".join(f"- Rule {i}: always do thing {i} carefully\n" for i in range(40))


def snapshot_ids(proj):
    return [s["sha"] for s in core_mod.log(50, project_root=proj)]


class Cases(Base):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.proj, "CLAUDE.md"), BIG)

    def snap(self, message="s"):
        core_mod.init_repo()
        return core_mod.snap(self.proj, message)

    def hook(self, payload, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env):
            rc = guard_mod.run_hook(io.StringIO(json.dumps(payload)), out)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        return json.loads(text) if text else None

    def pre(self, tool, tool_input, **env):
        return self.hook({"hook_event_name": "PreToolUse", "tool_name": tool,
                          "tool_input": tool_input, "cwd": self.proj}, **env)

    def claude_md(self):
        return os.path.join(self.proj, "CLAUDE.md")

    def in_snapshot(self, sha, rel):
        p = core_mod._run_git(core_mod.REPO_DIR, ("cat-file", "-e",
                                                  f"{sha}:files/{rel}"))
        return p.returncode == 0


# ---------------------------------------------------------------------------
class TestSecretScan(unittest.TestCase):
    def test_the_common_shapes_are_recognised(self):
        for text, kind in (
                (AWS, "AWS access key id"),
                ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
                ("ghp_" + "a1" * 20, "GitHub token"),
                ("xoxb-1234567890-abcdefghij", "Slack token"),
                ("sk-ant-" + "abc123" * 5, "API key"),
                ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",
                 "JSON web token"),
                ("https://dan:hunter22@example.com/x", "password inside"),
                ('api_key = "a1b2c3d4e5f6g7h8i9"', "assignment")):
            kinds = secretscan.scan(text.encode())
            self.assertTrue(any(kind in k for k in kinds), (text, kinds))

    def test_ordinary_prose_is_not_flagged(self):
        for text in ("token: use the cache layer", "password: required",
                     "Set the api key = see the vault",
                     "Use sk-learn and the disk-usage-report script",
                     "https://example.com/docs", "secret: handled by ops",
                     "# Rules\n- Never commit .env files\n"):
            self.assertEqual(secretscan.scan(text.encode()), [], text)

    def test_only_a_kind_is_ever_returned_never_the_text(self):
        for kind in secretscan.scan(f"x {AWS} y".encode()):
            self.assertNotIn("AKIA", kind)

    def test_hide_replaces_a_line_with_a_secret(self):
        self.assertNotIn("AKIA", secretscan.hide(f"key {AWS}"))
        self.assertEqual(secretscan.hide("be kind"), "be kind")

    def test_the_environment_turns_it_off(self):
        with mock.patch.dict(os.environ, {"SESSIONSTEWARD_SECRET_SCAN": "0"}):
            self.assertFalse(secretscan.enabled())
        with mock.patch.dict(os.environ, {"SESSIONSTEWARD_SECRET_SCAN": "1"}):
            self.assertTrue(secretscan.enabled())


class TestSnapshotsLeaveSecretsOut(Cases):
    def test_a_watched_file_with_a_secret_is_not_snapshotted_and_not_printed(self):
        write(os.path.join(self.proj, "NOTES.md"), f"key {AWS}\n")
        self.cli("watch", "--add", "NOTES.md")
        code, out, err = self.cli("init")
        self.assertEqual(code, 0)
        self.assertIn("not snapshotted: NOTES.md", err)
        self.assertIn("AWS access key id", err)
        self.assertNotIn(AWS, out + err)
        sha = snapshot_ids(self.proj)[0]
        self.assertFalse(self.in_snapshot(sha, "NOTES.md"))
        self.assertTrue(self.in_snapshot(sha, "CLAUDE.md"))
        blob = core_mod._run_git(core_mod.REPO_DIR, ("grep", "-c", AWS, sha))
        self.assertNotEqual(blob.returncode, 0)

    def test_a_file_inside_a_watched_folder_is_left_out_alone(self):
        write(os.path.join(self.proj, ".claude", "rules", "bad.md"),
              f"token {AWS}\n")
        write(os.path.join(self.proj, ".claude", "rules", "ok.md"), "fine\n")
        _code, _out, err = self.cli("init")
        self.assertIn("not snapshotted: ", err)
        self.assertIn("bad.md", err)
        sha = snapshot_ids(self.proj)[0]
        self.assertFalse(self.in_snapshot(sha, ".claude/rules/bad.md"))
        self.assertTrue(self.in_snapshot(sha, ".claude/rules/ok.md"))

    def test_restore_leaves_the_secret_file_where_it_is(self):
        bad = os.path.join(self.proj, ".claude", "rules", "bad.md")
        write(bad, f"token {AWS}\n")
        self.cli("init")
        write(self.claude_md(), "changed\n")
        self.cli("snap", "-m", "two")
        code, _out, _err = self.cli("undo")
        self.assertEqual(code, 0)
        self.assertEqual(slurp(bad).decode(), f"token {AWS}\n")
        self.assertEqual(slurp(self.claude_md()).decode(), BIG)

    def test_the_live_files_still_match_the_snapshot(self):
        write(os.path.join(self.proj, ".claude", "bad.md"), f"x {AWS}\n")
        sha = self.snap()
        self.assertTrue(core_mod.worktree_matches_snapshot(sha, self.proj))

    def test_a_file_that_becomes_secret_is_not_reported_deleted(self):
        write(os.path.join(self.proj, "NOTES.md"), "plain notes\n" * 30)
        self.cli("watch", "--add", "NOTES.md")
        first = self.snap("one")
        write(os.path.join(self.proj, "NOTES.md"),
              "plain notes\n" * 30 + f"key {AWS}\n")
        second = self.snap("two")
        self.assertNotEqual(first, second)
        lines = core_mod.guard_lines(second, first)
        self.assertEqual([l for l in lines if "NOTES.md" in l], [], lines)

    def test_the_check_can_be_turned_off(self):
        write(os.path.join(self.proj, ".claude", "bad.md"), f"x {AWS}\n")
        with mock.patch.dict(os.environ, {"SESSIONSTEWARD_SECRET_SCAN": "0"}):
            _code, _out, err = self.cli("init")
        self.assertNotIn("not snapshotted", err)
        sha = snapshot_ids(self.proj)[0]
        self.assertTrue(self.in_snapshot(sha, ".claude/bad.md"))

    def test_the_config_file_can_turn_it_off(self):
        self.cli("init")
        cfg = core_mod.load_config()
        cfg["secret_scan"] = False
        core_mod.save_config(cfg)
        self.assertFalse(secretscan.enabled())


# ---------------------------------------------------------------------------
class TestResultingSize(unittest.TestCase):
    def test_write_edit_and_multiedit(self):
        cur = b"hello world hello"
        self.assertEqual(guard_mod.resulting_size(
            "Write", {"content": "abc"}, cur), 3)
        self.assertEqual(guard_mod.resulting_size(
            "Edit", {"old_string": "hello", "new_string": "bye"}, cur),
            len(b"bye world hello"))
        self.assertEqual(guard_mod.resulting_size(
            "Edit", {"old_string": "hello", "new_string": "",
                     "replace_all": True}, cur), len(b" world "))
        self.assertEqual(guard_mod.resulting_size(
            "MultiEdit", {"edits": [
                {"old_string": "hello", "new_string": "x"},
                {"old_string": "world", "new_string": "y"}]}, cur),
            len(b"x y hello"))

    def test_what_cannot_be_told_is_none(self):
        cur = b"abc"
        for tool, inp in (("Edit", {"old_string": "zzz", "new_string": ""}),
                          ("Edit", {"old_string": "", "new_string": "x"}),
                          ("Write", {"content": 5}), ("Write", {}),
                          ("MultiEdit", {"edits": "no"}),
                          ("Bash", {"command": "rm"})):
            self.assertIsNone(guard_mod.resulting_size(tool, inp, cur))


class TestShrinkGuardBeforeAnEdit(Cases):
    def test_a_big_cut_asks_and_takes_a_snapshot_first(self):
        first = self.snap()
        write(os.path.join(self.proj, "x.txt"), "unrelated\n")
        answer = self.pre("Write", {"file_path": self.claude_md(),
                                    "content": "- one bullet\n"})
        out = answer["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "ask")
        reason = out["permissionDecisionReason"]
        self.assertIn("CLAUDE.md", reason)
        self.assertIn("99% smaller", reason)
        self.assertIn(f"from {len(BIG):,} to 13 bytes", reason)
        self.assertIn(first[:7], reason)       # unchanged state: same snapshot
        self.assertNotIn("Rule 1", reason)     # never the file's content

    def test_a_new_snapshot_is_named_when_the_file_changed_since(self):
        self.snap()
        with open(self.claude_md(), "a", encoding="utf-8") as f:
            f.write("- late addition\n")
        answer = self.pre("Write", {"file_path": self.claude_md(),
                                    "content": "x\n"})
        sha = snapshot_ids(self.proj)[0]
        self.assertIn(sha[:7],
                      answer["hookSpecificOutput"]["permissionDecisionReason"])

    def test_edit_that_removes_most_of_the_file_is_caught(self):
        self.snap()
        body = BIG
        answer = self.pre("Edit", {"file_path": self.claude_md(),
                                   "old_string": body[:1500], "new_string": ""})
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"],
                         "ask")

    def test_small_cuts_and_growth_pass_silently(self):
        self.assertIsNone(self.pre("Edit", {
            "file_path": self.claude_md(), "old_string": "Rule 1:",
            "new_string": "Rule one:"}))
        self.assertIsNone(self.pre("Write", {
            "file_path": self.claude_md(), "content": BIG + "more\n"}))

    def test_block_mode_denies_and_takes_no_snapshot(self):
        self.snap()
        before = snapshot_ids(self.proj)
        with open(self.claude_md(), "a", encoding="utf-8") as f:
            f.write("- unsnapshotted\n")
        answer = self.pre("Write", {"file_path": self.claude_md(),
                                    "content": "x\n"},
                          SESSIONSTEWARD_GUARD_MODE="block")
        out = answer["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("Nothing was changed", out["permissionDecisionReason"])
        self.assertEqual(snapshot_ids(self.proj), before)

    def test_warn_mode_lets_it_through_and_tells_claude(self):
        self.snap()
        answer = self.pre("Write", {"file_path": self.claude_md(),
                                    "content": "x\n"},
                          SESSIONSTEWARD_GUARD_MODE="warn")
        out = answer["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", out)
        self.assertIn("shrink CLAUDE.md", out["additionalContext"])

    def test_the_percent_is_configurable(self):
        self.snap()
        edit = {"file_path": self.claude_md(), "old_string": BIG[:600],
                "new_string": ""}                # about 38% of the file
        self.assertIsNone(self.pre("Edit", edit))
        answer = self.pre("Edit", edit, SESSIONSTEWARD_SHRINK_PERCENT="30")
        self.assertIsNotNone(answer)

    def test_a_bad_percent_falls_back_to_the_default(self):
        for bad in ("0", "100", "-5", "abc", "nan", "12.5"):
            with mock.patch.dict(os.environ,
                                 {"SESSIONSTEWARD_SHRINK_PERCENT": bad}):
                self.assertEqual(guard_mod.settings()[0], 50, bad)

    def test_a_small_file_is_ignored(self):
        write(self.claude_md(), "tiny\n")
        self.assertIsNone(self.pre("Write", {"file_path": self.claude_md(),
                                             "content": ""}))

    def test_a_file_nobody_watches_is_ignored(self):
        other = os.path.join(self.proj, "src.py")
        write(other, "x = 1\n" * 100)
        self.assertIsNone(self.pre("Write", {"file_path": other,
                                             "content": ""}))

    def test_a_file_inside_a_watched_folder_is_guarded(self):
        rule = os.path.join(self.proj, ".claude", "rules", "style.md")
        write(rule, "- style rule\n" * 40)
        answer = self.pre("Write", {"file_path": rule, "content": "x"})
        self.assertIn("style.md", answer["hookSpecificOutput"]
                      ["permissionDecisionReason"])

    def test_other_tools_and_missing_files_are_ignored(self):
        self.assertIsNone(self.pre("Bash", {"command": "rm CLAUDE.md"}))
        self.assertIsNone(self.pre("Write", {
            "file_path": os.path.join(self.proj, "AGENTS.md"),
            "content": "new file"}))

    def test_saved_settings_apply_and_the_environment_wins(self):
        guard_mod.set_settings(percent=20, mode="block")
        self.assertEqual(guard_mod.settings(), (20, "block"))
        with mock.patch.dict(os.environ, {"SESSIONSTEWARD_GUARD_MODE": "warn"}):
            self.assertEqual(guard_mod.settings(), (20, "warn"))

    def test_broken_input_and_broken_code_answer_nothing(self):
        for raw in ("", "not json", "[]", '{"hook_event_name": 7}'):
            out = io.StringIO()
            self.assertEqual(guard_mod.run_hook(io.StringIO(raw), out), 0)
            self.assertEqual(out.getvalue(), "")
        with mock.patch.object(guard_mod, "before_edit",
                               side_effect=RuntimeError("boom")):
            out = io.StringIO()
            payload = {"hook_event_name": "PreToolUse"}
            self.assertEqual(guard_mod.run_hook(
                io.StringIO(json.dumps(payload)), out), 0)
            self.assertEqual(out.getvalue(), "")

    def test_a_failed_snapshot_is_said_not_hidden(self):
        with mock.patch.object(core_mod, "snap",
                               side_effect=core_mod.StewardError("no")):
            answer = self.pre("Write", {"file_path": self.claude_md(),
                                        "content": "x\n"})
        self.assertIn("No snapshot could be taken",
                      answer["hookSpecificOutput"]["permissionDecisionReason"])


class TestAfterAnyChange(Cases):
    def changed(self, **extra):
        payload = {"hook_event_name": "FileChanged",
                   "file_path": self.claude_md(), "event": "change",
                   "cwd": self.proj}
        payload.update(extra)
        return self.hook(payload)

    def test_a_shrunk_file_warns_the_user_and_names_the_snapshot(self):
        sha = self.snap()
        write(self.claude_md(), "- one bullet\n" * 3)
        answer = self.changed()
        self.assertIn("CLAUDE.md shrank", answer["systemMessage"])
        self.assertIn(sha[:7], answer["systemMessage"])
        self.assertNotIn("hookSpecificOutput", answer)   # nothing for Claude

    def test_a_deleted_file_is_reported(self):
        self.snap()
        os.unlink(self.claude_md())
        self.assertIn("is gone", self.changed(event="unlink")["systemMessage"])

    def test_an_unchanged_or_grown_file_says_nothing(self):
        self.snap()
        self.assertIsNone(self.changed())
        with open(self.claude_md(), "a", encoding="utf-8") as f:
            f.write("more\n")
        self.assertIsNone(self.changed())

    def test_the_biggest_recent_snapshot_is_the_baseline(self):
        self.snap("big")
        write(self.claude_md(), "- one bullet\n" * 3)
        self.snap("already shrunk")            # a watch daemon got there first
        answer = self.changed()
        self.assertIn(f"{len(BIG):,}", answer["systemMessage"])

    def test_nothing_without_snapshots(self):
        self.assertIsNone(self.changed())


class TestCompactionReminder(Cases):
    def test_it_names_the_files_and_the_latest_snapshot(self):
        sha = self.snap()
        out = io.StringIO()
        self.assertEqual(guard_mod.run_remind(self.proj, out), 0)
        answer = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(answer["hookEventName"], "SessionStart")
        text = answer["additionalContext"]
        self.assertIn("just compacted", text)
        self.assertIn("CLAUDE.md", text)
        self.assertIn(sha[:7], text)
        self.assertLess(len(text), 1200)

    def test_it_mentions_a_file_that_got_much_smaller(self):
        self.snap()
        write(self.claude_md(), "- one bullet\n" * 3)
        out = io.StringIO()
        guard_mod.run_remind(self.proj, out)
        text = json.loads(out.getvalue())["hookSpecificOutput"][
            "additionalContext"]
        self.assertIn("much smaller", text)
        self.assertIn("without asking", text)

    def test_nothing_to_say_without_watched_files(self):
        empty = os.path.join(self._tmp, "empty")
        os.makedirs(empty)
        out = io.StringIO()
        guard_mod.run_remind(empty, out)
        self.assertEqual(out.getvalue(), "")

    def test_the_command_runs_through_the_cli(self):
        self.snap()
        code, out, _err = self.cli("remind")
        self.assertEqual(code, 0)
        self.assertIn("additionalContext", out)


class TestGuardCommand(Cases):
    def test_set_and_status(self):
        code, out, _ = self.cli("guard", "set", "--percent", "30",
                                "--mode", "block")
        self.assertEqual(code, 0)
        self.assertIn("more than 30% smaller refuses the edit", out)
        code, out, _ = self.cli("guard", "status")
        self.assertIn("(mode: block)", out)
        self.assertIn("warns you only", out)

    def test_bad_values_are_refused(self):
        self.assertEqual(self.cli("guard", "set")[0], 2)
        code, _out, err = self.cli("guard", "set", "--percent", "100")
        self.assertEqual(code, 2)
        self.assertIn("1 to 99", err)

    def test_status_says_whether_the_hook_is_installed(self):
        settings = os.path.join(self._home, ".claude", "settings.json")
        with mock.patch.object(cli_mod, "CLAUDE_SETTINGS", settings):
            self.assertIn("no - run", self.cli("guard", "status")[1])
            core_mod.write_hooks_file(settings)
            self.assertIn(": yes", self.cli("guard", "status")[1])

    def test_the_hook_subcommand_reads_stdin(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                   "cwd": self.proj, "tool_input": {
                       "file_path": self.claude_md(), "content": "x"}}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            code, out, _ = self.cli("guard", "hook")
        self.assertEqual(code, 0)
        self.assertIn('"permissionDecision": "ask"', out)


class TestTheHookAsARealProcess(Cases):
    def test_stdin_to_stdout_through_python_dash_m(self):
        self.snap()
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                   "cwd": self.proj, "tool_input": {
                       "file_path": self.claude_md(), "content": "x\n"}}
        p = self.run_child("guard", "hook", input=json.dumps(payload).encode())
        self.assertEqual(p.returncode, 0, p.stderr)
        answer = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(answer["permissionDecision"], "ask")

    def test_garbage_on_stdin_is_silent_and_exit_zero(self):
        p = self.run_child("guard", "hook", input=b"\xff\xfe not json")
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_the_file_changed_matcher_lists_the_documented_names(self):
        matcher = core_mod.build_hooks_json()["hooks"]["FileChanged"][0][
            "matcher"]
        self.assertEqual(matcher, "|".join(guard_mod.FILE_CHANGED_NAMES))


class TestTheModuleSaysWhatItCannotDo(unittest.TestCase):
    def test_docstring_names_each_event_and_its_limits(self):
        doc = guard_mod.__doc__
        for text in ("PreToolUse", "FileChanged", "SessionStart",
                     "does NOT see", "cannot block or undo",
                     "not sent to\n      Claude", "PostCompact"):
            self.assertIn(text, doc)


# ---------------------------------------------------------------------------
class TestHookInstallation(Cases):
    def test_the_payload_has_every_event_with_its_matcher(self):
        hooks = core_mod.build_hooks_json()["hooks"]
        self.assertEqual(sorted(hooks), ["FileChanged", "PreCompact",
                                         "PreToolUse", "SessionStart"])
        self.assertEqual([g["matcher"] for g in hooks["SessionStart"]],
                         ["", "compact"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"],
                         "Write|Edit|MultiEdit")
        self.assertEqual(hooks["FileChanged"][0]["matcher"],
                         "CLAUDE.md|CLAUDE.local.md|AGENTS.md")
        cmds = [h["command"] for groups in hooks.values() for g in groups
                for h in g["hooks"]]
        self.assertIn(core_mod.HOOK_GUARD_CMD, cmds)
        self.assertIn(core_mod.HOOK_REMIND_CMD, cmds)

    def test_merge_keeps_the_users_hooks_and_is_idempotent(self):
        mine = {"matcher": "Bash", "hooks": [{"type": "command",
                                              "command": "echo mine"}]}
        settings = {"hooks": {"PreToolUse": [mine]}, "model": "x"}
        core_mod.merge_hooks(settings)
        self.assertEqual(settings["hooks"]["PreToolUse"][0], mine)
        once = json.dumps(settings, sort_keys=True)
        core_mod.merge_hooks(settings)
        self.assertEqual(json.dumps(settings, sort_keys=True), once)
        self.assertEqual(settings["model"], "x")
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 2)

    def test_an_older_install_gains_the_new_hooks_without_duplicates(self):
        old = {"hooks": {
            "PreCompact": [{"matcher": "", "hooks": [{
                "type": "command", "command": "sessionsteward precompact"}]}],
            "SessionStart": [{"matcher": "", "hooks": [{
                "type": "command",
                "command": 'sessionsteward snap -m "session start"'}]}]}}
        core_mod.merge_hooks(old)
        self.assertEqual(len(old["hooks"]["PreCompact"]), 1)
        self.assertEqual(len(old["hooks"]["SessionStart"]), 2)
        self.assertIn("FileChanged", old["hooks"])

    def test_a_users_own_remind_command_is_not_taken_for_ours(self):
        mine = {"matcher": "compact", "hooks": [{
            "type": "command", "command": "echo remind"}]}
        settings = {"hooks": {"SessionStart": [mine]}}
        core_mod.merge_hooks(settings)
        self.assertEqual(settings["hooks"]["SessionStart"][0], mine)

    def test_install_hooks_names_the_new_wiring(self):
        _code, out, _err = self.cli("install-hooks")
        self.assertIn("guard hook", out)
        self.assertIn("FileChanged", out)
        self.assertIn("remind", out)


# ---------------------------------------------------------------------------
class TestWhy(Cases):
    def two(self, new_text):
        a = self.snap("before")
        write(self.claude_md(), new_text)
        b = self.snap("after")
        return a, b

    def test_lost_and_added_lines_are_listed(self):
        a, b = self.two(BIG.replace("Rule 3:", "Rule three:"))
        text = why_mod.render(why_mod.compare(a, b))
        self.assertIn("CLAUDE.md  changed", text)
        self.assertIn("1 line(s) lost, 1 added", text)
        self.assertIn("- - Rule 3: always do thing 3 carefully", text)
        self.assertIn("+ - Rule three: always do thing 3 carefully", text)

    def test_a_moved_paragraph_is_not_a_loss(self):
        lines = BIG.splitlines(keepends=True)
        a, b = self.two("".join(reversed(lines)))
        result = why_mod.compare(a, b)
        entry = result["files"][0]
        self.assertEqual(entry["lost"], [])
        self.assertIn("no line was lost", why_mod.render(result))

    def test_a_wiped_file_loses_every_line(self):
        a, b = self.two("- one bullet\n")
        entry = why_mod.compare(a, b)["files"][0]
        self.assertEqual(len(entry["lost"]), 40)
        self.assertEqual(entry["added"], ["- one bullet"])

    def test_a_deleted_file_is_named(self):
        a = self.snap("before")
        os.unlink(self.claude_md())
        write(os.path.join(self.proj, "AGENTS.md"), "keep me\n")
        b = self.snap("after")
        statuses = {f["name"]: f["status"]
                    for f in why_mod.compare(a, b)["files"]}
        self.assertEqual(statuses["CLAUDE.md"], "deleted")
        self.assertEqual(statuses["AGENTS.md"], "added")

    def test_a_secret_in_an_old_snapshot_is_not_printed(self):
        with mock.patch.dict(os.environ, {"SESSIONSTEWARD_SECRET_SCAN": "0"}):
            write(self.claude_md(), BIG + f"deploy key {AWS}\n")
            a = self.snap("old, from before the scan")
        write(self.claude_md(), BIG)
        b = self.snap("after")
        text = why_mod.render(why_mod.compare(a, b))
        self.assertNotIn(AWS, text)
        self.assertIn("looks like it holds a secret", text)

    def test_how_many_lines_are_shown_can_be_limited(self):
        a, b = self.two("- one bullet\n")
        text = why_mod.render(why_mod.compare(a, b), lines=5)
        self.assertIn("... 35 more", text)

    def test_the_cli_defaults_to_the_newest_snapshot(self):
        a, _b = self.two("- one bullet\n")
        code, out, _err = self.cli("why", a[:7])
        self.assertEqual(code, 0)
        self.assertIn("40 line(s) are no longer anywhere", out)

    def test_bad_snapshot_ids_are_refused_in_one_line(self):
        self.snap()
        for bad in ("--output=x", "zzzz", "deadbeef1"):
            code, _out, err = self.cli("why", "--", bad)
            self.assertEqual(code, 2, bad)
            self.assertEqual(err.count("\n"), 1)

    def test_two_projects_are_not_compared(self):
        a = self.snap()
        other = os.path.join(self._tmp, "other")
        write(os.path.join(other, "CLAUDE.md"), "other project\n" * 20)
        b = core_mod.snap(other, "other")
        with self.assertRaises(core_mod.CrossProjectError):
            why_mod.compare(a, b)

    def test_identical_snapshots_report_no_differences(self):
        a = self.snap()
        self.assertIn("no differences", why_mod.render(why_mod.compare(a, a)))


# ---------------------------------------------------------------------------
class TestPrunePlan(unittest.TestCase):
    def commits(self, n, project="/p", start=0.0, step=86400.0):
        return [{"sha": f"{project}{i}", "tree": "t", "when": start + i * step,
                 "date": "d", "message": "m", "project": project}
                for i in range(n)]

    def test_keep_n_per_project_and_the_newest_always_stays(self):
        a, b = self.commits(5, "/a"), self.commits(3, "/b")
        init = {"sha": "init", "tree": "t", "when": 0.0, "date": "d",
                "message": "init", "project": None}
        drop = retention_mod.plan([init, *a, *b], keep=2, days=None, now=1e9)
        self.assertEqual(sorted(c["sha"] for c in drop), ["/a0", "/a1", "/a2",
                                                          "/b0"])

    def test_days_keeps_recent_ones_even_beyond_keep(self):
        cs = self.commits(6)                  # one per day, newest at day 5
        now = 5 * 86400.0
        drop = retention_mod.plan(cs, keep=1, days=2.5, now=now)
        self.assertEqual([c["sha"] for c in drop], ["/p0", "/p1", "/p2"])

    def test_with_neither_the_newest_is_still_kept(self):
        drop = retention_mod.plan(self.commits(3), keep=None, days=0, now=1e9)
        self.assertEqual([c["sha"] for c in drop], ["/p0", "/p1"])

    def test_nothing_to_drop(self):
        self.assertEqual(retention_mod.plan(self.commits(2), 5, None), [])


class TestPrune(Cases):
    def history(self, n):
        core_mod.init_repo()
        shas = []
        for i in range(n):
            write(self.claude_md(), BIG + f"- change {i}\n")
            shas.append(core_mod.snap(self.proj, f"change {i}"))
        return shas

    def test_it_only_reports_without_yes(self):
        self.history(5)
        before = snapshot_ids(self.proj)
        code, out, _ = self.cli("prune", "--keep", "2")
        self.assertEqual(code, 0)
        self.assertIn("would remove 3 of 6", out)      # 5 + the first
        self.assertIn("Nothing was changed", out)
        self.assertEqual(snapshot_ids(self.proj), before)

    def test_yes_shortens_the_history_and_the_newest_still_restores(self):
        self.history(5)
        newest_text = slurp(self.claude_md()).decode()
        code, out, _ = self.cli("prune", "--keep", "2", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        snaps = core_mod.log(50, project_root=self.proj)
        self.assertEqual([s["message"] for s in snaps],
                         ["change 4", "change 3"])
        write(self.claude_md(), "clobbered\n")
        self.assertEqual(self.cli("restore", snaps[0]["sha"][:7])[0], 0)
        self.assertEqual(slurp(self.claude_md()).decode(), newest_text)
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(core_mod._git(core_mod.REPO_DIR, "fsck",
                                       "--no-dangling").strip(), "")

    def test_the_history_stays_in_order_with_messages_and_dates(self):
        self.history(4)
        old = core_mod._git(core_mod.REPO_DIR, "log", "-2",
                            "--pretty=%at|%s").splitlines()
        self.cli("prune", "--keep", "2", "--yes")
        new = core_mod._git(core_mod.REPO_DIR, "log", "-2",
                            "--pretty=%at|%s").splitlines()
        self.assertEqual(new, old)

    def test_a_second_project_keeps_its_newest_snapshot(self):
        self.history(4)
        other = os.path.join(self._tmp, "other")
        write(os.path.join(other, "CLAUDE.md"), "other\n" * 40)
        core_mod.snap(other, "other only")
        for i in range(3):
            write(self.claude_md(), BIG + f"- later {i}\n")
            core_mod.snap(self.proj, f"later {i}")
        self.cli("prune", "--keep", "1", "--days", "0", "--yes")
        self.assertEqual([s["message"] for s in
                          core_mod.log(50, project_root=other)],
                         ["other only"])
        self.assertEqual(len(core_mod.log(50, project_root=self.proj)), 1)

    def test_days_keeps_everything_recent(self):
        self.history(4)
        _code, out, _ = self.cli("prune", "--days", "1", "--yes")
        self.assertIn("nothing to remove", out)

    def test_the_policy_can_be_saved_and_is_used_by_default(self):
        self.history(5)
        self.cli("prune", "--keep", "3", "--save")
        self.assertEqual(core_mod.load_config()["retention"], {"keep": 3})
        _code, out, _ = self.cli("prune")
        self.assertIn("the newest 3 per project", out)

    def test_the_default_policy_is_stated(self):
        self.history(2)
        _code, out, _ = self.cli("prune")
        self.assertIn("the newest 50 per project", out)
        self.assertIn("everything from the last 30 days", out)

    def test_nonsense_is_refused(self):
        self.history(1)
        for args in (("--keep", "0"), ("--days", "-1"), ("--days", "nan")):
            code, _out, err = self.cli("prune", *args)
            self.assertEqual(code, 2, args)
            self.assertIn("error:", err)

    def test_no_repo_is_a_clear_error(self):
        code, _out, err = self.cli("prune")
        self.assertEqual(code, 2)
        self.assertIn("nothing to prune", err)


class TestReadmeAndVersion(unittest.TestCase):
    def test_the_readme_says_what_the_hooks_cannot_do(self):
        readme = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "README.md"), encoding="utf-8").read()
        self.assertTrue(readme.startswith("# Session Steward"))
        self.assertIn("**Undo for CLAUDE.md.**", readme)
        for text in ("sessionsteward why", "sessionsteward prune",
                     "FileChanged", "PostCompact", "cannot"):
            self.assertIn(text, readme)

    def test_the_version(self):
        import sessionsteward
        self.assertEqual(sessionsteward.__version__, "0.2.0")


if __name__ == "__main__":
    unittest.main()
