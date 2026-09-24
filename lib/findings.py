#!/usr/bin/env python
"""The shared finding store, pipeline gap P4.

Gates 1 to 6 write findings here and a later gate reads them, which is the
mechanism P2 needs: today Gate 7 can pass a task Gate 6 has already flagged,
because it cannot see Gate 6's output. An override (P12) lands here too, as an
ordinary finding with its own rule id, so a later gate sees that one happened.

The shape is SARIF 2.1.0, an OASIS standard that exists for exactly this
many-tools-one-consumer problem. No dependency is taken: the value is the
field names, not a library. See reference/DESIGN-BASIS.md.

One file per finding, not one appended log. That looks wasteful and is
deliberate. An appended log needs the append to be atomic across processes,
and on Windows the C runtime implements O_APPEND as seek-then-write, which is
not atomic between independently opened descriptors. Making appends safe
therefore means cross-process locking plus recovery of a half-written trailing
record, because a partial line with no newline silently swallows the next
record written after it. One file per finding, published by os.replace, needs
none of that: a torn write is one unreadable file rather than a poisoned
neighbour, and os.replace is atomic on both platforms.

Self-check: `python lib/findings.py` (offline, no key and no network needed).
"""

import glob
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse

FINDINGS_DIR = "~/.jev-gates/findings"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
LEVELS = ("error", "warning", "note", "none")

# Anchored with fullmatch, never match. Python's `$` also matches the position
# before a trailing newline, so `match` accepted "NUL\n" and handed back a
# name this guard exists to reject. An allowlist that accepts a character it
# does not list is not an allowlist.
_SAFE_SESSION = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")

# Windows resolves these stems to devices whatever the extension, so a store
# directory called NUL silently discards every finding written to it and CON
# writes them to a console. An audit store that loses evidence quietly is far
# worse than one with an ugly name, so these are hashed instead.
_WIN_DEVICES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10)))

_seq = 0


def session_slug(session_id):
    """A filename-safe handle for a session id.

    The id arrives in hook JSON and names a directory, so passing it through
    unchecked is a path-traversal primitive. An unsafe id is hashed rather
    than refused: a session with a strange id must still get a store, or the
    gate silently stops recording for it.

    A trailing dot is rejected because Windows strips it, which would quietly
    merge session "abc." into session "abc"'s store.

    Two residual collisions, which are different in kind and are not covered
    by one argument:

    - Deterministic aliases need no attacker at all. Windows filenames are
      case-insensitive, so ids differing only in case share a store, and an id
      of the exact shape this function's own hash emits would share a store
      with the id that hashes to it. Neither is reachable from a lowercase
      UUID, which is what Claude Code supplies, so this is accepted for the
      real caller and is NOT a general guarantee. A caller that mints its own
      session ids must supply distinct ones case-insensitively.
    - A true hash collision in the fallback needs a chosen id. 24 hex
      characters is 96 bits, so roughly 48-bit birthday resistance, which is
      ample against accident and is not a cryptographic commitment.
    """
    s = str(session_id or "")
    if (_SAFE_SESSION.fullmatch(s) and not s.endswith(".")
            and s.split(".")[0].upper() not in _WIN_DEVICES):
        return s
    return "h" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def session_dir(session_id):
    """This session's store directory.

    JEV_FINDINGS_DIR is trusted configuration, read from the process
    environment at launch. It is not hook-controlled: nothing parses it out of
    hook JSON. A symlink pre-positioned inside this directory would redirect a
    write, and that is accepted, because anyone who can write there can
    already run code as this user."""
    root = os.path.expanduser(os.environ.get("JEV_FINDINGS_DIR") or FINDINGS_DIR)
    return os.path.join(root, session_slug(session_id))


def artifact_uri(path):
    """A SARIF `artifactLocation.uri` for a filesystem path.

    SARIF wants a URI reference, and a Windows path is not one. Backslashes
    and spaces survive JSON schema validation untouched and then break in the
    consumer, which is the worst place to discover it, and Windows is this
    project's primary platform.

    A relative path stays relative, because that is what a code-scanning
    consumer wants in order to line a finding up against the repository. An
    absolute one becomes a file: URI, because a bare `C:/x` would otherwise be
    read as scheme `C`. A UNC path keeps its host in the authority, because
    `file:///server/share/x` is valid, resolvable, and names a file on the
    wrong machine, which is worse than being malformed."""
    p = str(path).replace("\\", "/")
    unc = re.match(r"^//([^/]+)/(.*)$", p)
    if unc:
        return ("file://" + urllib.parse.quote(unc.group(1), safe="")
                + "/" + urllib.parse.quote(unc.group(2), safe="/:"))
    if re.match(r"^[A-Za-z]:(?=/)", p) or p.startswith("/"):
        return "file:///" + urllib.parse.quote(p.lstrip("/"), safe="/:")
    return urllib.parse.quote(p, safe="/")


def fingerprint(rule_id, uri, message):
    """A stable handle for "the same finding", SARIF partialFingerprints.

    Deliberately excludes the line number. A fingerprint that changes when an
    unrelated line is inserted above it cannot do the one job it has, which is
    recognising a finding it has already seen.

    The cost is that two genuinely distinct occurrences with the same rule,
    file and message text share a fingerprint, and a consumer that dedupes on
    it will collapse them into one. That is the accepted side of the trade:
    edit-stability is the property this field exists for. No gate emits
    findings yet, so nothing triggers it today.

    ponytail: rule id, path and message only. If a gate ever emits repeated
    identical messages in one file, add a normalised snippet or the enclosing
    symbol as a fourth component rather than adding the line number back."""
    raw = "|".join(str(x or "") for x in (rule_id, uri, message))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def finding(gate, rule_id, message, level="warning", path=None, line=None,
            confidence=None, test_stub=None):
    """One SARIF `result`.

    `confidence` and `test_stub` go in `properties` because SARIF has no
    native slot for either. `test_stub` is the field worth insisting on: a
    finding that arrives with a failing test is verifiable rather than
    assertable, the same discipline Gate 7 already enforces by demanding
    evidence instead of prose.

    There is no `fixes[]` yet. SARIF requires a fix to carry a real edit, and
    no gate produces one. Prose dressed as a fix would be an invalid document
    for no gain."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, got {level!r}")
    uri = artifact_uri(path) if path else None
    res = {
        "ruleId": rule_id,
        "level": level,
        "message": {"text": message},
        "partialFingerprints": {
            "jevFingerprint/v1": fingerprint(rule_id, uri, message)},
        "properties": {"gate": gate,
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
    }
    if uri:
        phys = {"artifactLocation": {"uri": uri}}
        if line:
            phys["region"] = {"startLine": int(line)}
        res["locations"] = [{"physicalLocation": phys}]
    if confidence is not None:
        res["properties"]["confidence"] = float(confidence)
    if test_stub:
        res["properties"]["test_stub"] = test_stub
    return res


def _write_one(d, result):
    """Publish one finding into directory d. True if it reached disk.

    Written to a temporary file and then moved into place, so a reader
    globbing the directory never sees a half-written finding: os.replace is
    atomic on both platforms. Never raises, because a full disk must not
    silence a gate, but the caller is told what happened rather than left to
    assume."""
    global _seq
    try:
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        _seq += 1
        # time_ns first so a plain sort is chronological, pid and counter to
        # keep two concurrent writers from choosing one name.
        name = f"{time.time_ns():019d}-{os.getpid()}-{_seq}.json"
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, os.path.join(d, name))
            return True
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
    except OSError:
        return False


def record(session_id, results):
    """Append results to this session's store.

    Returns how many actually reached disk, which may be fewer than were
    handed in. Returning the input length regardless would mean an unwritable
    directory reports a full success, and a store that lies about what it
    holds is worse than no store."""
    d = session_dir(session_id)
    return sum(1 for r in results if _write_one(d, r))


def read(session_id):
    """Every result recorded for this session, oldest first.

    An unreadable file is skipped rather than fatal. It cannot be a torn
    write, because a finding only appears under its final name once it is
    complete, so a bad file here means real corruption and the remaining
    evidence still matters more than the crash would."""
    out = []
    for p in sorted(glob.glob(os.path.join(session_dir(session_id), "*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def sarif_log(results, tool_name="jev-gates"):
    """Wrap results in one minimal valid SARIF 2.1.0 log.

    Materialised once at the top, because collecting rule ids from a generator
    and then listing it again yields a document declaring every rule and
    carrying no results at all.

    Every ruleId a result cites gets a descriptor, because a result citing a
    rule the driver never declares is what makes a SARIF file fail validation
    in the consumers this shape exists to reach."""
    results = list(results)
    ids = sorted({r.get("ruleId") for r in results if r.get("ruleId")})
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {"driver": {"name": tool_name,
                                "rules": [{"id": i} for i in ids]}},
            "results": results,
        }],
    }


# --- offline self-check -------------------------------------------------

def selfcheck():
    # A session id names a directory, so a traversing id must never reach the
    # filesystem, and a normal one must survive untouched or every gate writes
    # to a different store than it reads.
    assert session_slug("abc-123_ok.9") == "abc-123_ok.9"
    assert session_slug("console") == "console"  # not a device, must survive
    for bad in ("../../etc/passwd", "..", ".hidden", "a/b", "a\\b", "", None,
                "x" * 200, "NUL", "nul", "CON.jsonl", "com1", "LPT9", "abc.",
                "abc\n", "NUL\n", "abc.\n", "a\tb", "a b", "a:b"):
        slug = session_slug(bad)
        assert slug.startswith("h") or slug == bad, (bad, slug)
        assert _SAFE_SESSION.fullmatch(slug), (bad, slug)
        assert slug == os.path.basename(slug) and slug not in (".", ".."), slug
    # The guard must reject these outright, not merely sanitise them, or a
    # device name reaches the filesystem and the store silently holds nothing.
    for bad in ("NUL", "nul", "abc.", "abc\n", "NUL\n", "a/b", "x" * 200):
        assert session_slug(bad).startswith("h"), (bad, session_slug(bad))
    assert session_slug("abc.") != session_slug("abc")
    assert session_slug("a/b") != session_slug("a/c")

    # A Windows path is not a URI reference. The schema accepts it and the
    # consumer then breaks, which is the worst place to find out.
    assert artifact_uri("lib/x.py") == "lib/x.py"
    assert artifact_uri("lib\\sub\\x.py") == "lib/sub/x.py"
    assert artifact_uri("lib/a b.py") == "lib/a%20b.py"
    assert artifact_uri("C:\\work\\a b.py") == "file:///C:/work/a%20b.py"
    assert artifact_uri("/home/u/a b.py") == "file:///home/u/a%20b.py"
    # A UNC path that loses its host still validates and names a file on the
    # wrong machine, which is worse than being malformed.
    assert artifact_uri("\\\\srv\\share\\a b.py") == "file://srv/share/a%20b.py"
    assert artifact_uri("\\\\srv.example.com\\s\\x.py") == (
        "file://srv.example.com/s/x.py")
    for p in ("lib/x.py", "lib\\a b.py", "C:\\work\\x.py", "/tmp/x.py",
              "\\\\srv\\share\\x.py"):
        u = artifact_uri(p)
        assert " " not in u and "\\" not in u, (p, u)

    # A fingerprint must survive a line moving and must not survive the
    # finding moving file.
    assert fingerprint("R", "a.py", "m") == fingerprint("R", "a.py", "m")
    assert fingerprint("R", "a.py", "m") != fingerprint("R", "b.py", "m")
    assert fingerprint("R", "a.py", "m") != fingerprint("S", "a.py", "m")
    # The fingerprint is taken over the URI, not the raw path, or the same
    # file spelled two ways would look like two findings.
    a = finding(6, "R", "m", path="lib\\x.py")
    b = finding(6, "R", "m", path="lib/x.py")
    assert a["partialFingerprints"] == b["partialFingerprints"], (a, b)

    f = finding(6, "unused-import", "os imported but unused", level="note",
                path="lib/x.py", line=12, confidence=0.82,
                test_stub="def test_x(): assert False")
    assert f["ruleId"] == "unused-import" and f["level"] == "note", f
    assert f["message"]["text"] == "os imported but unused", f
    loc = f["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "lib/x.py", f
    assert loc["region"]["startLine"] == 12, f
    assert f["properties"]["confidence"] == 0.82, f
    assert f["properties"]["test_stub"].startswith("def test_x"), f
    assert f["properties"]["gate"] == 6, f
    assert list(f["partialFingerprints"].values())[0], f
    assert "locations" not in finding(6, "R", "no file involved"), "bare"
    try:
        finding(6, "R", "m", level="critical")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown SARIF level must be refused")

    # Write then read, which is the whole point: Gate 7 must be able to see
    # what Gate 6 wrote earlier in the same session.
    root = tempfile.mkdtemp(prefix="jev-findings-")
    os.environ["JEV_FINDINGS_DIR"] = root
    try:
        assert read("s1") == []
        assert record("s1", [f, finding(3, "override", "operator override",
                                        level="none")]) == 2
        assert record("s2", [finding(4, "secret", "key in diff",
                                     level="error")]) == 1
        got = read("s1")
        assert len(got) == 2, got
        assert [r["ruleId"] for r in got] == ["unused-import", "override"], got
        assert got[0] == f, got[0]
        assert [r["ruleId"] for r in read("s2")] == ["secret"], read("s2")
        assert read("../../etc/passwd") == []
        # Sessions must not bleed into each other.
        assert not any(r["ruleId"] == "secret" for r in read("s1")), read("s1")

        # A write that did not happen must never be counted as one.
        assert record("s3", [{"b": object()}, {"b": object()}]) == 0
        assert record("s3", [finding(6, "R", "m"), {"b": object()}]) == 1
        assert len(read("s3")) == 1, read("s3")

        # A corrupt file must cost only itself, never a neighbour. This is
        # the property one-file-per-finding buys over an appended log, where
        # a record with no trailing newline swallows the next one written.
        # Note what this does NOT prove: the file is planted by hand, so it
        # tests the reader, not the writer.
        d = session_dir("s1")
        with open(os.path.join(d, "99999999999999999999-0-0.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"ruleId": "half')
        assert len(read("s1")) == 2, read("s1")

        # A finding that fails to publish must leave nothing visible and must
        # report failure. A writer that wrote straight to the final name
        # would leave a half-published finding here instead.
        #
        # Atomicity itself is a property of os.replace and is not asserted:
        # catching a torn read needs a concurrent reader, and a racing
        # self-check that passes most of the time is worse than none.
        before = sorted(glob.glob(os.path.join(d, "*.json")))
        real_replace = os.replace

        def _boom(src, dst):
            raise OSError("publish failed")

        os.replace = _boom
        try:
            assert record("s1", [finding(6, "R", "will not publish")]) == 0
        finally:
            os.replace = real_replace
        assert sorted(glob.glob(os.path.join(d, "*.json"))) == before, d
        # No temporary file may be left behind, including after that failure.
        assert not glob.glob(os.path.join(d, ".tmp-*")), os.listdir(d)

        doc = sarif_log(got)
        assert doc["version"] == SARIF_VERSION and len(doc["runs"]) == 1, doc
        run = doc["runs"][0]
        declared = {r["id"] for r in run["tool"]["driver"]["rules"]}
        assert declared == {"unused-import", "override"}, declared
        # Equality, not a subset: a subset assertion passes on an empty
        # results array, which is exactly the bug below.
        assert run["results"] == got, run["results"]
        json.dumps(doc)  # must round-trip as JSON, no stray objects

        # A generator must not be consumed while collecting rule ids and then
        # emit nothing. The subset assertion above would have passed.
        gen = sarif_log(finding(6, "R", f"m{i}") for i in range(3))
        assert len(gen["runs"][0]["results"]) == 3, gen["runs"][0]["results"]
    finally:
        del os.environ["JEV_FINDINGS_DIR"]
        for p in glob.glob(os.path.join(root, "*", "*")):
            os.unlink(p)
        for p in glob.glob(os.path.join(root, "*")):
            os.rmdir(p)
        os.rmdir(root)

    print("findings selfcheck: ok")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selfcheck())
