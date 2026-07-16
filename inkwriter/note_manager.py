"""
Quick note system: Ctrl+G anywhere opens a small overlay to jot a thought.
Notes are saved as individual timestamped .txt files in ~/Documents/inkwriter/notes/

Ctrl+S  = save and close
Ctrl+G  = save and close (same key used to open it — tap again to save)
Escape  = cancel without saving
Enter   = newline (use Ctrl+S or Ctrl+G to save)
"""

import curses
import time
from datetime import datetime
from pathlib import Path


class NoteManager:
    def __init__(self, config):
        self.config = config

    def save_note(self, content):
        """Save a note. Returns the Path, or None if content was empty."""
        content = content.strip()
        if not content:
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        hint = content[:30].replace("\n", " ").replace("/", "-").strip()
        hint = "".join(c if c.isalnum() or c in (' ', '-', '_') else '' for c in hint)
        hint = hint.strip().replace(" ", "_")[:20] or "note"
        filename = f"{ts}_{hint}.txt"
        path = self.config.notes_dir / filename
        self.config.notes_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def list_notes(self):
        """Return list of note paths, newest first."""
        return sorted(
            self.config.notes_dir.glob("*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

    def delete_note(self, path):
        """Move a note to notes_dir/.trash instead of permanently deleting."""
        path = Path(path)
        trash = self.config.notes_dir / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / f"{path.stem}_{int(time.time())}.txt"
        path.rename(dest)


class NoteEditor:
    """
    Minimal overlay editor for a quick note.

    Keys:
      Ctrl+S or Ctrl+G  — save and close
      Escape            — cancel without saving
      Enter             — insert newline
      Arrows            — move cursor
      Backspace         — delete
    """

    def __init__(self, stdscr, note_manager, config, display=None):
        self.stdscr = stdscr
        self.nm = note_manager
        self.config = config
        self.display = display          # may be None in pure-terminal mode
        self.lines = [""]
        self.cursor_row = 0
        self.cursor_col = 0
        self.status_msg = "^S/^G Save  Esc Cancel"

    def run(self):
        """Show note overlay. Returns saved Path or None."""
        height, width = self.stdscr.getmaxyx()
        box_h = min(12, height - 4)
        box_w = min(60, width - 4)
        box_y = (height - box_h) // 2
        box_x = (width - box_w) // 2

        while True:
            self._draw_overlay(box_y, box_x, box_h, box_w)
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                continue

            c = key if isinstance(key, int) else ord(key) if isinstance(key, str) else -1

            if c == 27:                          # Escape — cancel
                return None

            elif c in (19, 7):                   # Ctrl+S or Ctrl+G — save
                path = self._save()
                if path:
                    self.status_msg = f"Saved: {path.name}"
                else:
                    self.status_msg = "Nothing to save."
                self._draw_overlay(box_y, box_x, box_h, box_w)
                time.sleep(1.2)
                return path

            elif c in (curses.KEY_BACKSPACE, 127, 8):
                self._backspace()

            elif c in (10, 13):                  # Enter — newline
                self._insert_newline()

            elif isinstance(key, str) and key.isprintable():
                self._insert(key)
            elif isinstance(key, int) and 32 <= key <= 126:
                self._insert(chr(key))

            elif c == curses.KEY_UP:
                if self.cursor_row > 0:
                    self.cursor_row -= 1
                    self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))
            elif c == curses.KEY_DOWN:
                if self.cursor_row < len(self.lines) - 1:
                    self.cursor_row += 1
                    self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))
            elif c == curses.KEY_LEFT:
                if self.cursor_col > 0:
                    self.cursor_col -= 1
            elif c == curses.KEY_RIGHT:
                if self.cursor_col < len(self.lines[self.cursor_row]):
                    self.cursor_col += 1

    # -------------------------------------------------------------------------
    # Text manipulation
    # -------------------------------------------------------------------------

    def _insert(self, ch):
        row, col = self.cursor_row, self.cursor_col
        line = self.lines[row]
        self.lines[row] = line[:col] + ch + line[col:]
        self.cursor_col += 1

    def _backspace(self):
        row, col = self.cursor_row, self.cursor_col
        if col > 0:
            self.lines[row] = self.lines[row][:col-1] + self.lines[row][col:]
            self.cursor_col -= 1
        elif row > 0:
            prev = self.lines[row - 1]
            self.cursor_col = len(prev)
            self.lines[row - 1] = prev + self.lines[row]
            del self.lines[row]
            self.cursor_row -= 1

    def _insert_newline(self):
        row, col = self.cursor_row, self.cursor_col
        current = self.lines[row]
        self.lines[row] = current[:col]
        self.lines.insert(row + 1, current[col:])
        self.cursor_row += 1
        self.cursor_col = 0

    def _save(self):
        content = "\n".join(self.lines)
        return self.nm.save_note(content)

    # -------------------------------------------------------------------------
    # Drawing
    # -------------------------------------------------------------------------

    def _draw_overlay(self, by, bx, bh, bw):
        stdscr = self.stdscr
        inner_w = bw - 2
        # Rows available for text: box_h minus top border, title row,
        # divider, bottom hint row, bottom border = 5 rows of chrome
        inner_h = bh - 5

        try:
            # Top border
            stdscr.addstr(by,     bx, "+" + "-" * (bw - 2) + "+")
            # Title row
            title = "| QUICK NOTE"
            stdscr.addstr(by + 1, bx, (title + " " * (bw - len(title) - 1))[:bw - 1] + "|")
            # Divider
            stdscr.addstr(by + 2, bx, "+" + "-" * (bw - 2) + "+")
            # Text rows
            for row in range(inner_h):
                stdscr.addstr(by + 3 + row, bx, "|" + " " * (bw - 2) + "|")
            # Hint row
            hint = "| " + self.status_msg
            stdscr.addstr(by + 3 + inner_h, bx,
                          (hint + " " * (bw - len(hint) - 1))[:bw - 1] + "|")
            # Bottom border
            stdscr.addstr(by + bh - 1, bx, "+" + "-" * (bw - 2) + "+")
        except curses.error:
            pass

        # Text content inside box
        for i in range(inner_h):
            line = self.lines[i] if i < len(self.lines) else ""
            line = line[:inner_w]
            try:
                stdscr.addstr(by + 3 + i, bx + 1, line.ljust(inner_w)[:inner_w])
            except curses.error:
                pass

        # Cursor position
        cy = by + 3 + self.cursor_row
        cx = bx + 1 + self.cursor_col
        try:
            stdscr.move(cy, cx)
        except curses.error:
            pass

        stdscr.refresh()
        if self.display:
            self.display.paint_from_curses(stdscr)
            self.display.refresh(self.display.panel_region)


class NotesBrowser:
    """
    Browse saved quick notes and open one in the full Editor to view/edit it.

    Keys:
        Up/Down     = navigate
        Enter       = open note in the editor
        Ctrl+X      = delete note (moved to notes_dir/.trash)
        Backspace/Esc = back to file browser
    """

    def __init__(self, stdscr, note_manager, config, display=None):
        self.stdscr = stdscr
        self.nm = note_manager
        self.config = config
        self.display = display          # may be None in pure-terminal mode
        self.selected = 0
        self.message = ""
        self.message_time = 0

    def run(self):
        """
        Returns:
          Path - open this note in the editor
          None - go back to file browser
        """
        while True:
            notes = self.nm.list_notes()
            if notes:
                self.selected = min(self.selected, len(notes) - 1)
            self._draw(notes)
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue

            c = key if isinstance(key, int) else ord(key) if isinstance(key, str) and key else -1

            if c == curses.KEY_UP or key == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
            elif c == curses.KEY_DOWN or key == curses.KEY_DOWN:
                self.selected = min(len(notes) - 1, self.selected + 1) if notes else 0
            elif c in (10, 13, curses.KEY_ENTER):
                if notes:
                    return notes[self.selected]
            elif c == 24:   # Ctrl+X  delete
                if notes and self.selected < len(notes):
                    self.nm.delete_note(notes[self.selected])
                    self.selected = max(0, self.selected - 1)
                    self._set_message("Moved to trash.")
            elif c in (curses.KEY_BACKSPACE, 127) or c == 27:
                return None

    def _draw(self, notes):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        list_height = height - 3

        stdscr.erase()

        title = f" NOTES  ({len(notes)}) "
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(0, 0, title.ljust(width)[:width - 1])
        stdscr.attroff(curses.color_pair(2))

        if not notes:
            stdscr.addstr(2, 2, "No notes yet.  Ctrl+G from anywhere to jot one.")
        else:
            scroll = max(0, min(self.selected - list_height + 1, len(notes) - list_height))
            scroll = max(0, scroll)
            for i in range(list_height):
                idx = scroll + i
                if idx >= len(notes):
                    break
                path = notes[idx]
                preview = self._preview(path)
                mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                label = f"{mtime}  {preview}"
                y = i + 1
                if idx == self.selected:
                    marked = ("> " + label)[:width - 1]
                    try:
                        stdscr.addstr(y, 0, marked, curses.A_UNDERLINE)
                    except curses.error:
                        pass
                else:
                    try:
                        stdscr.addstr(y, 0, ("  " + label)[:width - 1])
                    except curses.error:
                        pass

        if self.message and time.time() - self.message_time < 3:
            status = f" {self.message} "
        else:
            self.message = ""
            status = " Enter=Open  ^X Delete  Backspace/Esc=Back "

        stdscr.attron(curses.color_pair(2))
        try:
            stdscr.addstr(height - 1, 0, status.ljust(width)[:width - 1])
        except curses.error:
            pass
        stdscr.attroff(curses.color_pair(2))

        stdscr.refresh()
        if self.display:
            self.display.paint_from_curses(stdscr)
            self.display.refresh(self.display.panel_region)

    def _preview(self, path):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            first_line = content.split("\n", 1)[0]
            return first_line[:60] if first_line else "(empty)"
        except Exception:
            return path.stem

    def _set_message(self, msg):
        self.message = msg
        self.message_time = time.time()
