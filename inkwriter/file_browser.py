"""
File browser: navigate directories and files with arrow keys.
Keyboard-only, designed for e-ink.
"""

import curses
import time
from pathlib import Path


class FileBrowser:
    """
    A keyboard-navigable file browser.

    Keys:
        Up/Down     = navigate
        Enter       = open file or enter directory
        Backspace   = go up one directory
        Ctrl+N      = new file in current directory
        Ctrl+D      = new directory
        Ctrl+G      = quick note
        Ctrl+V      = view saved notes
        Ctrl+T      = type-out mode
        Ctrl+X      = delete selected file (with confirm)
        Ctrl+F      = move selected file to another folder
        F1          = help
        Ctrl+Q      = quit app
        Ctrl+P      = power off device (press twice within 5s to confirm)
    """

    def __init__(self, stdscr, file_manager, config, display=None):
        self.stdscr = stdscr
        self.fm = file_manager
        self.config = config
        self.display = display          # may be None in pure-terminal mode
        self.current_dir = config.documents_dir
        self.selected = 0
        self.scroll = 0
        self.message = ""
        self.message_time = 0
        self._shutdown_armed_until = 0

    def _mirror(self):
        """Push whatever curses just drew onto the e-ink panel, if present.
        Every _draw()-style method in this class calls this right after
        stdscr.refresh() -- see display.py's paint_from_curses for why this
        is the one place that needs touching, not each drawing method."""
        if self.display:
            self.display.paint_from_curses(self.stdscr)
            self.display.refresh(self.display.panel_region)

    def run(self):
        """
        Returns:
          Path        - open this file in editor
          "new_file"  - create a new file
          "new_note"  - open quick note
          "view_notes"- open the saved-notes browser
          "type_out"  - open type-out screen
          "shutdown"  - power off the device
          None        - quit application
        """
        while True:
            entries = self._get_entries()
            self._draw(entries)
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue

            result = self._handle_key(key, entries)
            if result is not None:
                return result

    # -------------------------------------------------------------------------
    # Key handling
    # -------------------------------------------------------------------------

    def _handle_key(self, key, entries):
        c = key if isinstance(key, int) else ord(key) if isinstance(key, str) and key else -1

        if c == curses.KEY_UP or key == curses.KEY_UP:
            self.selected = max(0, self.selected - 1)
        elif c == curses.KEY_DOWN or key == curses.KEY_DOWN:
            self.selected = min(len(entries) - 1, self.selected + 1)
        elif c in (10, 13, curses.KEY_ENTER):   # Enter
            return self._activate(entries)
        elif c in (curses.KEY_BACKSPACE, 127):
            self._go_up()
        elif c == 14:   # Ctrl+N
            return "new_file"
        elif c == 4:    # Ctrl+D  new directory
            self._new_directory_interactive()
        elif c == 7:    # Ctrl+G
            return "new_note"
        elif c == 22:   # Ctrl+V  view saved notes
            return "view_notes"
        elif c == 20:   # Ctrl+T
            return "type_out"
        elif c == 24:   # Ctrl+X  delete
            self._delete_interactive(entries)
        elif c == 6:    # Ctrl+F  move to another folder
            self._move_interactive(entries)
        elif c == curses.KEY_F1:  # F1  help  (Ctrl+H collides with Backspace
                                   # on most terminals — ASCII 8 is the same
                                   # byte, so it can't be reliably bound here)
            self._show_help()
        elif c == 17:   # Ctrl+Q  quit
            return None
        elif c == 16:   # Ctrl+P  power off (press twice within 5s)
            return self._handle_shutdown_key()

        return None   # stay in browser

    def _handle_shutdown_key(self):
        """
        Two-press confirm instead of a modal dialog -- consistent with the
        rest of the app (no popups), but a destructive system action still
        deserves more friction than a single accidental keypress.
        """
        now = time.time()
        if now < self._shutdown_armed_until:
            return "shutdown"
        self._shutdown_armed_until = now + 5
        self._set_message("Press Ctrl+P again within 5s to power off")
        return None

    def _activate(self, entries):
        if not entries:
            return "new_file"
        entry = entries[self.selected]
        if entry["type"] == "dir":
            self.current_dir = entry["path"]
            self.selected = 0
            self.scroll = 0
            return None   # stay in browser, redraws with new dir
        else:
            return entry["path"]

    def _go_up(self):
        parent = self.current_dir.parent
        if parent != self.current_dir and str(parent).startswith(str(self.config.documents_dir)):
            self.current_dir = parent
            self.selected = 0
            self.scroll = 0

    # -------------------------------------------------------------------------
    # Drawing
    # -------------------------------------------------------------------------

    def _draw(self, entries):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        list_height = height - 3   # title + status + blank

        # Scroll
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + list_height:
            self.scroll = self.selected - list_height + 1

        stdscr.erase()

        # Title bar: current directory path (relative to documents root)
        try:
            rel = self.current_dir.relative_to(self.config.documents_dir)
            dir_label = "/" + str(rel) if str(rel) != "." else "/"
        except ValueError:
            dir_label = str(self.current_dir)
        title = f" INKWRITER  {dir_label} "
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(0, 0, title.ljust(width)[:width-1])
        stdscr.attroff(curses.color_pair(2))

        # Entry list
        if not entries:
            # A dead-end blank list is a bad place to land for anyone
            # already fighting to get started -- nudge toward the one
            # action that matters instead of just stating a fact.
            lines = ["Nothing here yet.", "", "Ctrl+N to start writing"]
            for i, line in enumerate(lines):
                try:
                    stdscr.addstr(2 + i, 2, line[:width - 2])
                except curses.error:
                    pass
        else:
            for i in range(list_height):
                idx = self.scroll + i
                if idx >= len(entries):
                    break
                entry = entries[idx]
                icon = "[D] " if entry["type"] == "dir" else "    "
                label = icon + entry["name"]
                if entry["type"] == "file" and entry.get("words") is not None:
                    label += f"  ({entry['words']}w)"
                y = i + 1
                if idx == self.selected:
                    # A leading marker + underline instead of a solid
                    # inverted block -- easier on the eyes than a full
                    # black bar, matches the editor's cursor-line
                    # treatment, and flips far fewer e-ink pixels.
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

        # Status bar
        if self.message and time.time() - self.message_time < 3:
            status = f" {self.message} "
        else:
            self.message = ""
            total = len([e for e in entries if e["type"] == "file"])
            status = (f" {total} file(s) | ^N New  ^D Dir  ^G Note  ^V Notes  "
                      f"^T TypeOut  ^X Del  ^F Move  F1 Help  ^Q Quit ")

        stdscr.attron(curses.color_pair(2))
        try:
            stdscr.addstr(height - 1, 0, status.ljust(width)[:width-1])
        except curses.error:
            pass
        stdscr.attroff(curses.color_pair(2))

        stdscr.refresh()
        self._mirror()

    # -------------------------------------------------------------------------
    # Data
    # -------------------------------------------------------------------------

    def _get_entries(self):
        entries = []
        # Subdirectories first
        for d in self.fm.list_directories(self.current_dir):
            entries.append({"type": "dir", "name": d.name, "path": d})
        # Files
        for f in self.fm.list_files(self.current_dir):
            if f.parent == self.current_dir:
                try:
                    info = self.fm.file_info(f)
                    entries.append({
                        "type": "file",
                        "name": f.stem,
                        "path": f,
                        "words": info["words"],
                        "modified": info["modified"],
                    })
                except Exception:
                    entries.append({"type": "file", "name": f.stem, "path": f, "words": None})
        # Clamp selection
        if entries:
            self.selected = min(self.selected, len(entries) - 1)
        return entries

    # -------------------------------------------------------------------------
    # Interactive helpers
    # -------------------------------------------------------------------------

    def _new_directory_interactive(self):
        height, width = self.stdscr.getmaxyx()
        self.stdscr.addstr(height - 1, 0, " Folder name: ".ljust(width)[:width-1], curses.color_pair(2))
        curses.echo()
        try:
            name = self.stdscr.getstr(height - 1, 14, 40).decode("utf-8").strip()
        except Exception:
            name = ""
        curses.noecho()
        curses.raw()   # restore raw mode after echo prompt
        if name:
            self.fm.create_directory(name, self.current_dir)
            self._set_message(f"Created folder: {name}")

    def _delete_interactive(self, entries):
        if not entries or self.selected >= len(entries):
            return
        entry = entries[self.selected]
        if entry["type"] == "dir":
            self._set_message("Cannot delete folders here.")
            return
        height, width = self.stdscr.getmaxyx()
        prompt = f" Delete '{entry['name']}'? (y/N): "
        self.stdscr.addstr(height - 1, 0, prompt.ljust(width)[:width-1], curses.color_pair(2))
        self.stdscr.refresh()
        self._mirror()
        c = self.stdscr.getch()
        if c in (ord('y'), ord('Y')):
            self.fm.delete_file(entry["path"])
            self.selected = max(0, self.selected - 1)
            self._set_message("Moved to trash.")
        else:
            self._set_message("Cancelled.")

    def _move_interactive(self, entries):
        if not entries or self.selected >= len(entries):
            return
        entry = entries[self.selected]
        if entry["type"] == "dir":
            self._set_message("Cannot move folders.")
            return
        dest = self._pick_folder()
        if dest is None:
            self._set_message("Move cancelled.")
            return
        if dest == entry["path"].parent:
            self._set_message("Already in that folder.")
            return
        self.fm.move_file(entry["path"], dest)
        self.selected = max(0, self.selected - 1)
        self._set_message(f"Moved to /{self._rel_label(dest)}")

    def _pick_folder(self):
        """
        Small nested folder browser for choosing a move destination.
        Returns the chosen Path, or None if cancelled.
        """
        current = self.config.documents_dir
        idx = 0
        while True:
            dirs = self.fm.list_directories(current)
            options = []
            if current != self.config.documents_dir:
                options.append(("..", "up"))
            options.append((f"[Move here: /{self._rel_label(current)}]", "select"))
            for d in dirs:
                options.append((d.name + "/", d))
            idx = min(idx, len(options) - 1)

            self._draw_folder_picker(current, options, idx)
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue

            c = key if isinstance(key, int) else ord(key) if isinstance(key, str) and key else -1

            if c == curses.KEY_UP or key == curses.KEY_UP:
                idx = max(0, idx - 1)
            elif c == curses.KEY_DOWN or key == curses.KEY_DOWN:
                idx = min(len(options) - 1, idx + 1)
            elif c in (10, 13, curses.KEY_ENTER):
                _, target = options[idx]
                if target == "up":
                    current = current.parent
                    idx = 0
                elif target == "select":
                    return current
                else:
                    current = target
                    idx = 0
            elif c in (curses.KEY_BACKSPACE, 127):
                if current != self.config.documents_dir:
                    current = current.parent
                    idx = 0
            elif c == 27:  # Esc — cancel
                return None

    def _draw_folder_picker(self, current, options, idx):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        list_height = height - 3

        stdscr.erase()
        title = f" MOVE TO FOLDER  /{self._rel_label(current)} "
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(0, 0, title.ljust(width)[:width - 1])
        stdscr.attroff(curses.color_pair(2))

        for i in range(list_height):
            if i >= len(options):
                break
            label, _ = options[i]
            y = i + 1
            if i == idx:
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

        status = " Enter=Select  Backspace=Up  Esc=Cancel "
        stdscr.attron(curses.color_pair(2))
        try:
            stdscr.addstr(height - 1, 0, status.ljust(width)[:width - 1])
        except curses.error:
            pass
        stdscr.attroff(curses.color_pair(2))
        stdscr.refresh()
        self._mirror()

    def _rel_label(self, path):
        try:
            rel = path.relative_to(self.config.documents_dir)
            return "" if str(rel) == "." else str(rel)
        except ValueError:
            return str(path)

    def _show_help(self):
        help_text = [
            "FILE BROWSER",
            "",
            "  Up/Down     Navigate",
            "  Enter       Open file or folder",
            "  Backspace   Go up one folder",
            "",
            "  Ctrl+N      New document",
            "  Ctrl+D      New folder",
            "  Ctrl+G      Quick note",
            "  Ctrl+V      View saved notes",
            "  Ctrl+T      Type-out to computer",
            "  Ctrl+X      Delete selected file",
            "  Ctrl+F      Move file to another folder",
            "  F1          This help",
            "  Ctrl+Q      Quit",
            "  Ctrl+P      Power off (press twice within 5s)",
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
        self._mirror()
        self.stdscr.getch()

    def _set_message(self, msg):
        self.message = msg
        self.message_time = time.time()