"""Terminal interface for the command-line mode: text header, arrow-key menus, masked secrets,
inline validation and live progress. Standard library only; falls back to plain text without a colour TTY."""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import textwrap
from typing import Callable, Iterable

# The tartan, as in the web assistant (installer/static/style.css).
PALETTE = {
    "green": (30, 91, 69), "red": (184, 36, 47), "gold": (232, 178, 48), "navy": (34, 48, 94),
    "plum": (107, 45, 92), "teal": (19, 129, 124), "rust": (196, 90, 28), "grey": (140, 136, 150),
}
_ANSI_256 = {"green": 29, "red": 124, "gold": 178, "navy": 18, "plum": 89, "teal": 30, "rust": 166, "grey": 245}
_KEYS = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1bOA": "up", "\x1bOB": "down",
         "\x1bOC": "right", "\x1bOD": "left", "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace",
         "\x03": "ctrl-c", "\x04": "ctrl-d", "\x15": "ctrl-u", "\t": "tab"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def split_keys(raw: str) -> list[str]:
    """Turns what a raw terminal delivers (one read may hold several keys, or a paste) into key names and chars."""
    found, i = [], 0
    while i < len(raw):
        for seq in ("\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"):
            if raw.startswith(seq, i):
                found.append(_KEYS[seq])
                i += len(seq)
                break
        else:
            ch = raw[i]
            if ch == "\x1b":  # an unknown escape sequence: skip it whole
                j = i + 1
                if j < len(raw) and raw[j] in "[O":
                    j += 1
                    while j < len(raw) and not raw[j].isalpha() and raw[j] != "~":
                        j += 1
                    j += 1
                found.append("esc")
                i = j
                continue
            found.append(_KEYS.get(ch, ch))
            i += 1
    return found


class _RawKeys:
    """Reads keys from a real terminal in raw mode, one at a time."""

    def __init__(self, stream=sys.stdin):
        self.fd = stream.fileno()
        self.pending: list[str] = []

    def __call__(self) -> str:
        import termios
        import tty
        while not self.pending:
            old = termios.tcgetattr(self.fd)
            try:
                tty.setraw(self.fd)
                data = os.read(self.fd, 64).decode(errors="ignore")
            finally:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, old)
            self.pending = split_keys(data)
        return self.pending.pop(0)


def supports_color(out) -> bool:
    return (hasattr(out, "isatty") and out.isatty() and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb")


class Term:
    def __init__(self, read_key: Callable[[], str] | None = None, out=None, color: bool | None = None):
        self.out = out or sys.stdout
        self.color = supports_color(self.out) if color is None else color
        self.read_key = read_key or _RawKeys()
        self.truecolor = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
        self.columns = shutil.get_terminal_size((80, 24)).columns  # the real width, for counting rows
        self.width = max(40, min(self.columns, 88))  # the width text is wrapped to
        self._drawn = 0  # screen rows of the widget currently shown, redrawn in place

    # ------------------------------------------------------------------ styling
    def _code(self, name: str, ground: int) -> str:
        if self.truecolor:
            r, g, b = PALETTE[name]
            return f"{ground};2;{r};{g};{b}"
        return f"{ground};5;{_ANSI_256[name]}"

    def style(self, text: str, fg: str | None = None, bg: str | None = None, bold=False, dim=False,
              underline=False) -> str:
        if not self.color:
            return text
        codes = [c for c, on in (("1", bold), ("2", dim), ("4", underline)) if on]
        if fg:
            codes.append(self._code(fg, 38))
        if bg:
            codes.append(self._code(bg, 48))
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes else text

    def write(self, text: str = "") -> None:
        self.out.write(text)
        self.out.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def wrap(self, text: str, indent: int = 2) -> list[str]:
        return textwrap.wrap(text, self.width - indent - 2) or [""]

    def pad(self, text: str, width: int) -> str:
        """Left-aligns styled text in a column: colour codes take no room on screen."""
        return text + " " * max(0, width - visible_len(text))

    # ------------------------------------------------------------------ redraw in place
    def _draw(self, lines: list[str]) -> None:
        if self.color and self._drawn:
            self.write(f"\x1b[{self._drawn}F\x1b[J")  # back to the widget's first row, clear below
        self.write("".join(line + "\n" for line in lines))
        # A line wider than the terminal wraps onto several rows: climb them all next time.
        self._drawn = sum(max(1, -(-visible_len(line) // max(1, self.columns))) for line in lines)

    def _settle(self) -> None:
        self._drawn = 0

    # ------------------------------------------------------------------ blocks
    def banner(self, title: str, subtitle: str) -> None:
        """Text only: the name, then what it does."""
        self.line()
        self.line("  " + self.style(title, bold=True))
        for chunk in self.wrap(subtitle):
            self.line("  " + self.style(chunk, dim=True))

    # ------------------------------------------------------------------ full-screen pages
    @contextlib.contextmanager
    def fullscreen(self):
        """A screen of its own, as less or htop do: questions leave no trace in the terminal's history,
        and whatever is printed after the block stays."""
        if self.color:
            self.write("\x1b[?1049h\x1b[H")
        try:
            yield
        finally:
            if self.color:
                self.write("\x1b[?1049l")
            self._drawn = 0

    def page(self, title: str, help: str = "", step: tuple[int, int] | None = None,
             recap: list[tuple[str, str]] | None = None, intro: str = "") -> None:
        """Clears the screen and draws one step: header, progress bar, what was answered, the question."""
        if self.color:
            self.write("\x1b[H\x1b[2J")
        self._drawn = 0
        self.line()
        head = self.style("Plaid", bold=True)
        if step:
            head += self.style(f"  ·  {step[0]}/{step[1]}", dim=True)
        self.line("  " + head)
        if step:
            n, total = step
            self.line("  " + "".join(self.style("━━━━━ ", fg="green") if i < n - 1 else
                                     self.style("━━━━━ ", fg="red") if i == n - 1 else
                                     self.style("╍╍╍╍╍ ", dim=True) for i in range(total)))
        for chunk in (self.wrap(intro) if intro else []):
            self.line("  " + self.style(chunk, dim=True))
        for label, value in recap or []:
            self.line("  " + self.style("✓", fg="green") + " " + self.style(f"{label} : ", dim=True) + value)
        self.line()
        self.line("  " + self.style(title, bold=True))
        for chunk in (self.wrap(help) if help else []):
            self.line("  " + self.style(chunk, dim=True))
        self.line()

    def pause(self, text: str) -> None:
        self.line()
        self.line("  " + self.style(text, dim=True))
        if self.read_key() in ("ctrl-c", "ctrl-d"):
            raise KeyboardInterrupt

    def step(self, n: int, total: int, title: str, help: str = "") -> None:
        bar = "".join(self.style("━━━━━ ", fg="green") if i < n - 1 else
                      self.style("━━━━━ ", fg="red") if i == n - 1 else self.style("╍╍╍╍╍ ", dim=True)
                      for i in range(total))
        self.line()
        self.line("  " + bar + self.style(f" {n}/{total}", dim=True))
        self.line("  " + self.style(title, bold=True))
        for chunk in (self.wrap(help) if help else []):
            self.line("  " + self.style(chunk, dim=True))

    def note(self, text: str, kind: str = "info") -> None:
        mark, color = {"ok": ("✓", "green"), "warn": ("!", "gold"), "error": ("✗", "red")}.get(kind, ("·", "grey"))
        lines = self.wrap(text, 4)
        self.line("  " + self.style(mark, fg=color, bold=True) + " " + lines[0])
        for chunk in lines[1:]:
            self.line("    " + chunk)

    def heading(self, text: str) -> None:
        self.line()
        self.line("  " + self.style(text, bold=True))

    # ------------------------------------------------------------------ questions
    def text(self, label: str, default: str = "", secret: bool = False, validate=None, hint: str = "") -> str:
        value, error = "", ""
        while True:
            shown = "•" * len(value) if secret else value
            field = shown + self.style("▌", fg="green") if value else self.style(default or " ", dim=True) + \
                self.style("▌", fg="green")
            lines = ["  " + self.style(label, bold=True) + (self.style("  " + hint, dim=True) if hint else ""),
                     "  " + self.style("›", fg="green", bold=True) + " " + field]
            if error:
                lines.append("    " + self.style(error, fg="red"))
            self._draw(lines)
            key = self.read_key()
            if key in ("ctrl-c", "ctrl-d"):
                raise KeyboardInterrupt
            if key == "enter":
                result = value or default
                error = validate(result) if validate else None
                if not error:
                    self._draw(lines[:1] + ["  " + self.style("✓", fg="green") + " " +
                                            ("•" * len(result) if secret else result)])
                    self._settle()
                    return result
                value = ""
            elif key == "backspace":
                value = value[:-1]
            elif key == "ctrl-u":
                value = ""
            elif len(key) == 1 and key.isprintable():
                value += key

    def select(self, label: str, options: Iterable, default=None, hint: str = "") -> str:
        """options: (value, label) or (value, label, colour, detail). Arrows to move, Enter to choose."""
        opts = [tuple(o) + (None, "")[len(o) - 2:] for o in options]
        index = next((i for i, o in enumerate(opts) if o[0] == default), 0)
        while True:
            lines = ["  " + self.style(label, bold=True)]
            for i, (value, text, colour, detail) in enumerate(opts):
                swatch = self.style("■", fg=colour) + " " if colour else ""
                if i == index:
                    row = self.style("❯ ", fg="green", bold=True) + swatch + self.style(text, bold=True)
                else:
                    row = "  " + swatch + text
                lines.append("  " + row + (self.style("  " + detail, dim=True) if detail and i == index else ""))
            lines.append("  " + self.style(hint, dim=True) if hint else "")
            self._draw(lines)
            key = self.read_key()
            if key in ("ctrl-c", "ctrl-d"):
                raise KeyboardInterrupt
            if key in ("up", "k"):
                index = (index - 1) % len(opts)
            elif key in ("down", "j", "tab"):
                index = (index + 1) % len(opts)
            elif key == "enter":
                self._draw([lines[0], "  " + self.style("✓", fg="green") + " " + opts[index][1]])
                self._settle()
                return opts[index][0]

    def confirm(self, question: str, yes: str, no: str, default: bool = False) -> bool:
        choice = default
        while True:
            pill = lambda text, on: (self.style(f" {text} ", fg="green", bold=True, underline=True) if on
                                     else self.style(f" {text} ", dim=True)) if self.color else (
                f"[{text}]" if on else f" {text} ")
            lines = ["  " + self.style(question, bold=True), "  " + pill(yes, choice) + "  " + pill(no, not choice)]
            self._draw(lines)
            key = self.read_key()
            if key in ("ctrl-c", "ctrl-d"):
                raise KeyboardInterrupt
            if key in ("left", "right", "tab"):
                choice = not choice
            elif key.lower() in (yes[:1].lower(), "y"):
                choice = True
            elif key.lower() in (no[:1].lower(), "n"):
                choice = False
            elif key == "enter":
                self._draw([lines[0], "  " + self.style("✓", fg="green") + " " + (yes if choice else no)])
                self._settle()
                return choice


class Steps:
    """Live list of install steps: ✓ done, spinner on the current one, sub-lines for the apps set up."""

    def __init__(self, term: Term, labels: list[tuple[str, str]]):
        self.term, self.labels = term, labels
        self.current, self.subs, self.failed, self.done, self.tick = 0, [], None, False, 0

    def render(self) -> None:
        t = self.term
        lines = []
        for i, (_, label) in enumerate(self.labels):
            if self.done or i < self.current:
                mark = t.style("✓", fg="green", bold=True)
            elif i == self.current and self.failed:
                mark = t.style("✗", fg="red", bold=True)
            elif i == self.current:
                mark = t.style(SPINNER[self.tick % len(SPINNER)] if t.color else "…", fg="gold")
            else:
                mark = t.style("·", dim=True)
            lines.append(f"  {mark} " + (t.style(label, bold=True) if i == self.current and not self.done else label))
            if i == self.current and self.subs and not self.done:
                lines += ["    " + t.style(chunk, dim=True) for chunk in t.wrap(" · ".join(self.subs), 4)]
        t._draw(lines)
        self.tick += 1

    def advance(self, key: str) -> None:
        keys = [k for k, _ in self.labels]
        if key in keys:
            self.current = keys.index(key)
            self.subs = []

    def finish(self) -> None:
        self.done = True
        self.render()
        self.term._settle()
