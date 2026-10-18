"""Test isolation: one private directory for the whole test process.

Import this module FIRST in every test file. Everything the tests create
(project folders, fake homes, snapshot repos) goes under a single temporary
directory that is removed when the test process ends, instead of piling up
in the shared temp directory, and nothing reads the developer's own home
directory or git configuration: HOME and USERPROFILE, the XDG config
location and git's global configuration (an empty FILE, never the null
device) all point inside it, git's system configuration is switched off, and
no GIT_* variable that could redirect git survives. PYTHONUNBUFFERED is
dropped too: it hides the buffered-output failures that the tests with real
child processes exist to catch.

Where the temp directory lives is still up to TMPDIR (TEMP / TMP on
Windows): point it at a case-insensitive mount and the tests that need one
run there.
"""
import atexit
import os
import shutil
import stat
import sys
import tempfile


def _rmtree(path: str) -> None:
    """shutil.rmtree that copes with the read-only files git leaves behind
    (Windows) and with folders the tests made unreadable on purpose."""
    def _writable(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p) or ".", stat.S_IRWXU)
            os.chmod(p, stat.S_IRWXU)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable)
    else:
        shutil.rmtree(path, onerror=_writable)


BASE = tempfile.mkdtemp(prefix="ss-test-")
tempfile.tempdir = BASE
for _var in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_var] = BASE
atexit.register(_rmtree, BASE)

for _name in [n for n in os.environ if n.upper().startswith("GIT_")]:
    del os.environ[_name]
os.environ.pop("PYTHONUNBUFFERED", None)

TEST_HOME = os.path.join(BASE, "home")
os.makedirs(TEST_HOME)
_GITCONFIG = os.path.join(BASE, "gitconfig")
with open(_GITCONFIG, "w", encoding="utf-8") as _f:
    _f.write("")
os.environ.update({
    "HOME": TEST_HOME,
    "USERPROFILE": TEST_HOME,
    "XDG_CONFIG_HOME": os.path.join(BASE, "xdg"),
    "GIT_CONFIG_GLOBAL": _GITCONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
})
os.environ.pop("CLAUDE_CONFIG_DIR", None)
