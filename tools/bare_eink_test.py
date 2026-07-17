#!/usr/bin/env python3
"""
Bare-bones e-ink SPI test -- NOT the real Inkwriter app.

A minimal, standalone text editor that talks directly to the epd5in79
driver, with none of Inkwriter's other subsystems involved: no boot
animation, no growth/progress tracking, no shutdown screens, no image
loading, no file browser, no config.py, no safe_mode. Built specifically
to isolate whether the SPI/e-ink hardware path itself causes instability,
with as little other code running as possible so there's nothing else to
blame.

Run this manually in the foreground over SSH -- NOT as a systemd service.
That way you can watch its step-by-step prints directly and Ctrl+C out
immediately if something hangs, rather than waiting on a systemd stop
timeout:

    cd ~/inkwriter
    python3 tools/bare_eink_test.py

Everything is kept in memory while running. On a clean exit (Ctrl+Q)
your typed text is dumped to bare_test_output.txt in the project root,
purely so you don't lose it -- there's no autosave, no file management.

Watch the printed output closely, especially around "Calling
epd.init()" -- if that's the last line printed and nothing follows, the
hang is happening inside the driver's own init()/ReadBusy() call, which
is exactly the fault this script exists to isolate.
"""
import curses
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WIDTH, HEIGHT = 792, 272
DRIVER = "epd5in79"


def init_display():
    """Import + init the panel directly. No Inkwriter Display class, no
    art loading -- minimal surface area so a hang or crash here can only
    be the driver/hardware, not our own code."""
    import importlib

    print("Importing waveshare_epd." + DRIVER + " ...", flush=True)
    epd_module = importlib.import_module(f"waveshare_epd.{DRIVER}")
    print("Import OK. Constructing EPD() ...", flush=True)
    epd = epd_module.EPD()
    print("EPD() constructed. Calling epd.init() -- this is the step that", flush=True)
    print("hung before. Waiting for it to return...", flush=True)
    epd.init()
    print("epd.init() returned OK.", flush=True)
    print("Calling Clear() ...", flush=True)
    try:
        epd.Clear(0xFF)
    except TypeError:
        epd.Clear()
    print("Clear() returned OK. Panel should be blank/white now.", flush=True)
    return epd, epd_module


def render_text(epd, text):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("1", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 16
        )
    except Exception:
        font = ImageFont.load_default()

    lines = []
    for raw_line in text.split("\n"):
        while len(raw_line) > 90:
            lines.append(raw_line[:90])
            raw_line = raw_line[90:]
        lines.append(raw_line)
    lines = lines[-16:]

    y = 4
    for line in lines:
        draw.text((4, y), line, font=font, fill=0)
        y += 16

    epd.display(epd.getbuffer(img))


def curses_main(stdscr, epd):
    curses.curs_set(1)
    # Timeout (not indefinite blocking) so Ctrl+C/signals are never stuck
    # waiting on a keypress that may never come -- fixes the same class
    # of bug we found in the real app's shutdown handling.
    stdscr.timeout(500)
    stdscr.clear()
    stdscr.addstr(0, 0, "Bare e-ink test. Type freely. Ctrl+Q to quit and save.")
    stdscr.refresh()

    buf = ""
    last_render = 0.0
    dirty = False

    while True:
        try:
            ch = stdscr.get_wch()
        except curses.error:
            ch = None  # just a timeout tick, no key pressed
        except KeyboardInterrupt:
            break

        if ch is not None:
            if ch == "\x11":  # Ctrl+Q
                break
            elif ch in ("\n", "\r"):
                buf += "\n"
                dirty = True
            elif ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
                buf = buf[:-1]
                dirty = True
            elif isinstance(ch, str) and ch.isprintable():
                buf += ch
                dirty = True

            stdscr.clear()
            stdscr.addstr(0, 0, "Bare e-ink test. Ctrl+Q to quit and save.")
            for i, line in enumerate(buf.split("\n")[-15:]):
                stdscr.addstr(2 + i, 0, line[:78])
            stdscr.refresh()

        now = time.time()
        if dirty and (now - last_render) > 2.0:
            render_text(epd, buf)
            last_render = now
            dirty = False

    if dirty:
        render_text(epd, buf)

    return buf


def main():
    print("=== Inkwriter bare e-ink test ===")
    print("Talks directly to the panel driver. Nothing else Inkwriter")
    print("normally does is running -- no config, no growth tracking, no")
    print("images, no file browser. Ctrl+C at any point to abort.\n")

    epd, _epd_module = init_display()

    try:
        buf = curses.wrapper(curses_main, epd)
    finally:
        try:
            epd.sleep()
            print("epd.sleep() OK.")
        except Exception as exc:
            print(f"epd.sleep() raised (non-fatal): {exc}")

    out_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "bare_test_output.txt"
    )
    with open(out_path, "w") as f:
        f.write(buf)
    print(f"\nSaved what you typed to {out_path}")


if __name__ == "__main__":
    main()
