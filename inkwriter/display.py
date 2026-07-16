"""
Display abstraction layer for Inkwriter.

Boot-time detection priority
-----------------------------
1. SPI e-ink  — detected by /dev/spidev0.0 existing AND waveshare driver
                importable.  Always wins if present.
2. HDMI / framebuffer — detected by a live framebuffer device (/dev/fb0)
                        with a non-zero resolution, meaning something is
                        actually driving a screen.
3. Terminal (curses only) — fallback when neither hardware display is found.
                             Works on any SSH session or desktop terminal for
                             development.

The Display object exposes a small API used by the rest of the app:
  .mode          str  "eink" | "hdmi" | "terminal"
  .is_eink       bool
  .refresh()     trigger a panel refresh (no-op in terminal mode)
  .paint_from_curses(stdscr)  mirror whatever curses just drew into the
                 e-ink image buffer (cheap, CPU-only -- call this after
                 every screen redraw; call .refresh() separately to
                 actually push it to the physical panel)
  .panel_region  (x, y, w, h) tuple covering the whole panel, for refresh()
  .sleep()       low-power standby
  .wake()        restore from standby

HDMI/framebuffer notes
----------------------
Inkwriter is a curses application; curses already handles all the screen
drawing when running on HDMI.  The Display object in HDMI mode is therefore
mostly a bookkeeping shim — it records that we are in HDMI mode and lets
curses do its normal job.  The one addition is that we write a one-shot
splash to /dev/fb0 using a raw framebuffer write so the screen isn't blank
while Python is still starting up (optional, silently skipped on failure).
"""

import logging
import os
import struct
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Character cell size, matching the bundled Spleen 8x16 bitmap font exactly
# (inkwriter/fonts/spleen-8x16.pil) -- a true pixel font, so partial-refresh
# rectangles computed from these constants line up precisely with the drawn
# glyphs. This is the single source of truth other modules (editor.py,
# file_browser.py, ...) import from, rather than each hardcoding its own
# copy that could silently drift out of sync if the font ever changes.
CELL_W = 8
CELL_H = 16


# ---------------------------------------------------------------------------
# Hardware probing helpers
# ---------------------------------------------------------------------------

def _spi_available() -> bool:
    """True when the SPI device node for the Waveshare HAT exists."""
    return os.path.exists("/dev/spidev0.0")


def _eink_driver_importable(driver_name: str) -> bool:
    """True when the configured waveshare_epd panel driver can be imported."""
    try:
        import importlib
        importlib.import_module(f"waveshare_epd.{driver_name}")
        return True
    except Exception:
        return False


def _hdmi_available() -> bool:
    """
    True when a framebuffer device exists AND reports a non-zero resolution,
    which means a monitor (or HDMI dummy plug) is connected and active.
    """
    fb = "/dev/fb0"
    if not os.path.exists(fb):
        return False
    # Read virtual resolution via FBIOGET_VSCREENINFO ioctl (0x4600).
    # The first two uint32 fields are xres and yres.
    try:
        import fcntl
        FBIOGET_VSCREENINFO = 0x4600
        buf = b"\x00" * 160          # fb_var_screeninfo is ~160 bytes
        with open(fb, "rb") as f:
            result = fcntl.ioctl(f, FBIOGET_VSCREENINFO, buf)
        xres, yres = struct.unpack_from("II", result, 0)
        return xres > 0 and yres > 0
    except Exception:
        # If ioctl fails but /dev/fb0 exists, assume something is there.
        return True


def detect_display_mode(driver_name: str = "epd5in79") -> str:
    """
    Return the best available display mode string.

    Priority: "eink" > "hdmi" > "terminal"
    """
    if _spi_available():
        if _eink_driver_importable(driver_name):
            log.info("Display probe: SPI device found + driver importable → eink mode")
            return "eink"
        else:
            log.warning(
                f"Display probe: /dev/spidev0.0 exists but waveshare_epd.{driver_name} "
                "could not be imported — falling through to HDMI check."
            )
    else:
        log.info("Display probe: no /dev/spidev0.0")

    if _hdmi_available():
        log.info("Display probe: /dev/fb0 active → hdmi mode")
        return "hdmi"

    log.info("Display probe: no hardware display found → terminal mode")
    return "terminal"


# ---------------------------------------------------------------------------
# Display class
# ---------------------------------------------------------------------------

class Display:
    def __init__(self, config):
        self.config = config

        # config.display_type is "auto" (new default) or a forced override.
        forced = config.display_type  # "auto" | "eink" | "hdmi" | "terminal"

        if forced == "auto":
            self.mode = detect_display_mode(config.display_driver)
        else:
            self.mode = forced
            log.info(f"Display mode forced by config: {self.mode}")

        # Store detected mode back into config so other modules can read it.
        config._cfg.set("display", "type", self.mode)

        self._epd = None
        self._image = None
        self._draw_ctx = None
        self._font = None
        self._partial_count = 0
        self._refresh_interval = 10
        self._art_cache = {}

        if self.mode == "eink":
            try:
                self._init_eink()
                log.info("E-ink display initialised")
            except Exception as exc:
                log.warning(f"E-ink init failed ({exc}); degrading to terminal mode")
                self.mode = "terminal"

        elif self.mode == "hdmi":
            self._init_hdmi()

        # "terminal" needs no hardware init — curses handles everything.

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_eink(self) -> bool:
        return self.mode == "eink"

    @property
    def is_hdmi(self) -> bool:
        return self.mode == "hdmi"

    # ------------------------------------------------------------------
    # Init helpers
    # ------------------------------------------------------------------

    def _init_eink(self):
        import importlib
        from PIL import Image, ImageDraw, ImageFont

        driver_name = self.config.display_driver
        driver_module = importlib.import_module(f"waveshare_epd.{driver_name}")

        self._epd = driver_module.EPD()
        self._epd.init()
        self._epd.Clear(0xFF)

        self._refresh_interval = self.config._cfg.getint(
            "display", "refresh_full_interval"
        )

        # Persistent image buffer for partial redraws.
        #
        # IMPORTANT: build the buffer at the *configured* width/height (the
        # landscape resolution the UI actually draws in), not by swapping
        # epd.width/epd.height. Waveshare's getbuffer() auto-detects
        # orientation by comparing the image size against self.width/
        # self.height: it takes the fast "already correct orientation" path
        # when they match directly, and only rotates when they're swapped.
        # Panels that are natively landscape (e.g. the 5.79" HAT, 792x272)
        # need a non-swapped buffer; panels that are natively portrait
        # (e.g. the 2.13" HAT, 122x250 native) need the swapped one. Using
        # the configured width/height directly works for both, since
        # config.ini's width/height should already match the panel you set
        # display.driver to.
        self._image = Image.new(
            "1", (self.config.display_width, self.config.display_height), 255
        )
        self._draw_ctx = ImageDraw.Draw(self._image)

        # Font: prefer the bundled Spleen bitmap font. It's a true pixel
        # font (no antialiasing, fixed 8x16 glyph cells) so it renders
        # crisp on e-ink and every character occupies exactly the same
        # pixel rectangle — which is what makes per-character partial
        # refresh regions cheap and predictable to compute. Falls back to
        # a system TrueType mono font, then PIL's built-in default, if the
        # bitmap font is ever missing (e.g. a fonts/ dir that didn't make
        # it into a deploy).
        fonts_dir = Path(__file__).resolve().parent / "fonts"
        font_name = self.config._cfg.get(
            "display", "font_name", fallback="spleen-8x16"
        )
        bitmap_font_path = fonts_dir / f"{font_name}.pil"

        try:
            self._font = ImageFont.load(str(bitmap_font_path))
            log.info(f"Loaded bitmap font: {bitmap_font_path.name}")
        except Exception as exc:
            log.warning(
                f"Bitmap font '{bitmap_font_path.name}' unavailable ({exc}); "
                "falling back to DejaVu Sans Mono TTF"
            )
            try:
                font_size = self.config._cfg.getint("display", "font_size")
                self._font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                    font_size,
                )
            except Exception:
                self._font = ImageFont.load_default()

    def _init_hdmi(self):
        """
        Write a brief startup splash to /dev/fb0 so the screen isn't blank
        during the Python boot phase.  Silently skipped on any error —
        curses will take over momentarily anyway.
        """
        try:
            import fcntl
            FBIOGET_VSCREENINFO = 0x4600
            buf = b"\x00" * 160
            with open("/dev/fb0", "rb") as f:
                info = fcntl.ioctl(f, FBIOGET_VSCREENINFO, buf)
            xres, yres, _, _, bits_per_pixel = struct.unpack_from("IIIII", info, 0)
            bytes_per_pixel = bits_per_pixel // 8

            # Fill screen with black (a simple solid colour is instant)
            line = b"\x00" * (xres * bytes_per_pixel)
            with open("/dev/fb0", "wb") as fb:
                for _ in range(yres):
                    fb.write(line)
            log.info(f"HDMI framebuffer cleared ({xres}x{yres} @ {bits_per_pixel}bpp)")
        except Exception as exc:
            log.debug(f"HDMI splash skipped: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, region=None):
        """
        Trigger a display refresh.

        region : (x, y, w, h) pixel rect, only used in e-ink mode.
        No-op in HDMI and terminal modes (curses manages those).
        """
        if self.mode != "eink":
            return

        self._partial_count += 1
        if self._partial_count >= self._refresh_interval:
            self._full_refresh()
            self._partial_count = 0
        else:
            self._partial_refresh(region)

    @property
    def panel_region(self):
        """(x, y, w, h) covering the entire physical panel -- the region to
        pass to refresh() for any redraw that isn't a single small cursor
        cell (opening a file, scrolling, switching screens, etc.)."""
        return (0, 0, self.config.display_width, self.config.display_height)

    def paint_from_curses(self, stdscr):
        """
        Read back exactly what curses just drew, cell by cell (via
        stdscr.inch()), and paint the same character grid into the e-ink
        image buffer. This is the single chokepoint that keeps the
        physical panel in sync with every curses screen in the app --
        editor, file browser, notes, help text, dialogs -- without each of
        those screens needing its own e-ink-specific drawing code.

        Reverse-video cells (title/status bars) are drawn filled-black
        with white text. Underlined cells (the cursor's line in the
        editor, the selected row in list screens) get a thin rule under
        the text instead of a full block -- softer to look at than a solid
        highlight bar, and cheaper to refresh since far fewer pixels flip.

        Pure CPU/PIL work, no hardware I/O, so it's cheap to call after
        every screen redraw. Pushing the result to the physical panel is a
        separate, more expensive step (see refresh()/panel_region) that
        callers control independently, so a single keystroke can still do
        a tiny, fast hardware flip instead of repainting everything.

        Limitation: stdscr.inch()'s A_CHARTEXT mask recovers plain ASCII
        reliably but can mangle multi-byte/wide characters on some ncursesw
        builds. Fine for this app's content (plain-text documents, ASCII
        UI chrome); anyone typing heavy non-Latin text may see the rare
        mis-rendered glyph on the e-ink mirror even though the saved file
        content itself is untouched (this reads the screen, never the
        document).
        """
        import curses

        if self.mode != "eink" or self._draw_ctx is None:
            return

        height, width = stdscr.getmaxyx()
        panel_w, panel_h = self.config.display_width, self.config.display_height
        max_cols = min(width, panel_w // CELL_W)
        max_rows = min(height, panel_h // CELL_H)

        self._draw_ctx.rectangle([0, 0, panel_w, panel_h], fill=255)

        for row in range(max_rows):
            y = row * CELL_H
            self._paint_curses_row(stdscr, curses, row, y, max_cols)

    def _paint_curses_row(self, stdscr, curses, row, y, max_cols):
        """
        Paint one text row, batching consecutive cells that share the same
        reverse/underline state into a single PIL text draw call instead of
        one per character -- inch() still has to run per-cell, but this
        keeps the actual drawing to a handful of calls per row.
        """
        run_chars = []
        run_reverse = False
        run_underline = False
        run_start_col = 0

        def flush(end_col):
            if not run_chars:
                return
            text = "".join(run_chars)
            x = run_start_col * CELL_W
            if run_reverse:
                self._draw_ctx.rectangle([x, y, end_col * CELL_W, y + CELL_H], fill=0)
                self._draw_ctx.text((x, y), text, font=self._font, fill=255)
            else:
                self._draw_ctx.text((x, y), text, font=self._font, fill=0)
            if run_underline:
                self._draw_ctx.line(
                    [x, y + CELL_H - 1, end_col * CELL_W, y + CELL_H - 1], fill=0
                )

        for col in range(max_cols):
            try:
                cell = stdscr.inch(row, col)
            except curses.error:
                cell = ord(" ")
            ch = chr(cell & curses.A_CHARTEXT)
            attr = cell & curses.A_ATTRIBUTES
            # The app marks bars with curses.color_pair(2) (an explicit
            # black-on-white pair), not the A_REVERSE attribute bit, so
            # both need checking -- pair 2 is this app's one "inverted bar"
            # convention (title/status bars); A_REVERSE is honored too in
            # case any screen ever sets it directly instead.
            reverse = bool(attr & curses.A_REVERSE) or curses.pair_number(attr) == 2
            underline = bool(attr & curses.A_UNDERLINE)

            if (reverse, underline) != (run_reverse, run_underline):
                flush(col)
                run_chars = []
                run_reverse, run_underline = reverse, underline
                run_start_col = col

            run_chars.append(ch if ch.isprintable() else " ")

        flush(max_cols)

    def show_milestone_screen(self, headline, subtext=""):
        """
        One-time full-screen message shown at boot when a word-count
        milestone was crossed since the last session (see progress.py /
        main.py). No-op outside e-ink mode -- terminal mode shows its own
        plain-curses version instead, handled by the caller.
        """
        if self.mode != "eink":
            return

        def draw(ctx, w, h):
            self._draw_centered_text(ctx, w, h, headline, subtext)

        self._render_full_screen(draw)

    def show_boot_animation(self, logo_path=None, cols=14, rows=5, delay=0.045,
                             hold=0.6):
        """
        One-time boot flourish, all built from the panel's own native
        partial-refresh operation -- no special hardware needed, just a
        sequence of small updates:

          1. Iris reveal: the logo appears from the center outward, like
             an aperture opening -- a nod to the logo itself being
             concentric rings. Blocks are revealed in order of distance
             from the image center rather than a flat left-right/
             diagonal sweep.
          2. Hold, then a full refresh to leave a crisp final image.
          3. Wipe-clear: the whole panel sweeps to blank in vertical
             strips, turning the handoff to the file browser into a
             deliberate transition instead of an abrupt cut.

        Skips itself silently (not an error) if boot_animation is off in
        config or the logo file is missing -- this is a nice-to-have,
        never something that should block or break startup.

        Runs once, at process start, entirely via direct PIL/SPI calls --
        no curses involvement, same as the shutdown/milestone screens.
        """
        if self.mode != "eink":
            return

        if logo_path is None:
            logo_path = Path(__file__).resolve().parent / "art" / "logo.png"
        if not Path(logo_path).exists():
            return

        art = self._load_art_fit(logo_path, self.config.display_width, self.config.display_height)
        if art is None:
            return

        w, h = self.config.display_width, self.config.display_height
        art_w, art_h = art.size
        x0 = max(0, (w - art_w) // 2)
        y0 = max(0, (h - art_h) // 2)

        # Start from a known-blank buffer -- the panel was already
        # cleared during _init_eink(), but this guarantees it regardless
        # of call order, so only the logo's own pixels ever need to flip.
        self._draw_ctx.rectangle([0, 0, w, h], fill=255)

        block_w = max(1, art_w // cols)
        block_h = max(1, art_h // rows)
        blocks = []
        for r in range(rows):
            for c in range(cols):
                bx, by = c * block_w, r * block_h
                bw = block_w if c < cols - 1 else art_w - bx
                bh = block_h if r < rows - 1 else art_h - by
                blocks.append((bx, by, bw, bh))

        # Iris/aperture reveal -- order blocks by distance from the
        # image's center so the wipe expands outward like a ring opening,
        # rather than sweeping flatly across in one direction.
        cx, cy = art_w / 2.0, art_h / 2.0

        def _dist(block):
            bx, by, bw, bh = block
            return ((bx + bw / 2.0 - cx) ** 2 + (by + bh / 2.0 - cy) ** 2) ** 0.5

        blocks.sort(key=_dist)

        for bx, by, bw, bh in blocks:
            region = art.crop((bx, by, bx + bw, by + bh))
            self._image.paste(region, (x0 + bx, y0 + by))
            self._partial_refresh((x0 + bx, y0 + by, bw, bh))
            time.sleep(delay)

        # One clean full refresh at the end: resets the accumulated
        # partial-refresh ghosting from all those small updates and
        # leaves a crisp final image before the hold and wipe-clear.
        self._full_refresh()
        self._partial_count = 0
        time.sleep(hold)   # brief pause so the completed logo is actually seen

        self._wipe_clear()

    def _wipe_clear(self, strips=12, delay=0.03):
        """
        Sweep the entire panel to blank in vertical strips, left to
        right, via real partial refreshes -- the transition out of the
        boot animation and into the file browser, so the logo visibly
        wipes away instead of just cutting to the next screen. Ends with
        a full refresh so the browser's very first draw starts from a
        clean, ghost-free panel.
        """
        w, h = self.config.display_width, self.config.display_height
        strip_w = max(1, w // strips)
        for i in range(strips):
            x = i * strip_w
            sw = strip_w if i < strips - 1 else w - x
            self._draw_ctx.rectangle([x, 0, x + sw, h], fill=255)
            self._partial_refresh((x, 0, sw, h))
            time.sleep(delay)
        self._full_refresh()
        self._partial_count = 0

    def show_bt_waiting_screen(self, mac, attempt):
        """
        Shown at boot when a Bluetooth keyboard is configured
        (config.keyboard_mac) but not yet connected -- landing silently in
        the editor with no way to type or navigate would be worse than a
        clear status screen. Redrawn every few seconds while main.py's
        wait loop keeps polling (see _wait_for_keyboard), so this is a
        full refresh each call like the other full-screen states rather
        than a partial update -- simple and correct for what's meant to be
        a rare, short-lived state, not a per-keystroke hot path.
        """
        if self.mode != "eink":
            return

        def draw(ctx, w, h):
            self._draw_centered_text(
                ctx, w, h,
                "Waiting for keyboard...",
                f"Bluetooth {mac}  (attempt {attempt})",
                "SSH in to reconnect it, or edit config.ini",
            )

        self._render_full_screen(draw)

    def show_shutdown_screen(self, art_path, caption="", layout="centered"):
        """
        Draw a static image right before sleep(). E-ink holds its last
        image with zero power draw, so this persists for as long as the
        device stays off.

        Burn-in note: e-ink doesn't burn in the way OLED does, but holding
        an *identical* static charge pattern for very long stretches can
        leave faint ghosting. Two cheap mitigations, both effectively free
        on a Pi Zero W:
          1. A full black -> full white flash cycle right before drawing,
             which cycles every pixel's charge instead of only the ones
             that differ from whatever was on screen before. This is the
             same technique e-readers use ("flash every N page turns").
          2. Content varies call to call (random quote / growth stage from
             shutdown_screen.py), so the exact same pixel pattern is never
             held twice in a row.
        Both run once, at shutdown -- not during editing -- so neither
        adds any per-keystroke cost.
        """
        if self.mode != "eink" or self._epd is None:
            return

        try:
            self._epd.init()
            self._epd.Clear(0x00)   # full black
            self._epd.Clear(0xFF)   # full white -- clears residual charge
        except Exception as exc:
            log.warning(f"Shutdown flash cycle skipped: {exc}")

        def draw(ctx, w, h):
            if layout == "fullbleed":
                self._draw_fullbleed_with_caption(ctx, w, h, art_path, caption)
            elif layout == "fit":
                self._draw_fit_with_caption(ctx, w, h, art_path, caption)
            else:
                self._draw_art_and_caption(ctx, w, h, art_path, caption)

        self._render_full_screen(draw)

    # ------------------------------------------------------------------
    # Full-screen rendering helpers (milestone + shutdown screens)
    # ------------------------------------------------------------------

    def _render_full_screen(self, draw_fn):
        """
        Draw into the full-panel buffer via draw_fn(draw_ctx, width, height)
        and push a single full refresh. Cheap: plain PIL primitives on an
        already-allocated buffer, no resizing/filtering, one hardware
        refresh call -- negligible CPU even on a Pi Zero W.
        """
        if self.mode != "eink" or self._draw_ctx is None:
            return
        self._draw_ctx.rectangle(
            [0, 0, self.config.display_width, self.config.display_height], fill=255
        )
        draw_fn(self._draw_ctx, self.config.display_width, self.config.display_height)
        self._full_refresh()
        self._partial_count = 0

    def _draw_centered_text(self, ctx, w, h, *lines):
        lines = [ln for ln in lines if ln]
        line_h = 16
        total_h = len(lines) * line_h
        y = max(0, (h - total_h) // 2)
        for line in lines:
            tw = ctx.textlength(line, font=self._font)
            x = max(0, int((w - tw) // 2))
            ctx.text((x, y), line, font=self._font, fill=0)
            y += line_h

    def _load_art(self, art_path):
        """
        Load a pixel-art PNG and cache it in memory. Shutdown/milestone
        screens are the only callers and each only fires once per boot at
        most, but caching means a second shutdown in the same run (e.g.
        power off, power back on, power off again without a reboot in
        between) doesn't re-hit the SD card.
        """
        from PIL import Image

        key = str(art_path)
        cached = self._art_cache.get(key)
        if cached is not None:
            return cached
        try:
            img = Image.open(art_path).convert("1")
        except Exception as exc:
            log.warning(f"Could not load art asset {art_path}: {exc}")
            img = None
        self._art_cache[key] = img
        return img

    def _load_art_cover(self, art_path, target_w, target_h):
        """
        Load an arbitrary image (any format/mode PIL can read -- PNG, JPG,
        whatever a photo/background gets exported as) and cover-fit it to
        exactly (target_w, target_h): scale up to cover the target box,
        then crop the overflow using a content-aware offset (see
        _smart_crop_offset) rather than blindly cropping to center --
        landscape art often has its subject off-center, and a plain
        center-crop on a very wide/short panel can cut the subject out
        entirely. NEAREST-only resize, no smoothing, so pixel art stays
        crisp with no blur/antialiasing. Dithered to 1-bit with
        Floyd-Steinberg afterwards so continuous-tone shading (unlike the
        pre-dithered flat-color growth sprites) still reads on 1-bit
        e-ink instead of crushing to solid black/white -- that's a
        separate step from the resize and doesn't reintroduce any blur.

        Cached per (path, target size) -- the cover-crop depends on the
        target box, so a different caption height (with/without text)
        would need a different crop. Only ever runs at most once or twice
        per boot (shutdown screen only), so the one-time dither cost here
        is a non-issue even on a Pi Zero W.
        """
        from PIL import Image

        key = (str(art_path), target_w, target_h)
        cached = self._art_cache.get(key)
        if cached is not None:
            return cached

        try:
            img = Image.open(art_path).convert("RGB")
            src_w, src_h = img.size
            scale = max(target_w / src_w, target_h / src_h)
            new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
            img = img.resize((new_w, new_h), Image.NEAREST)
            left, top = self._smart_crop_offset(img, target_w, target_h)
            img = img.crop((left, top, left + target_w, top + target_h))
            img = img.convert("L").convert("1", dither=Image.FLOYDSTEINBERG)
        except Exception as exc:
            log.warning(f"Could not load background art {art_path}: {exc}")
            img = None

        self._art_cache[key] = img
        return img

    def _smart_crop_offset(self, img, target_w, target_h):
        """
        Pick a crop offset that keeps the most visually "busy" region in
        frame, instead of blindly cropping to center. Landscape art often
        has its subject off-center (temple in the lower third, ship high
        in the sky), so a plain center-crop on a very wide/short panel can
        cut the subject out entirely.

        Cheap saliency proxy: per-row / per-column gradient energy (how
        much pixels change along that row/column -- flat sky and ground
        score low, detailed structures score high), then a sliding-window
        sum to find the target-sized band with the most total energy.
        numpy is already a required dependency (see INSTALL.md), and this
        is one array pass over one image at most once or twice per boot --
        negligible even on a Pi Zero W.
        """
        import numpy as np

        gray = np.asarray(img.convert("L"), dtype=np.int16)
        new_h, new_w = gray.shape

        if new_h > target_h:
            row_energy = np.abs(np.diff(gray, axis=0)).sum(axis=1)
            row_energy = np.append(row_energy, row_energy[-1])
            cumsum = np.concatenate(([0], np.cumsum(row_energy)))
            window_sums = cumsum[target_h:] - cumsum[:-target_h]
            top = int(np.argmax(window_sums))
        else:
            top = 0

        if new_w > target_w:
            col_energy = np.abs(np.diff(gray, axis=1)).sum(axis=0)
            col_energy = np.append(col_energy, col_energy[-1])
            cumsum = np.concatenate(([0], np.cumsum(col_energy)))
            window_sums = cumsum[target_w:] - cumsum[:-target_w]
            left = int(np.argmax(window_sums))
        else:
            left = 0

        return left, top

    def _load_art_fit(self, art_path, target_w, target_h):
        """
        Load an arbitrary image and fit (contain) it within (target_w,
        target_h), preserving aspect ratio with no cropping -- the whole
        image stays visible, letterboxed with the panel's white background
        on whichever axis has slack. NEAREST-only scaling, same as
        _load_art_cover. Used when the source aspect ratio is too far from
        the panel's to crop sensibly without cutting off the composition.
        """
        from PIL import Image

        key = ("fit", str(art_path), target_w, target_h)
        cached = self._art_cache.get(key)
        if cached is not None:
            return cached

        try:
            img = Image.open(art_path).convert("RGB")
            src_w, src_h = img.size
            scale = min(target_w / src_w, target_h / src_h)
            new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
            img = img.resize((new_w, new_h), Image.NEAREST)
            img = img.convert("L").convert("1", dither=Image.FLOYDSTEINBERG)
        except Exception as exc:
            log.warning(f"Could not load background art {art_path}: {exc}")
            img = None

        self._art_cache[key] = img
        return img

    def _draw_fit_with_caption(self, ctx, w, h, art_path, caption=""):
        """Whole image visible, centered, letterboxed by the panel's
        default white background -- no cropping, unlike _draw_fullbleed_with_caption."""
        line_h = 16
        caption_h = line_h + 8 if caption else 0
        image_h = h - caption_h

        art = self._load_art_fit(art_path, w, image_h) if art_path else None
        if art is not None:
            art_w, art_h = art.size
            x = max(0, (w - art_w) // 2)
            y = max(0, (image_h - art_h) // 2)
            self._image.paste(art, (x, y))

        if caption:
            ctx.rectangle([0, image_h, w, h], fill=255)
            tw = ctx.textlength(caption, font=self._font)
            x = max(0, int((w - tw) // 2))
            ctx.text((x, image_h + 4), caption, font=self._font, fill=0)

    def _draw_fullbleed_with_caption(self, ctx, w, h, art_path, caption=""):
        """
        Full-panel background image (e.g. a user-supplied sci-fi piece)
        with a solid white strip reserved at the bottom for the caption --
        guarantees the text stays readable no matter how busy the image
        is, rather than risking it landing on a dark/detailed area.
        """
        line_h = 16
        caption_h = line_h + 8 if caption else 0
        image_h = h - caption_h

        art = self._load_art_cover(art_path, w, image_h) if art_path else None
        if art is not None:
            self._image.paste(art, (0, 0))

        if caption:
            ctx.rectangle([0, image_h, w, h], fill=255)
            tw = ctx.textlength(caption, font=self._font)
            x = max(0, int((w - tw) // 2))
            ctx.text((x, image_h + 4), caption, font=self._font, fill=0)

    def _draw_art_and_caption(self, ctx, w, h, art_path, caption=""):
        """
        Pixel-art image centered in the upper portion of the panel,
        caption pinned near the bottom edge. Pasting a pre-rendered 1-bit
        PNG is just a memory copy -- no resizing/filtering -- so this is
        cheap even on a Pi Zero W. Art is drawn at its native pixel size
        (no scaling) to keep every pixel crisp.
        """
        line_h = 16
        caption_h = line_h + 8 if caption else 0

        art = self._load_art(art_path) if art_path else None
        if art is not None:
            art_w, art_h = art.size
            x = max(0, (w - art_w) // 2)
            y = max(4, (h - art_h - caption_h) // 2)
            self._image.paste(art, (x, y))

        if caption:
            tw = ctx.textlength(caption, font=self._font)
            x = max(0, int((w - tw) // 2))
            ctx.text((x, h - line_h - 4), caption, font=self._font, fill=0)

    def sleep(self):
        """Put e-ink panel into low-power sleep.  No-op otherwise."""
        if self.mode == "eink" and self._epd:
            try:
                self._epd.sleep()
            except Exception as exc:
                log.warning(f"Display sleep failed: {exc}")

    def wake(self):
        """Wake e-ink panel from sleep.  No-op otherwise."""
        if self.mode == "eink" and self._epd:
            try:
                self._epd.init()
            except Exception as exc:
                log.warning(f"Display wake failed: {exc}")

    # ------------------------------------------------------------------
    # Internal e-ink refresh helpers
    # ------------------------------------------------------------------

    def _full_refresh(self):
        if self._epd is None or self._image is None:
            return
        try:
            self._epd.init()
            self._epd.display(self._epd.getbuffer(self._image))
        except Exception as exc:
            log.warning(f"Full e-ink refresh failed: {exc}")

    def _partial_refresh(self, region=None):
        if self._epd is None or self._image is None:
            return
        try:
            if region is not None and hasattr(self._epd, "displayPartial"):
                x, y, w, h = region
                if hasattr(self._epd, "init_Fast"):
                    self._epd.init_Fast()
                self._epd.displayPartial(
                    self._epd.getbuffer(self._image), x, y, w, h
                )
            else:
                if hasattr(self._epd, "init_Fast"):
                    self._epd.init_Fast()
                self._epd.display(self._epd.getbuffer(self._image))
        except Exception as exc:
            log.warning(f"Partial e-ink refresh failed: {exc}")
