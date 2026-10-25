"""Claude Code hooks that watch your instruction files while you work.

`sessionsteward guard hook` is the one command behind three events. Which
event sends what, and what each can and cannot do (checked against
https://code.claude.com/docs/en/hooks, October 2026, never run by a real
Claude Code):

  PreToolUse (Write | Edit | MultiEdit)
      Runs BEFORE the edit. Works out how big the watched file would be
      afterwards. If that is more than N% smaller (default 50) it takes a
      snapshot first, then asks you ("ask", the default), tells Claude
      ("warn": the edit goes ahead) or refuses ("block"). This is the only
      moment a shrink can be stopped. It sees edits made with the Write and
      Edit tools. It does NOT see `sed -i`, `> file`, an editor, or any
      other process. In a non-interactive `claude -p` run nobody can answer
      "ask", and Claude Code then refuses the edit.
  FileChanged (CLAUDE.md, CLAUDE.local.md, AGENTS.md)
      Runs AFTER any change to those files in the project folder, whoever
      made it (a Bash command included). It compares the file with the
      biggest it was in your last snapshots and shows a warning to YOU. It
      cannot block or undo anything, and what it prints is not sent to
      Claude. It does not watch .claude/, other watched paths or files
      outside the project folder.
  SessionStart (compact)  ->  `sessionsteward remind`, not this command
      Runs when a session continues after a compaction. Its output is added
      to Claude's context (the docs say so for SessionStart; PostCompact
      output, by contrast, is discarded). It reminds Claude that the files
      are read from disk and tells it of any watched file that is much
      smaller than in your last snapshot. Whether Claude acts on that is up
      to Claude.

Common to all: a file under 200 bytes is ignored (a shrink of a stub says
nothing); the hook never fails an edit because sessionsteward itself broke
(it answers nothing and the edit goes ahead).
"""

from __future__ import annotations

import json
import os
import sys

from . import core

DEFAULT_PERCENT = 50
MODES = ("ask", "warn", "block")
DEFAULT_MODE = "ask"
#: A file smaller than this is never reported.
MIN_BYTES = 200
#: How many of the newest snapshots set "how big the file used to be".
BASELINE_SNAPSHOTS = 5
#: The most hook input read (a Write carries the whole new file).
MAX_INPUT = 8 * 1024 * 1024
EDIT_TOOLS = ("Write", "Edit", "MultiEdit")
#: File names the FileChanged hook is installed for (a literal list: Claude
#: Code watches exactly these names in the project folder).
FILE_CHANGED_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")


# -- settings --------------------------------------------------------------

def settings() -> tuple[int, str]:
    """(percent, mode): the environment (SESSIONSTEWARD_SHRINK_PERCENT,
    SESSIONSTEWARD_GUARD_MODE), then the config file's "guard" section, then
    the defaults. A value that is not usable is ignored."""
    try:
        section = core.load_config().get("guard")
    except core.StewardError:
        section = None
    section = section if isinstance(section, dict) else {}
    percent = _percent(os.environ.get("SESSIONSTEWARD_SHRINK_PERCENT"))
    if percent is None:
        percent = _percent(section.get("percent"))
    mode = os.environ.get("SESSIONSTEWARD_GUARD_MODE") or section.get("mode")
    if mode not in MODES:
        mode = DEFAULT_MODE
    return (DEFAULT_PERCENT if percent is None else percent), mode


def _percent(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not 1 <= number <= 99:
        return None
    return int(number) if number == int(number) else None


def set_settings(percent: int | None = None, mode: str | None = None) -> dict:
    """Save the guard settings in the config file; returns the section."""
    cfg = core.load_config()
    section = cfg.get("guard") if isinstance(cfg.get("guard"), dict) else {}
    if percent is not None:
        if _percent(percent) is None:
            raise core.StewardError("--percent must be a whole number from "
                                    "1 to 99")
        section["percent"] = int(percent)
    if mode is not None:
        if mode not in MODES:
            raise core.StewardError(f"--mode must be one of "
                                    f"{', '.join(MODES)}")
        section["mode"] = mode
    cfg["guard"] = section
    core.init_repo()
    core.save_config(cfg)
    return section


# -- what a file used to be, and is --------------------------------------------

def _is_watched_file(path: str, root: str) -> bool:
    """Is `path` one of the watched files, or a file inside a watched
    folder (ignored names such as logs excepted)?"""
    target = os.path.normcase(os.path.abspath(path))
    for ap in core.watched_paths(root):
        base = os.path.normcase(os.path.abspath(ap))
        if target == base:
            return True
        if os.path.isdir(ap) and target.startswith(base.rstrip(os.sep)
                                                    + os.sep):
            return not core._ignored_name(os.path.basename(target))
    return False


def resulting_size(tool: str, tool_input: dict, current: bytes) -> int | None:
    """How many bytes the file will have after the tool runs, or None when
    that cannot be told (the tool will fail by itself, or the input is not
    in the shape the docs describe)."""
    if tool == "Write":
        content = tool_input.get("content")
        return len(content.encode("utf-8")) if isinstance(content, str) \
            else None
    if tool == "Edit":
        edits = [tool_input]
    elif tool == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
    else:
        return None
    text = current.decode("utf-8", "replace")
    for e in edits:
        if not isinstance(e, dict):
            return None
        old, new = e.get("old_string"), e.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return None
        if old not in text:
            return None
        text = (text.replace(old, new) if e.get("replace_all")
                else text.replace(old, new, 1))
    return len(text.encode("utf-8"))


def _baseline(root: str) -> dict[str, tuple[int, str]]:
    """{abspath: (biggest size, snapshot id)} over this project's newest
    snapshots (BASELINE_SNAPSHOTS of them)."""
    best: dict[str, tuple[int, str]] = {}
    for snap in core.log(BASELINE_SNAPSHOTS, project_root=root):
        sha = snap["sha"]
        files = core._manifest_at(sha).get("files", {})
        for rel, size in core._sizes(sha).items():
            ap = files.get(rel)
            if ap is None:
                for key, base in files.items():
                    if rel.startswith(key + "/"):
                        ap = os.path.join(base, *rel[len(key) + 1:].split("/"))
                        break
            if ap is None:
                continue
            ap = os.path.normcase(os.path.abspath(ap))
            if ap not in best or size > best[ap][0]:
                best[ap] = (size, sha)
    return best


def shrunk_files(root: str, percent: int,
                 only: str | None = None) -> list[tuple]:
    """Watched files that are now more than `percent` smaller than in the
    newest snapshots: [(path, old size, new size or None, snapshot id)].
    `only`, when given, restricts the check to that one file."""
    if not core.repo_ready():
        return []
    base = _baseline(root)
    if only is not None:
        paths = [os.path.abspath(only)]
    else:
        paths = list(core.iter_files(core.watched_paths(root)))
    hits = []
    for path in paths:
        old = base.get(os.path.normcase(os.path.abspath(path)))
        if old is None or old[0] < MIN_BYTES:
            continue
        try:
            now = os.path.getsize(path) if os.path.isfile(path) else None
        except OSError:
            now = None
        if now is None or now < old[0] * (1 - percent / 100):
            hits.append((path, old[0], now, old[1]))
    return hits


def _name(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


def _change(old: int, new: int) -> str:
    return f"from {old:,} to {new:,} bytes"


def _pct(old: int, new: int) -> str:
    return f"{100 - round(new * 100 / old)}% smaller"


# -- the three answers -----------------------------------------------------------

def before_edit(payload: dict) -> dict | None:
    """The PreToolUse answer, or None to let the edit go ahead unremarked."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool not in EDIT_TOOLS or not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    root = payload.get("cwd") if isinstance(payload.get("cwd"), str) \
        else os.getcwd()
    path = os.path.join(root, path)   # absolute paths stay as they are
    if not os.path.isfile(path) or not _is_watched_file(path, root):
        return None
    try:
        with open(path, "rb") as f:
            current = f.read(MAX_INPUT)
    except OSError:
        return None
    if len(current) < MIN_BYTES:
        return None
    new = resulting_size(tool, tool_input, current)
    percent, mode = settings()
    if new is None or new >= len(current) * (1 - percent / 100):
        return None
    name = _name(path, root)
    what = (f"this change would shrink {name} "
            f"{_change(len(current), new)} ({_pct(len(current), new)}; the "
            f"limit is {percent}%)")
    backup = ""
    if mode != "block":
        try:
            sha = core.snap(root, f"before {tool} shrank {name}")
            backup = (f" A snapshot of the current version was taken first "
                      f"({sha[:7]}): `sessionsteward restore {sha[:7]}` "
                      f"brings it back.")
        except (core.StewardError, OSError):
            backup = (" No snapshot could be taken just now, so copy the "
                      "file before going ahead.")
    if mode == "block":
        return _pretool("deny", f"sessionsteward: {what}. Nothing was "
                        f"changed. Tell the user what you wanted to cut and "
                        f"why, and make the change in smaller steps only if "
                        f"they agree.")
    if mode == "warn":
        return _pretool(None, "", f"sessionsteward: {what}.{backup} Check "
                        f"that nothing the user relies on is being lost.")
    return _pretool("ask", f"sessionsteward: {what}.{backup}")


def _pretool(decision, reason, context=None) -> dict:
    out: dict = {"hookEventName": "PreToolUse"}
    if decision:
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = reason
    if context:
        out["additionalContext"] = context
    return {"hookSpecificOutput": out}


def after_change(payload: dict) -> dict | None:
    """The FileChanged answer: a warning for the person at the keyboard."""
    path = payload.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    root = payload.get("cwd") if isinstance(payload.get("cwd"), str) \
        else os.getcwd()
    percent, _mode = settings()
    hits = shrunk_files(root, percent, only=path)
    if not hits:
        return None
    path, old, new, sha = hits[0]
    name = _name(path, root)
    if new is None:
        what = f"{name} is gone (it was {old:,} bytes in snapshot {sha[:7]})"
    else:
        what = (f"{name} shrank {_change(old, new)} ({_pct(old, new)}) "
                f"since snapshot {sha[:7]}")
    return {"systemMessage": f"sessionsteward: {what}. "
            f"`sessionsteward restore {sha[:7]}` brings the old version back."}


def reminder(root: str) -> dict | None:
    """The text for SessionStart(compact): see the module docstring."""
    paths = core.watched_paths(root)
    if not paths:
        return None
    names = [_name(p, root) for p in paths][:6]
    more = len(paths) - len(names)
    listing = ", ".join(names) + (f" and {more} more" if more > 0 else "")
    text = (f"sessionsteward: this conversation was just compacted. Your "
            f"instruction files ({listing}) are read from disk, not from "
            f"the summary: if you are unsure a rule survived, read the file "
            f"again before you go on.")
    if core.repo_ready():
        snaps = core.log(1, project_root=root)
        if snaps:
            text += (f" The latest snapshot is {snaps[0]['sha'][:7]} "
                     f"({snaps[0]['date'][:16]}).")
        percent, _mode = settings()
        hits = shrunk_files(root, percent)[:3]
        if hits:
            parts = [f"{_name(p, root)} "
                     f"({'deleted' if n is None else _change(o, n) + ', ' + _pct(o, n)}; "
                     f"snapshot {s[:7]})" for p, o, n, s in hits]
            text += (" These files are much smaller than in your recent "
                     "snapshots: " + "; ".join(parts) + ". Tell the user, "
                     "and do not shorten them further without asking.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": text}}


# -- the hook command -------------------------------------------------------------

def read_payload(stream) -> dict:
    try:
        raw = stream.read(MAX_INPUT + 1)
    except (OSError, ValueError):
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw or len(raw) > MAX_INPUT:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def run_hook(stdin=None, stdout=None) -> int:
    """`sessionsteward guard hook`: read the event on stdin, answer on
    stdout. Always exit 0; any trouble of our own means no answer."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    payload = read_payload(stdin)
    event = payload.get("hook_event_name")
    try:
        if event == "PreToolUse":
            answer = before_edit(payload)
        elif event == "FileChanged":
            answer = after_change(payload)
        else:
            answer = None
    except Exception:   # noqa: BLE001 - a broken guard must not break edits
        answer = None
    if answer:
        stdout.write(json.dumps(answer) + "\n")
    return 0


def run_remind(root: str, stdout=None) -> int:
    """`sessionsteward remind`: print the SessionStart(compact) answer."""
    stdout = stdout or sys.stdout
    try:
        answer = reminder(root)
    except Exception:   # noqa: BLE001
        answer = None
    if answer:
        stdout.write(json.dumps(answer) + "\n")
    return 0
