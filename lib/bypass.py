#!/usr/bin/env python
"""P9: a sanctioned, logged, one-time bypass for a gate's DENY
(reference/DESIGN-BASIS.md, "Pipeline-level gaps", P9: "With no logged
one-time bypass, the realistic operator response is to disable the hook
entirely, which yields no gate and no record. A bypass that is logged is
strictly safer than a gate that gets switched off.").

Scope, an explicit operator decision (2026-09-23): a ticket can only ever
lift the one Jev-tier DENY each gate actually wires it to - Gate 3's
Jev-unreachable-fail-closed DENY, Gate 4's Jev-judged-secret DENY - never
a fixed-floor catastrophic or self-protection DENY (Gate 3's
`rm -rf /`, wiping `~/.claude/settings.json`; Gate 4's regex secret-shape
match). This is enforced structurally, not by inspecting the ticket:
`consume()` is called from exactly one place in each gate's `decide()`,
the Jev-tier DENY branch, and from nowhere else - a ticket, however
worded, has no code path that lets it reach a fixed-floor DENY at all.

One ticket per (session, gate), atomic write via `tempfile.mkstemp()` +
`os.replace()` (same pattern P6/P11 already settled on), consumed -
deleted - on first use, so a grant is single-use by construction. Every
consumption writes an audit-trail finding to the shared store
(`lib/findings.py`, rule `gate-bypass-used`) and a `hook_error` log line,
so a bypass is visible to a later gate and to the operator even though
its own verdict renders as an ordinary allow.

    python lib/bypass.py --grant --session <id> --gate <3|4> --reason "..."
    python lib/bypass.py                          # offline self-check
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import findings  # noqa: E402
import jevgate  # noqa: E402

BYPASS_DIR = "~/.jev-gates/session-bypass"

# Same filename-safety rule as jevgate.py's and findings.py's own copies,
# duplicated rather than imported - this store stands on its own, same
# reasoning jevgate.py's own docstring gives for its copy.
_SAFE_SESSION = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
_WIN_DEVICES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10)))


def _session_slug(session_id):
    s = str(session_id or "")
    if (_SAFE_SESSION.fullmatch(s) and not s.endswith(".")
            and s.split(".")[0].upper() not in _WIN_DEVICES):
        return s
    return "h" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def _ticket_path(session_id, gate):
    root = os.path.expanduser(os.environ.get("JEV_BYPASS_DIR") or BYPASS_DIR)
    return os.path.join(root, _session_slug(session_id), f"gate{gate}.json")


def grant(session_id, gate, reason):
    """Write a one-time bypass ticket for (session_id, gate).

    Overwrites any prior unused ticket for the same pair - granting again
    supersedes, not stacks. Refuses a blank reason: an ungrounded bypass
    with nothing recorded about why defeats the one thing that makes a
    bypass safer than disabling the gate - the audit trail."""
    if gate not in (3, 4) or not session_id or not str(reason or "").strip():
        return False
    path = _ticket_path(session_id, gate)
    try:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix="", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"reason": jevgate.redact(str(reason))[:300],
                       "granted_ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def consume(session_id, gate, would_be):
    """If a ticket exists for (session_id, gate), delete it, write the
    audit-trail finding and log line, and return the reason string.
    No ticket, any failure reading one, or a failure to delete it after
    reading, returns None - a broken store must never grant a bypass it
    cannot prove was single-use, and a ticket that survives its own
    consumption is exactly the unbounded-reuse case this function exists
    to prevent, so a delete failure fails closed rather than granting the
    bypass anyway.

    `would_be` is a short, already-composed description of the DENY this
    bypass is lifting (e.g. "jev-unavailable-fail-closed: <rule>: <why>"),
    recorded in the audit finding so the trail says what was allowed
    through, not only that something was."""
    if gate not in (3, 4):
        return None
    path = _ticket_path(session_id, gate)
    try:
        with open(path, encoding="utf-8") as f:
            ticket = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.remove(path)
    except OSError as exc:
        jevgate.hook_error(
            gate, f"bypass ticket could not be deleted, so the bypass was "
                 f"refused rather than granted with a stale ticket still "
                 f"on disk: {exc}")
        return None
    reason = ticket.get("reason") or "(no reason recorded)"
    try:
        findings.record(session_id, [findings.finding(
            gate, "gate-bypass-used",
            f"operator bypass used - reason: {reason} - "
            f"would have been: {would_be}", level="warning")])
    except Exception as exc:  # noqa: BLE001  the trail must not crash the gate it is auditing
        jevgate.hook_error(gate, f"could not write bypass audit finding: "
                                 f"{exc}")
    jevgate.hook_error(gate, f"bypass used, reason: {reason}")
    return reason


# --- CLI -----------------------------------------------------------------

def main(argv):
    if not argv or "--selfcheck" in argv:
        return selfcheck()
    if "--grant" not in argv:
        print("usage: python lib/bypass.py --grant --session <id> "
              "--gate <3|4> --reason \"...\"", file=sys.stderr)
        return 2

    def _opt(name):
        if name in argv:
            i = argv.index(name)
            return argv[i + 1] if i + 1 < len(argv) else None
        return None

    session_id, gate_s, reason = (
        _opt("--session"), _opt("--gate"), _opt("--reason"))
    if not session_id or gate_s not in ("3", "4") or not reason:
        print("--session, --gate (3 or 4), and --reason are all required",
              file=sys.stderr)
        return 2
    ok = grant(session_id, int(gate_s), reason)
    if not ok:
        print("could not write the ticket", file=sys.stderr)
        return 1
    tier = ("jev-unavailable-fail-closed" if gate_s == "3"
            else "jev-judged-secret")
    print(f"granted: one bypass of gate {gate_s}'s {tier} DENY "
          f"for session {session_id!r}, reason: {reason!r}")
    return 0


# --- offline self-check -------------------------------------------------

def selfcheck():
    import tempfile as tmp
    import unittest.mock

    # No ticket: consume() finds nothing, writes nothing.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert consume("p9-selfcheck", 3, "would deny") is None

    # Blank/whitespace reason is refused - no ticket written.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert grant("p9-selfcheck", 3, "") is False
            assert grant("p9-selfcheck", 3, "   ") is False
            assert consume("p9-selfcheck", 3, "would deny") is None

    # Gate is restricted to {3, 4} inside grant()/consume() themselves,
    # not only by their callers - a stray/out-of-range gate value must
    # never reach _ticket_path()'s unvalidated f"gate{gate}.json", which
    # would otherwise let a non-int/path-shaped gate traverse outside the
    # session's own ticket directory (security review, 2026-09-23).
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert grant("p9-selfcheck", 5, "out of range") is False
            assert grant("p9-selfcheck", "../../etc", "path-shaped") is False
            assert consume("p9-selfcheck", 5, "would deny") is None
            assert consume(
                "p9-selfcheck", "../../etc", "would deny") is None

    # Grant, then consume: reason comes back, secret-shaped text in it is
    # redacted the same way every other gate's log line already is.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(
                os.environ, {"JEV_BYPASS_DIR": td,
                            "JEV_FINDINGS_DIR": os.path.join(td, "f")}):
            assert grant("p9-selfcheck", 3,
                        "testing, token tsk-p9-selfcheck-secret") is True
            reason = consume("p9-selfcheck", 3, "would deny on rule x")
    assert reason is not None
    assert "tsk-p9-selfcheck-secret" not in reason, reason
    assert "<redacted>" in reason, reason

    # Single-use: the same ticket cannot be consumed a second time.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert grant("p9-selfcheck", 3, "one-time only") is True
            assert consume("p9-selfcheck", 3, "would deny") is not None
            assert consume("p9-selfcheck", 3, "would deny") is None

    # Fails closed, not open: if the ticket file cannot be deleted after
    # being read, consume() must refuse the bypass rather than granting
    # it with a stale ticket left on disk (review finding, 2026-09-23) -
    # otherwise a delete failure would let the same ticket keep granting
    # bypasses indefinitely, defeating single-use by construction.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert grant("p9-selfcheck", 3, "delete will fail") is True
            with unittest.mock.patch.object(
                    os, "remove", side_effect=OSError("locked")):
                assert consume("p9-selfcheck", 3, "would deny") is None
            # The ticket is still there (delete never succeeded) and a
            # real consume() on it - now unmocked - still works once.
            assert consume("p9-selfcheck", 3, "would deny") is not None

    # Gate-scoped: a gate 3 ticket does not consume against gate 4.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert grant("p9-selfcheck", 3, "gate 3 only") is True
            assert consume("p9-selfcheck", 4, "would deny") is None
            assert consume("p9-selfcheck", 3, "would deny") is not None

    # Session-scoped: two sessions never share a ticket.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            assert grant("p9-s1", 3, "s1 only") is True
            assert consume("p9-s2", 3, "would deny") is None
            assert consume("p9-s1", 3, "would deny") is not None

    # Consuming writes an audit-trail finding a later gate can read.
    with tmp.TemporaryDirectory() as td:
        with unittest.mock.patch.dict(
                os.environ, {"JEV_BYPASS_DIR": td,
                            "JEV_FINDINGS_DIR": os.path.join(td, "f")}):
            assert grant("p9-audit", 3, "audit trail check") is True
            consume("p9-audit", 3, "would deny on rule y")
            rows = findings.read("p9-audit")
    assert any(r.get("ruleId") == "gate-bypass-used" for r in rows), rows

    # CLI: --grant with all fields present writes a ticket that
    # consume() can then find; a blank --reason is refused.
    with tmp.TemporaryDirectory() as td:
        import io
        with unittest.mock.patch.dict(os.environ, {"JEV_BYPASS_DIR": td}):
            out = io.StringIO()
            with unittest.mock.patch.object(sys, "stdout", out):
                r = main(["--grant", "--session", "p9-cli", "--gate", "3",
                          "--reason", "from the CLI"])
            assert r == 0, r
            assert consume("p9-cli", 3, "would deny") == "from the CLI"

            err = io.StringIO()
            with unittest.mock.patch.object(sys, "stderr", err):
                r = main(["--grant", "--session", "p9-cli2", "--gate", "3",
                          "--reason", ""])
            assert r == 2, r

    print("bypass selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
