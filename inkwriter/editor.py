"""
Full-screen text editor with word wrap, autosave, and keyboard shortcuts.
Designed for e-ink displays: minimal redraws, clean layout.

Changes vs original
-------------------
- Editor now accepts an optional `display` parameter (Display instance).
- After each character insertion / deletion the editor calls
  display.refresh(region) with the approximate pixel rectangle of the
  changed cursor area so the e-ink panel only refreshes that box.
- _draw() passes a region tuple to display.refresh() covering the full
  text area on non-character events (scrolling, full redraw).
- FileManager is instantiated once at __init__ time (not re-created on
  every save / load / word-count call) to avoid repeated FS overhead.
- Ctrl+N now correctly returns "new_file" instead of going to browser.
"""

import curses
import time
import threading
from pathlib import Path

from .display import CELL_W, CELL_H

_TITLE_BAR_PX = CELL_H   # one line reserved for title


class Editor:
    """
    A simple terminal text editor optimised for e-ink + keyboard-only use.

    Key bindings (all Ctrl-based, no mouse):
        Ctrl+S  = Save
        Ctrl+Q  = Quit to browser
        Ctrl+G  = Quick note
        Ctrl+T  = Type-out mode
        Ctrl+W  = Word count
        Ctrl+R  = Rename file
        F1      = Help
        Ctrl+N  = New file
        Ctrl+O  = Open browser
        Ctrl+P  = Power off (press twice within 5s to confirm)
        Arrow keys, Home, End, PgUp, PgDn = navigation
    """

    def __init__(self, stdscr, filepath, config, display=None, progress=None):
        self.stdscr = stdscr
        self.filepath = Path(filepath)
        self.config = config
        self.display = display          # may be None in pure-terminal mode
        self._progress = progress       # may be None (progress tracking disabled)
        self.lines = [""]
        self.cursor_row = 0
        self.cursor_col = 0
        self.scroll_offset = 0
        self.modified = False
        self.last_save = time.time()
        self.message = ""
        self.message_time = 0
        self._char_refresh_done = False
        self._pending_char_region = None
        self._shutdown_armed_until = 0

        # Single FileManager for the lifetime of this editor
        from .file_manager import FileManager
        self._fm = FileManager(config)

        self._load()
        # Baseline word count for delta tracking on save -- see _save().
        # Only ever counts increases, so deleting text you wrote earlier
        # can't claw back word-count/streak credit.
        self._last_saved_words = self._fm.word_count(self._get_content())

        if self._progress:
            self._progress.record_session()

        if config.autosave:
            self._start_autosave()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self):
        """Main loop. Returns action string for app controller."""
        self.stdscr.clear()
        self._draw()

        while True:
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue

            self._char_refresh_done = False
            self._pending_char_region = None
            action = self._handle_key(key)
            if action:
                return action

            # _draw() always repaints the e-ink buffer, but only pushes the
            # small precomputed region to hardware when this key was a
            # same-line character edit (set via _notify_char_change) --
            # otherwise every keystroke would drive the panel twice, which
            # both slows typing and doubles how often the "full refresh
            # every N partials" counter fires.
            self._draw()

    def save(self):
        """Public save (for signal handlers etc)."""
        self._save()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def _handle_key(self, key):
        c = key if isinstance(key, int) else (ord(key) if isinstance(key, str) and len(key) == 1 else -1)

        # --- Control keys ---
        if c == 19:    # Ctrl+S
            self._save()
            self._set_message("Saved.")
        elif c == 17:  # Ctrl+Q
            if self.modified:
                self._save()
            self._stop_autosave()
            self._show_exit_summary()
            return "browser"
        elif c == 14:  # Ctrl+N
            if self.modified:
                self._save()
            self._stop_autosave()
            return "new_file"
        elif c == 15:  # Ctrl+O
            if self.modified:
                self._save()
            self._stop_autosave()
            self._show_exit_summary()
            return "browser"
        elif c == 7:   # Ctrl+G  quick note
            return "note"
        elif c == 20:  # Ctrl+T  type out
            if self.modified:
                self._save()
            return "type_out"
        elif c == 23:  # Ctrl+W  word count
            content = self._get_content()
            wc = self._fm.word_count(content)
            cc = self._fm.char_count(content)
            self._set_message(f"Words: {wc}  Chars: {cc}")
        elif c == 5:   # Ctrl+E  rename  (Ctrl+R is reserved by the terminal)
            self._rename_interactive()
        elif c == curses.KEY_F1:  # F1  help  (Ctrl+H is indistinguishable from
                                   # Backspace on most terminals — ASCII 8 is
                                   # literally the Backspace byte on many
                                   # terminfo entries, so it can never be
                                   # reliably bound to a different action)
            self._show_help()
        elif c == 16:  # Ctrl+P  power off (press twice within 5s to confirm)
            result = self._handle_shutdown_key()
            if result:
                if self.modified:
                    self._save()
                self._stop_autosave()
                return result

        # --- Navigation ---
        elif c == curses.KEY_UP or key == curses.KEY_UP:
            self._move_up()
        elif c == curses.KEY_DOWN or key == curses.KEY_DOWN:
            self._move_down()
        elif c == curses.KEY_LEFT or key == curses.KEY_LEFT:
            self._move_left()
        elif c == curses.KEY_RIGHT or key == curses.KEY_RIGHT:
            self._move_right()
        elif c == curses.KEY_HOME or key == curses.KEY_HOME:
            self.cursor_col = 0
        elif c == curses.KEY_END or key == curses.KEY_END:
            self.cursor_col = len(self.lines[self.cursor_row])
        elif c == curses.KEY_PPAGE:
            self._page_up()
        elif c == curses.KEY_NPAGE:
            self._page_down()

        # --- Editing ---
        elif c in (curses.KEY_BACKSPACE, 127):
            structural = self._backspace()
            # A backspace at column 0 merges the current line into the
            # previous one -- every line from there on shifts up a row,
            # not just the cursor's cell. Falling through without calling
            # _notify_char_change() means _draw() takes its normal
            # full-text-area refresh path instead of the tiny single-cell
            # one, so the shifted lines actually get redrawn.
            if not structural:
                self._notify_char_change()
        elif c == curses.KEY_DC:
            structural = self._delete_forward()
            if not structural:
                self._notify_char_change()
        elif c in (10, 13):
            self._insert_newline()
            # Always structural (splits the line, shifts everything after
            # it down a row) -- same reasoning as the backspace-merge case
            # above, so no _notify_char_change() here either.
        elif c == 9:
            self._insert_text(" " * self.config.tab_size)
            self._notify_char_change()
        elif isinstance(key, str) and key.isprintable():
            self._insert_text(key)
            self._notify_char_change()
        elif isinstance(key, int) and 32 <= key <= 126:
            self._insert_text(chr(key))
            self._notify_char_change()

        return None

    # ------------------------------------------------------------------
    # Text manipulation
    # ------------------------------------------------------------------

    def _insert_text(self, text):
        row, col = self.cursor_row, self.cursor_col
        line = self.lines[row]
        self.lines[row] = line[:col] + text + line[col:]
        self.cursor_col += len(text)
        self.modified = True

    def _backspace(self):
        """Returns True if this merged two lines (structural edit --
        everything below shifts up a row), False if it only shortened the
        current line in place."""
        row, col = self.cursor_row, self.cursor_col
        structural = False
        if col > 0:
            self.lines[row] = self.lines[row][:col-1] + self.lines[row][col:]
            self.cursor_col -= 1
        elif row > 0:
            prev = self.lines[row - 1]
            self.cursor_col = len(prev)
            self.lines[row - 1] = prev + self.lines[row]
            del self.lines[row]
            self.cursor_row -= 1
            structural = True
        self.modified = True
        return structural

    def _delete_forward(self):
        """Returns True if this merged two lines (structural edit), False
        if it only shortened the current line in place."""
        row, col = self.cursor_row, self.cursor_col
        structural = False
        if col < len(self.lines[row]):
            self.lines[row] = self.lines[row][:col] + self.lines[row][col+1:]
        elif row < len(self.lines) - 1:
            self.lines[row] = self.lines[row] + self.lines[row + 1]
            del self.lines[row + 1]
            structural = True
        self.modified = True
        return structural

    def _insert_newline(self):
        row, col = self.cursor_row, self.cursor_col
        current = self.lines[row]
        self.lines[row] = current[:col]
        self.lines.insert(row + 1, current[col:])
        self.cursor_row += 1
        self.cursor_col = 0
        self.modified = True

    # ------------------------------------------------------------------
    # Cursor movement
    # ------------------------------------------------------------------

    def _move_up(self):
        if self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))

    def _move_down(self):
        if self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))

    def _move_left(self):
        if self.cursor_col > 0:
            self.cursor_col -= 1
        elif self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = len(self.lines[self.cursor_row])

    def _move_right(self):
        if self.cursor_col < len(self.lines[self.cursor_row]):
            self.cursor_col += 1
        elif self.cursor_row < len(self.lines) - 1:
            self.cursor_row += 1
            self.cursor_col = 0

    def _page_up(self):
        h = self._text_height()
        self.cursor_row = max(0, self.cursor_row - h)
        self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))

    def _page_down(self):
        h = self._text_height()
        self.cursor_row = min(len(self.lines) - 1, self.cursor_row + h)
        self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        text_height = height - 2

        self._scroll_to_cursor(text_height, width)
        stdscr.erase()

        # Title bar. Long filenames on narrow panels (2.13" HAT) truncate
        # from the front instead of the back -- the tail of a filename
        # (a date suffix, a "_v2", etc.) is usually what disambiguates it
        # from similarly-named files, so that's the part worth keeping
        # visible. (A true two-line title bar would need a second reserved
        # row, which would mean touching CELL_H/_TITLE_BAR_PX -- the same
        # constants the partial-refresh pixel math depends on -- so this
        # smarter single-line truncation gets most of the benefit for
        # much less risk.)
        title = self.filepath.stem + (" *" if self.modified else "")
        max_title = width - 2
        if len(title) > max_title > 3:
            title = "..." + title[-(max_title - 3):]
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(0, 0, f" {title} ".ljust(width)[:width-1])
        stdscr.attroff(curses.color_pair(2))

        # Text area
        for screen_row in range(text_height):
            line_idx = self.scroll_offset + screen_row
            if line_idx >= len(self.lines):
                break
            line = self.lines[line_idx][:width - 1]
            # Underline just the current line so it's easy to spot after a
            # scroll or partial refresh -- deliberately not a full inverted
            # block: that would toggle a whole extra row of pixels on
            # every cursor move, which costs more on e-ink than a thin
            # underline for no real legibility gain.
            attr = curses.A_UNDERLINE if line_idx == self.cursor_row else curses.A_NORMAL
            try:
                stdscr.addstr(screen_row + 1, 0, line, attr)
            except curses.error:
                pass

        # Status bar
        if self.message and time.time() - self.message_time < 3:
            status = f" {self.message} "
        else:
            self.message = ""
            wc = len(self._get_content().split())
            today_part = ""
            if self._progress and self.config.show_today_word_count:
                today_part = f"  Today {self._progress.words_today}"
            status = (f" Ln {self.cursor_row+1}  Col {self.cursor_col+1}"
                      f"  Words {wc}{today_part} | ^S Save  ^Q Quit  ^G Note  ^E Rename  F1 Help ")

        stdscr.attron(curses.color_pair(2))
        try:
            stdscr.addstr(height - 1, 0, status.ljust(width)[:width-1])
        except curses.error:
            pass
        stdscr.attroff(curses.color_pair(2))

        # Cursor
        screen_cursor_row = (self.cursor_row - self.scroll_offset) + 1
        screen_cursor_col = min(self.cursor_col, width - 1)
        try:
            stdscr.move(screen_cursor_row, screen_cursor_col)
        except curses.error:
            pass

        stdscr.refresh()

        # Mirror onto the e-ink panel. Painting into the buffer (cheap,
        # CPU-only) always happens so the panel's software copy matches
        # what curses just showed; only the *hardware* push region varies:
        # a single same-line character edit uses the small precomputed
        # cell-to-end-of-row rectangle from _notify_char_change(), any
        # other redraw (scrolling, structural edits, opening a file, a
        # status message appearing/expiring) uses the whole panel so
        # nothing stale is left on screen.
        if self.display:
            self.display.paint_from_curses(stdscr)
            if self._char_refresh_done and self._pending_char_region:
                region = self._pending_char_region
            else:
                region = self.display.panel_region
            self.display.refresh(region)

    def _notify_char_change(self):
        """
        Called after a same-line character edit (insert or in-line
        delete/backspace, not a structural line split/merge). Computes the
        pixel rectangle from the cursor's column to the end of that screen
        row -- an insert/delete shifts every character after the cursor on
        that line, not just the cell the cursor sits on -- and stashes it
        for _draw() to use as the hardware refresh region instead of the
        whole panel.
        """
        self._char_refresh_done = True
        self._pending_char_region = None

        if self.display is None or not self.display.is_eink:
            return

        _, width = self.stdscr.getmaxyx()
        text_height = self._text_height()
        self._scroll_to_cursor(text_height, width)

        screen_row = (self.cursor_row - self.scroll_offset) + 1  # +1 for title bar
        # One cell before the cursor too, in case a fallback proportional
        # font's glyph overhangs slightly into the previous cell.
        start_col = max(0, min(self.cursor_col, width - 1) - 1)

        px = start_col * CELL_W
        py = (screen_row - 1) * CELL_H + _TITLE_BAR_PX   # -1 because screen_row starts at 1
        pw = (width - start_col) * CELL_W
        ph = CELL_H

        self._pending_char_region = (px, py, pw, ph)

    def _scroll_to_cursor(self, text_height, width):
        if self.cursor_row < self.scroll_offset:
            self.scroll_offset = self.cursor_row
        elif self.cursor_row >= self.scroll_offset + text_height:
            self.scroll_offset = self.cursor_row - text_height + 1

    def _text_height(self):
        height, _ = self.stdscr.getmaxyx()
        return height - 2

    # ------------------------------------------------------------------
    # Save / Load
    # ------------------------------------------------------------------

    def _save(self):
        self._fm.save_file(self.filepath, self._get_content())
        self.modified = False
        self.last_save = time.time()

        if not self._progress:
            return

        if self.config.remember_cursor_position:
            self._progress.set_cursor(
                self.filepath, self.cursor_row, self.cursor_col, self.scroll_offset
            )

        current_words = self._fm.word_count(self._get_content())
        delta = current_words - self._last_saved_words
        if delta > 0 and self.config.growth_enabled:
            milestone = self._progress.record_words(delta)
            if milestone and not self.config.show_milestones:
                # Milestones disabled specifically -- still record the
                # word/streak data above, just don't queue the screen.
                self._progress.pop_pending_milestone()
        self._last_saved_words = current_words

        self._progress.save()

    def _load(self):
        content = self._fm.load_file(self.filepath)
        self.lines = content.split("\n") if content else [""]
        if not self.lines:
            self.lines = [""]

        if self._progress and self.config.remember_cursor_position:
            pos = self._progress.get_cursor(self.filepath)
            if pos:
                row, col, scroll = pos
                self.cursor_row = max(0, min(row, len(self.lines) - 1))
                self.cursor_col = max(0, min(col, len(self.lines[self.cursor_row])))
                self.scroll_offset = max(0, min(scroll, len(self.lines) - 1))

    def _get_content(self):
        return "\n".join(self.lines)

    # ------------------------------------------------------------------
    # Autosave
    # ------------------------------------------------------------------

    def _start_autosave(self):
        self._autosave_stop = threading.Event()
        self._autosave_thread = threading.Thread(target=self._autosave_loop, daemon=True)
        self._autosave_thread.start()

    def _stop_autosave(self):
        if hasattr(self, '_autosave_stop'):
            self._autosave_stop.set()

    def _autosave_loop(self):
        interval = self.config.auto_save_interval
        while not self._autosave_stop.wait(interval):
            if self.modified:
                self._save()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_message(self, msg):
        self.message = msg
        self.message_time = time.time()

    def _handle_shutdown_key(self):
        """
        Two-press confirm (same pattern as the file browser) instead of a
        modal dialog -- no popups anywhere in this app, but a destructive
        system action still deserves more friction than one keypress.
        """
        now = time.time()
        if now < self._shutdown_armed_until:
            return "shutdown"
        self._shutdown_armed_until = now + 5
        self._set_message("Press Ctrl+P again within 5s to power off")
        return None

    def _show_exit_summary(self):
        """
        Phase 2.2: a short, non-modal summary shown right as you leave the
        editor back to the browser. Reuses the existing status-bar message
        mechanic (no new UI). Only fires if you actually wrote something
        today -- an empty-session summary would just be noise.
        """
        if not (self._progress and self.config.show_session_summary):
            return
        summary = self._progress.session_summary_text()
        if not summary:
            return
        self._set_message(summary)
        self._draw()
        time.sleep(1.8)

    def _rename_interactive(self):
        height, width = self.stdscr.getmaxyx()
        self.stdscr.addstr(height - 1, 0, " New name: ".ljust(width)[:width-1], curses.color_pair(2))
        curses.echo()
        curses.curs_set(1)
        try:
            name = self.stdscr.getstr(height - 1, 11, 50).decode("utf-8").strip()
        except Exception:
            name = ""
        curses.noecho()
        curses.raw()   # restore raw mode — echo() drops it, leaving Ctrl keys broken
        if name:
            new_path = self._fm.rename_file(self.filepath, name)
            self.filepath = new_path
            self._set_message(f"Renamed to {new_path.stem}")

    def _show_help(self):
        help_text = [
            "INKWRITER KEYBOARD SHORTCUTS",
            "",
            "  Ctrl+S    Save",
            "  Ctrl+Q    Quit / back to files",
            "  Ctrl+N    New document",
            "  Ctrl+O    Open file browser",
            "  Ctrl+G    Quick note",
            "  Ctrl+T    Type out to computer",
            "  Ctrl+W    Word / char count",
            "  Ctrl+E    Rename this file",
            "  Ctrl+P    Power off (press twice within 5s)",
            "  F1        This help screen",
            "",
            "  Arrows    Move cursor",
            "  PgUp/Dn   Page up / down",
            "  Home/End  Line start / end",
            "",
            "  Press any key to close.",
        ]
        height, width = self.stdscr.getmaxyx()
        self.stdscr.clear()
        for i, line in enumerate(help_text):
            if i >= height - 1:
                break
            try:
                self.stdscr.addstr(i, 0, line[:width])
            except curses.error:
                pass
        self.stdscr.refresh()
        if self.display:
            self.display.paint_from_curses(self.stdscr)
            self.display.refresh(self.display.panel_region)
        self.stdscr.getch()
        self._draw()
