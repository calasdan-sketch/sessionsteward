"""Round-6 regression tests (second verification panel).

Temp dirs only. The environment is hermetic (see Base in the round-5 tests):
HOME and the git config locations point into a temp dir, every GIT_*
variable is removed, and the child processes get PYTHONUNBUFFERED removed.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import cli as cli_mod
from sessionsteward import core as core_mod

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from .test_round5_fixes import Base, plain_git, slurp, write
except ImportError:
    from test_round5_fixes import Base, plain_git, slurp, write

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def case_insensitive(directory: str) -> bool:
    """Is `directory` on a file system that treats Notes.md and notes.md as
    one name (Windows, macOS by default)?"""
    probe = tempfile.mkdtemp(prefix="CaseProbe-", dir=directory)
    try:
        write(os.path.join(probe, "Probe.txt"), "x")
        return os.path.exists(os.path.join(probe, "probe.txt"))
    finally:
        for name in os.listdir(probe):
            os.remove(os.path.join(probe, name))
        os.rmdir(probe)


def listed(rev: str = "HEAD") -> list[str]:
    """Every file path in snapshot `rev`, exactly as git spells it."""
    out = plain_git(core_mod.REPO_DIR, "ls-tree", "-r", "-z", "--name-only",
                    rev)
    return sorted(p for p in out.split("\0") if p)


class TestCaseOnlyRenameDoesNotWedgeTheStore(Base):
    """HIGH (round 2): on a case-insensitive file system (Windows, macOS) git
    keeps the OLD-case path in the index after a file or directory is
    renamed by case alone, and never stages the new-case name. The snapshot
    check then saw a mirrored file git "did not record" and refused every
    later snap, undo and restore - for good, so a clobbered CLAUDE.md could
    not be recovered.

    The tests put the store in that state on any file system: with
    core.ignorecase=true git looks names up case-insensitively, and an
    assume-unchanged index entry is one git does not notice go missing - which
    is what the old-case path is on a file system where the new-case name
    still reaches the file."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, "CLAUDE.md")
        self.dot = os.path.join(self.proj, ".claude")
        write(os.path.join(self.dot, "Notes.md"), "notes v1\n")
        write(os.path.join(self.dot, "Sub", "Inner.md"), "inner v1\n")
        self.assertEqual(self.cli("init")[0], 0)

    def as_case_insensitive_store(self, *tracked: str) -> None:
        """What a case-insensitive file system makes of the store: git was
        told so at `init`, and these index entries cannot be seen to vanish."""
        plain_git(core_mod.REPO_DIR, "config", "--local", "core.ignorecase",
                  "true")
        plain_git(core_mod.REPO_DIR, "update-index", "--assume-unchanged",
                  "--", *(f"files/{t}" for t in tracked))

    def rename(self, old: str, new: str) -> None:
        os.rename(os.path.join(self.dot, *old.split("/")),
                  os.path.join(self.dot, *new.split("/")))

    def assert_recovers_claude_md(self):
        """The point of the tool: a clobbered CLAUDE.md comes back."""
        good = slurp(self.claude)
        write(self.claude, "- one bullet\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(self.claude), good)

    def test_snap_after_a_case_only_file_rename(self):
        self.as_case_insensitive_store(".claude/Notes.md")
        self.rename("Notes.md", "notes.md")
        write(os.path.join(self.dot, "notes.md"), "notes v2\n")
        code, out, err = self.cli("snap", "-m", "renamed")
        self.assertEqual((code, err), (0, ""), out)
        names = listed()
        self.assertIn("files/.claude/notes.md", names)
        self.assertNotIn("files/.claude/Notes.md", names)
        self.assertEqual(plain_git(core_mod.REPO_DIR, "show",
                                   "HEAD:files/.claude/notes.md"),
                         "notes v2\n")
        self.assertEqual(self.log()[0]["message"], "renamed")

    def test_a_clobbered_claude_md_is_recovered_after_a_case_only_rename(self):
        self.as_case_insensitive_store(".claude/Notes.md")
        self.rename("Notes.md", "notes.md")
        self.assertEqual(self.cli("snap", "-m", "renamed")[0], 0)
        self.assert_recovers_claude_md()

    def test_undo_straight_after_the_rename_still_works(self):
        # no snap in between: undo's own pre-undo snapshot must not be
        # the thing that refuses
        self.as_case_insensitive_store(".claude/Notes.md")
        self.rename("Notes.md", "notes.md")
        self.assert_recovers_claude_md()

    def test_restore_of_an_earlier_snapshot_after_a_case_only_rename(self):
        first = self.log()[0]["sha"]
        self.as_case_insensitive_store(".claude/Notes.md")
        self.rename("Notes.md", "notes.md")
        write(self.claude, "- one bullet\n")
        code, out, err = self.cli("restore", first)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")

    def test_snap_after_a_case_only_directory_rename(self):
        self.as_case_insensitive_store(".claude/Sub/Inner.md")
        self.rename("Sub", "SUB")
        code, out, err = self.cli("snap", "-m", "renamed dir")
        self.assertEqual((code, err), (0, ""), out)
        names = listed()
        self.assertIn("files/.claude/SUB/Inner.md", names)
        self.assertNotIn("files/.claude/Sub/Inner.md", names)
        self.assert_recovers_claude_md()

    def test_both_renamed_and_edited_at_once(self):
        self.as_case_insensitive_store(".claude/Notes.md",
                                       ".claude/Sub/Inner.md")
        self.rename("Notes.md", "notes.md")
        self.rename("Sub", "SUB")
        write(os.path.join(self.dot, "SUB", "Inner.md"), "inner v2\n")
        self.assertEqual(self.cli("snap")[0], 0)
        self.assertEqual(plain_git(core_mod.REPO_DIR, "show",
                                   "HEAD:files/.claude/SUB/Inner.md"),
                         "inner v2\n")
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0)

    def test_the_next_snap_heals_a_store_the_old_version_wedged(self):
        # state left behind by the previous version: HEAD is the last good
        # snapshot, the index still holds the old-case path, and the mirror
        # and manifest were rewritten by the snaps that were refused
        self.as_case_insensitive_store(".claude/Notes.md")
        self.rename("Notes.md", "notes.md")
        head = core_mod._git(core_mod.REPO_DIR, "rev-parse", "HEAD").strip()
        refused = core_mod.StewardError("snapshot incomplete: as before")
        with mock.patch.object(core_mod, "_check_staged",
                               side_effect=refused):
            for _ in range(2):
                self.assertEqual(self.cli("snap")[0], 2)
        self.assertEqual(core_mod._git(core_mod.REPO_DIR, "rev-parse",
                                       "HEAD").strip(), head)
        code, out, err = self.cli("snap", "-m", "after the wedge")
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotEqual(core_mod._git(core_mod.REPO_DIR, "rev-parse",
                                          "HEAD").strip(), head)
        self.assertIn("files/.claude/notes.md", listed())
        self.assertNotIn("files/.claude/Notes.md", listed())

    def test_repeating_the_snap_commits_nothing_new(self):
        self.as_case_insensitive_store(".claude/Notes.md")
        self.rename("Notes.md", "notes.md")
        self.assertEqual(self.cli("snap")[0], 0)
        head = core_mod._git(core_mod.REPO_DIR, "rev-parse", "HEAD").strip()
        self.assertEqual(self.cli("snap")[0], 0)
        self.assertEqual(core_mod._git(core_mod.REPO_DIR, "rev-parse",
                                       "HEAD").strip(), head)

    def test_a_rename_is_snapped_the_same_on_a_case_sensitive_store(self):
        self.rename("Notes.md", "notes.md")
        self.assertEqual(self.cli("snap")[0], 0)
        names = listed()
        self.assertIn("files/.claude/notes.md", names)
        self.assertNotIn("files/.claude/Notes.md", names)
        self.assert_recovers_claude_md()

    def test_the_premise_git_keeps_the_old_case_and_skips_the_new(self):
        # Documents what the fix is for. If a git ever stops doing this,
        # the emulation above no longer reproduces the wedge: revisit it.
        repo = os.path.join(self._tmp, "premise")
        os.makedirs(repo)
        plain_git(repo, "init", "-q")
        plain_git(repo, "config", "core.ignorecase", "true")
        write(os.path.join(repo, "d", "Notes.md"), "x\n")
        plain_git(repo, "add", "-A")
        plain_git(repo, "update-index", "--assume-unchanged", "d/Notes.md")
        os.rename(os.path.join(repo, "d", "Notes.md"),
                  os.path.join(repo, "d", "notes.md"))
        plain_git(repo, "add", "-A", "-f")
        self.assertEqual(plain_git(repo, "ls-files").split(), ["d/Notes.md"])


@unittest.skipUnless(case_insensitive(tempfile.gettempdir()),
                     "needs a case-insensitive file system (Windows, macOS; "
                     "on Linux point TMPDIR at e.g. a ciopfs mount)")
class TestCaseOnlyRenameOnARealCaseInsensitiveFileSystem(Base):
    """The same wedge, with nothing emulated."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.proj, "CLAUDE.md")
        self.dot = os.path.join(self.proj, ".claude")
        write(os.path.join(self.dot, "Notes.md"), "notes v1\n")
        write(os.path.join(self.dot, "Sub", "Inner.md"), "inner v1\n")
        self.assertEqual(self.cli("init")[0], 0)

    def test_file_rename_snap_and_undo(self):
        os.rename(os.path.join(self.dot, "Notes.md"),
                  os.path.join(self.dot, "notes.md"))
        code, out, err = self.cli("snap", "-m", "renamed")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("files/.claude/notes.md", listed())
        self.assertNotIn("files/.claude/Notes.md", listed())
        write(self.claude, "- one bullet\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")

    def test_directory_rename_snap_and_undo(self):
        tmp_name = os.path.join(self.dot, "Sub.tmp")
        os.rename(os.path.join(self.dot, "Sub"), tmp_name)
        os.rename(tmp_name, os.path.join(self.dot, "SUB"))
        code, out, err = self.cli("snap", "-m", "renamed dir")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("files/.claude/SUB/Inner.md", listed())
        self.assertNotIn("files/.claude/Sub/Inner.md", listed())
        write(self.claude, "- one bullet\n")
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(slurp(self.claude), b"# Project\n\nBe helpful.\n")

    def test_the_old_case_comes_back_when_the_old_snapshot_is_restored(self):
        first = self.log()[0]["sha"]
        os.rename(os.path.join(self.dot, "Notes.md"),
                  os.path.join(self.dot, "notes.md"))
        self.assertEqual(self.cli("snap", "-m", "renamed")[0], 0)
        code, out, err = self.cli("restore", first)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Notes.md", os.listdir(self.dot))


class TestCheckStagedNames(Base):
    """_check_staged: every file in the mirror must be in the index under
    the name the mirror gives it."""

    def setUp(self):
        super().setUp()
        core_mod.init_repo()
        self.files = os.path.join(core_mod.REPO_DIR, "files")
        os.makedirs(self.files)
        blob = os.path.join(self._tmp, "blob.txt")
        write(blob, "content\n")
        self.blob = plain_git(core_mod.REPO_DIR, "hash-object", "-w",
                              blob).strip()

    def mirror(self, *names: str) -> None:
        for n in names:
            write(os.path.join(self.files, *n.split("/")), n)

    def stage(self, *names: str) -> None:
        """Put `names` (relative to files/) in the index, whatever the
        mirror holds."""
        for n in names:
            plain_git(core_mod.REPO_DIR, "update-index", "--add",
                      "--cacheinfo", f"100644,{self.blob},files/{n}")

    def check(self, kinds=None):
        core_mod._check_staged(self.files, kinds or {})

    def test_every_mirrored_file_staged_passes(self):
        self.mirror("a.md", ".claude/b.md")
        self.stage("a.md", ".claude/b.md")
        self.check()

    def test_a_mirrored_file_the_index_lacks_is_named(self):
        self.mirror("a.md", "b.md")
        self.stage("a.md")
        with self.assertRaises(core_mod.StewardError) as cm:
            self.check()
        self.assertIn("snapshot incomplete", str(cm.exception))
        self.assertIn("files/b.md", str(cm.exception))

    def test_a_watched_file_that_never_reached_the_mirror_is_caught(self):
        self.mirror("a.md")
        self.stage("a.md")
        with self.assertRaises(core_mod.StewardError):
            self.check({"a.md": "file", "gone.md": "file"})

    def test_a_case_twin_in_the_index_does_not_stand_in_for_the_file(self):
        # after the fix the index is rebuilt from the mirror, so a name
        # that differs only in case is a different (missing) name
        self.mirror(".claude/notes.md")
        self.stage(".claude/Notes.md")
        with self.assertRaises(core_mod.StewardError):
            self.check()

    def test_a_name_git_composed_is_the_name_on_disk(self):
        # macOS keeps names decomposed (e + combining acute), and git with
        # core.precomposeunicode stores them composed (e-acute)
        decomposed = "caf" + "e" + chr(0x301) + ".md"
        composed = "caf" + chr(0xE9) + ".md"
        self.assertNotEqual(decomposed, composed)
        self.assertEqual(unicodedata.normalize("NFC", decomposed), composed)
        self.mirror(f".claude/{decomposed}")
        self.stage(f".claude/{composed}")
        self.check()

    @unittest.skipIf(sys.platform == "darwin",
                     "macOS folds the two spellings into one file")
    def test_a_missing_file_is_not_hidden_by_a_composed_twin(self):
        decomposed = "caf" + "e" + chr(0x301) + ".md"
        composed = "caf" + chr(0xE9) + ".md"
        # two different files on disk, one name in the index
        self.mirror(f".claude/{decomposed}", f".claude/{composed}")
        self.stage(f".claude/{composed}")
        with self.assertRaises(core_mod.StewardError):
            self.check()

    @unittest.skipIf(sys.platform == "darwin",
                     "macOS folds the two spellings into one file")
    def test_one_index_entry_accounts_for_only_one_file(self):
        # two spellings of one name (the combining marks in either order),
        # both on disk; git recorded the composed name once
        one = "a" + chr(0x323) + chr(0x301) + ".md"
        other = "a" + chr(0x301) + chr(0x323) + ".md"
        composed = unicodedata.normalize("NFC", one)
        self.assertEqual(composed, unicodedata.normalize("NFC", other))
        self.assertNotIn(composed, (one, other))
        self.mirror(f".claude/{one}", f".claude/{other}")
        self.stage(f".claude/{composed}")
        with self.assertRaises(core_mod.StewardError) as cm:
            self.check()
        self.assertIn("1 of the mirrored", str(cm.exception))

    def test_a_decomposed_name_the_index_lacks_altogether_is_caught(self):
        decomposed = "caf" + "e" + chr(0x301) + ".md"
        self.mirror(f".claude/{decomposed}", "a.md")
        self.stage("a.md")
        with self.assertRaises(core_mod.StewardError):
            self.check()


class TestWhatTheMirrorTakes(Base):
    """LOW (round 2, #2): the forced add (which is what keeps a git-ignored
    CLAUDE.local.md in the snapshot) also took whatever sat in an ignored
    dependency directory - a 100 MB file doubled the store and made `init`
    25 times slower - and the store itself was readable by other users."""

    def setUp(self):
        super().setUp()
        self.dot = os.path.join(self.proj, ".claude")
        self.claude = os.path.join(self.proj, "CLAUDE.md")

    def init(self):
        code, out, err = self.cli("init")
        self.assertEqual(code, 0, out + err)
        return out, err

    def assert_matches_the_live_files(self):
        self.assertEqual(core_mod.current_position(self.log(), self.proj), 0,
                         "the live files no longer match the snapshot")

    # -- virtual environments and build output ------------------------------
    def skill(self, name="x"):
        return os.path.join(self.dot, "skills", name)

    def test_virtual_environments_are_not_snapshotted(self):
        write(os.path.join(self.skill(), "SKILL.md"), "skill\n")
        write(os.path.join(self.skill(), ".venv", "lib", "a.py"), "x")
        # any name, as long as it is one
        write(os.path.join(self.skill(), "venv", "pyvenv.cfg"), "home = /x\n")
        write(os.path.join(self.skill(), "venv", "lib", "b.py"), "x")
        write(os.path.join(self.skill(), "env", "pyvenv.cfg"), "home = /x\n")
        write(os.path.join(self.skill(), "env", "lib", "c.py"), "x")
        self.init()
        names = listed()
        self.assertIn("files/.claude/skills/x/SKILL.md", names)
        self.assertEqual([n for n in names if "/skills/x/" in n
                          and not n.endswith("SKILL.md")], [])
        self.assert_matches_the_live_files()
        live = core_mod.file_mtimes(core_mod.watched_paths(self.proj))
        self.assertEqual([p for p in live if p.endswith(".py")
                          or p.endswith("pyvenv.cfg")], [])

    BUILDS = (  # skill, its build manifest, its build output
        ("js", "package.json", "dist"), ("js2", "package.json", "build"),
        ("rust", "Cargo.toml", "target"), ("c", "Makefile", "build"),
        ("py", "pyproject.toml", "dist"))

    def test_build_output_next_to_a_build_manifest_is_not_snapshotted(self):
        for name, manifest, out_dir in self.BUILDS:
            write(os.path.join(self.skill(name), manifest), "{}\n")
            write(os.path.join(self.skill(name), out_dir, "out.js"), "x")
        self.init()
        names = listed()
        for name, manifest, out_dir in self.BUILDS:
            base = f"files/.claude/skills/{name}"
            self.assertIn(f"{base}/{manifest}", names)
            self.assertNotIn(f"{base}/{out_dir}/out.js", names, name)
        self.assert_matches_the_live_files()

    def test_only_the_output_directories_are_left_out_not_their_siblings(self):
        # `docs/`, and `build/` where no manifest sits beside it
        write(os.path.join(self.skill(), "package.json"), "{}\n")
        write(os.path.join(self.skill(), "dist", "out.js"), "x")
        write(os.path.join(self.skill(), "docs", "guide.md"), "mine\n")
        write(os.path.join(self.skill(), "docs", "build", "notes.md"), "mine\n")
        self.init()
        names = listed()
        self.assertIn("files/.claude/skills/x/docs/guide.md", names)
        self.assertIn("files/.claude/skills/x/docs/build/notes.md", names)
        self.assertNotIn("files/.claude/skills/x/dist/out.js", names)

    def test_the_name_alone_never_leaves_a_directory_out(self):
        # rules/build/ and commands/dist/ are the user's own
        for rel in ("rules/build/conventions.md", "commands/dist/ship.md",
                    "commands/target/aim.md", "rules/venv/notes.md",
                    "skills/x/build/SKILL.md"):
            write(os.path.join(self.dot, *rel.split("/")), "mine\n")
        self.init()
        for rel in ("rules/build/conventions.md", "commands/dist/ship.md",
                    "commands/target/aim.md", "rules/venv/notes.md",
                    "skills/x/build/SKILL.md"):
            self.assertIn(f"files/.claude/{rel}", listed(), rel)
        self.assert_matches_the_live_files()

    def test_a_file_named_like_build_output_is_kept(self):
        # `build` or `target` as a FILE is a script someone wrote
        write(os.path.join(self.skill(), "package.json"), "{}\n")
        for name in ("dist", "build", "target", ".venv"):
            write(os.path.join(self.skill(), name), "#!/bin/sh\n")
        self.init()
        for name in ("dist", "build", "target", ".venv"):
            self.assertIn(f"files/.claude/skills/x/{name}", listed(), name)
        self.assert_matches_the_live_files()

    def test_restore_leaves_a_virtual_environment_in_the_trash_copy(self):
        write(os.path.join(self.dot, ".venv", "keep.txt"), "mine\n")
        write(os.path.join(self.dot, "rules.md"), "v1\n")
        self.init()
        write(os.path.join(self.dot, "rules.md"), "v2\n")
        self.assertEqual(self.cli("snap")[0], 0)
        write(os.path.join(self.dot, "rules.md"), "clobbered\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(os.path.join(self.dot, "rules.md")), b"v2\n")
        # not part of a snapshot, so not carried over by the restore ...
        self.assertFalse(os.path.exists(os.path.join(self.dot, ".venv")))
        # ... but never lost: it is in the trash copy the restore names
        trashed = [e for e in self.trash_entries() if e.startswith(".claude.")]
        self.assertEqual(len(trashed), 1, self.trash_entries())
        self.assertTrue(os.path.isfile(os.path.join(
            core_mod.TRASH_DIR, trashed[0], ".venv", "keep.txt")))

    # -- the size limit ----------------------------------------------------
    def limit(self, megabytes):
        os.environ["SESSIONSTEWARD_MAX_FILE_MB"] = str(megabytes)

    def test_a_file_over_the_limit_is_left_out_and_said_so(self):
        self.limit(0.001)    # 1048 bytes
        write(os.path.join(self.dot, "big.bin"), "x" * 3000)
        write(os.path.join(self.dot, "small.md"), "small\n")
        out, err = self.init()
        names = listed()
        self.assertIn("files/.claude/small.md", names)
        self.assertNotIn("files/.claude/big.bin", names)
        lines = err.strip().splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("warning: "), lines[0])
        self.assertIn("big.bin", lines[0])
        self.assertIn("SESSIONSTEWARD_MAX_FILE_MB", lines[0])
        self.assertNotIn("warning", out)
        self.assert_matches_the_live_files()

    def test_every_snap_says_it_again_in_one_line(self):
        self.limit(0.001)
        write(os.path.join(self.dot, "big1.bin"), "x" * 3000)
        write(os.path.join(self.dot, "big2.bin"), "x" * 4000)
        self.init()
        write(self.claude, "changed\n")
        code, out, err = self.cli("snap")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("2 files", err)
        self.assertIn("big1.bin", err)
        code, out, err = self.cli("precompact")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)

    def test_the_limit_is_inclusive(self):
        self.limit(1 / 1024)     # exactly 1024 bytes
        write(os.path.join(self.dot, "exact.md"), "x" * 1024)
        write(os.path.join(self.dot, "over.md"), "x" * 1025)
        self.init()
        names = listed()
        self.assertIn("files/.claude/exact.md", names)
        self.assertNotIn("files/.claude/over.md", names)
        self.assert_matches_the_live_files()   # the walk draws the same line

    def test_the_default_limit_is_ten_megabytes_and_can_be_changed(self):
        os.environ.pop("SESSIONSTEWARD_MAX_FILE_MB", None)
        self.assertEqual(core_mod.max_file_bytes(), 10 * 1024 * 1024)
        self.limit(2)
        self.assertEqual(core_mod.max_file_bytes(), 2 * 1024 * 1024)
        self.limit(0)            # no limit
        self.assertIsNone(core_mod.max_file_bytes())
        for junk in ("abc", "-3", "nan", "inf", ""):
            self.limit(junk)
            self.assertEqual(core_mod.max_file_bytes(), 10 * 1024 * 1024,
                             junk)

    def test_no_limit_snapshots_everything(self):
        self.limit(0)
        write(os.path.join(self.dot, "big.bin"), "x" * 3000)
        out, err = self.init()
        self.assertIn("files/.claude/big.bin", listed())
        self.assertEqual(err, "")

    def test_a_file_you_named_is_never_left_out_for_its_size(self):
        # only what is found inside a watched directory is subject to the
        # limit: a watched file with no snapshot could not be restored
        self.limit(0.001)
        write(self.claude, "x" * 3000)
        out, err = self.init()
        self.assertIn("files/CLAUDE.md", listed())
        self.assertEqual(err, "")
        self.assert_matches_the_live_files()
        self.assertIn(self.claude, core_mod.file_mtimes([self.claude]))
        write(self.claude, "clobbered\n")
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(slurp(self.claude), b"x" * 3000)

    def test_a_file_over_the_limit_does_not_trigger_the_watcher_or_undo(self):
        self.limit(0.001)
        big = os.path.join(self.dot, "big.bin")
        write(big, "x" * 3000)
        write(os.path.join(self.dot, "rules.md"), "v1\n")
        self.init()
        before = core_mod.file_mtimes(core_mod.watched_paths(self.proj))
        self.assertNotIn(big, before)
        write(big, "y" * 5000)                      # grows: still nobody's business
        self.assertEqual(core_mod.file_mtimes(
            core_mod.watched_paths(self.proj)), before)
        write(self.claude, "clobbered\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertIn("unchanged .claude", out)
        self.assertEqual(slurp(big), b"y" * 5000)   # `.claude` was not swapped

    # -- who can read the store -------------------------------------------
    posix = unittest.skipIf(os.name == "nt", "POSIX permission bits")

    @posix
    def test_a_private_file_is_not_mirrored_wider(self):
        os.umask(0)
        self.addCleanup(os.umask, 0o022)
        secret = os.path.join(self.dot, "secret.md")
        write(secret, "token\n")
        os.chmod(secret, 0o600)
        script = os.path.join(self.dot, "hook.sh")
        write(script, "#!/bin/sh\n")
        os.chmod(script, 0o750)
        self.init()
        files = os.path.join(core_mod.REPO_DIR, "files", ".claude")
        self.assertEqual(stat.S_IMODE(os.stat(
            os.path.join(files, "secret.md")).st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(os.stat(
            os.path.join(files, "hook.sh")).st_mode) & 0o007, 0)
        # what git records is still only "executable or not"
        modes = core_mod._tree_blobs("HEAD")
        self.assertEqual(modes[".claude/secret.md"], "100644")
        self.assertEqual(modes[".claude/hook.sh"], "100755")

    @posix
    def test_the_store_folder_is_private(self):
        os.umask(0)
        self.addCleanup(os.umask, 0o022)
        self.init()
        root = os.path.dirname(core_mod.REPO_DIR)
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)

    @posix
    def test_a_store_folder_an_older_version_made_wide_is_tightened(self):
        self.init()
        root = os.path.dirname(core_mod.REPO_DIR)
        os.chmod(root, 0o755)
        write(self.claude, "changed\n")
        self.assertEqual(self.cli("snap")[0], 0)
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)


class TestInstallHooksOnlyRewritesItsOwn(Base):
    """LOW (round 2, #3): `install-hooks --write` rewrote ANY hook whose
    command contained `sessionsteward` as a word - `cd ~/code/sessionsteward
    && git pull` included - and said "existing settings preserved". Only the
    commands sessionsteward itself writes are replaced."""

    PRE = core_mod.HOOK_PRECOMPACT_CMD
    START = core_mod.HOOK_SESSIONSTART_CMD

    def setUp(self):
        super().setUp()
        self.settings = os.path.join(self._tmp, "claude", "settings.json")
        os.makedirs(os.path.dirname(self.settings))
        p = mock.patch.object(cli_mod, "CLAUDE_SETTINGS", self.settings)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def entry(*commands):
        return {"matcher": "", "hooks": [{"type": "command", "command": c}
                                         for c in commands]}

    def put(self, hooks) -> bytes:
        raw = json.dumps({"model": "opus", "hooks": hooks}, indent=2).encode()
        write(self.settings, raw, binary=True)
        return raw

    def commands(self, event):
        merged = json.loads(slurp(self.settings).decode("utf-8"))
        return core_mod._hook_commands(merged["hooks"][event])

    # the user's own commands: they mention sessionsteward, none is ours
    THEIRS = (
        "cd ~/code/sessionsteward && git pull",       # the panel's hook
        "sessionsteward snap -m checkpoint",
        "sessionsteward log",
        "python -m sessionsteward diff",
        'python -u -m sessionsteward snap -m "session start"',
        "echo sessionsteward precompact",
        "sessionsteward precompact --root /somewhere/else",
        "tail -f ~/logs/sessionsteward.log",
    )

    def test_a_hook_that_only_mentions_sessionsteward_is_left_alone(self):
        original = self.put({
            "PreCompact": [self.entry(*self.THEIRS)],
            "SessionStart": [self.entry(*self.THEIRS)]})
        code, out, err = self.cli("install-hooks", "--write")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.commands("PreCompact"),
                         [*self.THEIRS, self.PRE])
        self.assertEqual(self.commands("SessionStart"),
                         [*self.THEIRS, self.START])
        self.assertEqual(slurp(self.settings + ".bak"), original)

    def test_the_panels_hook_survives_and_a_second_write_changes_nothing(self):
        pull = "cd ~/code/sessionsteward && git pull"
        self.put({"SessionStart": [self.entry(pull)]})
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertEqual(self.commands("SessionStart"), [pull, self.START])
        first = slurp(self.settings)
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertEqual(slurp(self.settings), first)

    def test_what_sessionsteward_wrote_is_still_rewritten_in_place(self):
        ours_pre = ("sessionsteward precompact",                       # 1st version
                    "/usr/bin/python3 -m sessionsteward precompact",
                    "'/old venv/bin/python' -m sessionsteward precompact",
                    '"C:\\old venv\\Scripts\\python.exe" -m sessionsteward '
                    "precompact",
                    self.PRE)
        ours_start = ('sessionsteward snap -m "session start"',
                      '/usr/bin/python3 -m sessionsteward snap -m '
                      '"session start"',
                      '"C:\\old venv\\Scripts\\python.exe" -m sessionsteward '
                      'snap -m "session start"',
                      self.START)
        for stale in ours_pre:
            self.put({"PreCompact": [self.entry("echo before", stale)]})
            self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
            self.assertEqual(self.commands("PreCompact"),
                             ["echo before", self.PRE], stale)
        for stale in ours_start:
            self.put({"SessionStart": [self.entry(stale, "echo after")]})
            self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
            self.assertEqual(self.commands("SessionStart"),
                             [self.START, "echo after"], stale)

    def test_duplicates_of_our_own_collapse_but_the_users_stay(self):
        self.put({"PreCompact": [
            self.entry("sessionsteward precompact", "echo mine"),
            self.entry("/old/python -m sessionsteward precompact",
                       "cd ~/code/sessionsteward && git pull")]})
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertEqual(self.commands("PreCompact"),
                         [self.PRE, "echo mine",
                          "cd ~/code/sessionsteward && git pull"])

    def test_a_command_is_ours_only_under_the_event_it_was_written_for(self):
        self.put({"SessionStart": [self.entry("sessionsteward precompact")],
                  "PreCompact": [self.entry('sessionsteward snap -m '
                                            '"session start"')]})
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertEqual(self.commands("SessionStart"),
                         ["sessionsteward precompact", self.START])
        self.assertEqual(self.commands("PreCompact"),
                         ['sessionsteward snap -m "session start"', self.PRE])

    def test_commands_that_cannot_be_parsed_or_are_not_text_are_not_ours(self):
        odd = ['sessionsteward precompact "unterminated', "", "   "]
        self.put({"PreCompact": [self.entry(*odd), {"hooks": [
            {"type": "command", "command": 5}, {"type": "command"}]}]})
        self.assertEqual(self.cli("install-hooks", "--write")[0], 0)
        self.assertEqual(self.commands("PreCompact")[:3], odd)
        self.assertEqual(self.commands("PreCompact")[-1], self.PRE)


class TestWatchIntervalIsValidated(Base):
    """LOW (round 2, #4): `watch --interval -1|nan|inf` ended in a traceback
    (time.sleep refuses them), `0` spun a CPU, and a huge value overflowed."""

    BAD = ("-1", "0", "nan", "inf", "-inf", "abc", "", "1e999", "1e300",
           "0.05", "0.099", "86400.5", "-0.0")
    # argparse itself cannot tell "-inf" as a separate word from a flag
    SEPARATE_WORD_REFUSED_BY_ARGPARSE = ("-inf",)

    def run_watch(self, *args):
        """`watch` with sleep replaced by an immediate Ctrl-C, so a valid
        interval ends the daemon at once."""
        stop = mock.Mock(sleep=mock.Mock(side_effect=KeyboardInterrupt))
        with mock.patch.object(cli_mod, "time", stop):
            return self.cli("watch", *args)

    def test_a_bad_interval_is_one_error_line_and_exit_2(self):
        for bad in self.BAD:
            forms = [[f"--interval={bad}"]]
            if bad not in self.SEPARATE_WORD_REFUSED_BY_ARGPARSE:
                forms.append(["--interval", bad])
            for argv in forms:
                code, out, err = self.run_watch(*argv)
                self.assertEqual(code, 2, (argv, out, err))
                self.assertEqual(len(err.strip().splitlines()), 1, (argv, err))
                self.assertTrue(err.startswith("error: "), (argv, err))
                self.assertIn("--interval", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "", argv)

    def test_minus_inf_as_a_separate_word_is_a_usage_error_all_the_same(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_watch("--interval", "-inf")
        self.assertEqual(cm.exception.code, 2)

    def test_a_bad_interval_is_refused_before_anything_is_touched(self):
        self.assertEqual(self.run_watch("--interval", "nan")[0], 2)
        self.assertFalse(os.path.exists(core_mod.REPO_DIR))
        self.assertFalse(os.path.exists(os.path.dirname(core_mod.REPO_DIR)))

    def test_a_bad_interval_is_refused_with_add_and_remove_too(self):
        for flag in ("--add", "--remove"):
            code, out, err = self.run_watch(flag, "NOTES.md", "--interval",
                                            "0")
            self.assertEqual(code, 2, (flag, out, err))
            self.assertIn("--interval", err)

    def test_the_limits_and_ordinary_values_are_accepted(self):
        for good, shown in (("0.1", "0.1"), ("5", "5.0"), ("1e-1", "0.1"),
                            ("86400", "86400.0"), ("2.5", "2.5")):
            code, out, err = self.run_watch("--interval", good)
            self.assertEqual(code, 0, (good, out, err))
            self.assertIn(f"every {shown}s", out)
            self.assertEqual(err, "")

    def test_the_default_still_works(self):
        code, out, err = self.run_watch()
        self.assertEqual(code, 0, out + err)
        self.assertIn("every 5.0s", out)

    def test_a_real_process_never_prints_a_traceback(self):
        for bad in ("-1", "nan", "inf", "0", "1e300"):
            p = self.run_child("watch", "--interval", bad)
            self.assertEqual(p.returncode, 2, (bad, p.stderr))
            self.assertNotIn(b"Traceback", p.stderr)
            self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)


class TestWatchEntriesThatNameOneFile(Base):
    """Two watch-list entries that differ only by case and name one file (on a
    case-insensitive file system: `watch --add claude.md` next to the default
    CLAUDE.md) are one watched path. Mirrored twice, the second spelling lands
    on the first one's file, git records one name, and the completeness check
    refuses every snap, undo and restore until the entry is removed. A hard
    link stands in for the second spelling here (same file, two names)."""

    def _hard_link(self, name: str, alias: str) -> str:
        dst = os.path.join(self.proj, alias)
        try:
            os.link(os.path.join(self.proj, name), dst)
        except (OSError, AttributeError, NotImplementedError):
            self.skipTest("cannot make a hard link here")
        return dst

    def _entries(self) -> list[str]:
        return [rel for rel, _ in core_mod._watched_entries(self.proj)]

    def test_a_second_spelling_of_a_watched_file_is_not_a_second_entry(self):
        self._hard_link("CLAUDE.md", "claude.md")
        self.assertEqual(self.cli("watch", "--add", "claude.md")[0], 0)
        entries = self._entries()
        self.assertIn("CLAUDE.md", entries)
        self.assertNotIn("claude.md", entries)    # the first spelling wins

    def test_the_order_of_the_list_decides_which_spelling_is_kept(self):
        self._hard_link("CLAUDE.md", "claude.md")
        with mock.patch.object(core_mod, "watch_list",
                               lambda: ["claude.md", "CLAUDE.md"]):
            self.assertEqual(self._entries(), ["claude.md"])

    def test_the_snapshot_holds_the_file_once_and_undo_restores_it(self):
        self._hard_link("CLAUDE.md", "claude.md")
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(self.cli("watch", "--add", "claude.md")[0], 0)
        code, out, err = self.cli("snap", "-m", "with the alias")
        self.assertEqual((code, err), (0, ""), out)
        manifest = core_mod._manifest_at("HEAD")
        self.assertIn("CLAUDE.md", manifest["files"])
        self.assertNotIn("claude.md", manifest["files"])
        self.assertEqual([p for p in listed() if p.lower() == "files/claude.md"],
                         ["files/CLAUDE.md"])
        write(os.path.join(self.proj, "CLAUDE.md"), "- clobbered\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"# Project\n\nBe helpful.\n")

    def test_a_hard_link_under_another_name_is_still_its_own_entry(self):
        # only a case twin is the same path; AGENTS.md and CLAUDE.md are two
        # names someone chose to link, and both are watched
        self._hard_link("CLAUDE.md", "AGENTS.md")
        entries = self._entries()
        self.assertIn("CLAUDE.md", entries)
        self.assertIn("AGENTS.md", entries)

    @unittest.skipIf(not hasattr(os, "symlink"), "no symlinks")
    def test_a_symlink_is_its_own_entry_whatever_it_points_at(self):
        link = os.path.join(self.proj, "Claude.md")
        try:
            os.symlink("CLAUDE.md", link)
        except (OSError, NotImplementedError):
            self.skipTest("cannot make a symlink here")
        self.assertEqual(self.cli("watch", "--add", "Claude.md")[0], 0)
        entries = self._entries()
        self.assertIn("CLAUDE.md", entries)
        self.assertIn("Claude.md", entries)

    def test_two_files_that_only_differ_by_case_are_both_watched(self):
        if case_insensitive(self._tmp):
            self.skipTest("one name on this file system")
        write(os.path.join(self.proj, "Notes.md"), "upper\n")
        write(os.path.join(self.proj, "notes.md"), "lower\n")
        self.assertEqual(self.cli("watch", "--add", "Notes.md", "notes.md")[0], 0)
        entries = self._entries()
        self.assertIn("Notes.md", entries)
        self.assertIn("notes.md", entries)
        self.assertEqual(self.cli("init")[0], 0)
        self.assertEqual(sorted(p for p in listed() if p.lower() == "files/notes.md"),
                         ["files/Notes.md", "files/notes.md"])

    def test_a_path_that_does_not_exist_is_not_an_entry(self):
        self.assertEqual(self.cli("watch", "--add", "claude.md")[0], 0)
        if case_insensitive(self._tmp):
            self.skipTest("claude.md exists here: it is CLAUDE.md")
        self.assertNotIn("claude.md", self._entries())


@unittest.skipUnless(case_insensitive(tempfile.gettempdir()),
                     "needs a case-insensitive file system (Windows, macOS; "
                     "on Linux point TMPDIR at e.g. a ciopfs mount)")
class TestWatchingThePathInAnotherCaseOnARealCaseInsensitiveFileSystem(Base):
    """The same, with nothing emulated."""

    def setUp(self):
        super().setUp()
        write(os.path.join(self.proj, ".claude", "Notes.md"), "notes\n")
        self.assertEqual(self.cli("init")[0], 0)

    def test_a_file_listed_in_the_other_case_snaps_and_undoes(self):
        self.assertEqual(self.cli("watch", "--add", "claude.md")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "- edited\n")
        code, out, err = self.cli("snap", "-m", "alias")
        self.assertEqual((code, err), (0, ""), out)
        write(os.path.join(self.proj, "CLAUDE.md"), "- clobbered\n")
        code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"- edited\n")

    def test_the_same_folder_spelled_in_another_case_is_another_project(self):
        # a documented limit (README, "Windows and macOS"): a project is
        # identified by its path as it is spelled
        other = os.path.join(os.path.dirname(self.proj), "PROJ")
        self.assertTrue(os.path.samefile(other, self.proj))   # one folder
        self.assertEqual(len(self.log()), 1)
        self.assertEqual(self.log(root=other), [])

    def test_a_directory_listed_in_the_other_case_snaps_and_undoes(self):
        self.assertEqual(self.cli("watch", "--add", ".CLAUDE")[0], 0)
        write(os.path.join(self.proj, "CLAUDE.md"), "- edited\n")
        code, out, err = self.cli("snap", "-m", "alias")
        self.assertEqual((code, err), (0, ""), out)
        write(os.path.join(self.proj, "CLAUDE.md"), "- clobbered\n")
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(slurp(os.path.join(self.proj, "CLAUDE.md")),
                         b"- edited\n")
