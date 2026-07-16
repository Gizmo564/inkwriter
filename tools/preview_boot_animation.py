#!/usr/bin/env python3
"""
Render a preview GIF of the e-ink boot animation on a regular computer --
no Pi or e-ink hardware required.

This reimplements the same diagonal-cascade block logic as
Display.show_boot_animation() in display.py, but instead of pushing each
step to a physical panel via SPI, it saves each step as a frame and
stitches them into an animated GIF you can open anywhere.

Usage:
    python3 tools/preview_boot_animation.py [path/to/logo.png] [output.gif]

Defaults to inkwriter/art/logo.png and boot_animation_preview.gif in the
current directory.
"""

import sys
from pathlib import Path

from PIL import Image, ImageOps


def render_preview(logo_path, out_path, width=792, height=272, cols=6, rows=3,
                    frame_ms=80, hold_ms=900):
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

    block_w = max(1, art_w // cols)
    block_h = max(1, art_h // rows)
    blocks = []
    for r in range(rows):
        for c in range(cols):
            bx, by = c * block_w, r * block_h
            bw = block_w if c < cols - 1 else art_w - bx
            bh = block_h if r < rows - 1 else art_h - by
            blocks.append((c, r, bx, by, bw, bh))
    blocks.sort(key=lambda b: (b[1] + b[0], b[0]))

    frames = [canvas.convert("L").convert("RGB")]
    for c, r, bx, by, bw, bh in blocks:
        region = art.crop((bx, by, bx + bw, by + bh))
        canvas.paste(region, (x0 + bx, y0 + by))
        frames.append(canvas.convert("L").convert("RGB"))

    # A few held frames of the completed logo, matching the ~0.8s hold in
    # show_boot_animation() before the file browser appears.
    hold_frames = max(1, hold_ms // frame_ms)
    frames.extend([frames[-1]] * hold_frames)

    durations = [frame_ms] * (len(frames) - hold_frames) + [frame_ms] * hold_frames

    # Upscale for visibility -- the real panel is physically ~5.8in, tiny
    # on a laptop screen at 1:1 pixel size.
    scale_up = 2
    frames = [f.resize((width * scale_up, height * scale_up), Image.NEAREST) for f in frames]

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
