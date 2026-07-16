#!/usr/bin/env python3
"""
Inkwriter - A distraction-free writing device for Raspberry Pi Zero WH
Designed for e-ink displays with keyboard-only navigation.

Changes vs original
-------------------
- Display instance is passed down to Editor so partial e-ink refreshes work.
- _run_editor handles "new_file" return from editor (was missing).
- display.sleep() called on clean exit; display.wake() on re-entry to editor.
"""

import os
import sys
import curses
import time
import signal
import logging
import termios
from pathlib import Path

from .config import Config
from .file_manager import FileManager
from .editor import Editor
from .file_browser import FileBrowser
from .note_manager import NoteManager
from .display import Display
from .progress import Progress
from . import shutdown_screen

_log_dir = Path.home() / ".config" / "inkwriter"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_log_dir / "inkwriter.log"),
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)


def disable_flow_control():
    """Prevent the terminal from intercepting Ctrl+S and Ctrl+Q."""
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[1] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass


def main():
    config = Config()
    file_manager = FileManager(config)
    note_manager = NoteManager(config)
    display = Display(config)   # auto-detects eink vs terminal
    progress = Progress(config)  # shared across every Editor opened this run

    if config.boot_animation:
        try:
            display.show_boot_animation()
        except Exception:
            log.exception("Boot animation failed; continuing without it")

    # Mutable holder so the nested `run` closure (called via curses.wrapper,
    # which we don't control the return value of) can report back whether
    # the user actually requested a system shutdown.
    result_state = {"shutdown_requested": False}

    def run(stdscr):
        curses.curs_set(1)
        curses.start_color()
        curses.use_default_colors()
        curses.raw()
        disable_flow_control()

        # In e-ink mode, force curses's logical grid to exactly match the
        # physical panel (display_width/height // cell size) instead of
        # whatever the tty happens to report (a default 80x24 tty would
        # otherwise disagree with the 99x17 grid the panel actually has
        # room for, leaving part of the panel unused or mis-mapping rows).
        if display.is_eink:
            from .display import CELL_W, CELL_H
            rows = config.display_height // CELL_H
            cols = config.display_width // CELL_W
            try:
                curses.resizeterm(rows, cols)
            except curses.error:
                pass

        # Pair 2 is the app's one "bar" convention -- black-on-white,
        # used for title/status bars. (Pairs 1/3/4 used to duplicate this
        # or sit unused; selection highlights now use a plain underline
        # instead of a second inverted-block pair -- softer to look at and
        # cheaper to refresh on e-ink. See display.py's paint_from_curses,
        # which specifically watches for pair 2 to mirror bars correctly.)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)

        if config.show_milestones:
            milestone = progress.pop_pending_milestone()
            if milestone:
                _show_milestone_screen(stdscr, display, milestone)

        app = InkwriterApp(stdscr, config, file_manager, note_manager, display, progress)
        app.run()
        result_state["shutdown_requested"] = app.shutdown_requested

    _wait_for_keyboard(display, config)

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception("Fatal error")
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            content = shutdown_screen.build(config, file_manager, progress)
            if content:
                art, caption, layout = content
                display.show_shutdown_screen(art, caption, layout=layout)
        except Exception:
            log.exception("Shutdown screen failed; sleeping without it")
        display.sleep()

        if result_state["shutdown_requested"]:
            _trigger_system_shutdown()


def _wait_for_keyboard(display, config):
    """
    If a Bluetooth keyboard is configured (config.keyboard_mac, written by
    install.sh after pairing) and require_keyboard_at_boot is on, block
    here -- before curses even starts -- until that keyboard shows as
    actually connected. Landing silently in the editor with no way to
    type or navigate is worse than a clear "waiting" screen; bt-reconnect
    .service already retries the OS-level connection several times at
    boot (see INSTALL.md), but that's a bounded, one-shot attempt -- this
    is the app's own unbounded fallback for whenever that didn't land in
    time (keyboard was left off, walked out of range, needed a keypress
    to wake up, etc).

    No-op outside e-ink mode (HDMI/terminal dev sessions always have a
    real local keyboard already) and if no keyboard_mac is configured at
    all (nothing to gate on).

    Two ways out without the keyboard ever connecting, both usable over
    SSH: physically/remotely trigger the connection yourself
    (`bluetoothctl connect <mac>` -- the next poll picks it up), or edit
    ~/.config/inkwriter/config.ini and flip require_keyboard_at_boot to
    false (re-read fresh every cycle, so this takes effect within one
    poll interval, no restart needed).
    """
    if not display.is_eink:
        return
    mac = config.keyboard_mac
    if not mac:
        return
    if not config.require_keyboard_at_boot:
        return

    if _bt_is_connected(mac):
        return   # already connected -- no wait screen needed at all

    log.info(f"Waiting for keyboard {mac} to connect before starting...")
    display.wake()
    attempt = 0
    while True:
        attempt += 1
        display.show_bt_waiting_screen(mac, attempt)
        time.sleep(5)

        if _bt_is_connected(mac):
            log.info(f"Keyboard {mac} connected after {attempt} attempt(s)")
            return

        # Re-read config from disk each cycle -- an SSH user flipping
        # require_keyboard_at_boot off mid-wait should be noticed without
        # needing a restart.
        try:
            config._cfg.read(config.config_file)
        except Exception:
            pass
        if not config.require_keyboard_at_boot:
            log.info("require_keyboard_at_boot turned off mid-wait -- continuing without keyboard")
            return


def _bt_is_connected(mac):
    """True if bluetoothctl reports this MAC as currently connected.
    Any failure (bluetoothd not running, command missing, timeout) is
    treated as 'not connected' rather than raising -- this is a status
    poll in a loop, not something that should ever crash the app."""
    import subprocess
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", mac],
            capture_output=True, text=True, timeout=5,
        )
        return "Connected: yes" in result.stdout
    except Exception:
        return False


def _trigger_system_shutdown():
    """
    Power off the Pi itself, once the app has cleanly exited and the
    e-ink panel is already asleep (so the shutdown screen stays as the
    last thing shown). Requires the inkwriter user to be able to run
    `poweroff` via sudo without a password prompt -- see the "Shutdown
    key" section in INSTALL.md for the one-line sudoers entry. Failure is
    logged, not raised -- a missing sudoers rule shouldn't crash the app,
    it should just leave the Pi running with something in the log.
    """
    import subprocess
    try:
        subprocess.run(["sudo", "-n", "systemctl", "poweroff"], check=True)
    except Exception as exc:
        log.warning(f"System poweroff failed (check sudoers config): {exc}")


def _show_milestone_screen(stdscr, display, milestone):
    """
    One-time congratulatory screen shown at boot when a lifetime word-count
    milestone was crossed in a previous session. Never interrupts active
    writing -- only ever shown here, before the editor/browser even starts.
    Dismissed by any keypress.
    """
    headline = f"milestone: {milestone:,} words"
    subtext = "keep going"

    if display.is_eink:
        display.wake()
        display.show_milestone_screen(headline, subtext)

    stdscr.clear()
    h, w = stdscr.getmaxyx()
    y = max(0, h // 2 - 1)
    try:
        stdscr.addstr(y, max(0, (w - len(headline)) // 2), headline)
        stdscr.addstr(y + 1, max(0, (w - len(subtext)) // 2), subtext)
    except curses.error:
        pass
    stdscr.refresh()

    stdscr.timeout(-1)   # block for a keypress
    try:
        stdscr.get_wch()
    except curses.error:
        pass
    stdscr.clear()
    stdscr.refresh()


class InkwriterApp:
    """Top-level application controller."""

    MODE_EDITOR    = "editor"
    MODE_BROWSER   = "browser"
    MODE_NOTE      = "note"
    MODE_NOTES_LIST = "notes_list"
    MODE_TYPE_OUT  = "type_out"

    def __init__(self, stdscr, config, file_manager, note_manager, display, progress=None):
        self.stdscr = stdscr
        self.config = config
        self.file_manager = file_manager
        self.note_manager = note_manager
        self.display = display
        self.progress = progress
        self.mode = self.MODE_BROWSER
        self.editor = None
        self.browser = FileBrowser(stdscr, file_manager, config, display=display)
        self.running = True
        self.shutdown_requested = False

        signal.signal(signal.SIGTERM, self._handle_signal)
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, self._handle_signal)

    def _handle_signal(self, signum, frame):
        if self.editor:
            self.editor.save()
        self.running = False

    def run(self):
        while self.running:
            if self.mode == self.MODE_BROWSER:
                self._run_browser()
            elif self.mode == self.MODE_EDITOR:
                self._run_editor()
            elif self.mode == self.MODE_NOTE:
                self._run_note()
            elif self.mode == self.MODE_NOTES_LIST:
                self._run_notes_list()
            elif self.mode == self.MODE_TYPE_OUT:
                self._run_type_out()

    # ------------------------------------------------------------------
    # Mode runners
    # ------------------------------------------------------------------

    def _run_browser(self):
        result = self.browser.run()
        if result is None:
            self.running = False
        elif result == "shutdown":
            self.running = False
            self.shutdown_requested = True
        elif result == "new_file":
            path = self.file_manager.create_new_file()
            self.display.wake()
            self.editor = Editor(self.stdscr, path, self.config, display=self.display, progress=self.progress)
            self.mode = self.MODE_EDITOR
        elif result == "new_note":
            self.mode = self.MODE_NOTE
        elif result == "view_notes":
            self.mode = self.MODE_NOTES_LIST
        elif result == "type_out":
            self.mode = self.MODE_TYPE_OUT
        elif isinstance(result, Path):
            self.display.wake()
            self.editor = Editor(self.stdscr, result, self.config, display=self.display, progress=self.progress)
            self.mode = self.MODE_EDITOR

    def _run_editor(self):
        result = self.editor.run()
        if result == "browser":
            self.editor = None
            self.mode = self.MODE_BROWSER
        elif result == "shutdown":
            self.running = False
            self.shutdown_requested = True
        elif result == "new_file":
            # Editor requested a new document
            path = self.file_manager.create_new_file(
                directory=self.editor.filepath.parent if self.editor else None
            )
            self.editor = Editor(self.stdscr, path, self.config, display=self.display, progress=self.progress)
            # mode stays EDITOR
        elif result == "note":
            self.mode = self.MODE_NOTE
        elif result == "type_out":
            self.mode = self.MODE_TYPE_OUT

    def _run_notes_list(self):
        from .note_manager import NotesBrowser
        browser = NotesBrowser(self.stdscr, self.note_manager, self.config, display=self.display)
        result = browser.run()
        if isinstance(result, Path):
            # Open the note in the regular editor — notes are plain .txt
            # files, so FileManager/Editor handle them exactly like any
            # other document, including autosave and Ctrl+S.
            self.display.wake()
            self.editor = Editor(self.stdscr, result, self.config, display=self.display, progress=self.progress)
            self.mode = self.MODE_EDITOR
        else:
            self.mode = self.MODE_BROWSER

    def _run_note(self):
        from .note_manager import NoteEditor
        note_ed = NoteEditor(self.stdscr, self.note_manager, self.config, display=self.display)
        note_ed.run()   # return value (path or None) already shown to user in overlay
        # Force a full redraw so the overlay doesn't leave ghost pixels
        self.stdscr.clear()
        self.stdscr.refresh()
        # Return to wherever we came from
        self.mode = self.MODE_EDITOR if self.editor else self.MODE_BROWSER

    def _run_type_out(self):
        from .usb_hid import TypeOutScreen
        screen = TypeOutScreen(self.stdscr, self.file_manager, self.config, display=self.display)
        screen.run()
        # Return to wherever we came from, same as _run_note does, instead
        # of always dropping back to the browser (which discarded the open
        # document's context when type-out was triggered via Ctrl+T from
        # inside the editor).
        self.mode = self.MODE_EDITOR if self.editor else self.MODE_BROWSER


if __name__ == "__main__":
    main()
