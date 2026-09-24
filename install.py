#!/usr/bin/env python
"""Install or remove jev-seatbelts' 7 gates as global Claude Code hooks.

    python install.py             install (safe to run more than once)
    python install.py --uninstall remove every hook this installer added
    python install.py --dry-run   print what would change, change nothing

Appends to `~/.claude/settings.json` rather than replacing it: every hook,
every setting you already have stays exactly as it is. Detected by the
presence of this repository's own path inside a hook's command string, so
running install twice does not duplicate anything, and uninstall removes
only what install added.

No third-party dependency, no admin rights, no network call. Uses
`sys.executable` for every hook command, so it invokes whichever Python
interpreter is running this installer - not a guess at "python" vs
"python3"."""
import argparse
import json
import os
import sys

SETTINGS = os.path.expanduser("~/.claude/settings.json")
REPO = os.path.dirname(os.path.abspath(__file__))
GATE_ENV = [f"GATE{n}_ENABLED" for n in (1, 2, 3, 4, 6, 7)] + ["DRIFTGUARD_ENABLED"]


def _cmd(rel):
    return f'"{sys.executable}" "{os.path.join(REPO, *rel.split("/"))}"'


def _hook(rel, timeout, status):
    return {"type": "command", "command": _cmd(rel), "timeout": timeout,
            "statusMessage": status}


PRETOOLUSE = [
    {"matcher": "ExitPlanMode", "hooks": [_hook(
        "skills/plan-gate/scripts/plan_gate.py", 30,
        "jev-seatbelts Gate 1: checking the plan")]},
    {"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit", "hooks": [_hook(
        "skills/command-safety/scripts/command_safety.py", 10,
        "jev-seatbelts Gate 3: checking the command")]},
    {"matcher": "Bash", "hooks": [_hook(
        "skills/commit-screening/scripts/commit_screening.py", 10,
        "jev-seatbelts Gate 4: checking the commit")]},
    {"matcher": "Bash", "hooks": [_hook(
        "skills/package-check/scripts/package_check.py", 10,
        "jev-seatbelts Gate 2: checking the package")]},
    {"matcher": "Bash", "hooks": [_hook(
        "skills/code-quality/scripts/code_quality.py", 30,
        "jev-seatbelts Gate 6: measuring code quality")]},
]
STOP = [{"hooks": [_hook(
    "skills/completion-check/scripts/completion_check.py", 30,
    "jev-seatbelts Gate 7: checking the completion claim")]}]
SUBAGENTSTOP = [{"hooks": [_hook(
    "skills/completion-check/scripts/completion_check.py", 30,
    "jev-seatbelts Gate 7: checking the completion claim")]}]
SESSIONSTART = [{"hooks": [_hook(
    "lib/driftcheck_hook.py", 30,
    "jev-seatbelts: checking Jev's own judgment still looks right")]}]

BLOCKS = {"PreToolUse": PRETOOLUSE, "Stop": STOP,
          "SubagentStop": SUBAGENTSTOP, "SessionStart": SESSIONSTART}


def _load():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f)


def _save(settings):
    """Write settings.json owner-only, matching every other file this
    project creates (see lib/jevgate.py's append_jsonl and
    _write_session_state) - this file is not itself a secrets store since
    the Jev key is never written here, but it is a live hook-command list,
    and a fresh install on a machine with a permissive umask should not be
    the one file in this project world-readable by default. A no-op on
    Windows, where chmod bits do not apply the same way; harmless there."""
    d = os.path.dirname(SETTINGS)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    with open(SETTINGS, encoding="utf-8") as f:
        json.load(f)  # re-parse: prove the file we just wrote is valid JSON


def _is_ours(hook_entry):
    return any(REPO in h.get("command", "")
               for h in hook_entry.get("hooks", []))


def installed(settings):
    return any(_is_ours(entry)
               for block in settings.get("hooks", {}).values()
               for entry in block)


def install(dry_run=False):
    settings = _load()
    if installed(settings):
        print("already installed (found a hook pointing at this repo) - "
              "no change made")
        return 0

    added_hooks = 0
    settings.setdefault("hooks", {})
    for event, entries in BLOCKS.items():
        settings["hooks"].setdefault(event, []).extend(entries)
        added_hooks += len(entries)

    settings.setdefault("env", {})
    added_env = [k for k in GATE_ENV if k not in settings["env"]]
    settings["env"].update({k: "1" for k in GATE_ENV})

    if dry_run:
        print(f"would add {added_hooks} hooks and set {len(added_env)} "
              f"env flags in {SETTINGS}")
        return 0

    _save(settings)
    print(f"installed: {added_hooks} hooks across "
          f"{', '.join(BLOCKS)}, {len(added_env)} env flags set in "
          f"{SETTINGS}.")
    print("Restart Claude Code (or start a new session) for the new hooks "
          "and env flags to take effect.")
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("No TYPESAFE_API_KEY found in this shell's environment. Gate "
              "7 and the Jev-judged tiers of Gates 1, 2, 4 and 6 need one - "
              "see README.md, \"Requirements\". Every other gate's fixed "
              "floor still works with no key at all.")
    return 0


def uninstall(dry_run=False):
    settings = _load()
    if not installed(settings):
        print("nothing to remove - no hook here points at this repo")
        return 0

    removed = 0
    for event in list(settings.get("hooks", {})):
        before = settings["hooks"][event]
        after = [e for e in before if not _is_ours(e)]
        removed += len(before) - len(after)
        settings["hooks"][event] = after

    if dry_run:
        print(f"would remove {removed} hooks from {SETTINGS}; "
              "GATE*_ENABLED/DRIFTGUARD_ENABLED flags would be left as-is "
              "in case something else set them")
        return 0

    _save(settings)
    print(f"removed {removed} hooks from {SETTINGS}. "
          "GATE*_ENABLED/DRIFTGUARD_ENABLED were left in place - delete "
          "them by hand if nothing else relies on them.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.uninstall:
        return uninstall(args.dry_run)
    return install(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
