#!/usr/bin/env python3
"""
Render a preview GIF of the e-ink boot animation on a regular computer --
no Pi or e-ink hardware required.

This reimplements the same logic as Display.show_boot_animation() /
Display._wipe_clear() in display.py: an iris-style reveal from the
image's center outward (growing filled circles), then a wipe-to-blank
transition -- but instead of pushing each step to a physical panel via
SPI, it saves each step as a frame and stitches them into an animated
GIF you can open anywhere. Keep this in sync with display.py if you
tune the real animation.

Note on frame count vs. real hardware: this preview renders many extra
in-between frames (`substeps` per ring) purely so the GIF plays back
smoothly. The real device does none of that -- it only performs `steps`
+ `wipe_strips` + 2 actual panel refreshes total (see the timing note
in show_boot_animation()'s docstring), since each one costs real
physical time on the panel. Frame count here has no bearing on real
boot time.

Usage:
    python3 tools/preview_boot_animation.py [path/to/logo.png] [output.gif]

Defaults to inkwriter/art/logo.png and boot_animation_preview.gif in the
current directory.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def render_preview(logo_path, out_path, width=792, height=272,
                    steps=6, substeps=6, frame_ms=25, hold_ms=600,
                    wipe_strips=4, wipe_ms=60, end_hold_ms=700):
    logo_path = Path(logo_path)
    if not logo_path.exists():
        raise SystemExit(f"Logo not found: {logo_path}")

    # Same "fit" (contain, letterboxed, dithered to 1-bit) treatment as
    # Display._load_art_fit, so the preview matches what the panel would
    # actually show.
    src = Image.open(logo_path).convert("RGB")
    src_w, src_h = src.size
    scale = min(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    art = src.resize((new_w, new_h), Image.NEAREST)
    art = art.convert("L").convert("1", dither=Image.FLOYDSTEINBERG)

    art_w, art_h = art.size
    x0 = max(0, (width - art_w) // 2)
    y0 = max(0, (height - art_h) // 2)

    canvas = Image.new("1", (width, height), 255)
    draw_frames = []
    durations = []

    def snapshot(ms):
        draw_frames.append(canvas.convert("L").convert("RGB"))
        durations.append(ms)

    snapshot(frame_ms)

    # --- Iris reveal: same growing-circle mask as show_boot_animation(),
    # just sampled at steps*substeps points instead of `steps` for
    # smoother GIF playback (cosmetic only, see module docstring).
    cx, cy = art_w / 2.0, art_h / 2.0
    max_r = (cx ** 2 + cy ** 2) ** 0.5
    blank = Image.new("1", (art_w, art_h), 255)
    total = steps * substeps

    for i in range(1, total + 1):
        r = max_r * i / total
        mask = Image.new("L", (art_w, art_h), 0)
        ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
        frame = Image.composite(art, blank, mask)
        canvas.paste(frame, (x0, y0))
        snapshot(frame_ms)

    # Hold on the completed logo.
    snapshot(hold_ms)

    # --- Wipe-clear: sweep the whole panel to blank in a few vertical
    # strips, matching Display._wipe_clear()'s default strip count.
    strip_w = max(1, width // wipe_strips)
    for i in range(wipe_strips):
        x = i * strip_w
        sw = strip_w if i < wipe_strips - 1 else width - x
        canvas = canvas.copy()
        ImageDraw.Draw(canvas).rectangle([x, 0, x + sw, height], fill=255)
        snapshot(wipe_ms)

    # A couple of held blank frames so it's clear the wipe finished
    # (this is the moment the real UI would appear).
    snapshot(end_hold_ms)

    # Upscale for visibility -- the real panel is physically ~5.8in, tiny
    # on a laptop screen at 1:1 pixel size.
    scale_up = 2
    frames = [f.resize((width * scale_up, height * scale_up), Image.NEAREST) for f in draw_frames]

    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
    )
    print(f"Wrote {out_path} ({len(frames)} frames)")


if __name__ == "__main__":
    logo = sys.argv[1] if len(sys.argv) > 1 else "inkwriter/art/logo.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "boot_animation_preview.gif"
    render_preview(logo, out)
