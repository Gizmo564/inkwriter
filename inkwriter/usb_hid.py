"""
USB HID output: types the current document as keyboard input.

Backends
--------
1. USB gadget (/dev/hidg0) on Pi – raw HID reports over USB.
2. uinput on Linux – virtual keyboard via /dev/uinput (testing on desktop).
3. Simulation – logs chars but sends no actual keystrokes (ultimate fallback).

Bug fixes vs original
---------------------
- uinput is imported at module level inside _try_uinput so that
  subsequent _send_uinput_char calls can reference the module.
- uinput.KEY_EVENT does not exist; the correct API is passing
  (uinput.EV_KEY, code) tuples to uinput.Device().
- TypeOutScreen now calls file_manager.list_all_files() so that it
  gets the full recursive list, not just top-level files.
"""

import curses
import time
import os
import logging

log = logging.getLogger(__name__)

# Module-level reference populated when uinput loads successfully
_uinput = None

# ---------- HID keycode table (for gadget mode) ----------
_ASCII_HID = {
    ' ':  (0x00, 0x2C),
    'a':  (0x00, 0x04), 'b': (0x00, 0x05), 'c': (0x00, 0x06),
    'd':  (0x00, 0x07), 'e': (0x00, 0x08), 'f': (0x00, 0x09),
    'g':  (0x00, 0x0A), 'h': (0x00, 0x0B), 'i': (0x00, 0x0C),
    'j':  (0x00, 0x0D), 'k': (0x00, 0x0E), 'l': (0x00, 0x0F),
    'm':  (0x00, 0x10), 'n': (0x00, 0x11), 'o': (0x00, 0x12),
    'p':  (0x00, 0x13), 'q': (0x00, 0x14), 'r': (0x00, 0x15),
    's':  (0x00, 0x16), 't': (0x00, 0x17), 'u': (0x00, 0x18),
    'v':  (0x00, 0x19), 'w': (0x00, 0x1A), 'x': (0x00, 0x1B),
    'y':  (0x00, 0x1C), 'z': (0x00, 0x1D),
    '1':  (0x00, 0x1E), '2': (0x00, 0x1F), '3': (0x00, 0x20),
    '4':  (0x00, 0x21), '5': (0x00, 0x22), '6': (0x00, 0x23),
    '7':  (0x00, 0x24), '8': (0x00, 0x25), '9': (0x00, 0x26),
    '0':  (0x00, 0x27),
    'A':  (0x02, 0x04), 'B': (0x02, 0x05), 'C': (0x02, 0x06),
    'D':  (0x02, 0x07), 'E': (0x02, 0x08), 'F': (0x02, 0x09),
    'G':  (0x02, 0x0A), 'H': (0x02, 0x0B), 'I': (0x02, 0x0C),
    'J':  (0x02, 0x0D), 'K': (0x02, 0x0E), 'L': (0x02, 0x0F),
    'M':  (0x02, 0x10), 'N': (0x02, 0x11), 'O': (0x02, 0x12),
    'P':  (0x02, 0x13), 'Q': (0x02, 0x14), 'R': (0x02, 0x15),
    'S':  (0x02, 0x16), 'T': (0x02, 0x17), 'U': (0x02, 0x18),
    'V':  (0x02, 0x19), 'W': (0x02, 0x1A), 'X': (0x02, 0x1B),
    'Y':  (0x02, 0x1C), 'Z': (0x02, 0x1D),
    '!':  (0x02, 0x1E), '@': (0x02, 0x1F), '#': (0x02, 0x20),
    '$':  (0x02, 0x21), '%': (0x02, 0x22), '^': (0x02, 0x23),
    '&':  (0x02, 0x24), '*': (0x02, 0x25), '(': (0x02, 0x26),
    ')':  (0x02, 0x27),
    '\n': (0x00, 0x28),
    '\t': (0x00, 0x2B),
    '-':  (0x00, 0x2D), '_': (0x02, 0x2D),
    '=':  (0x00, 0x2E), '+': (0x02, 0x2E),
    '[':  (0x00, 0x2F), '{': (0x02, 0x2F),
    ']':  (0x00, 0x30), '}': (0x02, 0x30),
    '\\': (0x00, 0x31), '|': (0x02, 0x31),
    ';':  (0x00, 0x33), ':': (0x02, 0x33),
    "'":  (0x00, 0x34), '"': (0x02, 0x34),
    '`':  (0x00, 0x35), '~': (0x02, 0x35),
    ',':  (0x00, 0x36), '<': (0x02, 0x36),
    '.':  (0x00, 0x37), '>': (0x02, 0x37),
    '/':  (0x00, 0x38), '?': (0x02, 0x38),
}

_NULL_REPORT = b'\x00' * 8

# ---------- uinput evdev key codes ----------
_EV_KEY = {
    'a': 30, 'b': 48, 'c': 46, 'd': 32, 'e': 18, 'f': 33, 'g': 34, 'h': 35,
    'i': 23, 'j': 36, 'k': 37, 'l': 38, 'm': 50, 'n': 49, 'o': 24, 'p': 25,
    'q': 16, 'r': 19, 's': 31, 't': 20, 'u': 22, 'v': 47, 'w': 17, 'x': 45,
    'y': 21, 'z': 44,
    '1': 2,  '2': 3,  '3': 4,  '4': 5,  '5': 6,
    '6': 7,  '7': 8,  '8': 9,  '9': 10, '0': 11,
    '-': 12, '=': 13, '[': 26, ']': 27, '\\': 43,
    ';': 39, "'": 40, '`': 41, ',': 51, '.': 52,
    '/': 53, ' ': 57, '\n': 28, '\t': 15,
}

_SHIFTED = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()_+{}|:"<>?~')

# evdev key constants (matched to _EV_KEY codes above)
_KEY_LEFTSHIFT = 42
_KEY_ENTER     = 28
_KEY_TAB       = 15
_KEY_SPACE     = 57


class USBHIDTyper:
    """
    Sends text via the best available backend:
      - USB HID gadget (/dev/hidg0) on Pi
      - uinput virtual keyboard on Linux (testing)
      - pure simulation (no actual keystrokes)
    """

    def __init__(self, config):
        self.config = config
        self._delay = 1.0 / max(1, config.hid_chars_per_second)
        self._word_delay = config._cfg.getfloat("usb_hid", "delay_between_words")
        self._mode = "simulation"
        self._gadget_fd = None
        self._uinput_device = None

        if self._try_gadget():
            self._mode = "gadget"
            log.info("Using USB gadget for type-out")
        elif self._try_uinput():
            self._mode = "uinput"
            log.info("Using uinput virtual keyboard for type-out")
        else:
            self._mode = "simulation"
            log.info("No usable keyboard backend; type-out will be simulated")

    # ------------------------------------------------------------------
    # Backend detection
    # ------------------------------------------------------------------

    def _try_gadget(self):
        device = self.config.hid_device
        if not os.path.exists(device):
            return False
        try:
            fd = open(device, 'wb')
            fd.write(b'\x00' * 8)
            fd.flush()
            self._gadget_fd = fd
            return True
        except Exception as e:
            log.warning(f"Gadget {device} not usable: {e}")
            return False

    def _try_uinput(self):
        global _uinput
        try:
            import uinput as _uinput_mod
            _uinput = _uinput_mod

            # Build the event list from our key code table.
            # uinput.Device accepts (uinput.EV_KEY, code) tuples.
            event_codes = set(_EV_KEY.values()) | {_KEY_LEFTSHIFT}
            events = [(uinput.EV_KEY, code) for code in event_codes]

            self._uinput_device = _uinput.Device(events)
            return True
        except Exception as e:
            log.warning(f"uinput init failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def type_text(self, text, progress_cb=None):
        total = len(text)
        if self._mode == "gadget":
            self._type_gadget(text, total, progress_cb)
        elif self._mode == "uinput":
            self._type_uinput(text, total, progress_cb)
        else:
            self._simulate(text, total, progress_cb)

    # Alias so both type_text() and type_string() work
    def type_string(self, text):
        self.type_text(text)

    # ------------------------------------------------------------------
    # Gadget backend
    # ------------------------------------------------------------------

    def _type_gadget(self, text, total, progress_cb):
        fd = self._gadget_fd
        for i, ch in enumerate(text):
            report = self._make_hid_report(ch)
            if report:
                fd.write(report)
                fd.flush()
                time.sleep(self._delay)
                fd.write(_NULL_REPORT)
                fd.flush()
            if ch == ' ' and self._word_delay:
                time.sleep(self._word_delay)
            if progress_cb:
                progress_cb(i + 1, total)

    def _make_hid_report(self, ch):
        entry = _ASCII_HID.get(ch)
        if not entry:
            return None
        modifier, keycode = entry
        return bytes([modifier, 0x00, keycode, 0, 0, 0, 0, 0])

    # ------------------------------------------------------------------
    # uinput backend
    # ------------------------------------------------------------------

    def _type_uinput(self, text, total, progress_cb):
        for i, ch in enumerate(text):
            self._send_uinput_char(ch)
            time.sleep(self._delay)
            if ch == ' ' and self._word_delay:
                time.sleep(self._word_delay)
            if progress_cb:
                progress_cb(i + 1, total)

    def _send_uinput_char(self, ch):
        dev = self._uinput_device
        ui = _uinput   # module reference

        if ch == '\n':
            dev.emit((ui.EV_KEY, _KEY_ENTER), 1)
            dev.emit((ui.EV_KEY, _KEY_ENTER), 0)
            dev.syn()
            return
        if ch == '\t':
            dev.emit((ui.EV_KEY, _KEY_TAB), 1)
            dev.emit((ui.EV_KEY, _KEY_TAB), 0)
            dev.syn()
            return

        shift_needed = ch in _SHIFTED or (ch.isupper() and ch.isalpha())
        lower = ch.lower()
        if lower in _EV_KEY:
            code = _EV_KEY[lower]
            if shift_needed:
                dev.emit((ui.EV_KEY, _KEY_LEFTSHIFT), 1)
            dev.emit((ui.EV_KEY, code), 1)
            dev.emit((ui.EV_KEY, code), 0)
            if shift_needed:
                dev.emit((ui.EV_KEY, _KEY_LEFTSHIFT), 0)
            dev.syn()

    # ------------------------------------------------------------------
    # Simulation backend
    # ------------------------------------------------------------------

    def _simulate(self, text, total, progress_cb):
        log.debug("Simulating type-out (no actual keystrokes)")
        for i, ch in enumerate(text):
            time.sleep(self._delay)
            if ch == ' ' and self._word_delay:
                time.sleep(self._word_delay)
            if progress_cb:
                progress_cb(i + 1, total)


# ---------- UI screen ----------

class TypeOutScreen:
    """Full-screen UI for selecting a file and typing it out."""

    def __init__(self, stdscr, file_manager, config, display=None):
        self.stdscr = stdscr
        self.fm = file_manager
        self.config = config
        self.display = display          # may be None in pure-terminal mode
        self.selected = 0
        self.typer = USBHIDTyper(config)

    def _mirror(self):
        if self.display:
            self.display.paint_from_curses(self.stdscr)
            self.display.refresh(self.display.panel_region)

    def run(self):
        # Use list_all_files() for a recursive document list
        files = self.fm.list_all_files()
        if not files:
            self._show_message("No documents found. Press any key.")
            self.stdscr.getch()
            return

        while True:
            self._draw(files)
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue

            c = key if isinstance(key, int) else ord(key) if isinstance(key, str) else -1

            if c == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
            elif c == curses.KEY_DOWN:
                self.selected = min(len(files) - 1, self.selected + 1)
            elif c in (10, 13, curses.KEY_ENTER):
                self._type_file(files[self.selected])
                return
            elif c == 27:
                return

    def _type_file(self, path):
        content = self.fm.load_file(path)
        if not content:
            self._show_message("File is empty. Press any key.")
            self.stdscr.getch()
            return

        total = len(content)
        mode_label = {
            "gadget":     "USB Gadget",
            "uinput":     "uinput (virtual keyboard)",
            "simulation": "SIMULATION",
        }.get(self.typer._mode, "unknown")

        height, width = self.stdscr.getmaxyx()

        self.stdscr.erase()
        if self.typer._mode == "uinput":
            msg = "Focus the target window, then press any key to start..."
        else:
            msg = "Press any key to start typing..."
        try:
            self.stdscr.addstr(height // 2, (width - len(msg)) // 2, msg[:width])
        except curses.error:
            pass
        self.stdscr.refresh()
        self._mirror()
        self.stdscr.getch()

        # Type-out fires this callback once per character -- at the default
        # 60 chars/sec that's every ~16ms. A full e-ink mirror does a
        # hardware panel refresh, which takes a noticeable fraction of a
        # second; doing that every character would badly bottleneck typing
        # speed and needlessly wear the panel for a progress bar nobody's
        # watching (the user's looking at the target computer, not this
        # screen). Throttle the e-ink push to a few times a second --
        # curses/HDMI still updates every character for anyone watching
        # locally, only the e-ink hardware refresh is rate-limited.
        last_mirror = [0.0]

        def progress(done, total):
            pct = int(done * 100 / total)
            bar_w = width - 20
            filled = int(bar_w * done / total)
            bar = "█" * filled + "░" * (bar_w - filled)
            self.stdscr.erase()
            label = f" Typing ({mode_label}): {path.stem} "
            self.stdscr.attron(curses.color_pair(2))
            self.stdscr.addstr(0, 0, label.ljust(width)[:width])
            self.stdscr.attroff(curses.color_pair(2))
            try:
                self.stdscr.addstr(height // 2 - 1, 2, f"Progress: {pct}%")
                self.stdscr.addstr(height // 2,     2, bar[:width - 4])
                self.stdscr.addstr(height // 2 + 1, 2, f"{done}/{total} chars")
            except curses.error:
                pass
            self.stdscr.refresh()
            now = time.time()
            if done >= total or now - last_mirror[0] > 0.5:
                self._mirror()
                last_mirror[0] = now

        self.stdscr.nodelay(False)
        self.typer.type_text(content, progress_cb=progress)
        self._show_message("Done! Press any key.")
        self.stdscr.getch()

    def _draw(self, files):
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()

        mode_label = {
            "gadget":     "USB Gadget",
            "uinput":     "uinput (virtual keyboard)",
            "simulation": "SIMULATION",
        }.get(self.typer._mode, "unknown")
        title = f" TYPE OUT TO COMPUTER [{mode_label}]"
        self.stdscr.attron(curses.color_pair(2))
        self.stdscr.addstr(0, 0, title.ljust(width)[:width])
        self.stdscr.attroff(curses.color_pair(2))

        self.stdscr.addstr(2, 2, "Select document to type out, then press Enter:")

        list_start = 4
        for i, f in enumerate(files):
            if list_start + i >= height - 2:
                break
            label = f.stem
            if i == self.selected:
                marked = ("> " + label)[:width - 1]
                try:
                    self.stdscr.addstr(list_start + i, 0, marked, curses.A_UNDERLINE)
                except curses.error:
                    pass
            else:
                try:
                    self.stdscr.addstr(list_start + i, 0, ("  " + label)[:width - 1])
                except curses.error:
                    pass

        status = " Enter=Type  Esc=Back "
        self.stdscr.attron(curses.color_pair(2))
        try:
            self.stdscr.addstr(height - 1, 0, status.ljust(width)[:width])
        except curses.error:
            pass
        self.stdscr.attroff(curses.color_pair(2))
        self.stdscr.refresh()
        self._mirror()

    def _show_message(self, msg):
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()
        try:
            self.stdscr.addstr(height // 2, max(0, (width - len(msg)) // 2), msg[:width])
        except curses.error:
            pass
        self.stdscr.refresh()
        self._mirror()
