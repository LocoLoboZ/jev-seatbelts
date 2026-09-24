#!/usr/bin/env python
"""A small, quote-aware bash reader for Gate 3.

It answers one question: *what command is this word, and do we actually
know?* Every word carries a provenance, and a command name is returned only
when that provenance is `literal`. Anything touched by an expansion, a
substitution or a glob is reported as unknown rather than guessed at.

Two layers, on purpose, both used by the gate:

1. `parse()` - a structured read into segments, for precision.
2. `unquoted_view()` - an independent quote-aware scan of the raw text that
   runs whatever `parse()` did, to catch what the parser cannot even parse.

Caps fail CLOSED. Over a cap raises, and the gate turns that into "ask",
never into "allow". Claude Code's own deny evaluator reportedly stops
evaluating past 50 subcommands and falls back to a prompt, and reportedly
inspects only the first token of a compound. This does the opposite.

`$(...)` and backtick bodies are LIFTED OUT as their own segments, leaving a
sentinel in the enclosing word. A space would cut `out-$(date).txt` into two
innocuous-looking halves and hide the substitution entirely.

Backend seam: `parse(cmd, backend=...)`. Only the stdlib backend ships, so a
public user with no compiler gets a working gate. `tree-sitter-bash` is MIT
and therefore admissible as an optional accuracy tier later; an unknown
backend name raises rather than silently falling back, because a silent
fallback is how an accuracy claim becomes false.

Design credit, ideas not code: word provenance, the dual layer and the caps
from `kenryu42/cc-safety-net` (MIT); the tokeniser shape and the lift-out
rule from `MikahNiehaus/ClaudeBoost` `bash-guard.py` (MIT). See
`skills/command-safety/references/PROVENANCE.md`.

Self-check: `python lib/bashparse.py` (offline, no key needed).
"""
import re
import sys

LITERAL = "literal"
VARIABLE = "variable"
CMDSUB = "command-substitution"
ARITH = "arithmetic"
GLOB = "glob"
UNKNOWN = "unknown"

# Worst first. A word that is part literal and part substitution is a
# substitution, because the literal half tells us nothing about the rest.
_PRECEDENCE = (CMDSUB, VARIABLE, ARITH, UNKNOWN, GLOB, LITERAL)

# What is left in a word where a substitution was lifted out.
SENTINEL = "@CMDSUB@"

# Caps. Over any of these, parsing raises and the gate asks.
MAX_CHARS = 8000
MAX_WORDS = 400
MAX_SEGMENTS = 60
MAX_DEPTH = 6

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NAME_CH = re.compile(r"[A-Za-z0-9_]")

OPERATORS = (";;", "&&", "||", ";", "|", "&", "\n")
REDIR_OPS = (">>", "<<<", ">|", ">&", "<&", ">", "<")
# Redirections that WRITE. `<` and `<<<` read, and treating a read as a
# write made `cat < ~/.bashrc` a denial.
WRITE_REDIRS = (">>", ">|", ">&", ">")

# Shell control words are not commands. Without this, `if true; then rm -rf
# /; fi` reads `then` as the verb and the real command as its arguments,
# which hides it from every family check.
RESERVED = frozenset(
    ("if", "then", "else", "elif", "fi", "while", "until", "for", "do",
     "done", "case", "esac", "in", "select", "function", "coproc",
     "{", "}", "!", "[[", "]]"))


class ParseError(Exception):
    """The text could not be read with confidence. Always means ask."""


class CapExceeded(ParseError):
    """Input larger than the gate is willing to reason about."""


class Word:
    __slots__ = ("text", "provenance")

    def __init__(self, text, provenance):
        self.text = text
        self.provenance = provenance

    @property
    def literal(self):
        return self.provenance == LITERAL

    def __repr__(self):
        return f"Word({self.text!r}, {self.provenance})"

    def __eq__(self, other):
        return (isinstance(other, Word) and other.text == self.text
                and other.provenance == self.provenance)


class Segment:
    """One simple command: its words, and its redirection targets.

    Redirection targets are kept because `echo x > ~/.claude/settings.json`
    is a write to a protected file even though the verb is `echo`.
    """

    __slots__ = ("words", "redirects", "lifted")

    def __init__(self, words, redirects, lifted=False):
        self.words = words
        self.redirects = redirects
        self.lifted = lifted  # came out of a $(...) or backtick body

    def __repr__(self):
        return f"Segment({self.words!r}, redirects={self.redirects!r})"

    def command(self):
        """(name, args). name is None when provenance is not literal.

        Leading `VAR=value` assignments are skipped, so `IFS=x rm -rf /` still
        names `rm`. A name is returned only for a literal word: that is the
        whole point of tracking provenance."""
        words = list(self.words)
        while words and _ASSIGN.match(words[0].text) and words[0].literal:
            words.pop(0)
        if not words:
            return None, []
        head = words[0]
        return (head.text if head.literal else None), words[1:]

    def nonliteral(self):
        """Words whose value we cannot know without running the command."""
        return [w for w in self.words + [r[1] for r in self.redirects]
                if w.provenance in (VARIABLE, CMDSUB, ARITH, UNKNOWN)]


def _resolve(marks):
    for p in _PRECEDENCE:
        if p in marks:
            return p
    return LITERAL


def _find_close(s, i, opener, closer):
    """Index of the closer matching the opener at s[i], quote-aware.

    Raises rather than guessing. An unbalanced substitution is exactly the
    shape where guessing is worst."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ParseError("unterminated single quote")
            i = j + 1
            continue
        if c == '"':
            i = _skip_dquote(s, i)
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError(f"unbalanced {opener}{closer}")


def _skip_dquote(s, i):
    """Index just past the double-quoted run starting at s[i]."""
    i += 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i + 1
        i += 1
    raise ParseError("unterminated double quote")


class _Reader:
    def __init__(self, text, depth, budget):
        self.s = text
        self.i = 0
        self.depth = depth
        self.budget = budget
        self.segments = []
        self.words = []
        self.redirects = []
        self.buf = []
        self.marks = set()
        self.open_word = False
        self.pending_redirect = None

    # --- accumulation ---------------------------------------------------

    def _put(self, text, mark=LITERAL):
        self.buf.append(text)
        self.marks.add(mark)
        self.open_word = True

    def _end_word(self):
        if not self.open_word:
            return
        word = Word("".join(self.buf), _resolve(self.marks))
        self.buf, self.marks, self.open_word = [], set(), False
        if (word.literal and word.text in RESERVED and not self.words
                and not self.redirects and self.pending_redirect is None):
            return  # a control word, so the next word is the real verb
        self.budget["words"] += 1
        if self.budget["words"] > MAX_WORDS:
            raise CapExceeded(f"more than {MAX_WORDS} words")
        if self.pending_redirect:
            self.redirects.append((self.pending_redirect, word))
            self.pending_redirect = None
        else:
            self.words.append(word)

    def _end_segment(self):
        self._end_word()
        if self.words or self.redirects:
            self.budget["segments"] += 1
            if self.budget["segments"] > MAX_SEGMENTS:
                raise CapExceeded(f"more than {MAX_SEGMENTS} segments")
            self.segments.append(Segment(self.words, self.redirects))
        self.words, self.redirects = [], []

    # --- substitutions --------------------------------------------------

    def _sub(self, body):
        """Lift a substitution body out as its own segments."""
        self._lift(body, self.depth)
        self._put(SENTINEL, CMDSUB)

    def scan_expansions(self, body, depth):
        """Lift substitutions hiding inside an expansion body.

        `${X:-$(rm -rf /)}` and `$((1+$(rm -rf /)))` both run the inner
        command. Storing the expansion wholesale made that command invisible
        and let the nesting cap be walked straight past, so the body is
        walked with the same shared budget and the same depth limit."""
        if depth > MAX_DEPTH:
            raise CapExceeded(f"nested deeper than {MAX_DEPTH}")
        i, n = 0, len(body)
        while i < n:
            c = body[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                j = body.find("'", i + 1)
                i = n if j < 0 else j + 1
                continue
            if body.startswith("$((", i):
                j = _find_close(body, i + 2, "(", ")")
                self.scan_expansions(body[i + 3:j], depth + 1)
                i = j + 2
                continue
            if body.startswith("$(", i):
                j = _find_close(body, i + 1, "(", ")")
                self._lift(body[i + 2:j], depth)
                i = j + 1
                continue
            if body.startswith("${", i):
                j = _find_close(body, i + 1, "{", "}")
                self.scan_expansions(body[i + 2:j], depth + 1)
                i = j + 1
                continue
            if c == "`":
                j = body.find("`", i + 1)
                if j < 0:
                    raise ParseError("unterminated backtick")
                self._lift(body[i + 1:j], depth)
                i = j + 1
                continue
            i += 1

    def _lift(self, body, depth):
        if depth + 1 > MAX_DEPTH:
            raise CapExceeded(f"nested deeper than {MAX_DEPTH}")
        inner = _Reader(body, depth + 1, self.budget).run()
        for seg in inner:
            seg.lifted = True
        self.segments.extend(inner)

    def _dollar(self):
        s, i = self.s, self.i
        rest = s[i:]
        if rest.startswith("$(("):
            j = _find_close(s, i + 2, "(", ")")
            # $(( ... )) closes with two parens; the finder matched the inner.
            end = j + 1 if s[j:j + 2] == "))" else j
            self.scan_expansions(s[i + 3:j], self.depth + 1)
            self._put(s[i:end + 1], ARITH)
            self.i = end + 1
            return
        if rest.startswith("$("):
            j = _find_close(s, i + 1, "(", ")")
            self._sub(s[i + 2:j])
            self.i = j + 1
            return
        if rest.startswith("${"):
            j = _find_close(s, i + 1, "{", "}")
            self.scan_expansions(s[i + 2:j], self.depth + 1)
            self._put(s[i:j + 1], VARIABLE)
            self.i = j + 1
            return
        j = i + 1
        while j < len(s) and _NAME_CH.match(s[j]):
            j += 1
        if j == i + 1:  # a lone $, or $? $$ $! and friends
            j = i + 2 if j < len(s) and s[j] in "?$!#*@0123456789-" else i + 1
            if j == i + 1:
                self._put("$", LITERAL)
                self.i = i + 1
                return
        self._put(s[i:j], VARIABLE)
        self.i = j

    def _backtick(self):
        s, i = self.s, self.i
        j, k = i + 1, None
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == "`":
                k = j
                break
            j += 1
        if k is None:
            raise ParseError("unterminated backtick")
        self._sub(s[i + 1:k])
        self.i = k + 1

    def _dquote(self):
        """Double quotes suppress globbing and word splitting, not $ or `."""
        s = self.s
        self.i += 1
        self.open_word = True  # "" is an empty word, not nothing
        self.marks.add(LITERAL)
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < len(s) and s[self.i + 1] in '\\"$`\n':
                self._put(s[self.i + 1], LITERAL)
                self.i += 2
                continue
            if c == "$":
                self._dollar()
                continue
            if c == "`":
                self._backtick()
                continue
            self._put(c, LITERAL)
            self.i += 1
        raise ParseError("unterminated double quote")

    # --- main loop ------------------------------------------------------

    def run(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]

            if c in " \t":
                self._end_word()
                self.i += 1
                continue

            if c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("unterminated single quote")
                self._put(s[self.i + 1:j], LITERAL)
                self.i = j + 1
                continue

            if c == '"':
                self._dquote()
                continue

            if c == "\\":
                if self.i + 1 >= len(s):
                    raise ParseError("trailing backslash")
                nxt = s[self.i + 1]
                if nxt != "\n":  # a line continuation is not a word
                    self._put(nxt, LITERAL)
                self.i += 2
                continue

            if c == "$":
                self._dollar()
                continue

            if c == "`":
                self._backtick()
                continue

            op = next((o for o in OPERATORS if s.startswith(o, self.i)), None)
            if op:
                self._end_segment()
                self.i += len(op)
                continue

            if c in "()" or (c in "{}" and not self.open_word
                             and (self.i + 1 >= len(s) or s[self.i + 1] in " \t\n")):
                # A subshell or a group is a segment boundary, not a word.
                self._end_segment()
                self.i += 1
                continue

            rop = next((o for o in REDIR_OPS if s.startswith(o, self.i)), None)
            if rop:
                # A leading fd, as in `2>`, belongs to the operator.
                if self.open_word and "".join(self.buf).isdigit():
                    self.buf, self.marks, self.open_word = [], set(), False
                else:
                    self._end_word()
                self.pending_redirect = rop
                self.i += len(rop)
                continue

            if c in "*?[":
                self._put(c, GLOB)
                self.i += 1
                continue

            self._put(c, LITERAL)
            self.i += 1

        self._end_segment()
        if self.pending_redirect:
            raise ParseError("redirection with no target")
        return self.segments


# `<<EOF` and `<<-'EOF'`, but never `<<<`, which is a here-string.
_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)-?\s*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?[A-Za-z_][A-Za-z0-9_]*))")


def strip_heredocs(text):
    """(text with heredoc bodies and operators removed, [(quoted, body)]).

    A heredoc body is stdin, not a command. Reading it as shell turned
    `cat <<'EOF' >> notes.md` with a dangerous line inside into a denial,
    which is the documentation-quoting false positive this gate exists to
    remove. An unterminated heredoc raises, so it becomes ask."""
    bodies = []
    while True:
        m = _HEREDOC.search(text)
        if not m:
            return text, bodies
        raw = m.group(1) if m.group(1) is not None else (
            m.group(2) if m.group(2) is not None else m.group(3))
        # A quoted or backslashed delimiter means the body is inert text.
        quoted = (m.group(1) is not None or m.group(2) is not None
                  or raw.startswith("\\"))
        delim = raw.lstrip("\\")
        nl = text.find("\n", m.end())
        if nl < 0:
            raise ParseError("heredoc with no body")
        rest = text[nl + 1:].split("\n")
        body, tail = [], None
        for idx, line in enumerate(rest):
            if line.strip() == delim:
                tail = "\n".join(rest[idx + 1:])
                break
            body.append(line)
        if tail is None:
            raise ParseError("unterminated heredoc")
        bodies.append((quoted, "\n".join(body)))
        text = text[:m.start()] + text[m.end():nl + 1] + tail


def _parse_stdlib(command):
    if len(command) > MAX_CHARS:
        raise CapExceeded(f"longer than {MAX_CHARS} characters")
    clean, bodies = strip_heredocs(command)
    budget = {"words": 0, "segments": 0}
    segments = _Reader(clean, 0, budget).run()
    for quoted, body in bodies:
        if quoted:
            continue  # inert text, by the shell's own rules
        # An unquoted delimiter means expansions inside the body still run.
        r = _Reader("", 0, budget)
        r.scan_expansions(body, 1)
        segments.extend(r.segments)
    return segments


BACKENDS = {"stdlib": _parse_stdlib}


def parse(command, backend="stdlib"):
    """Segments, or raise ParseError. Never returns a partial read.

    An unknown backend raises rather than falling back: a silent fallback
    would let an accuracy claim stay true in the README and false on the
    machine."""
    fn = BACKENDS.get(backend)
    if fn is None:
        raise ParseError(f"unknown backend {backend!r}")
    return fn(command)


def unquoted_view(text):
    """The text with every quoted run replaced by a space.

    Best effort and never raises: this is the layer that has to survive
    input the parser rejects. A dangerous string sitting inside quotes is an
    argument, not a command, which is why the quoted run is blanked rather
    than kept."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            out.append(" ")
            continue
        if c == "'":
            j = text.find("'", i + 1)
            out.append(" ")
            i = (j + 1) if j >= 0 else n
            continue
        if c == '"':
            j, i = i, i + 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            out.append(" ")
            i = min(i + 1, n)
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --- offline self-check -------------------------------------------------

def selfcheck():
    segs = parse("ls -la")
    assert len(segs) == 1
    assert segs[0].command()[0] == "ls"

    # Chained commands must become separate segments, each with its own verb.
    segs = parse('git commit -m "wip" && rm -rf /tmp/x')
    assert [s.command()[0] for s in segs] == ["git", "rm"], segs
    # The quoted message must stay one word, and stay literal.
    assert segs[0].words[3] == Word("wip", LITERAL), segs[0].words

    # Provenance: a variable is never a command name.
    segs = parse("$CMD -rf /")
    assert segs[0].command()[0] is None, segs[0].words
    assert segs[0].words[0].provenance == VARIABLE

    # Assignments prefixing the verb must not hide it.
    segs = parse("IFS=x rm -rf /tmp/y")
    assert segs[0].command()[0] == "rm", segs[0].words

    # A substitution is lifted out as its own segment, and the enclosing
    # word keeps a sentinel rather than being split in half.
    segs = parse("echo out-$(date).txt")
    assert [s.command()[0] for s in segs] == ["date", "echo"], segs
    assert segs[1].words[1].text == f"out-{SENTINEL}.txt", segs[1].words
    assert segs[1].words[1].provenance == CMDSUB
    # And the lifted danger is visible as a command, not as text.
    segs = parse("echo `rm -rf /`")
    assert [s.command()[0] for s in segs] == ["rm", "echo"], segs
    assert segs[0].lifted is True and segs[1].lifted is False

    # Inside double quotes a substitution still substitutes; inside single
    # quotes nothing does.
    assert [s.command()[0] for s in parse('echo "$(id)"')] == ["id", "echo"]
    segs = parse("echo '$(id)'")
    assert [s.command()[0] for s in segs] == ["echo"], segs
    assert segs[0].words[1] == Word("$(id)", LITERAL), segs[0].words

    # Redirection targets are kept, and the verb is still the verb.
    segs = parse("echo hi > ~/.claude/settings.json")
    assert segs[0].command()[0] == "echo"
    assert segs[0].redirects == [(">", Word("~/.claude/settings.json", LITERAL))], \
        segs[0].redirects
    segs = parse("cmd 2>/dev/null")
    assert segs[0].command()[0] == "cmd", segs[0].words
    assert segs[0].redirects[0][0] == ">"

    # Globs are marked, so a destructive family can refuse to resolve them.
    segs = parse("rm -rf build/*")
    assert segs[0].words[2].provenance == GLOB, segs[0].words

    # Control words are not commands. Reading `then` as the verb hid the
    # real command from every family check. Found by independent review.
    segs = parse("if true; then rm -rf /; fi")
    assert [s.command()[0] for s in segs] == ["true", "rm"], segs
    assert parse("while read l; do rm -rf /tmp/x; done")[-1].command()[0] == "rm"

    # A substitution inside an expansion body still runs, so it must still
    # be lifted. Storing the expansion wholesale hid it. Same review.
    for hidden in ("echo ${X:-$(rm -rf /)}", "echo $((1+$(rm -rf /)))",
                   "echo ${X:-`rm -rf /`}"):
        assert "rm" in [s.command()[0] for s in parse(hidden)], hidden
    # And the depth cap must apply inside an expansion, not just outside it.
    try:
        parse("echo " + "$((1+" * (MAX_DEPTH + 2) + "1" + ")" * (2 * (MAX_DEPTH + 2)))
    except CapExceeded:
        pass
    else:
        raise AssertionError("nesting cap skipped inside an expansion")

    # A heredoc body is stdin, not shell. Parsing it as commands made
    # documentation that quotes a dangerous line a denial. Same review.
    segs = parse("cat <<'EOF' >> docs/notes.md\nrm -rf /\nEOF")
    assert [s.command()[0] for s in segs] == ["cat"], segs
    assert segs[0].redirects == [(">>", Word("docs/notes.md", LITERAL))], \
        segs[0].redirects
    # An unquoted delimiter still expands, so what it hides must be lifted.
    assert "rm" in [s.command()[0] for s in
                    parse("cat <<EOF\n$(rm -rf /)\nEOF")]
    # A here-string is not a heredoc.
    assert parse("cat <<< hello")[0].command()[0] == "cat"
    for bad in ("cat <<'EOF'\nno terminator\n", "cat <<EOF"):
        try:
            parse(bad)
        except ParseError:
            pass
        else:
            raise AssertionError(f"parsed {bad!r} without complaint")

    # Unbalanced quoting must raise, never return a confident half-read.
    for bad in ("echo 'x", 'echo "x', "echo $(id", "echo `id", "echo x \\"):
        try:
            parse(bad)
        except ParseError:
            pass
        else:
            raise AssertionError(f"parsed {bad!r} without complaint")

    # Caps must raise, which the gate turns into ask. Fail closed.
    for bad, exc in (("x " * (MAX_WORDS + 5), CapExceeded),
                     ("a;" * (MAX_SEGMENTS + 5), CapExceeded),
                     ("x" * (MAX_CHARS + 1), CapExceeded),
                     ("$(" * (MAX_DEPTH + 2) + ")" * (MAX_DEPTH + 2), ParseError)):
        try:
            parse(bad)
        except exc:
            pass
        else:
            raise AssertionError(f"no cap fired for {bad[:20]!r}")

    # An unknown backend must raise rather than quietly using the stdlib one.
    try:
        parse("ls", backend="tree-sitter")
    except ParseError:
        pass
    else:
        raise AssertionError("unknown backend fell back silently")

    # The raw-text layer must blank quoted runs and survive what the parser
    # rejects. This is what makes the printed-string case allowable.
    assert "rm -rf /" not in unquoted_view("""python -c 'print("rm -rf /")'""")
    assert "rm -rf /" in unquoted_view("rm -rf / # oops")
    assert unquoted_view("echo 'unterminated") == "echo  "

    print("bashparse selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(selfcheck())
