"""`sessionsteward doctor` and `sessionsteward config show`.

doctor looks at the things that fail quietly: git missing, hooks installed
but pointing at a command that moved, a lock wait longer than the hook is
allowed to run, hooks that skipped work (hook.log). config show prints every
setting that matters with where its value comes from.
"""

from __future__ import annotations

import os
import shutil

from . import core, guardhook

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _hook_findings(settings_path: str) -> list[tuple[str, str]]:
    out = []
    try:
        with open(settings_path, "rb") as f:
            settings = core._parse_settings(f.read(), settings_path)
    except FileNotFoundError:
        return [(WARN, f"{settings_path} does not exist: no hooks are "
                       f"installed (`sessionsteward install-hooks --write`)")]
    except (OSError, core.SettingsError) as e:
        return [(FAIL, f"cannot read {settings_path}: {e}")]
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    expected = {}
    for event, _matcher, args, cmd in core.HOOK_SPECS:
        expected.setdefault((event, tuple(args)), cmd)
    seen = set()
    for event, _matcher, args, cmd in core.HOOK_SPECS:
        key = (event, tuple(args))
        if key in seen:
            continue
        seen.add(key)
        entries = hooks.get(event)
        mine = [c for c in core._hook_commands(entries if isinstance(entries, list) else [])
                if core._is_ours(event, c, args)]
        label = f"{event} {' '.join(args)}"
        if not mine:
            out.append((WARN, f"hook not installed: {label}"))
            continue
        try:
            words = core.split_command(mine[0])
        except ValueError:
            out.append((FAIL, f"hook {label}: the command cannot be read"))
            continue
        exe = words[0] if words else ""
        want = core.split_command(cmd)[0]
        found = exe if os.path.isabs(exe) and os.path.exists(exe) else shutil.which(exe)
        if not found:
            out.append((FAIL, f"hook {label} runs {exe}, which does not exist "
                              f"any more (`sessionsteward install-hooks --write`)"))
        elif os.path.normcase(os.path.realpath(found)) != os.path.normcase(
                os.path.realpath(want)):
            out.append((WARN, f"hook {label} runs {exe}, but this install is "
                              f"{want}: it may be an older or other copy "
                              f"(`install-hooks --write` points it here)"))
        else:
            out.append((OK, f"hook {label} points at this install"))
    return out


def run_doctor(settings_path: str) -> list[tuple[str, str]]:
    """[(level, message)]. FAIL means something does not work."""
    out: list[tuple[str, str]] = []
    try:
        core._run_git(core.REPO_DIR, ("--version",))
        out.append((OK, "git runs"))
    except core.GitError as e:
        out.append((FAIL, str(e)))
    if core.repo_ready():
        snaps = core.log(100000)
        out.append((OK, f"snapshots repository: {len(snaps)} snapshot(s)"))
        recorded = core.ledger()
        old = [s for s in snaps if s["sha"] not in recorded]
        if old:
            out.append((WARN, f"{len(old)} snapshot(s) have no manifest "
                              f"checksum recorded (taken by an older version "
                              f"or before a prune): they are restored "
                              f"without that check"))
        else:
            out.append((OK, "every snapshot has its manifest checksum"))
    else:
        out.append((WARN, "no snapshots yet (`sessionsteward init`)"))
    out.extend(_hook_findings(settings_path))
    if core.HOOK_LOCK_WAIT >= core.HOOK_TIMEOUT_SECONDS:
        out.append((FAIL, "the lock wait in a hook is not shorter than the "
                          "hook timeout"))
    else:
        out.append((OK, f"a hook waits at most {core.HOOK_LOCK_WAIT:g}s for "
                        f"the lock; Claude Code stops it after "
                        f"{core.HOOK_TIMEOUT_SECONDS}s"))
    try:
        with open(core.hook_log_path(), encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        lines = []
    if lines:
        out.append((WARN, f"hook.log has {len(lines)} line(s); the last: "
                          f"{lines[-1]} ({core.hook_log_path()})"))
    return out


def render_doctor(findings: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{lvl:>4}] {msg}" for lvl, msg in findings) + "\n"


def config_show() -> list[tuple[str, str, str]]:
    """[(setting, value, where it comes from)]."""
    cfg = core.load_config()
    rows = []

    def env(name):
        v = os.environ.get(name, "").strip()
        return v or None

    rows.append(("watch", ", ".join(core.watch_list(cfg)),
                 "config.json (+ built-in defaults)"
                 if cfg.get("watch") or cfg.get("unwatch")
                 else "built-in defaults"))
    percent, mode = guardhook.settings()
    rows.append(("guard.percent", str(percent),
                 "environment" if env("SESSIONSTEWARD_SHRINK_PERCENT")
                 else "config.json" if isinstance(cfg.get("guard"), dict)
                 and "percent" in cfg["guard"] else "default"))
    eff, _note = guardhook.effective_mode()
    rows.append(("guard.mode", mode if eff == mode else f"{mode} (run as {eff})",
                 {"env": "environment", "config": "config.json",
                  "default": "default"}[guardhook.mode_source()]))
    scan = env("SESSIONSTEWARD_SECRET_SCAN")
    rows.append(("secret_scan", "off" if (scan and scan.lower() in ("0", "off", "false", "no"))
                 or (scan is None and cfg.get("secret_scan") is False) else "on",
                 "environment" if scan else "config.json" if "secret_scan" in cfg
                 else "default"))
    ret = cfg.get("retention")
    rows.append(("retention", (f"keep {ret.get('keep')}, days {ret.get('days')}"
                               if isinstance(ret, dict) and ret
                               else f"keep {core.AUTO_KEEP}, days {core.AUTO_DAYS}"),
                 "config.json (prune --save)" if isinstance(ret, dict) and ret
                 else "default (auto-prune)"))
    auto = env("SESSIONSTEWARD_AUTO_PRUNE")
    rows.append(("auto_prune", "on" if core._auto_prune_on() else "off",
                 "environment" if auto else "config.json" if "auto_prune" in cfg
                 else "default"))
    mb = env("SESSIONSTEWARD_MAX_FILE_MB")
    lim = core.max_file_bytes()
    rows.append(("max file size", "no limit" if lim is None
                 else f"{lim / 1048576:g} MB",
                 "environment" if mb else "default"))
    lt = env("SESSIONSTEWARD_LOCK_TIMEOUT")
    rows.append(("lock wait", f"{core._lock_timeout():g}s "
                 f"(hooks: at most {core.HOOK_LOCK_WAIT:g}s)",
                 "environment" if lt else "default"))
    rows.append(("follow external links", "yes" if core.follow_external_links() else "no",
                 "environment/config" if env("SESSIONSTEWARD_FOLLOW_LINKS")
                 or "follow_links" in cfg else "default"))
    return rows


def render_config(rows) -> str:
    width = max(len(r[0]) for r in rows)
    return "\n".join(f"{name:<{width}}  {value}  [{src}]"
                     for name, value, src in rows) + "\n"
