#!/usr/bin/env python
"""Shared plumbing for every Jev gate.

Nothing in here is specific to one gate. A gate module imports this, then
supplies only its own trigger, its own evidence, and its own questions.

Extracted from gate 7 when it was the only gate, on purpose: the same key
lookup, transcript parsing, threshold parsing, API call and logging are
needed by gates 3 to 6, and copying them would turn every future fix into
six edits.

Gates import it with `bootstrap()`:

    import os, sys
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 3, "lib"))
    import jevgate

Self-check: `python lib/jevgate.py --selfcheck` (offline, no key needed).
"""
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
# Pinned, not the `jev-latest` alias. The vendor states that an alias moves
# when a new release ships, and that a caller holding thresholds tuned
# against a version should pin that version and move on its own schedule.
# Every gate threshold here is calibrated, so the alias would silently
# invalidate the calibration. Bump this deliberately, re-run every gate
# eval against its baseline, and diff the vendor's jaggedness page for the
# new version before keeping the bump.
MODEL = "jev-1.13.0"
KEY_VARS = ("TYPESAFE_API_KEY",)
# urllib's default User-Agent is "Python-urllib/3.x", which Cloudflare's
# bot protection in front of the API started blocking with a 403 and
# response body "error code: 1010" (2026-09-23, this session) - before the
# request ever reaches Jev's own auth check. Confirmed by direct
# reproduction: the exact same request, key, and body succeeds with any
# self-identifying User-Agent and fails with none. Not a key problem, not
# an account problem, and not worth retrying (403 is correctly excluded
# from RETRY_STATUS below) - a WAF signature block does not change on
# retry.
USER_AGENT = "jev-seatbelts-gates/1.0"

# One attempt's socket timeout, and the retry schedule for 429/529. A Claude
# Code hook is given ~30 s total (unconfirmed for PreToolUse specifically;
# carried from Gate 7's own figure for Stop, see reference/GSTACK-REVIEW.md).
# The worst case here (8 + 1 + 8 + 2 + 8 = 27 s) does NOT leave comfortable
# room for the rest of a gate on top of it - a claim this comment used to
# make without the arithmetic behind it. A caller with any real work before
# the call (transcript parsing, git subprocess calls, evidence gathering)
# needs to check its own remaining budget against CALL_WORST_MS below
# before starting the call, not merely confirm its own budget has not yet
# fully expired. Two gates independently found this gap one round apart:
# Gate 3's check was a no-op (created and checked with zero elapsed time
# between them), Gate 7's checked "expired" rather than "enough headroom
# for the worst case" - different bugs, same missing property. Computed
# here once so both gates check the same real number rather than each
# guessing or duplicating the arithmetic.
TIMEOUT = 8
RETRY_SLEEPS = (1.0, 2.0)
RETRY_STATUS = (429, 529, 500, 502, 503, 504)
CALL_WORST_MS = int((TIMEOUT * (len(RETRY_SLEEPS) + 1)
                     + sum(RETRY_SLEEPS)) * 1000)
CALL_MARGIN_MS = 1000

TAIL_BYTES = 512 * 1024
N_CONTEXT = 3
MAXLEN = 2000
MAX_TOOLS = 40
# One tool line's budget, split across both ends of the command. The same
# 200 characters as before, so no more of the operator's shell history
# leaves the machine than already did.
TOOL_LINE_MAX = 200
TOOL_LINE_HEAD = 110
TOOL_LINE_TAIL = 83
# Spent on naming what the dropped middle invoked. Widens a truncated line
# beyond TOOL_LINE_MAX on purpose: a shorter line that hides the one command
# a gate is being asked about is not cheaper, it is wrong.
TOOL_LINE_MID = 90
# How far a cut may travel to avoid splitting a token. Past this the text is
# one long unbroken run and there is no boundary worth reaching for.
TOOL_LINE_SNAP = 40
# How far the head cut may travel back to reach the start of the chained
# segment it landed inside. Larger than TOOL_LINE_SNAP because a whole
# command is longer than one token, and keeping the command intact is the
# entire point of the middle summary.
TOOL_LINE_SEG = 80

# Directories a transcript may legitimately come from. Claude Code writes
# them under ~/.claude/projects; eval harnesses write to the system temp
# directory. Anything else is refused. See safe_transcript().
TRANSCRIPT_ROOTS = tuple(
    os.path.realpath(p) for p in (
        os.path.expanduser("~/.claude/projects"),
        tempfile.gettempdir(),
    )
)

# User-role transcript lines the harness injected rather than the human
# typing them. Treating these as human input silently shifts what counts as
# "this turn", so the list matters more than it looks.
NOT_HUMAN = (
    "Another Claude session sent", "The coordinator sent a message",
    "[SYSTEM NOTIFICATION", "[Request interrupted", "[Image:",
    "Stop hook feedback:", "Caveat: The messages below", "API Error",
)


def bootstrap(script_file, levels=3):
    """Put lib/ on sys.path from inside a gate script. Returns the repo root."""
    root = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(script_file)), *[os.pardir] * levels))
    lib = os.path.join(root, "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    return root


# --- config -------------------------------------------------------------

def api_key():
    """The Jev key, or None. Never logged, never passed on a command line."""
    for var in KEY_VARS:
        if os.environ.get(var):
            return os.environ[var]
    if sys.platform != "win32":
        return None
    # The operator keeps the key as a Windows *User* variable, which is not
    # inherited by every shell that launches a hook. Read the registry
    # directly rather than shelling out to PowerShell.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for var in KEY_VARS:
                try:
                    return winreg.QueryValueEx(k, var)[0]
                except OSError:
                    continue
    except Exception:  # noqa: BLE001  a missing key is not an error here
        pass
    return None


# A rule's evidence class decides the strongest action it may take.
#
# "observed": the evidence is a tool result or a working-tree fact. The agent
#   being judged did not write it, so the rule may block.
# "stated":   the evidence is text the agent wrote. The vendor is explicit
#   that Jev does not treat state as hostile, and that text arguing for its
#   own classification can move the answer. A rule scored over words the
#   subject chose may warn, and may never block.
#
# An untagged rule is treated as "stated", because that is the safe default:
# a mis-tagged rule then under-fires rather than blocking on the agent's own
# prose.
OBSERVED = "observed"
STATED = "stated"
_RULE_RE = re.compile(r"^- (?:\[(observed|stated)\]\s*)?(.+)$")


# An evidence collector's own tri-state, a different axis from OBSERVED and
# STATED above (those classify a *rule*; this classifies a *reading*).
#
# Nine of thirteen defects found across review rounds four and five were one
# class: an unmeasured, denied, or truncated read rendered as a value that
# reads exactly like a confident "no problem here" -- `git status` timing out
# and turning into `""`, which looks identical to a clean working tree. A
# collector must never coerce "I could not tell" into a value indistinguishable
# from "I checked and it is fine".
READ_OK = "observed"      # the read completed; value is the real answer
READ_NA = "unmeasured"    # nothing to read (e.g. cwd is not a git repo) --
                           # not a failure, just no fact to report
READ_ERR = "error"        # the read itself failed: timeout, denied, missing
                           # tool. Never fold this into an empty/false value.
_READ_STATES = (READ_OK, READ_NA, READ_ERR)


def reading(state, value=None, detail=""):
    """One evidence collector's answer, honest about what it actually knows.

    `state` is READ_OK (value is the real fact), READ_NA (there was nothing
    to measure) or READ_ERR (the measurement attempt failed). Render with
    `render_reading` rather than reading `value` directly, so READ_NA and
    READ_ERR can never be mistaken for a checked, empty, or false result."""
    assert state in _READ_STATES, state
    return {"state": state, "value": value, "detail": detail}


def render_reading(r):
    """The text a rule-scoring call should see for one reading.

    An observed value is passed through as-is (an empty string here is a
    real fact: the check ran and found nothing). unmeasured/error are
    rendered as a tagged sentence, never collapsed to "" or False, so they
    cannot be read as a confident negative downstream."""
    if r["state"] == READ_OK:
        return r["value"]
    return f"[{r['state']}] {r['detail'] or 'no detail given'}"


RULES_HEADING = "## The rules"


def load_rules_with_class(path):
    """[(evidence_class, rule_text)] for the "- " lines under RULES_HEADING.

    Scoped to that one section deliberately. The page also carries anchor
    tables written as bullets, and an unscoped parse silently swallowed them
    as rules, turning 7 rules into 37. Prose elsewhere on the page must stay
    free to use bullets."""
    out, inside = [], False
    with open(path, encoding="utf-8") as f:
        for ln in f:
            s = ln.rstrip()
            if s.startswith("## "):
                inside = s.strip() == RULES_HEADING
                continue
            if not inside:
                continue
            m = _RULE_RE.match(s)
            if m:
                out.append((m.group(1) or STATED, m.group(2).strip()))
    return out


def load_rules(path):
    """Rule text only, tags stripped. Everything else on the page is prose."""
    return [r for _, r in load_rules_with_class(path)]


def thresholds(env_var, default):
    """Parse a threshold spec: either one number for every rule, or
    "substring=0.8,other=0.6" for per-rule values. Malformed parts are
    ignored rather than fatal, so a typo can never disable a gate."""
    spec = (os.environ.get(env_var) or "").strip()
    if not spec:
        return default, {}
    try:
        return float(spec), {}
    except ValueError:
        pass
    keyed = {}
    for part in spec.split(","):
        k, _, v = part.rpartition("=")
        try:
            if k.strip():
                keyed[k.strip()] = float(v)
        except ValueError:
            continue
    return default, keyed


def threshold_for(rule, spec):
    default, keyed = spec
    return next((t for k, t in keyed.items() if k in rule), default)


# --- transcript ---------------------------------------------------------

def _clean_human(s):
    s = (s or "").strip()
    if not s or s.startswith(NOT_HUMAN) or re.match(r"<[a-z][a-z_-]*[ >]", s):
        return None
    return s


def human_text(row):
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return None
    c = (row.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = "\n".join(b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
    return _clean_human(c) if isinstance(c, str) else None


def assistant_text(row):
    if row.get("type") != "assistant":
        return None
    c = (row.get("message") or {}).get("content")
    if not isinstance(c, list):
        return None
    s = "\n".join(b.get("text", "") for b in c
                  if isinstance(b, dict) and b.get("type") == "text").strip()
    return s or None


def tool_line(name, inp):
    """One line describing a tool call, both ends kept.

    Cutting only the tail hid what a chained command actually did. A gate
    asked whether tests ran was shown the first 200 characters of a 593
    character command and never received the words selfcheck or eval at
    all, so it answered no every time and was right about the evidence it
    had. The interesting part of a shell one-liner is usually at the end,
    so keep both ends and drop the middle."""
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"input": inp}
    inp = inp if isinstance(inp, dict) else {}
    v = (inp.get("command") or inp.get("file_path") or inp.get("path")
         or inp.get("pattern") or inp.get("url")
         or next((x for x in inp.values() if isinstance(x, str)), ""))
    s = f"{name}: {v}".strip()
    if len(s) <= TOOL_LINE_MAX:
        return s
    lo, hi = _token_bounds(s)
    mid = _segment_leaders(s[lo:hi])
    if not mid:
        return s[:lo] + " [...] " + s[hi:]
    return s[:lo] + " [...] " + mid + " [...] " + s[hi:]


def _token_bounds(s):
    """Where to cut, moved off the middle of a word.

    The offsets are fixed character counts, so a cut lands wherever it
    lands. One inside `python` left the head ending "...pytho" and the
    middle starting "n lib/jevgate.py", and no pattern matches either
    half. Measured: a buried `pytest -q` went invisible at 5 of 80 offsets
    and `python lib/jevgate.py` at 21 of 80.

    Both cuts move outward to the nearest whitespace, so every token stays
    whole and lands on exactly one side. Nothing is dropped, only shifted.
    If either cut would have to travel further than TOOL_LINE_SNAP, the
    text has no whitespace worth finding and the fixed offsets stand."""
    lo, hi = TOOL_LINE_HEAD, len(s) - TOOL_LINE_TAIL
    # Each side decides for itself. Deciding them together meant one
    # unsnappable side - a long unbroken run in the tail, say - cancelled
    # the snap on the other, which is where the buried command was.
    #
    # The head snaps to the start of the whole chained segment, not merely
    # to the start of a token. A pattern usually needs the interpreter and
    # the script together, and a token-only snap still let the cut fall in
    # the space between them: "... && python" on one side and
    # "lib/jevgate.py ..." on the other, with neither half matching.
    seps = [m.end() for m in re.finditer(r"&&|\|\||[;|\n]", s[:lo])]
    if seps and (lo - seps[-1]) <= TOOL_LINE_SEG:
        lo = seps[-1]
    else:
        lo2 = lo
        while lo2 > 0 and not s[lo2 - 1].isspace():
            lo2 -= 1
        if lo2 > 0 and (lo - lo2) <= TOOL_LINE_SNAP:
            lo = lo2
    hi2 = hi
    while hi2 < len(s) and not s[hi2].isspace():
        hi2 += 1
    if hi2 < len(s) and (hi2 - hi) <= TOOL_LINE_SNAP:
        hi = hi2
    if lo >= hi:
        return TOOL_LINE_HEAD, len(s) - TOOL_LINE_TAIL
    return lo, hi


_MID_SEP = re.compile(r"&&|\|\||[;|\n]")


def _split_unquoted(text, sep_re):
    """Split on sep_re, but never inside a single- or double-quoted span.

    A literal string containing a separator character - a commit message
    reading "notes; pytest", an echo argument with " && " in it - is not a
    second shell segment. Splitting inside it fabricated a leader token
    that this project's own detector then read as a command that ran:
    `git commit -m "notes; pytest"` produced a synthesised " ; pytest"
    segment matching TEST_PATTERNS even though pytest never executed.
    Found by independent review. Escaping is not modelled beyond a literal
    backslash immediately before the quote character - enough for the
    common case, and it fails closed (keeps a span merged) rather than
    open on anything stranger, which only ever makes a leader token
    longer, never invents a new one. POSIX single quotes have no escape
    character at all - a backslash inside them is literal, so a closing '
    always closes regardless of what precedes it. Only double quotes
    support backslash-escaping their own closing quote. Round 8 review:
    applying the same backslash-count check to both quote kinds read
    'C:\\' (a complete, literal Windows path) as still open, silently
    merging a trailing `&& pytest -q` into the span and dropping it."""
    out, buf, quote, i, n = [], [], None, 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == quote:
                if quote == "'":
                    # POSIX single quotes have no escape character at all,
                    # so a closing ' always closes.
                    quote = None
                else:
                    # A single trailing backslash escapes the quote; a pair
                    # of them is an escaped backslash followed by a real
                    # closing quote (e.g. "C:\\"). Only an odd run of
                    # backslashes means the quote itself is escaped.
                    j, nbs = i - 1, 0
                    while j >= 0 and text[j] == "\\":
                        nbs += 1
                        j -= 1
                    if nbs % 2 == 0:
                        quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        m = sep_re.match(text, i)
        if m:
            out.append("".join(buf))
            buf = []
            i = m.end()
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def _segment_leaders(mid):
    """What each chained segment in the dropped middle actually invoked.

    Keeping both ends still loses everything between them. A 593-character
    chain whose only `pytest -q` sits in the middle reads, to a gate asked
    whether tests ran, as a turn that ran nothing, and the gate blocks -
    correctly, on evidence its own extractor removed. That is the same
    defect as cutting the tail off, one level in.

    Two tokens per segment, not one, because this project's own checks are
    `python lib/jevgate.py`: the program name alone does not identify them.
    Bounded by TOOL_LINE_MID, so a long chain cannot grow the line without
    limit."""
    out, seen = [], set()
    for seg in _split_unquoted(mid, _MID_SEP):
        toks = [t for t in seg.split()
                if t not in ("sudo", "env", "time", "nohup", "exec")]
        if not toks:
            continue
        lead = " ".join(toks[:2])
        if lead not in seen:
            seen.add(lead)
            out.append(lead)
    return " ; ".join(out)[:TOOL_LINE_MID]


def tools_since_turn(rows):
    """Tool calls since the last human message, oldest first, each tagged
    with its outcome: "Bash: pytest -q -> ok"."""
    start = 0
    for j in range(len(rows) - 1, -1, -1):
        if human_text(rows[j]):
            start = j
            break
    uses, results = [], {}
    for row in rows[start:]:
        for b in (row.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if row.get("type") == "assistant" and b.get("type") == "tool_use":
                uses.append((b.get("id"), tool_line(b.get("name"), b.get("input"))))
            elif row.get("type") == "user" and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = (
                    "denied" if row.get("toolDenialKind")
                    else "error" if b.get("is_error") else "ok")
    return [f"{ln} -> {results[i]}" if i in results else ln
            for i, ln in uses][-MAX_TOOLS:]


def safe_transcript(tp):
    """Absolute path of tp if it is somewhere a transcript may legitimately
    live, else None.

    A gate reads this path and sends part of its contents to a third-party
    API, so an unconstrained path is an exfiltration primitive. Only the
    harness writes this field today, and anyone who can forge it can already
    run code as this user, so this is defence in depth rather than a live
    hole. It is cheap, so it is here.

    This must NOT be constrained to `cwd`: Claude Code keeps transcripts
    under ~/.claude/projects, so a cwd check would reject every real
    transcript and silently disable the gate."""
    if not tp:
        return None
    real = os.path.realpath(os.path.expanduser(tp))
    for base in TRANSCRIPT_ROOTS:
        try:
            if os.path.commonpath([real, base]) == base:
                return real
        except ValueError:  # different drive on Windows
            continue
    return None


def tail_rows(path, nbytes=TAIL_BYTES):
    with open(path, "rb") as f:
        start = max(0, os.path.getsize(path) - nbytes)
        f.seek(start)
        text = f.read().decode("utf-8", "replace")
    if start:  # the first line of the window is cut mid-way
        text = text.split("\n", 1)[-1]
    rows = []
    for ln in text.splitlines():
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    return rows


def read_tail(path):
    """(previous messages newest first, final assistant message, tool calls)."""
    rows = tail_rows(path)
    texts = [s for r in rows for s in [assistant_text(r) or human_text(r)] if s]
    if not texts:
        return [], None, []
    *ctx, last = texts
    return ([c[:MAXLEN] for c in ctx[::-1][:N_CONTEXT]], last[:MAXLEN],
            tools_since_turn(rows))


# --- jev ----------------------------------------------------------------

def call_jev(state, questions, key, model=MODEL):
    """POST one evaluation. Retries 429 and 529 with backoff, as the API
    docs require. Without this a single rate-limited call makes a gate
    silently fail open, which is indistinguishable from having no gate.

    Raises on final failure. Callers decide whether that means fail open or
    fail closed, because the answer differs per gate."""
    body = json.dumps({"model": model, "state": state, "questions": questions},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(len(RETRY_SLEEPS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in RETRY_STATUS:
                raise  # 401 and 422 are our bug; retrying cannot help
        except urllib.error.URLError as e:
            # urllib wraps only failures in connect/send as URLError. A
            # timeout or reset while awaiting the response surfaces raw
            # (TimeoutError/OSError) and is NOT retried: the request was
            # already sent, so a retry can bill twice and charge the
            # session cap twice for one judgement. Idea from
            # jkudish/jev-mcp (src/provider.ts, MIT).
            last_err = e
        if attempt < len(RETRY_SLEEPS):
            time.sleep(RETRY_SLEEPS[attempt])
    raise last_err


SHELL_TOOLS = ("Bash", "PowerShell")


def shell_command(hook, powershell_hint):
    """The command a shell-gate should read from this hook payload, or
    None when the call is not its concern.

    Gates matched on the Bash tool only, so on a machine where Claude
    Code's primary shell is the PowerShell tool every commit, install and
    destructive command skipped them entirely. The gates read commands
    with bashparse, which shares PowerShell's words, `;`, `&&` and `|` but
    not its quoting, so many ordinary PowerShell commands will not parse,
    and a gate answers an unparseable command with ASK. Sending every
    PowerShell command through would ask on nearly all of them. So a
    PowerShell command reaches the gate only when its raw text matches
    the gate's own topic (`powershell_hint`), and from there it gets the
    same treatment as Bash, including ASK when it cannot be read."""
    if not isinstance(hook, dict) or hook.get("tool_name") not in SHELL_TOOLS:
        return None
    ti = hook.get("tool_input")
    cmd = ti.get("command") if isinstance(ti, dict) else None
    if cmd is None:
        return None
    if hook.get("tool_name") == "PowerShell" and not (
            isinstance(cmd, str) and powershell_hint.search(cmd)):
        return None
    return cmd


_WIN_EXT = (".exe", ".cmd", ".bat", ".ps1")


def bare_command(name):
    """`git`, whether written git, GIT, /usr/bin/git or git.exe. The
    commit and package gates compared the command word exactly, so on
    Windows `git.exe commit` or `npm.cmd install x` was not recognised."""
    if not isinstance(name, str):
        return name
    base = re.split(r"[\\/]", name)[-1].lower()
    for ext in _WIN_EXT:
        if base.endswith(ext):
            return base[:-len(ext)]
    return base


# git's global options that take a separate value. Without skipping the
# value, `git -C repo commit` read "repo" as the subcommand and the commit
# gates saw no commit at all.
_GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                   "--exec-path", "--super-prefix", "--config-env",
                   "--attr-source")


def git_subcommand(texts):
    """The git subcommand in an argument list, past global options."""
    i = 0
    while i < len(texts):
        t = texts[i]
        if t in _GIT_VALUE_OPTS:
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            return t
    return None


# `git` then `commit` as a word of its own, in one line. Shared by the
# commit gates (4 and 6). `commit-graph` and `commit-tree` are excluded.
GIT_COMMIT_HINT = re.compile(
    r"\bgit(?:\.exe)?\b(?:[^\r\n]|`\r?\n)*?(?<![\w-])commit(?![\w-])",
    re.IGNORECASE)


def noul_p(answers, key):
    """One Noul probability from an answer map, or None when it is not a
    usable probability. Every gate reads its answers through this.

    json.load accepts the NaN and Infinity literals, and NaN compares False
    against every threshold, so a gate testing `p >= deny_at` would ALLOW
    on an answer it could not read. Out-of-range values, bools (an int
    subclass) and non-dict entries are rejected the same way: None is the
    one value every gate already treats as "no judgement". Strict answer
    validation modelled on jkudish/jev-mcp (src/index.ts, MIT), re-derived
    here, not copied."""
    entry = answers.get(key) if isinstance(answers, dict) else None
    p = entry.get("noul") if isinstance(entry, dict) else None
    if isinstance(p, bool) or not isinstance(p, (int, float)):
        return None
    p = float(p)
    return p if 0.0 <= p <= 1.0 else None  # NaN fails both comparisons


def choice_p(answers, key, options):
    """(chosen option, its probability) from a Choice answer, or
    (None, None) when the answer is not trustworthy.

    Trusted only if the probabilities cover exactly the expected options,
    each is a real number in [0, 1], they sum to 1 within 0.02, and the
    chosen option is the most probable one. Anything else is "no
    judgement", never a guess. Checks modelled on jkudish/jev-mcp
    (src/index.ts, MIT), re-derived here."""
    entry = answers.get(key) if isinstance(answers, dict) else None
    if not isinstance(entry, dict):
        return None, None
    probs, choice = entry.get("probabilities"), entry.get("choice")
    if not isinstance(probs, dict) or set(probs) != set(options):
        return None, None
    vals = {}
    for k, v in probs.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, None
        if not 0.0 <= float(v) <= 1.0:  # NaN fails too
            return None, None
        vals[k] = float(v)
    if abs(sum(vals.values()) - 1.0) > 0.02 or choice not in vals:
        return None, None
    if vals[choice] < max(vals.values()):
        return None, None
    return choice, vals[choice]


def probs_from(answers, rules, prefix="r"):
    """{rule: probability or None} from a Noul answer map keyed r0, r1, ..."""
    return {rule: noul_p(answers, f"{prefix}{i}")
            for i, rule in enumerate(rules)}


def noul_questions(rules, preamble, true_means, false_means, prefix="r"):
    """One Noul question per rule, keyed r0, r1, ... to match probs_from."""
    return {
        f"{prefix}{i}": {
            "type": "noul",
            "instructions": f"{preamble} Rule: {r}",
            "criteria": {"true": true_means, "false": false_means},
        }
        for i, r in enumerate(rules)
    }


# --- logging ------------------------------------------------------------

HOOK_ERRORS = "~/.jev-gates/hook-errors.log"

# Token shapes to strip before anything is written to disk. urllib exceptions
# do not carry request headers, so the key is not expected here, but the
# error log is written on unpredictable failure paths and scrubbing costs
# nothing. Redacting beats trusting every future exception type.
_SECRET_SHAPES = re.compile(
    r"(Bearer\s+\S+|\b(?:sk|tsk|key)[-_][A-Za-z0-9_-]{8,}|"
    r"\b[A-Za-z0-9_-]{40,}\b)", re.IGNORECASE)


def redact(text):
    """Remove the live key and anything token-shaped from a string.

    Deliberately narrow: the exception type and message are the diagnostic
    value of the error log, and stripping them wholesale would make the log
    useless, which defeats the reason it exists."""
    s = str(text)
    try:
        k = api_key()
        if k and len(k) >= 8:
            s = s.replace(k, "<redacted-key>")
    except Exception:  # noqa: BLE001  never fail while redacting
        pass
    return _SECRET_SHAPES.sub("<redacted>", s)


def hook_error(gate, msg):
    """Record an internal failure, best effort, and never raise.

    A gate that fails open silently is indistinguishable from a gate that is
    switched off. The decision log only gets written if the gate survives far
    enough to reach it, so a crash while parsing stdin used to leave no trace
    anywhere. This is the trace. Pattern taken from gstack's
    ~/.gstack/hook-errors.log; see reference/GSTACK-REVIEW.md."""
    try:
        path = os.path.expanduser(
            os.environ.get("JEV_HOOK_ERRORS") or HOOK_ERRORS)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} gate{gate}: "
                    f"{redact(msg)[:500]}\n")
    except Exception:  # noqa: BLE001  logging must never break a hook
        pass


class Budget:
    """An internal time budget, separate from the harness timeout.

    Claude Code's hook timeout is the outer belt. A gate that leans only on
    it can spend its whole allowance parsing a transcript and then start an
    API call it has no time to finish. This makes the gate stop itself first,
    with a recorded reason."""

    def __init__(self, ms):
        self.deadline = time.time() + ms / 1000.0

    def left(self):
        return self.deadline - time.time()

    def expired(self):
        return self.left() <= 0


# --- pipeline-wide call budget (P6) --------------------------------------
#
# A per-gate Budget bounds one hook's own wall-clock time. Nothing bounded
# how many real Jev calls a whole session could rack up across all of Gates
# 3, 4, 5 and 7 firing repeatedly over a long session. This is that cap.

SESSION_CALLS_DIR = "~/.jev-gates/session-calls"

# Not yet calibrated against a real session's actual gate-firing rate - a
# reasoned starting point, the same caveat every other uncalibrated
# threshold in this project carries (see reference/DESIGN-BASIS.md,
# "Threshold calibration"). JEV_SESSION_CALL_CAP overrides it for one run.
SESSION_CALL_CAP = 200

_SAFE_SESSION = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
_WIN_DEVICES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10)))


def _session_slug(session_id):
    """Same filename-safety rule as lib/findings.py's session_slug(),
    duplicated rather than imported: this module has no other reason to
    depend on findings.py, and the two stores serve different consumers -
    this one counts calls, findings.py's is cross-gate evidence a later
    gate reads. Kept byte-for-byte identical in behaviour on purpose."""
    s = str(session_id or "")
    if (_SAFE_SESSION.fullmatch(s) and not s.endswith(".")
            and s.split(".")[0].upper() not in _WIN_DEVICES):
        return s
    return "h" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def _session_calls_dir(session_id):
    root = os.path.expanduser(
        os.environ.get("JEV_SESSION_CALLS_DIR") or SESSION_CALLS_DIR)
    return os.path.join(root, _session_slug(session_id))


def session_calls_used(session_id):
    """How many real Jev calls this session has already spent, by any gate.

    Reporting only - every real gate's enforcement path now goes through
    try_charge_session_call() below, which does its own independent count
    and fails closed on its own. This function's only remaining callers are
    a hook_error() diagnostic message ("N calls this session") at each call
    site and the legacy session_budget_ok() pair below it, neither of which
    gates a real decision, so it deliberately keeps its original never-
    raises, unreadable-store-counts-as-zero contract rather than adding
    Optional[int] handling to six call sites for a value nothing acts on.
    uses os.listdir(), not glob.glob(), for consistency with
    try_charge_session_call()'s own read step and because os.listdir() is
    also simply the right tool once no glob pattern is actually needed -
    the directory holds nothing but call markers."""
    try:
        return len(os.listdir(_session_calls_dir(session_id)))
    except OSError:
        return 0


def session_budget_ok(session_id, cap=None):
    """Whether this session may still spend one more Jev call.

    No session id means nothing to scope a count to - a direct call or a
    test, not a real hook - so this always answers yes rather than guess."""
    if not session_id:
        return True
    return session_calls_used(session_id) < (
        SESSION_CALL_CAP if cap is None else cap)


def _new_call_marker(d):
    """Create one call marker in d, atomically, with a collision-safe name.

    tempfile.mkstemp() already guarantees a unique name via its own
    random-suffix retry loop - no rename-to-publish step is needed on top
    of it. The marker holds no content; its existence is the entire
    signal, so mkstemp's own atomic creation is already the full
    visibility event a rename pattern exists to provide elsewhere (see
    lib/findings.py, which protects a write of real content, not a bare
    marker). An earlier version of this built the name from
    time.time_ns() + os.getpid() and renamed into it - NOT guaranteed
    unique: two same-process callers whose calls land within one clock
    tick produce the identical name, and os.replace() then silently
    overwrote the earlier reservation instead of creating a second one.
    Found by independent review, reproduced with a forced timestamp
    collision. Raises OSError on failure; callers decide the fail policy."""
    fd, marker = tempfile.mkstemp(dir=d, prefix="", suffix=".call")
    os.close(fd)
    return marker


def charge_session_call(session_id):
    """Record that one real Jev call was just attempted, success or
    failure alike - a failed call still spent the request against whatever
    quota or cost is being bounded here.

    Internal building block for try_charge_session_call() below, which is
    what a gate should actually call. Kept as a standalone function only
    because the offline selfcheck exercises it directly; a live gate that
    calls this alone (bare write, no cap check) recreates the TOCTOU race
    try_charge_session_call() exists to close."""
    if not session_id:
        return
    d = _session_calls_dir(session_id)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        _new_call_marker(d)
    except OSError:
        pass  # accounting must never be what breaks a gate


def try_charge_session_call(session_id, cap=None):
    """Atomically reserve one session-wide Jev call slot. Call this once,
    immediately before the real call_jev() invocation it is guarding,
    in place of the old session_budget_ok() + charge_session_call() pair.

    That pair was a bare read followed by a fully decoupled bare write, with
    every one of the six real call sites re-checking and re-charging
    independently. Two concurrent hooks could both read "under cap" before
    either charged, and both proceed - confirmed by direct reproduction:
    201 charges landed against a cap of 200.

    This charges first, then counts, then rolls its own charge back if the
    count came in over cap. Under a genuine simultaneous race this can
    occasionally under-admit (two racers both see themselves as the one
    that went over and both roll back, when only one needed to) - it can
    never over-admit. The cap is a ceiling meant to bound a whole session's
    spend, not a slot machine that owes every caller a precise turn, so
    erring toward fewer calls under rare contention is the correct
    direction, and any under-admitted caller simply degrades exactly the
    way "budget spent" already degrades everywhere else.

    A storage failure at either the write or the read - an unreadable or
    unwritable ~/.jev-gates/session-calls directory - counts as budget NOT
    available and returns False, rather than the old behaviour of silently
    reporting zero calls used and leaving every gate's real cap unenforced
    for the rest of the session. A budget mechanism that fails open on I/O
    errors is not a budget mechanism; every caller already has a defined,
    tested fallback for "the session budget is spent" (Gate 3 fails closed
    to its fixed floor, every other gate here fails open to its own
    pre-existing no-key policy), so routing a storage failure through that
    same, already-reviewed path costs nothing new.

    The read step uses os.listdir(), not glob.glob() - glob's own
    internals catch OSError from a failing scandir and silently return an
    empty list rather than propagating it, so an unreadable directory used
    to read back as "zero calls used" and admit the call anyway, the exact
    failure mode this function exists to close. os.listdir() raises
    OSError on the same failure instead. Found by independent review,
    reproduced with a broken directory read that returned True at cap=0.

    No session id means nothing to scope a count to - a direct call or a
    test, not a real hook - so this always returns True, uncharged, exactly
    like the old session_budget_ok()."""
    if not session_id:
        return True
    d = _session_calls_dir(session_id)
    limit = SESSION_CALL_CAP if cap is None else cap
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        marker = _new_call_marker(d)
    except OSError:
        return False  # storage unusable: fail closed, never silently open

    try:
        used = len(os.listdir(d))
    except OSError:
        used = limit + 1  # can't verify the count: treat as over cap

    if used > limit:
        try:
            os.unlink(marker)
        except OSError:
            pass  # rollback is best-effort; a leaked marker only ever
                   # makes the cap stricter, never looser
        return False
    return True


# --- pipeline-wide "is Jev reachable" signal (P11) -----------------------
#
# Per-gate fail-open/fail-closed behaviour on a failed call is settled and
# correct - each gate already has its own tested policy for what to do when
# a real call_jev() raises. What was missing is a single place that says
# whether Jev itself is currently working THIS session, at all. During a
# real outage, six independent call sites each log their own "jev call
# failed" line to the shared hook-errors.log with no aggregate view - an
# operator (or a future status command) has to notice the pattern by
# reading scattered log lines rather than checking one state.
#
# Deliberately informational only: nothing here changes what a gate does
# on failure, or makes a gate skip attempting a real call because an
# earlier gate marked the session unreachable. Wiring that in would be a
# behaviour change needing its own explicit design (would a stale mark
# from ten minutes ago still apply? does one gate's outage justify
# another skipping its own attempt?) - out of scope for "a unified
# signal", which is what this closes.

JEV_STATE_DIR = "~/.jev-gates/session-jev-state"


def _session_state_path(session_id):
    root = os.path.expanduser(
        os.environ.get("JEV_SESSION_STATE_DIR") or JEV_STATE_DIR)
    return os.path.join(root, _session_slug(session_id) + ".json")


def _write_session_state(path, record):
    """Publish one session's aggregate Jev-reachability state, atomically.

    Last-write-wins by design: unlike the per-call markers above, this is
    a single mutable fact ("is Jev currently working"), not a count that
    every writer must contribute to independently - the most recent real
    call's outcome is the only one that matters. os.replace() into the
    final path means a reader never observes a partially-written file,
    even if two gates finish a call at nearly the same moment."""
    try:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix="", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError:
        pass  # a lost state update degrades to "unknown", never a crash


def mark_jev_unreachable(session_id, gate, reason):
    """Record that a real Jev call just failed, for the whole session to
    see - call this from the same except block that already calls
    hook_error() for a failed call_jev(), never for a no-key or
    budget-exhausted skip (those are policy, not evidence Jev is down)."""
    if not session_id:
        return
    _write_session_state(_session_state_path(session_id), {
        "reachable": False, "gate": gate, "reason": redact(reason)[:300],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})


def mark_jev_reachable(session_id):
    """Record that a real Jev call just succeeded. Call this once per
    successful call_jev() return, right alongside where a gate would
    otherwise start reading the response - a malformed answer is still
    evidence Jev was reachable, so this does not wait on the answer being
    usable."""
    if not session_id:
        return
    _write_session_state(_session_state_path(session_id), {
        "reachable": True, "gate": None, "reason": None,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})


def jev_unreachable(session_id):
    """The last known failure {gate, reason, ts} if this session's most
    recent real Jev call failed, else None.

    None covers both "reachable" and "no history yet" on purpose: a
    session that has made no real call at all is not evidence of an
    outage, only that nothing has been learned - the same distinction
    session_budget_ok() already draws for a missing session id. A caller
    that needs to tell those two apart reads the file at
    _session_state_path() directly."""
    if not session_id:
        return None
    try:
        with open(_session_state_path(session_id), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if record.get("reachable", True):
        return None
    return {"gate": record.get("gate"), "reason": record.get("reason"),
            "ts": record.get("ts")}


def append_jsonl(path, record):
    """Append one JSON row, owner-readable only, creating parents as needed.

    Returns True if the row reached disk. Never raises: a full disk must
    never silence a block. But the caller is told what happened rather than
    left to assume, because a store that reports a write it did not make is
    worse than one that reports the failure.

    One os.write of the whole line, in binary mode. Without O_BINARY Windows
    translates every newline on the way out, so the bytes on disk differ from
    the bytes handed in and os.write's return count is measured against the
    untranslated buffer, which makes the success check above meaningless.
    Verified on this machine: an 11-byte write landed as 12 bytes.

    NOT safe for concurrent writers on Windows. The C runtime implements
    O_APPEND as seek-then-write rather than one atomic operation, so two
    processes appending to the same file can overwrite each other, and a
    short write leaves a record with no trailing newline that swallows the
    next one appended after it. Every caller here is a single writer per
    file. The findings store needed concurrent writers and therefore does not
    use this function at all; see lib/findings.py, which gives each finding
    its own file and publishes it with os.replace."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(
            os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return False


def read_jsonl(path, strict=False):
    """Every well-formed row. A corrupt line is skipped, never fatal: a
    half-written record must not take the whole tool down.

    strict=True separates absent from unreadable. Default False keeps a
    missing log harmless for a hook, which must never be taken down by its
    own store. Every caller on the audit-evidence path passes True,
    because there "I am not allowed to read this" and "there is nothing to
    read" are opposite facts that both returned [] and both printed 0%.
    One open, not a probe followed by a read, so no window exists between
    the check and the use."""
    rows = []
    try:
        # errors="replace" rather than the default. A single bad byte made
        # iteration raise UnicodeDecodeError, which is a ValueError and not
        # an OSError, so it escaped every handler here and every handler
        # above and surfaced as a traceback. Replacing means an undecodable
        # line simply fails to parse as JSON and is skipped, which is what
        # this function already does with every other kind of corrupt line.
        f = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []
    try:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        # Strict must raise: a partial read presented as a whole one is the
        # fault this flag exists to prevent. Forgiving mode keeps what it
        # already parsed, because discarding it turned a readable log with
        # a bad tail into an absent one.
        if strict:
            raise
    finally:
        f.close()
    return rows


def log(record, env_var, default_path):
    """Append one decision to a JSONL log, owner-readable only.

    The log deliberately keeps the model's inputs and the decision. Those are
    the labels calibration fits thresholds against, so stripping them would
    leave every gate permanently stuck on a guessed number. The file holds
    the operator's own conversation, on their own machine, and is a strict
    subset of what ~/.claude/projects already stores. The mode is tightened
    rather than the content being removed.

    Returns whatever append_jsonl reported, so a caller that must not lose
    a row can check. Dropping that return meant every caller was told a
    write had happened whether or not it had."""
    return append_jsonl(
        os.environ.get(env_var) or os.path.expanduser(default_path), record)


def base_record(gate, hook, res, t0):
    """The fields every gate logs the same way.

    `model` is the *served* version as the API reports it. We now request a
    pinned version rather than an alias, so this field should equal MODEL.
    Keep logging it anyway: it is the only way to notice a server-side
    substitution, and it is the basis of the drift re-check procedure."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    # A stable handle so a decision can be adjudicated later without the log
    # ever being rewritten. Adjudications live in their own append-only file
    # and refer to this id. See lib/gatelog.py.
    rid = hashlib.sha256(
        f"{gate}|{hook.get('session_id')}|{ts}|{t0!r}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "id": rid,
        "ts": ts,
        "gate": gate,
        "model": res.get("model"),
        "session_id": hook.get("session_id"),
        # A subagent's hook payload carries the same session_id as its
        # parent, tagged with its own agent_id/agent_type - the only way to
        # tell a subagent's decision apart from the parent's after the
        # fact. Confirmed present on a real SubagentStop/subagent PreToolUse
        # payload by direct test (round 7 follow-up); both are None on the
        # parent's own turns, which is the correct, distinguishing signal.
        "agent_id": hook.get("agent_id"),
        "agent_type": hook.get("agent_type"),
        "cwd": hook.get("cwd"),
        "ms": int((time.time() - t0) * 1000),
        "usage": res.get("usage"),
        "error": res.get("error"),
    }


# --- Jev-answer verdict mapping (opt-in, not wired into any gate) -------
#
# Every gate above resolves its own jev-judged tier through a hand-written
# comparison against thresholds it calibrated itself (see JEV_THRESHOLDS
# in command_safety.py) - that per-rule calibration cannot be replaced by
# one shared cutoff, and nothing here attempts to. What repeats, gate to
# gate, is only the shape of the decision: take one Jev probability or
# chosen label and turn it into ALLOW / ASK / DENY. A future gate with no
# calibrated table of its own yet can start from this rather than
# re-deriving the same three-way split. Modelled on the execute / confirm
# / escalate / abort gate in the Jevbridge project (tacticocc/Jevbridge,
# src/gate.ts, MIT) - re-derived here in a dozen lines, stdlib only,
# because the shape is what is worth keeping, not the code.

ALLOW, ASK, DENY = "allow", "ask", "deny"


def verdict_from_noul(p, deny_above, ask_above=None):
    """One Noul probability -> ALLOW / ASK / DENY, or None if p is missing
    or not a real number (None or NaN).

    DENY when p >= deny_above. ASK when ask_above is given and
    ask_above <= p < deny_above. Omit ask_above for a two-way split
    straight to DENY, matching a gate whose jev-judged tier never asks a
    human (e.g. command_safety.py's jev_judge, which returns ALLOW or DENY
    only, by design).

    A NaN probability - a malformed or unparseable Jev answer coerced to
    float - compares False against every threshold in both directions and
    would otherwise fall through silently to ALLOW: the one answer a
    safety gate must never hand out on evidence it could not read. Treated
    the same as a missing answer instead. `p != p` is the portable NaN
    test (true only for NaN); avoids importing math for one check.

    Raises ValueError if ask_above is given and is not strictly below
    deny_above: that ordering makes ASK mathematically unreachable for
    every possible p, and a caller should be told at the call site rather
    than silently get a two-way gate it did not ask for."""
    if ask_above is not None and not ask_above < deny_above:
        raise ValueError(
            f"ask_above ({ask_above}) must be less than deny_above "
            f"({deny_above}), or ASK can never be returned")
    if p is None or (isinstance(p, float) and p != p):
        return None
    if p >= deny_above:
        return DENY
    if ask_above is not None and p >= ask_above:
        return ASK
    return ALLOW


def verdict_from_choice(choice, deny_choices=(), ask_choices=()):
    """One chosen label -> ALLOW / ASK / DENY by set membership, or None
    if choice is None (Jev returned no judgement) - the same missing-
    answer contract as verdict_from_noul, so a caller cannot mistake "Jev
    did not answer" for "Jev answered allow".

    A label present in both sets resolves to DENY, the safer of the two -
    never silently downgraded to ASK because of set ordering.

    Raises TypeError if deny_choices or ask_choices is a bare string:
    `choice in "abort"` matches any character or substring of "abort" (for
    example "b" or "or"), not the whole label "abort", and that is never
    what a caller means by passing one label."""
    if isinstance(deny_choices, str) or isinstance(ask_choices, str):
        raise TypeError(
            "deny_choices/ask_choices must be a collection of labels "
            "(tuple, list, set), not a bare str")
    if choice is None:
        return None
    if choice in deny_choices:
        return DENY
    if choice in ask_choices:
        return ASK
    return ALLOW


# --- offline self-check -------------------------------------------------

RULES_FIXTURE = """# t

- not a rule, prose above the section

## The rules

- [observed] A
- [stated] B
- C

## Anchors

- Clear pass: bullets here are prose
"""


def selfcheck():
    # The shared worst-case/margin both gates check their remaining budget
    # against. Computed, not hand-typed, so a change to TIMEOUT or
    # RETRY_SLEEPS cannot silently leave this stale relative to what
    # call_jev can actually take. Asserted here, once, rather than in each
    # gate's own selfcheck, so both gates are provably reading the same
    # number rather than each carrying a copy that could drift apart -
    # the exact risk a shared constant exists to remove.
    assert CALL_WORST_MS == int((TIMEOUT * (len(RETRY_SLEEPS) + 1)
                                 + sum(RETRY_SLEEPS)) * 1000), CALL_WORST_MS
    assert CALL_WORST_MS == 27000, CALL_WORST_MS  # today's real number
    assert CALL_MARGIN_MS > 0, CALL_MARGIN_MS

    # A missing User-Agent got every gate 403'd by Cloudflare's bot
    # protection (2026-09-23), reproduced live before being fixed. This
    # regression case would have caught it: assert the real Request object
    # call_jev builds carries a non-default User-Agent, without a live call.
    import unittest.mock
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"model": "x", "answers": {}, "usage": {}}'

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return _FakeResponse()

    with unittest.mock.patch("urllib.request.urlopen", _fake_urlopen):
        call_jev({"s": "x"}, {"q0": {"type": "noul", "instructions": "x",
                                     "criteria": {"true": "t", "false": "f"}}},
                 "fake-key")
    ua = captured["headers"].get("User-agent")  # urllib title-cases headers
    assert ua and "python-urllib" not in ua.lower(), captured["headers"]
    assert ua == USER_AGENT, ua

    md = RULES_FIXTURE
    fd, tmp = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(md)
        got = load_rules_with_class(tmp)
        assert got == [(OBSERVED, "A"), (STATED, "B"), (STATED, "C")], got
        assert load_rules(tmp) == ["A", "B", "C"], load_rules(tmp)
    finally:
        os.unlink(tmp)

    # A real read passes through untouched, including an empty-but-checked
    # value -- "" here means "checked, found nothing", not "did not check".
    assert render_reading(reading(READ_OK, "")) == "", "empty observed value"
    assert render_reading(reading(READ_OK, "M file.py")) == "M file.py"
    # unmeasured/error must never render as "" or another value a caller
    # could mistake for a checked negative.
    for state in (READ_NA, READ_ERR):
        out = render_reading(reading(state, detail="git timed out"))
        assert out not in ("", None, False), (state, out)
        assert out == f"[{state}] git timed out", (state, out)
    rejected = False
    try:
        reading("not-a-real-state")
    except AssertionError as e:
        rejected = "not-a-real-state" in str(e)
    assert rejected, "reading() must reject an unknown state"

    rows = [
        {"type": "user", "message": {"content": "add a test"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "pytest -q"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "All done, tests pass."}]}},
    ]
    assert tools_since_turn(rows) == ["Bash: pytest -q -> error"]

    # A long chained command must keep the end, where the work usually is.
    # Tail-only truncation left a gate judging "did tests run" without the
    # word it needed, and it answered no correctly every time on evidence
    # that had been cut. Both ends, or the evidence is not evidence.
    long_cmd = "cd /tmp && " + ("echo padding && " * 40) + "pytest -q"
    line = tool_line("Bash", {"command": long_cmd})
    assert len(line) <= TOOL_LINE_MAX + TOOL_LINE_MID + 14, len(line)
    assert line.startswith("Bash: cd /tmp &&"), line
    assert line.endswith("pytest -q"), line
    assert "pytest" in line, line

    # Keeping both ends still drops everything between them. The one test
    # command in this chain sits in the middle, where neither end reaches
    # it, so a gate asked whether tests ran saw a turn that ran none and
    # blocked an agent that had run the suite.
    buried = ("cd /tmp && " + ("echo padding && " * 20) + "pytest -q && "
              + ("echo more padding && " * 20) + "echo done")
    line = tool_line("Bash", {"command": buried})
    assert "pytest" in line, line
    assert len(line) <= TOOL_LINE_MAX + TOOL_LINE_MID + 14, len(line)

    # This project's own checks are two tokens, so one is not enough.
    buried2 = ("cd /tmp && " + ("echo padding && " * 20)
               + "python lib/jevgate.py && "
               + ("echo more padding && " * 20) + "echo done")
    assert "python lib/jevgate.py" in tool_line("Bash", {"command": buried2})

    # The cut is a fixed offset, so it lands wherever it lands. Every
    # alignment of a buried command against that offset must survive,
    # not just the convenient ones: a cut through the middle of `python`
    # used to leave "...pytho" on one side and "n lib/..." on the other,
    # and no pattern matches either half. Measured before the fix:
    # invisible at 5 of these offsets for pytest, 21 for the two-token one.
    for _pad in range(0, 140):
        _c = ("cd /tmp && " + ("x" * _pad) + " && pytest -q && echo done"
              + ("y" * 120))
        assert "pytest -q" in tool_line("Bash", {"command": _c}), _pad
        _c2 = ("cd /tmp && " + ("x" * _pad)
               + " && python lib/jevgate.py && echo done" + ("y" * 120))
        assert "python lib/jevgate.py" in tool_line(
            "Bash", {"command": _c2}), _pad

    # One unbroken run with no whitespace to snap to must still terminate
    # and stay bounded.
    _huge = tool_line("Bash", {"command": "a" * 5000})
    assert len(_huge) <= TOOL_LINE_MAX + TOOL_LINE_MID + 14, len(_huge)

    # A short command is passed through untouched, no marker added.
    assert tool_line("Bash", {"command": "ls -la"}) == "Bash: ls -la"

    # A quoted separator inside a literal argument must not fabricate a
    # segment. Found by independent review: a commit message reading
    # "notes; pytest" produced a synthesised " ; pytest" leader that a
    # downstream detector read as a real test run, when nothing but a
    # git commit happened.
    injected = ("cd /tmp && " + ("x" * 70)
               + ' && git commit -m "notes; pytest" && echo done'
               + ("y" * 120))
    assert "pytest" not in tool_line("Bash", {"command": injected})

    # strict separates absent from unreadable. Default stays forgiving, so
    # a hook is never taken down by its own store.
    _missing = os.path.join(tempfile.mkdtemp(), "nope.jsonl")
    assert read_jsonl(_missing) == [] and read_jsonl(_missing, strict=True) == []
    _dir = tempfile.mkdtemp()
    assert read_jsonl(_dir) == []
    try:
        read_jsonl(_dir, strict=True)
        raise AssertionError("strict must raise on a path that will not open")
    except OSError:
        pass

    # A bad byte must be skipped like any other corrupt line, not escape as
    # a UnicodeDecodeError. It is a ValueError, not an OSError, so it went
    # past every handler here and every handler above it.
    _bad = os.path.join(tempfile.mkdtemp(), "bad.jsonl")
    with open(_bad, "wb") as _f:
        _f.write(b'{"id": "a"}\n\xff\xfe\n{"id": "b"}\n')
    assert [r["id"] for r in read_jsonl(_bad)] == ["a", "b"], read_jsonl(_bad)
    assert [r["id"] for r in read_jsonl(_bad, strict=True)] == ["a", "b"]

    # Rows already parsed must survive a read that fails part way. Losing
    # them turned a readable log with a bad tail into an absent one.
    class _HalfFile:
        def __init__(self):
            self.lines = iter(['{"id": "a"}\n', '{"id": "b"}\n'])

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self.lines)
            except StopIteration:
                raise OSError(5, "Input/output error")

        def close(self):
            pass

    import builtins as _b
    _real = _b.open
    try:
        _b.open = lambda *a, **k: _HalfFile()
        assert [r["id"] for r in read_jsonl("x")] == ["a", "b"]
        try:
            read_jsonl("x", strict=True)
            raise AssertionError("strict must raise on a torn read")
        except OSError:
            pass
    finally:
        _b.open = _real

    # Harness-injected user lines must not be mistaken for the human.
    assert human_text({"type": "user", "message": {
        "content": "Stop hook feedback: keep going"}}) is None
    assert human_text({"type": "user", "message": {
        "content": "<system-reminder>hi</system-reminder>"}}) is None
    assert human_text({"type": "user",
                       "message": {"content": "add a test"}}) == "add a test"

    # A malformed spec must fall back, never disable a gate.
    os.environ["_JG_TEST"] = "garbage=,=0.4"
    assert threshold_for("any rule", thresholds("_JG_TEST", 0.75)) == 0.75
    os.environ["_JG_TEST"] = "0.9"
    assert threshold_for("any rule", thresholds("_JG_TEST", 0.75)) == 0.9
    os.environ["_JG_TEST"] = "tests=0.55"
    spec = thresholds("_JG_TEST", 0.75)
    assert threshold_for("Do not claim done without the tests", spec) == 0.55
    assert threshold_for("Some other rule", spec) == 0.75
    del os.environ["_JG_TEST"]

    # Hostile transcript paths refused, real ones still accepted. Getting
    # this backwards either leaks files or silently disables every gate.
    assert safe_transcript(None) is None
    assert safe_transcript(os.path.expanduser("~/.ssh/id_rsa")) is None
    assert safe_transcript(os.path.expanduser("~/.aws/credentials")) is None
    assert safe_transcript(
        os.path.expanduser("~/.claude/projects/../../.env")) is None
    good = os.path.join(tempfile.gettempdir(), "eval.jsonl")
    assert safe_transcript(good) == os.path.realpath(good)
    real = os.path.expanduser("~/.claude/projects/p/s.jsonl")
    assert safe_transcript(real) == os.path.realpath(real)

    # Answer mapping and question keys must agree, or every score is None.
    qs = noul_questions(["a", "b"], "Broken?", "yes", "no")
    assert set(qs) == {"r0", "r1"}
    got = probs_from({"r0": {"noul": 0.9}, "r1": {}}, ["a", "b"])
    assert got == {"a": 0.9, "b": None}, got

    # An unreadable probability must read as "no judgement", never as a
    # number a threshold test could silently pass. json.load really does
    # produce NaN/Infinity from the bare literals, so parse them for real.
    wild = json.loads('{"n": {"noul": NaN}, "i": {"noul": Infinity}, '
                      '"hi": {"noul": 1.7}, "lo": {"noul": -0.1}, '
                      '"b": {"noul": true}, "s": {"noul": "0.9"}, '
                      '"x": null, "ok0": {"noul": 0}, "ok1": {"noul": 1}}')
    for k in ("n", "i", "hi", "lo", "b", "s", "x", "missing"):
        assert noul_p(wild, k) is None, (k, noul_p(wild, k))
    assert noul_p(wild, "ok0") == 0.0 and noul_p(wild, "ok1") == 1.0
    assert noul_p(None, "n") is None and noul_p([1], "n") is None

    # Shell tools: Bash always reaches a gate, PowerShell only on its
    # topic, and nothing else ever does.
    def hk(tool, cmd):
        return {"tool_name": tool, "tool_input": {"command": cmd}}
    assert shell_command(hk("Bash", "ls"), GIT_COMMIT_HINT) == "ls"
    assert shell_command(hk("PowerShell", "Get-ChildItem"),
                         GIT_COMMIT_HINT) is None
    for c in ("git commit -m x", "git add -A; git commit -F msg.txt",
              "& git.exe -C repo commit -q", "GIT COMMIT -m x",
              "git `\n  commit -m x", "git -C repo `\r\n commit"):
        assert shell_command(hk("PowerShell", c), GIT_COMMIT_HINT) == c, c
    for c in ("git commit-graph write", "git log --grep commit-tree",
              "git status\ncommit"):
        assert shell_command(hk("PowerShell", c), GIT_COMMIT_HINT) is None, c
    assert shell_command(hk("Read", "git commit"), GIT_COMMIT_HINT) is None
    assert shell_command({"tool_name": "PowerShell", "tool_input": {}},
                         GIT_COMMIT_HINT) is None
    assert shell_command(hk("PowerShell", ["git commit"]),
                         GIT_COMMIT_HINT) is None
    assert shell_command(None, GIT_COMMIT_HINT) is None

    for n in ("git", "GIT", "/usr/bin/git", "git.exe", r"C:\Git\bin\git.exe"):
        assert bare_command(n) == "git", n
    assert bare_command("npm.cmd") == "npm" and bare_command(None) is None
    assert git_subcommand(["-C", "repo", "commit", "-m", "x"]) == "commit"
    assert git_subcommand(["-c", "a=b", "--no-pager", "commit"]) == "commit"
    assert git_subcommand(["--git-dir=x", "commit"]) == "commit"
    assert git_subcommand(["--attr-source", "main", "commit"]) == "commit"
    assert git_subcommand(["-C", "repo"]) is None and git_subcommand([]) is None

    # A Choice answer is trusted only when it is internally consistent.
    opts = ("a", "b", "c")

    def ch(choice, probs):
        return choice_p({"k": {"choice": choice, "probabilities": probs}},
                        "k", opts)
    assert ch("a", {"a": 0.7, "b": 0.2, "c": 0.1}) == ("a", 0.7)
    for bad in (ch("b", {"a": 0.7, "b": 0.2, "c": 0.1}),     # not the top
                ch("a", {"a": 0.7, "b": 0.2}),               # missing key
                ch("a", {"a": 0.7, "b": 0.2, "c": 0.1, "d": 0}),  # extra
                ch("a", {"a": 0.5, "b": 0.2, "c": 0.1}),     # sums to 0.8
                ch("a", {"a": float("nan"), "b": 0.2, "c": 0.1}),
                ch("a", {"a": 1.2, "b": -0.1, "c": -0.1}),
                ch("a", {"a": True, "b": 0.0, "c": 0.0}),
                ch("z", {"a": 0.7, "b": 0.2, "c": 0.1}),
                choice_p(None, "k", opts), choice_p({"k": "x"}, "k", opts)):
        assert bad == (None, None), bad

    # An internal budget must expire, and must not be confused with the
    # harness timeout.
    b = Budget(0)
    assert b.expired() is True
    assert Budget(5000).expired() is False

    # The error log must never raise, whatever it is handed.
    os.environ["JEV_HOOK_ERRORS"] = os.path.join(
        tempfile.gettempdir(), "jev-selfcheck-errors.log")
    # Start clean. A previous crashed run leaves this file behind, and its
    # stale lines would otherwise fail the redaction assertion below for a
    # reason that has nothing to do with the current code.
    if os.path.exists(os.environ["JEV_HOOK_ERRORS"]):
        os.unlink(os.environ["JEV_HOOK_ERRORS"])
    hook_error(7, "selfcheck probe")
    hook_error(7, object())

    # Anything token-shaped must be scrubbed before it reaches disk. The
    # exception type and message must survive, or the log loses its point.
    os.environ["TYPESAFE_API_KEY"] = "tsk-selfcheck-secret-value"
    try:
        r = redact("HTTPError 401 using tsk-selfcheck-secret-value")
        assert "selfcheck-secret-value" not in r, r
        assert "HTTPError 401" in r, r
        assert "<redacted" in r, r
        assert "Bearer" not in redact("sent Bearer abc123def456")
        assert redact("URLError: connection refused") ==             "URLError: connection refused"
        hook_error(7, "leak attempt tsk-selfcheck-secret-value")
        with open(os.path.expanduser(os.environ["JEV_HOOK_ERRORS"]),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert "selfcheck-secret-value" not in body, body
        assert "leak attempt" in body, body
    finally:
        del os.environ["TYPESAFE_API_KEY"]
    with open(os.path.expanduser(os.environ["JEV_HOOK_ERRORS"]),
              encoding="utf-8") as fh:
        assert "selfcheck probe" in fh.read()
    os.unlink(os.path.expanduser(os.environ["JEV_HOOK_ERRORS"]))
    del os.environ["JEV_HOOK_ERRORS"]

    # Bytes on disk must equal bytes handed in. Without O_BINARY, Windows
    # translates the newline and the file no longer matches what was written.
    jl = os.path.join(tempfile.gettempdir(), "jev-selfcheck-append.jsonl")
    if os.path.exists(jl):
        os.unlink(jl)
    try:
        assert append_jsonl(jl, {"a": 1}) is True
        assert append_jsonl(jl, {"b": "x" * 9000}) is True
        with open(jl, "rb") as fh:
            raw = fh.read()
        assert b"\r\n" not in raw, raw[:80]
        assert len(read_jsonl(jl)) == 2, read_jsonl(jl)
        assert append_jsonl(jl, {"c": {1, 2}}) is False   # unserialisable
        assert len(read_jsonl(jl)) == 2, read_jsonl(jl)
    finally:
        if os.path.exists(jl):
            os.unlink(jl)

    # The served model must reach the log, or the drift check cannot run.
    rec = base_record(7, {"session_id": "s"}, {"model": "jev-1.13.0"}, time.time())
    assert rec["model"] == "jev-1.13.0", rec
    assert len(rec["id"]) == 12 and rec["id"].isalnum(), rec["id"]
    a = base_record(7, {"session_id": "s"}, {"model": "x"}, 1.0)["id"]
    b = base_record(7, {"session_id": "s"}, {"model": "x"}, 1.0)["id"]
    assert a == b, (a, b)

    # A non-retryable status must not be retried.
    assert 401 not in RETRY_STATUS and 422 not in RETRY_STATUS
    assert 429 in RETRY_STATUS and 529 in RETRY_STATUS
    # Worst-case call time must fit inside a 30 s hook budget.
    assert TIMEOUT * (len(RETRY_SLEEPS) + 1) + sum(RETRY_SLEEPS) < 30

    # A timeout after the request was sent must not be re-sent (a retry
    # can bill twice). A connect-phase failure still retries.
    import unittest.mock
    for raised, want in ((TimeoutError("read timed out"), 1),
                         (ConnectionResetError("reset"), 1),
                         (urllib.error.URLError("refused"),
                          len(RETRY_SLEEPS) + 1)):
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=raised) as uo, \
                unittest.mock.patch("time.sleep"):
            try:
                call_jev({}, {}, "k")
                raise AssertionError("call_jev must raise")
            except type(raised):
                pass
            assert uo.call_count == want, (type(raised), uo.call_count)

    # --- P6: the pipeline-wide call budget --------------------------------
    calls_root = tempfile.mkdtemp(prefix="jev-session-calls-")
    real_calls_dir = os.environ.get("JEV_SESSION_CALLS_DIR")
    os.environ["JEV_SESSION_CALLS_DIR"] = calls_root
    try:
        # No session id: always ok, never counted - a direct call or test
        # has nothing to scope a session-wide count to.
        assert session_budget_ok(None)
        assert session_budget_ok("")
        charge_session_call(None)  # must be a no-op, never raise
        assert session_calls_used(None) == 0

        assert session_calls_used("s1") == 0
        assert session_budget_ok("s1", cap=2)
        charge_session_call("s1")
        assert session_calls_used("s1") == 1
        assert session_budget_ok("s1", cap=2)
        charge_session_call("s1")
        assert session_calls_used("s1") == 2
        assert not session_budget_ok("s1", cap=2)

        # A second session's count must never leak into the first's.
        assert session_calls_used("s2") == 0
        assert session_budget_ok("s2", cap=2)

        # A session id that would otherwise be a path-traversal primitive
        # is hashed exactly like lib/findings.py's own session_slug(),
        # never trusted as a directory name.
        charge_session_call("../../etc/passwd")
        assert session_calls_used("../../etc/passwd") == 1

        # A broken store must count as zero, not lock every gate out -
        # this is session_calls_used()'s own deliberate reporting-only
        # contract (see its docstring), distinct from
        # try_charge_session_call()'s enforcement-path fail-closed below.
        with unittest.mock.patch("os.listdir", side_effect=OSError("gone")):
            assert session_calls_used("s1") == 0
            assert session_budget_ok("s1", cap=1)

        # try_charge_session_call(): the atomic replacement a gate should
        # actually call. Charges first, counts second, rolls back if over
        # cap - never a bare read followed by a decoupled bare write.
        assert try_charge_session_call(None) is True
        assert try_charge_session_call("") is True
        assert session_calls_used(None) == 0  # None/"" never charges

        assert session_calls_used("s3") == 0
        assert try_charge_session_call("s3", cap=2) is True
        assert session_calls_used("s3") == 1
        assert try_charge_session_call("s3", cap=2) is True
        assert session_calls_used("s3") == 2
        assert try_charge_session_call("s3", cap=2) is False  # over cap
        assert session_calls_used("s3") == 2, "rejected attempt left a trace"

        # Path traversal hashed exactly like the legacy functions (a
        # different traversal string than the legacy test above used, so
        # the two tests do not share a hashed slug and step on each other).
        assert try_charge_session_call("../../etc/shadow", cap=5) is True
        assert session_calls_used("../../etc/shadow") == 1

        # A write failure fails CLOSED (budget not available), not open -
        # the defect this was written to fix let a broken store silently
        # remove enforcement for the rest of the session.
        with unittest.mock.patch("tempfile.mkstemp", side_effect=OSError("x")):
            assert try_charge_session_call("s4", cap=5) is False
        assert session_calls_used("s4") == 0  # nothing was ever charged

        # A read failure at the verify step also fails closed, and the
        # marker it could not verify is rolled back rather than kept -
        # a charge nobody could confirm must not silently count anyway.
        # os.listdir(), not glob.glob(): glob's own internals catch OSError
        # from a failing scandir and silently return [] rather than
        # raising, so a mock on glob.glob here would prove nothing about
        # the real failure mode - found by independent review after the
        # first fix used glob.glob and this exact case slipped through.
        before = session_calls_used("s5")
        with unittest.mock.patch("os.listdir", side_effect=OSError("x")):
            assert try_charge_session_call("s5", cap=5) is False
        assert session_calls_used("s5") == before, "unverified charge kept"

        # Regression guard for the swallowing failure mode itself: glob's
        # OSError-swallowing must never be relied on as this function's
        # read step again. A broken directory read via glob.glob() alone
        # (not os.listdir) must NOT be mistaken for "zero calls used".
        with unittest.mock.patch("os.scandir", side_effect=OSError("x")):
            assert glob.glob(os.path.join(_session_calls_dir("s5"), "*")) == [], (
                "glob.glob is expected to swallow this - if it stops "
                "doing so, the comment above about why this function "
                "uses os.listdir() instead is stale")

        # The bug this exists to fix needed real concurrency to reproduce
        # (201 charges landed against a cap of 200 with the old bare
        # read/bare write pair). Twenty threads racing ten slots must never
        # grant more than ten, and every grant must leave exactly one
        # marker behind - no leaks from a rolled-back attempt.
        import threading
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(20)

        def _racer():
            barrier.wait()
            r = try_charge_session_call("s6", cap=10)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_racer) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        granted = sum(results)
        assert granted <= 10, granted  # never over cap under a real race
        assert session_calls_used("s6") == granted, "grant/marker mismatch"
    finally:
        if real_calls_dir is None:
            os.environ.pop("JEV_SESSION_CALLS_DIR", None)
        else:
            os.environ["JEV_SESSION_CALLS_DIR"] = real_calls_dir
        import shutil
        shutil.rmtree(calls_root, ignore_errors=True)

    # --- P11: the aggregate "is Jev reachable" signal ----------------------
    state_root = tempfile.mkdtemp(prefix="jev-session-state-")
    real_state_dir = os.environ.get("JEV_SESSION_STATE_DIR")
    os.environ["JEV_SESSION_STATE_DIR"] = state_root
    try:
        # No history yet is not evidence of an outage.
        assert jev_unreachable("p11-s1") is None
        # No session id: always a no-op, never raises, never unreachable.
        mark_jev_unreachable(None, 3, "should be a no-op")
        assert jev_unreachable(None) is None

        mark_jev_unreachable(
            "p11-s1", 3, "HTTPError 401 tsk-p11-selfcheck-secret-value")
        bad = jev_unreachable("p11-s1")
        assert bad is not None and bad["gate"] == 3, bad
        # Secrets/token-shaped text must be redacted the same way
        # hook_error() redacts it - this state file is not exempt.
        assert "p11-selfcheck-secret-value" not in bad["reason"], bad
        assert "<redacted" in bad["reason"], bad

        # A later success clears it.
        mark_jev_reachable("p11-s1")
        assert jev_unreachable("p11-s1") is None

        # A second session's state must never leak into the first's.
        assert jev_unreachable("p11-s2") is None
        mark_jev_unreachable("p11-s2", 7, "timed out")
        assert jev_unreachable("p11-s1") is None
        assert jev_unreachable("p11-s2")["gate"] == 7

        # A corrupt or unreadable state file must read back as unknown
        # (None), never as a crash and never as a false "reachable".
        p = _session_state_path("p11-s3")
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        assert jev_unreachable("p11-s3") is None

        # A write failure must never raise - the mark is simply lost.
        with unittest.mock.patch("tempfile.mkstemp", side_effect=OSError("x")):
            mark_jev_unreachable("p11-s4", 4, "unreachable during a broken store")
        assert jev_unreachable("p11-s4") is None
    finally:
        if real_state_dir is None:
            os.environ.pop("JEV_SESSION_STATE_DIR", None)
        else:
            os.environ["JEV_SESSION_STATE_DIR"] = real_state_dir
        import shutil
        shutil.rmtree(state_root, ignore_errors=True)

    # --- Jev-answer verdict mapping (opt-in helper) ------------------------
    assert verdict_from_noul(None, deny_above=0.8) is None
    assert verdict_from_noul(0.9, deny_above=0.8) == DENY
    assert verdict_from_noul(0.5, deny_above=0.8, ask_above=0.4) == ASK
    assert verdict_from_noul(0.1, deny_above=0.8, ask_above=0.4) == ALLOW
    # No ask_above given: a two-way split straight to DENY, never ASK.
    assert verdict_from_noul(0.5, deny_above=0.8) == ALLOW
    # Exact boundary values: >= must trigger the higher-severity verdict,
    # not >. Every earlier assertion above used values strictly inside
    # each band, so a stray ">" instead of ">=" would have passed them all -
    # found by independent review.
    assert verdict_from_noul(0.8, deny_above=0.8, ask_above=0.4) == DENY
    assert verdict_from_noul(0.4, deny_above=0.8, ask_above=0.4) == ASK
    # A NaN probability must read as "no answer", never fall through both
    # comparisons (both False for NaN) to a silent ALLOW.
    assert verdict_from_noul(float("nan"), deny_above=0.8) is None
    # An inverted or equal threshold pair makes ASK unreachable for every
    # p and must be rejected at the call site, not silently accepted.
    try:
        verdict_from_noul(0.5, deny_above=0.5, ask_above=0.5)
        raise AssertionError("equal thresholds must raise ValueError")
    except ValueError:
        pass
    try:
        verdict_from_noul(0.5, deny_above=0.5, ask_above=0.8)
        raise AssertionError("inverted thresholds must raise ValueError")
    except ValueError:
        pass

    assert verdict_from_choice("abort", deny_choices=("abort",)) == DENY
    assert verdict_from_choice("maybe", ask_choices=("maybe",)) == ASK
    assert verdict_from_choice("proceed") == ALLOW
    # A label in both sets resolves to the safer outcome, DENY.
    assert verdict_from_choice(
        "x", deny_choices=("x",), ask_choices=("x",)) == DENY
    # A missing judgement must read as "no answer", the same contract as
    # verdict_from_noul(None, ...), never a silent ALLOW.
    assert verdict_from_choice(None, deny_choices=("abort",)) is None
    # A bare string instead of a collection would let `choice in "abort"`
    # match single characters and substrings ("b", "or") of the label, not
    # the whole label - must be rejected, not silently mismatched.
    try:
        verdict_from_choice("b", deny_choices="abort")
        raise AssertionError("a bare str deny_choices must raise TypeError")
    except TypeError:
        pass
    try:
        verdict_from_choice("b", ask_choices="abort")
        raise AssertionError("a bare str ask_choices must raise TypeError")
    except TypeError:
        pass

    print("jevgate selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(selfcheck())
