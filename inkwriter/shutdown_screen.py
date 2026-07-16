"""
Builds the *content* for the e-ink shutdown screen: which pixel-art asset
to show and one caption line underneath it. Deliberately separate from
Display -- this module only picks a filename and a string, it never
touches hardware or does any drawing, so it's easy to test on its own and
Display stays a thin rendering layer.

Art is a small, hand-authored set of growth-stage PNGs in inkwriter/art/
(see art/ -- generated once, shipped as static files), not procedurally
generated. A handful of curated stages look far better than anything
generated at runtime, and picking one is just a threshold lookup --
effectively free on a Pi Zero W.
"""

import logging
import random
from pathlib import Path

log = logging.getLogger(__name__)

_ART_DIR = Path(__file__).resolve().parent / "art"
# Drop any full-panel background images here (PNG/JPG, any resolution --
# they get cover-fit and dithered at draw time) to use the "custom" mode.
_CUSTOM_ART_DIR = _ART_DIR / "custom"
_CUSTOM_EXTS = {".png", ".jpg", ".jpeg"}

# (lifetime_words threshold, art filename). Sorted ascending; the highest
# threshold at or below the current lifetime count wins.
_GROWTH_STAGES = [
    (0,     "growth_1_seed.png"),
    (1000,  "growth_2_sprout.png"),
    (5000,  "growth_3_plant.png"),
    (10000, "growth_4_bush.png"),
    (25000, "growth_5_tree.png"),
    (50000, "growth_6_blooming.png"),
]

_DEFAULT_QUOTE = "the page is waiting for you"


def _growth_art_path(lifetime_words):
    filename = _GROWTH_STAGES[0][1]
    for threshold, name in _GROWTH_STAGES:
        if lifetime_words >= threshold:
            filename = name
        else:
            break
    return _ART_DIR / filename


def _pick_quote(file_manager):
    """
    Grab one line >= 40 chars from a random document, cheaply: shuffle the
    file list, open at most a handful of files, stop at the first usable
    line. Never scans the whole document library -- bounded work no matter
    how many files exist.
    """
    if file_manager is None:
        return _DEFAULT_QUOTE
    try:
        files = file_manager.list_all_files()
    except Exception as exc:
        log.warning(f"Shutdown quote: could not list files ({exc})")
        return _DEFAULT_QUOTE

    if not files:
        return _DEFAULT_QUOTE

    random.shuffle(files)
    for path in files[:8]:
        try:
            content = file_manager.load_file(path)
        except Exception:
            continue
        candidates = [ln.strip() for ln in content.splitlines() if len(ln.strip()) >= 40]
        if candidates:
            return random.choice(candidates)

    return _DEFAULT_QUOTE


def _pick_custom_background():
    """Random file from art/custom/, or None if the folder is empty/missing.
    Randomizing which one shows (like the quote picker) means the exact
    same pixel pattern isn't held on screen every single shutdown, which
    is one of the two burn-in mitigations noted in Display.show_shutdown_screen."""
    if not _CUSTOM_ART_DIR.is_dir():
        return None
    candidates = [
        p for p in _CUSTOM_ART_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _CUSTOM_EXTS
    ]
    if not candidates:
        return None
    return random.choice(candidates)


def _clamp_caption(text, max_len=60):
    text = text.strip()
    if len(text) > max_len:
        text = text[: max_len - 3].rstrip() + "..."
    return text


def build(config, file_manager, progress):
    """
    Return (art_path, caption, layout) for the configured shutdown mode,
    or None if shutdown screens are turned off. `progress` may be None
    (growth tracking disabled) -- falls back to the earliest growth stage
    and a generic caption. `layout` is "centered" (small art, lots of
    white space -- the growth sprites) or "fullbleed" (image fills the
    whole panel, caption in a reserved white strip -- custom backgrounds).
    """
    mode = getattr(config, "shutdown_screen", "off")
    if mode == "off":
        return None

    lifetime_words = progress.data.get("lifetime_words", 0) if progress else 0
    streak_days = progress.data.get("streak_days", 0) if progress else 0

    if mode == "custom":
        bg = _pick_custom_background()
        if bg is None:
            log.warning(
                "shutdown_screen=custom but inkwriter/art/custom/ has no "
                "images -- falling back to growth mode"
            )
            mode = "growth"
        else:
            caption = f"{lifetime_words} words and counting" if lifetime_words else ""
            # "fullbleed" -- edge-to-edge, cropped to fill the panel. The
            # crop itself is content-aware (see Display._smart_crop_offset),
            # so it centers on the busiest/most detailed part of the scene
            # instead of a blind center-crop that could cut off the subject.
            return bg, _clamp_caption(caption), "fullbleed"

    art_path = _growth_art_path(lifetime_words)

    if mode == "quote":
        caption = _pick_quote(file_manager)
    elif mode == "stats":
        caption = f"{lifetime_words} words written - {streak_days} day streak"
    elif mode == "growth":
        caption = f"{lifetime_words} words and counting"
    else:
        log.warning(f"Unknown shutdown_screen mode '{mode}', defaulting to growth")
        caption = f"{lifetime_words} words and counting"

    return art_path, _clamp_caption(caption), "centered"
