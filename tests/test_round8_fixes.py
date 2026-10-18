"""Round-8 regression tests (the Windows run of the round-7 build).

Temp dirs only, and the same hermetic environment as the earlier rounds
(tests/_isolate.py for the process, Base in the round-5 tests for every
test).
"""
from __future__ import annotations

import contextlib
import errno
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

try:   # pytest imports tests/ as a package, unittest discover as top-level
    from . import _isolate  # noqa: F401  (first: a private temp directory)
except ImportError:
    import _isolate  # noqa: F401

from sessionsteward import core as core_mod

try:
    from .test_round5_fixes import Base, slurp, write
except ImportError:
    from test_round5_fixes import Base, slurp, write

STAGING = ".steward-restore-"
NOT_FOUND = "The system cannot find the path specified"


@contextlib.contextmanager
def windows_path_limit(limit: int):
    """A Windows without long-path support, for the staging folders of a
    restore only: creating a folder or a file whose path is longer than
    `limit` fails the way Windows fails it (FileNotFoundError, WinError 3).
    Nothing else is limited, so the rest of the program behaves as usual."""
    real_mkdir, real_open = os.mkdir, tarfile.bltn_open

    def too_long(path):
        return STAGING in str(path) and len(str(path)) > limit

    def mkdir(path, *args, **kwargs):
        if too_long(path):
            raise FileNotFoundError(3, NOT_FOUND, str(path))
        return real_mkdir(path, *args, **kwargs)

    def opener(name, *args, **kwargs):
        if too_long(name):
            raise FileNotFoundError(3, NOT_FOUND, str(name))
        return real_open(name, *args, **kwargs)

    with mock.patch.object(os, "mkdir", mkdir), \
            mock.patch.object(tarfile, "bltn_open", opener):
        yield


class TestAFolderIsRestoredFromACopyNoDeeperThanItself(Base):
    """MED (the Windows run): `undo` could not bring back a watched folder
    whose path was long - ~/.claude/projects/<long project name>/memory on a
    PC where Claude Code keeps its auto memory. The restore unpacked
    `git archive files/<mirror path>` into a staging folder beside the live
    one, so the long part of the path was there twice and passed the
    260-character limit of a Windows without long-path support: "skipped
    ...: could not extract snapshot" (nothing lost, but nothing restored)."""

    DEEP = os.path.join("notes_kept_in_a_folder_with_a_long_name",
                        "and_another_one_below_that", "memory")

    def setUp(self):
        super().setUp()
        self.live = os.path.join(self.proj, self.DEEP)
        self.parent = os.path.dirname(self.live)
        write(os.path.join(self.live, "MEMORY.md"), "- prefers short answers\n")
        write(os.path.join(self.live, "topics", "style.md"), "plain words\n")
        self.assertEqual(self.cli("watch", "--add", self.DEEP)[0], 0)
        self.assertEqual(self.cli("init")[0], 0)
        # later: one file changed, one more, one gone
        write(os.path.join(self.live, "MEMORY.md"), "- gone\n")
        write(os.path.join(self.live, "extra.md"), "new\n")
        os.remove(os.path.join(self.live, "topics", "style.md"))
        self.assertEqual(self.cli("snap", "-m", "v2")[0], 0)

    def limit(self) -> int:
        # the staging folder is <parent>/<prefix><8 random characters>; the
        # copy may go 40 characters below it, which is far more than the
        # live folder needs (topics/style.md is 16) and far less than the
        # whole mirror path (files/<this folder>/..., 96 here)
        return len(self.parent) + 1 + len(STAGING) + 8 + 40

    def staging_leftovers(self):
        return [n for n in os.listdir(self.parent) if n.startswith(STAGING)]

    def trashed(self):
        return [name for _, _, names in os.walk(core_mod.TRASH_DIR)
                for name in names]

    def test_the_emulated_limit_refuses_the_whole_mirror_path(self):
        # a test that cannot fail proves nothing: the old layout (the whole
        # mirror path below the staging folder) must not fit in the limit
        deep = os.path.join(self.parent, STAGING + "abcdefgh", "files",
                            self.DEEP, "topics")
        with windows_path_limit(self.limit()):
            with self.assertRaises(FileNotFoundError):
                os.makedirs(deep)
            os.makedirs(os.path.join(self.parent, STAGING + "abcdefgh",
                                     "topics"))   # the new layout fits

    def test_undo_restores_the_folder_where_paths_are_limited(self):
        with windows_path_limit(self.limit()):
            code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("skipped", out)
        self.assertEqual(slurp(os.path.join(self.live, "MEMORY.md")),
                         b"- prefers short answers\n")
        self.assertEqual(slurp(os.path.join(self.live, "topics", "style.md")),
                         b"plain words\n")
        self.assertFalse(os.path.exists(os.path.join(self.live, "extra.md")))
        self.assertIn("extra.md", self.trashed())   # moved, never deleted
        self.assertEqual(self.staging_leftovers(), [])

    def test_restore_of_the_older_snapshot_finds_the_folder_unchanged(self):
        # (the folder is the snapshot after the undo, so a second one is a
        # no-op: the staged copy must have been exactly the snapshot)
        with windows_path_limit(self.limit()):
            self.assertEqual(self.cli("undo")[0], 0)
            code, out, err = self.cli("undo")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("skipped", out)
        self.assertEqual(core_mod.current_position(self.log(), self.proj),
                         len(self.log()) - 1)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_folder_keeps_its_permissions(self):
        os.chmod(self.live, 0o700)
        os.chmod(os.path.join(self.live, "MEMORY.md"), 0o600)
        self.assertEqual(self.cli("snap", "-m", "v3")[0], 0)
        write(os.path.join(self.live, "MEMORY.md"), "- changed again\n")
        self.assertEqual(self.cli("snap", "-m", "v4")[0], 0)
        self.assertEqual(self.cli("undo")[0], 0)
        self.assertEqual(os.stat(self.live).st_mode & 0o077, 0)
        self.assertEqual(os.stat(os.path.join(self.live, "MEMORY.md")).st_mode
                         & 0o077, 0)

    def test_a_folder_that_cannot_be_unpacked_is_left_alone_and_says_why(self):
        def broken(archive, prefix, dest):
            write(os.path.join(dest, "MEMORY.md"), "half of it\n")
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(core_mod, "_unpack_folder", broken):
            code, out, err = self.cli("undo")
        self.assertEqual(code, 1, out + err)
        self.assertRegex(out, r"skipped \S*memory: could not extract snapshot "
                              r"[0-9a-f]{7} \(No space left on device\) "
                              r"\(left untouched\)")
        # nothing half-unpacked was swapped in, nothing went to the trash
        self.assertEqual(slurp(os.path.join(self.live, "MEMORY.md")),
                         b"- gone\n")
        self.assertTrue(os.path.exists(os.path.join(self.live, "extra.md")))
        self.assertEqual(self.trashed(), [])
        self.assertEqual(self.staging_leftovers(), [])

    def test_a_snapshot_git_cannot_read_says_so(self):
        real = core_mod._run_git

        def run_git(repo, args, *a, **kw):
            if args and args[0] == "archive":
                return subprocess.CompletedProcess(args, 128, b"", b"fatal")
            return real(repo, args, *a, **kw)

        with mock.patch.object(core_mod, "_run_git", run_git):
            code, out, err = self.cli("undo")
        self.assertEqual(code, 1, out + err)
        self.assertIn("could not extract snapshot", out)
        self.assertIn("(git could not read it) (left untouched)", out)
        self.assertEqual(slurp(os.path.join(self.live, "MEMORY.md")),
                         b"- gone\n")


class TestUnpackFolder(unittest.TestCase):
    """_unpack_folder takes the folder out of a `git archive` stream as the
    destination itself, and nothing else."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="ss8-")
        self.addCleanup(_rmtree, self.dest)

    @staticmethod
    def archive(entries) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w",
                          format=tarfile.PAX_FORMAT) as tf:
            for name, kind, payload in entries:
                info = tarfile.TarInfo(name)
                data = None
                if kind == "dir":
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                elif kind == "file":
                    info.size, info.mode = len(payload), 0o644
                    data = io.BytesIO(payload)
                elif kind == "sym":
                    info.type, info.linkname = tarfile.SYMTYPE, payload
                elif kind == "hard":
                    info.type, info.linkname = tarfile.LNKTYPE, payload
                tf.addfile(info, data)
        return buf.getvalue()

    AROUND = [("files", "dir", None), ("files/a", "dir", None),
              ("files/a/b", "dir", None)]

    def listing(self):
        return sorted(os.path.relpath(os.path.join(root, n), self.dest)
                      .replace(os.sep, "/")
                      for root, dirs, files in os.walk(self.dest)
                      for n in dirs + files)

    def test_the_folder_becomes_the_destination(self):
        data = self.archive(self.AROUND + [
            ("files/a/b/x.md", "file", b"x\n"),
            ("files/a/b/sub", "dir", None),
            ("files/a/b/sub/y.md", "file", b"y\n")])
        core_mod._unpack_folder(data, "files/a/b", self.dest)
        self.assertEqual(self.listing(), ["sub", "sub/y.md", "x.md"])
        self.assertEqual(slurp(os.path.join(self.dest, "sub", "y.md")), b"y\n")

    def test_a_folder_with_a_similar_name_is_not_taken_along(self):
        data = self.archive(self.AROUND + [
            ("files/a/b/x.md", "file", b"x\n"),
            ("files/a/bb", "dir", None),
            ("files/a/bb/z.md", "file", b"z\n"),
            ("files/a/b.md", "file", b"b\n")])
        core_mod._unpack_folder(data, "files/a/b", self.dest)
        self.assertEqual(self.listing(), ["x.md"])

    def test_an_entry_that_climbs_out_is_refused(self):
        for bad in ("files/a/b/../../escape.md", "files/a/b//etc/escape.md",
                    "files/a/b/sub/../../../escape.md"):
            data = self.archive(self.AROUND + [(bad, "file", b"no\n")])
            with self.assertRaises(tarfile.TarError, msg=bad):
                core_mod._unpack_folder(data, "files/a/b", self.dest)
        parent = os.path.dirname(self.dest)
        self.assertFalse(os.path.exists(os.path.join(parent, "escape.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "escape.md")))

    def test_a_hard_link_is_refused(self):
        data = self.archive(self.AROUND + [
            ("files/a/b/x.md", "file", b"x\n"),
            ("files/a/b/copy.md", "hard", "files/a/b/x.md")])
        with self.assertRaises(tarfile.TarError):
            core_mod._unpack_folder(data, "files/a/b", self.dest)

    def test_a_folder_that_is_not_in_the_archive_is_an_error(self):
        data = self.archive(self.AROUND + [("files/a/b/x.md", "file", b"x\n")])
        with self.assertRaises(tarfile.TarError):
            core_mod._unpack_folder(data, "files/a/other", self.dest)
        with self.assertRaises(tarfile.TarError):
            core_mod._unpack_folder(b"", "files/a/b", self.dest)

    @unittest.skipIf(os.name == "nt", "symlinks need a privilege on Windows")
    def test_a_link_stays_a_link(self):
        data = self.archive(self.AROUND + [
            ("files/a/b/x.md", "file", b"x\n"),
            ("files/a/b/ln", "sym", "x.md")])
        core_mod._unpack_folder(data, "files/a/b", self.dest)
        self.assertEqual(os.readlink(os.path.join(self.dest, "ln")), "x.md")


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
