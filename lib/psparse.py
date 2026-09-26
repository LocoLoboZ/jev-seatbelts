#!/usr/bin/env python
"""A small, quote-aware PowerShell reader for Gate 3 (RR-18).

Same contract as bashparse, and it returns bashparse's own Word and Segment
objects, so the gate's family checks read both shells the same way. Every
word carries a provenance, a command name is returned only for a literal
word, and anything this cannot read with confidence raises ParseError,
which the gate turns into ask.

PowerShell is not Bash, which is why bashparse cannot read it:

- The backtick is the escape character and a trailing backtick continues
  the line. Backslash is a plain path character.
- Inside single quotes '' is a quote. Inside double quotes `` ` `` escapes,
  "" is a quote, and $name or $(...) expands. Curly quotes count as quotes.
- Comments are # to the end of the line and <# ... #>.
- Here-strings are @'...'@ (literal) and @"..."@ (expanding).
- $(...), @(...), (...) and {...} bodies are LIFTED OUT as their own
  segments, so the commands in a script block are checked wherever it runs.
- A statement that starts with a value ($x, 'text', 42, [Type], (...)) is
  an expression, not a command. An assignment's right-hand side is read as
  the command it is. An expression that calls a method is reported as a
  word this reader cannot resolve.
- Control keywords (if, foreach, try, ...) name no command. Their bodies
  are lifted and checked like any other.

Caps fail CLOSED, same as bashparse.

Self-check: `python lib/psparse.py` (offline, no key needed).
"""
import re
import sys

from bashparse import (CMDSUB, GLOB, LITERAL, MAX_CHARS, MAX_DEPTH,
                       MAX_SEGMENTS, MAX_WORDS, SENTINEL, UNKNOWN, VARIABLE,
                       CapExceeded, ParseError, Segment, Word, _resolve)

SQ = "'\u2018\u2019\u201a\u201b"
DQ = '"\u201c\u201d\u201e'
BLOCK = "@BLOCK@"  # what is left in a word where a {...} body was lifted

KEYWORDS = frozenset((
    "if", "elseif", "else", "switch", "foreach", "for", "while", "do",
    "until", "try", "catch", "finally", "trap", "function", "filter",
    "workflow", "configuration", "param", "begin", "process", "end",
    "dynamicparam", "clean", "class", "enum", "using", "data", "break",
    "continue", "parallel", "sequence", "inlinescript"))
# These take a pipeline after them, and that pipeline is a command.
PASS_THROUGH = frozenset(("return", "throw", "exit"))

_VAR = re.compile(r"\$(?:\{[^}\n]*\}|[A-Za-z_][\w]*(?::[\w]+)?|[?^$_])")
# `$x = ...`, `$x.Prop += ...`, `$a[0]=...` at the start of a statement.
_ASSIGN = re.compile(
    r"\$(?:\{[^}\n]*\}|[A-Za-z_][\w]*(?::[\w]+)?|_)"
    r"(?:\.\w+|\[[^\]\n]*\])*[ \t]*(?:[-+*/%]|\?\?)?=(?!=)")
_CONST = re.compile(r"\$(?:true|false|null)(?![\w:])", re.I)
_NUMBER = re.compile(
    r"^[+-]?(?:0x[0-9a-f]+|(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)"
    r"(?:kb|mb|gb|tb|pb)?[ld]?$", re.I)
_REDIR = re.compile(r"([1-6*])?(>>?)(&[12])?")


def _skip_sq(s, i):
    """Index after the single-quoted string opening at s[i]."""
    j = i + 1
    while j < len(s):
        if s[j] in SQ:
            if j + 1 < len(s) and s[j + 1] in SQ:
                j += 2
                continue
            return j + 1
        j += 1
    raise ParseError("an unterminated single-quoted string")


def _skip_dq(s, i):
    """Index after the double-quoted string opening at s[i]."""
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "`":
            j += 2
            continue
        if c == "$" and s[j + 1:j + 2] == "(":
            j = _close(s, j + 1) + 1
            continue
        if c in DQ:
            if j + 1 < len(s) and s[j + 1] in DQ:
                j += 2
                continue
            return j + 1
        j += 1
    raise ParseError("an unterminated double-quoted string")


def _herestring_end(s, i):
    """(content start, content end, index after) for a here-string at s[i]."""
    m = re.compile(r"@([" + SQ + DQ + r"])[ \t]*\r?\n").match(s, i)
    if not m:
        raise ParseError("a here-string opener not followed by a new line")
    q = SQ if m.group(1) in SQ else DQ
    end = re.compile(r"\r?\n[" + q + r"]@").search(s, m.end() - 1)
    if not end:
        raise ParseError("an unterminated here-string")
    return m.end(), end.start(), end.end()


def _close(s, i):
    """Index of the closer matching the opener at s[i], quote-aware."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack, j = [pairs[s[i]]], i + 1
    while j < len(s):
        c = s[j]
        if c == "`":
            j += 2
            continue
        if c in SQ:
            j = _skip_sq(s, j)
            continue
        if c in DQ:
            j = _skip_dq(s, j)
            continue
        if c == "@" and s[j + 1:j + 2] and s[j + 1] in SQ + DQ:
            _, _, j = _herestring_end(s, j)
            continue
        if c == "<" and s[j + 1:j + 2] == "#":
            k = s.find("#>", j + 2)
            if k < 0:
                raise ParseError("an unterminated <# comment")
            j = k + 2
            continue
        if c == "#" and (j == 0 or s[j - 1] in " \t\r\n;({|"):
            k = s.find("\n", j)
            j = len(s) if k < 0 else k
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")}]":
            if c != stack.pop():
                raise ParseError("mismatched brackets")
            if not stack:
                return j
        j += 1
    raise ParseError("an unclosed bracket")


class _Reader:
    def __init__(self, text, depth, out):
        if depth > MAX_DEPTH:
            raise CapExceeded("nesting deeper than the gate will follow")
        self.s, self.depth, self.out = text, depth, out
        self.i = 0
        self._new_statement()

    # --- state -----------------------------------------------------------

    def _new_statement(self):
        self.words, self.redirects = [], []
        self.first, self.call, self.method = None, False, False
        self.pending = None  # a redirection operator waiting for its target
        self._new_word()

    def _new_word(self):
        self.buf, self.marks, self.started = [], set(), False

    def _lift(self, body):
        self.out.extend(parse_body(body, self.depth + 1))

    def _finish_word(self):
        if not self.started:
            return
        w = Word("".join(self.buf), _resolve(self.marks))
        if self.pending:
            self.redirects.append((self.pending, w))
            self.pending = None
        else:
            if not self.words:
                self.first = self.first_char
            self.words.append(w)
            if len(self.words) > MAX_WORDS:
                raise CapExceeded("more words than the gate will read")
        self._new_word()

    def _finish_statement(self):
        self._finish_word()
        if self.pending:
            raise ParseError("a redirection with no target")
        seg = self._classify()
        if seg is not None:
            self.out.append(seg)
            if len(self.out) > MAX_SEGMENTS:
                raise CapExceeded("more commands than the gate will read")
        self._new_statement()

    def _classify(self):
        """A command segment, an expression's side effects, or nothing."""
        words, first = list(self.words), self.first
        while (not self.call and words and words[0].literal
               and words[0].text.lower() in PASS_THROUGH):
            words.pop(0)
            first = None if not words else "a"
        if not words:
            return Segment([], self.redirects) if self.redirects else None
        head = words[0].text
        if not self.call and words[0].literal and head.lower() in KEYWORDS:
            return Segment([], self.redirects) if self.redirects else None
        expression = not self.call and (
            first in SQ + DQ + "$[(@-!+" or _NUMBER.match(head)
            or head in (SENTINEL, BLOCK))
        if expression:
            if self.method:
                return Segment([Word("(an expression calling a method)",
                                     UNKNOWN)], self.redirects)
            return Segment([], self.redirects) if self.redirects else None
        return Segment(words, self.redirects)

    def _start(self, c):
        if not self.started:
            self.started, self.first_char = True, c

    # --- the reader ------------------------------------------------------

    def run(self):
        s = self.s
        if "--%" in re.split(r"\s+", s):
            raise ParseError("the --% stop-parsing token")
        while self.i < len(s):
            c = s[self.i]
            nxt = s[self.i + 1:self.i + 2]
            if c == "`":
                if nxt == "\n" or s[self.i + 1:self.i + 3] == "\r\n":
                    self._finish_word()  # a line continuation is a space
                    self.i += 2 if nxt == "\n" else 3
                    continue
                if not nxt:
                    raise ParseError("a trailing escape character")
                self._start(c)
                self.buf.append(nxt)
                self.i += 2
            elif c in " \t\r":
                self._finish_word()
                self.i += 1
            elif c in "\n;":
                self._finish_statement()
                self.i += 1
            elif c == "|":
                self._finish_statement()
                self.i += 2 if nxt == "|" else 1
            elif c == "&":
                if nxt == "&":
                    self._finish_statement()
                    self.i += 2
                elif not self.words and not self.started and not self.call:
                    self.call = True  # the call operator: a command follows
                    self.i += 1
                else:
                    self._finish_statement()  # a trailing & is a background job
                    self.i += 1
            elif c == "<" and nxt == "#" and not self.started:
                k = s.find("#>", self.i + 2)
                if k < 0:
                    raise ParseError("an unterminated <# comment")
                self._finish_word()
                self.i = k + 2
            elif c == "#" and not self.started:
                k = s.find("\n", self.i)
                self.i = len(s) if k < 0 else k
            elif c == "<":
                raise ParseError("the < operator, which PowerShell reserves")
            elif (c == ">" or (c in "123456*" and nxt == ">"
                               and not self.started)):
                self._redirect()
            elif c in SQ:
                j = _skip_sq(s, self.i)
                self._start(c)
                self.buf.append(re.sub("[" + SQ + "]{2}", "'",
                                       s[self.i + 1:j - 1]))
                self.i = j
            elif c in DQ:
                j = _skip_dq(s, self.i)
                self._start(c)
                self._expand(s[self.i + 1:j - 1])
                self.i = j
            elif c == "@" and nxt and nxt in SQ + DQ:
                a, b, j = _herestring_end(s, self.i)
                self._start(nxt)
                if nxt in SQ:
                    self.buf.append(s[a:b])
                else:
                    self._expand(s[a:b])
                self.i = j
            elif c == "@" and nxt in ("(", "{"):
                j = _close(s, self.i + 1)
                self._start("@")
                self._lift(s[self.i + 2:j])
                self.buf.append(SENTINEL if nxt == "(" else BLOCK)
                if nxt == "(":
                    self.marks.add(CMDSUB)
                self.i = j + 1
            elif c == "@" and re.match(r"\w", nxt or ""):
                m = re.compile(r"@\w+").match(s, self.i)
                self._start("@")
                self.buf.append(m.group())
                self.marks.add(VARIABLE)  # splatting
                self.i = m.end()
            elif c == "$":
                self._dollar()
            elif c == "(":
                j = _close(s, self.i)
                inside = self.started
                self._start("(")
                self._lift(s[self.i + 1:j])
                self.buf.append(SENTINEL)
                # Mid-word is a method call or an argument list.
                self.marks.add(UNKNOWN if inside else CMDSUB)
                self.method = self.method or inside
                self.i = j + 1
            elif c == "{":
                j = _close(s, self.i)
                self._start("{")
                self._lift(s[self.i + 1:j])
                self.buf.append(BLOCK)
                self.i = j + 1
            elif c in ")}]" and c != "]":
                raise ParseError(f"an unmatched {c!r}")
            else:
                self._start(c)
                if c in "*?[":
                    self.marks.add(GLOB)
                self.buf.append(c)
                self.i += 1
        self._finish_statement()

    def _redirect(self):
        self._finish_word()
        m = _REDIR.match(self.s, self.i)
        self.i = m.end()
        if m.group(3):
            return  # 2>&1 merges streams, no file is named
        self.pending = m.group(2)  # > or >>, the operators bashparse writes

    def _dollar(self):
        s, i = self.s, self.i
        nxt = s[i + 1:i + 2]
        if nxt == "(":
            j = _close(s, i + 1)
            self._start("$")
            self._lift(s[i + 2:j])
            self.buf.append(SENTINEL)
            self.marks.add(CMDSUB)
            self.i = j + 1
            return
        if not self.words and not self.started and not self.call:
            m = _ASSIGN.match(s, i)
            if m:
                self.i = m.end()  # the right-hand side is the command
                return
        m = _CONST.match(s, i)
        if m:
            self._start("$")
            self.buf.append(m.group())
            self.i = m.end()
            return
        m = _VAR.match(s, i)
        self._start("$")
        if m:
            self.buf.append(m.group())
            self.marks.add(VARIABLE)
            self.i = m.end()
        else:
            self.buf.append("$")
            self.i += 1

    def _expand(self, body):
        """The text of an expanding string, with its expansions marked."""
        k = 0
        while k < len(body):
            c = body[k]
            if c == "`" and k + 1 < len(body):
                self.buf.append(body[k + 1])
                k += 2
            elif c in DQ and body[k + 1:k + 2] and body[k + 1] in DQ:
                self.buf.append('"')
                k += 2
            elif c == "$" and body[k + 1:k + 2] == "(":
                j = _close(body, k + 1)
                self._lift(body[k + 2:j])
                self.buf.append(SENTINEL)
                self.marks.add(CMDSUB)
                k = j + 1
            elif c == "$" and _VAR.match(body, k) and not _CONST.match(body, k):
                m = _VAR.match(body, k)
                self.buf.append(m.group())
                self.marks.add(VARIABLE)
                k = m.end()
            else:
                self.buf.append(c)
                k += 1


def parse_body(text, depth):
    out = []
    reader = _Reader(text, depth, out)
    reader.run()
    for seg in out:
        if depth:
            seg.lifted = True
    return out


def parse(command):
    """Segments for one PowerShell command line. Raises ParseError."""
    if not isinstance(command, str):
        raise ParseError("the command is not a string")
    if len(command) > MAX_CHARS:
        raise CapExceeded("a command longer than the gate will read")
    return parse_body(command, 0)


# --- offline self-check -------------------------------------------------

def _cmds(text):
    return [(seg.command()[0], [w.text for w in seg.command()[1]])
            for seg in parse(text) if seg.words]


def selfcheck():
    assert _cmds("Get-ChildItem") == [("Get-ChildItem", [])]
    assert _cmds("git status; git log -1") == [
        ("git", ["status"]), ("git", ["log", "-1"])]
    # Backslash is a path character, the backtick escapes.
    assert _cmds(r"Remove-Item C:\tmp\x -Recurse") == [
        ("Remove-Item", [r"C:\tmp\x", "-Recurse"])]
    assert _cmds("Write-Host a`;b") == [("Write-Host", ["a;b"])]
    assert _cmds("Remove-Item `\n  -Recurse x") == [
        ("Remove-Item", ["-Recurse", "x"])]
    assert _cmds("Remove-Item `\r\n x") == [("Remove-Item", ["x"])]
    # Quotes.
    assert _cmds("Write-Host 'it''s' \"a\"\"b\"") == [
        ("Write-Host", ["it's", 'a"b'])]
    assert _cmds("Write-Host \u2018x\u2019") == [("Write-Host", ["x"])]
    seg = parse('Write-Host "hi $name"')[0]
    assert seg.words[1].provenance == VARIABLE
    assert parse("Write-Host '$name'")[0].words[1].literal
    assert parse("Remove-Item -Recurse:$true x")[0].words[1].literal
    # Pipelines, && and ||, comments.
    assert [c[0] for c in _cmds("a | b && c || d")] == ["a", "b", "c", "d"]
    assert _cmds("git status # rm -r x") == [("git", ["status"])]
    assert _cmds("<# Remove-Item x #> git status") == [("git", ["status"])]
    assert _cmds("Write-Host a#b") == [("Write-Host", ["a#b"])]
    # The call operator names the command, quoted or not.
    assert _cmds(r"& 'C:\Program Files\Git\bin\git.exe' push") == [
        (r"C:\Program Files\Git\bin\git.exe", ["push"])]
    assert parse("& $tool -x")[0].command()[0] is None
    # Bodies are lifted: script blocks, subexpressions, grouping.
    got = _cmds("Get-ChildItem | ForEach-Object { Remove-Item $_ -Recurse }")
    assert ("Remove-Item", ["$_", "-Recurse"]) in got, got
    assert ("Remove-Item", ["-Recurse", "x"]) in _cmds(
        "Write-Host $(Remove-Item -Recurse x)")
    assert ("Remove-Item", ["x"]) in _cmds('Write-Host "a $(Remove-Item x)"')
    assert ("Remove-Item", ["x"]) in _cmds("Write-Host (Remove-Item x)")
    assert ("Remove-Item", ["x"]) in _cmds("if ($a) { Remove-Item x }")
    assert all(s.lifted for s in parse("Write-Host (Remove-Item x)")
               if s.words and s.words[0].text == "Remove-Item")
    # Expressions are not commands, assignments are.
    assert _cmds("$x = 5") == []
    assert _cmds("$x.Count -gt 1") == []
    assert _cmds("Get-Item x | Where-Object { $_.Length -gt 1kb }") == [
        ("Get-Item", ["x"]), ("Where-Object", [BLOCK])]
    assert _cmds("$x = Remove-Item -Recurse C:\\") == [
        ("Remove-Item", ["-Recurse", "C:\\"])]
    assert _cmds("$env:X=Get-Date") == [("Get-Date", [])]
    assert _cmds("'text' | Out-File x.txt") == [("Out-File", ["x.txt"])]
    assert _cmds("return Remove-Item x") == [("Remove-Item", ["x"])]
    # A method call is a word this cannot resolve.
    for m in ("[IO.Directory]::Delete('C:\\', $true)", "$f.Delete()",
              "(New-Object Net.WebClient).DownloadString('u')"):
        segs = [s for s in parse(m) if s.words]
        assert any(s.words[0].provenance == UNKNOWN for s in segs), m
    # Redirections, including one on an expression.
    seg = parse("'x' > ~/.bashrc")[0]
    assert seg.words == [] and seg.redirects[0][1].text == "~/.bashrc"
    seg = parse("Get-Date 2>&1 >> log.txt")[0]
    assert seg.redirects == [(">>", Word("log.txt", LITERAL))]
    assert parse("Get-Date *> out.txt")[0].redirects[0][0] == ">"
    # Here-strings.
    assert _cmds("Set-Content x @'\nrm -rf /\n'@") == [
        ("Set-Content", ["x", "rm -rf /"])]
    assert parse('Write-Host @"\n$x\n"@')[0].words[1].provenance == VARIABLE
    # Numbers are values, a digit-led name is a command.
    assert _cmds("7z x a.zip") == [("7z", ["x", "a.zip"])]
    assert _cmds("42") == []
    # Refusals: every one of these must raise, never guess.
    for bad in ("Write-Host 'x", 'Write-Host "x', "Write-Host (x",
                "Write-Host x)", "Get-Content < x", "cmd --% /c dir",
                "Write-Host x`", "<# never closed", "Set-Content x @'\nno end",
                "(" * 40 + ")" * 40, "x" * (MAX_CHARS + 1)):
        try:
            parse(bad)
        except ParseError:
            continue
        raise AssertionError(f"accepted {bad[:30]!r}")
    try:
        parse(None)
    except ParseError:
        pass
    else:
        raise AssertionError("accepted a non-string")
    print("psparse selfcheck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(selfcheck())
