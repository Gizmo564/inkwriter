"""
Configuration for Inkwriter.
Reads from ~/.config/inkwriter/config.ini, with sane defaults for RPi Zero WH.
"""

import os
import configparser
from pathlib import Path


class Config:
    def __init__(self):
        self.config_dir = Path(os.environ.get("INKWRITER_CONFIG", Path.home() / ".config" / "inkwriter"))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.ini"

        self._defaults = {
            "storage": {
                "documents_dir": str(Path.home() / "Documents" / "inkwriter"),
                "notes_dir": str(Path.home() / "Documents" / "inkwriter" / "notes"),
                "auto_save_interval": "30",   # seconds
                "backup_on_save": "true",
            },
            "display": {
                # Display mode: auto (recommended) | eink | hdmi | terminal
                # auto = SPI e-ink wins if present, then HDMI, then terminal
                "type": "auto",
                # waveshare_epd driver module name for your panel, e.g.:
                #   epd5in79    -> Waveshare 5.79" e-Paper HAT (792x272)
                #   epd2in13_V4 -> Waveshare 2.13" e-Paper HAT V4 (250x122)
                "driver": "epd5in79",
                "width": "792",               # pixels (Waveshare 5.79" = 792x272)
                "height": "272",
                "refresh_full_interval": "10",  # full refresh every N partial refreshes
                # Bitmap font shipped in inkwriter/fonts/<name>.pil (+ .pbm).
                # Fixed-size pixel font: no antialiasing, crisp on e-ink,
                # every glyph is exactly the same pixel rectangle. If the
                # named font can't be loaded, display.py falls back to a
                # TrueType system font at font_size.
                "font_name": "spleen-8x16",
                "font_size": "12",     # only used by the TTF fallback path
                "line_height": "16",   # matches Spleen 8x16 glyph height
                # Shutdown screen shown right before the e-ink panel sleeps.
                # quote  = growth-stage art + a random line from your own writing
                # stats  = growth-stage art + lifetime words/streak
                # growth = growth-stage art + a running word count
                # custom = full-panel background image from
                #          inkwriter/art/custom/ (drop in PNG/JPG files;
                #          one is picked at random each shutdown). Falls
                #          back to growth mode if that folder is empty.
                # off    = no shutdown image, just sleep
                "shutdown_screen": "growth",
                # One-time flourish at boot (e-ink only): reveals
                # inkwriter/art/logo.png via a cascading wipe of real
                # partial refreshes before the file browser appears. Skips
                # itself silently if false, or if logo.png is missing.
                "boot_animation": "true",
            },
            "editor": {
                "tab_size": "4",
                "word_wrap": "true",
                "show_word_count": "true",
                "show_line_numbers": "false",
                "autosave": "true",
                # Phase 1: resume exactly where you left off, per file.
                "remember_cursor_position": "true",
                # Phase 1: running "words written today" shown in status bar.
                "show_today_word_count": "true",
            },
            "growth": {
                # Master switch -- false disables every reward/streak/
                # milestone feature below and the app behaves exactly like
                # Phase 1 only. Nothing here is punitive by design (streaks
                # pause instead of resetting, milestones only ever add).
                "show_growth_features": "true",
                "show_session_summary": "true",
                "show_milestones": "true",
                # Days of grace after a missed day before the streak
                # quietly restarts at 1 instead of continuing.
                "streak_grace_days": "3",
            },
            "keyboard": {
                # Key bindings (curses key names or decimal codes)
                "key_save":       "19",   # Ctrl+S
                "key_new_file":   "14",   # Ctrl+N
                "key_open_file":  "15",   # Ctrl+O (browser)
                "key_quit":       "17",   # Ctrl+Q
                "key_shutdown":   "16",   # Ctrl+P  (power off, press twice)
                "key_note":       "7",    # Ctrl+G  (quick note)
                "key_type_out":   "20",   # Ctrl+T  (type to computer)
                "key_word_count": "23",   # Ctrl+W
                "key_help":       "8",    # Ctrl+H
            },
            "usb_hid": {
                "device": "/dev/hidg0",
                "chars_per_second": "60",   # typing speed when sending to computer
                "delay_between_words": "0.01",
            },
            "bluetooth": {
                # MAC address of the paired keyboard, written by install.sh
                # after pairing. Blank = no keyboard-wait gate at boot (the
                # e-ink UI starts immediately regardless of Bluetooth
                # state) -- set automatically if you used install.sh, or
                # by hand if you paired manually.
                "keyboard_mac": "",
                # If keyboard_mac is set and this is true, Inkwriter shows
                # a "waiting for keyboard" screen at boot (e-ink only) and
                # blocks entering the UI until that keyboard is actually
                # connected -- landing in an editor you have no way to
                # type into is worse than a clear wait screen. Re-read
                # live during the wait, so flipping this to false over
                # SSH lets a boot proceed without the keyboard.
                "require_keyboard_at_boot": "true",
            },
        }

        self._cfg = configparser.ConfigParser()
        # Set defaults
        for section, values in self._defaults.items():
            if not self._cfg.has_section(section):
                self._cfg.add_section(section)
            for key, val in values.items():
                self._cfg.set(section, key, val)

        if self.config_file.exists():
            self._cfg.read(self.config_file)

        # Ensure directories exist
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)

        # Save config if new
        if not self.config_file.exists():
            self.save()

    def save(self):
        with open(self.config_file, "w") as f:
            self._cfg.write(f)

    # --- Convenience properties ---

    @property
    def documents_dir(self):
        return Path(self._cfg.get("storage", "documents_dir"))

    @property
    def notes_dir(self):
        return Path(self._cfg.get("storage", "notes_dir"))

    @property
    def auto_save_interval(self):
        return self._cfg.getint("storage", "auto_save_interval")

    @property
    def backup_on_save(self):
        return self._cfg.getboolean("storage", "backup_on_save")


    @property
    def display_type(self) -> str:
        """
        Configured display mode.
        'auto'     = detect at boot: SPI e-ink > HDMI > terminal
        'eink'     = force SPI e-ink
        'hdmi'     = force HDMI framebuffer
        'terminal' = curses-only, no hardware display
        """
        return self._cfg.get("display", "type")

    @property
    def display_driver(self) -> str:
        """waveshare_epd driver module name for the physically installed panel."""
        return self._cfg.get("display", "driver")

    @property
    def display_width(self) -> int:
        return self._cfg.getint("display", "width")

    @property
    def display_height(self) -> int:
        return self._cfg.getint("display", "height")

    @property
    def word_wrap(self):
        return self._cfg.getboolean("editor", "word_wrap")

    @property
    def tab_size(self):
        return self._cfg.getint("editor", "tab_size")

    @property
    def show_word_count(self):
        return self._cfg.getboolean("editor", "show_word_count")

    @property
    def autosave(self):
        return self._cfg.getboolean("editor", "autosave")

    @property
    def remember_cursor_position(self):
        return self._cfg.getboolean("editor", "remember_cursor_position")

    @property
    def show_today_word_count(self):
        return self._cfg.getboolean("editor", "show_today_word_count")

    @property
    def growth_enabled(self):
        return self._cfg.getboolean("growth", "show_growth_features")

    @property
    def show_session_summary(self):
        return self.growth_enabled and self._cfg.getboolean("growth", "show_session_summary")

    @property
    def show_milestones(self):
        return self.growth_enabled and self._cfg.getboolean("growth", "show_milestones")

    @property
    def streak_grace_days(self):
        return self._cfg.getint("growth", "streak_grace_days")

    @property
    def shutdown_screen(self) -> str:
        """'quote' | 'stats' | 'growth' | 'off'."""
        return self._cfg.get("display", "shutdown_screen")

    @property
    def hid_device(self):
        return self._cfg.get("usb_hid", "device")

    @property
    def hid_chars_per_second(self):
        return self._cfg.getint("usb_hid", "chars_per_second")

    @property
    def boot_animation(self):
        return self._cfg.getboolean("display", "boot_animation", fallback=True)

    @property
    def keyboard_mac(self):
        """Paired Bluetooth keyboard's MAC address, or '' if none set."""
        return self._cfg.get("bluetooth", "keyboard_mac", fallback="").strip()

    @property
    def require_keyboard_at_boot(self):
        return self._cfg.getboolean("bluetooth", "require_keyboard_at_boot", fallback=True)

    def key(self, name):
        """Return integer keycode for a named binding."""
        return self._cfg.getint("keyboard", f"key_{name}")