# Inkwriter: ADHD-focused feature plan

Goal: make Inkwriter more rewarding and lower-friction to open and keep using,
specifically targeting ADHD/executive-dysfunction barriers to writing (blank-page
paralysis, no immediate feedback, task-switching cost, delayed gratification).
Autism-friendly guardrails stay in place as a baseline (nothing punitive, nothing
that pops up uninvited, everything below is toggleable) but aren't the primary
design driver for this pass.

Three phases, each shippable and testable on its own. Do not start phase 2 until
phase 1 is on the Pi and has been used for a few real sessions — the point of
phasing is to find out which mechanics actually help before building more on
top of them.

---

## Phase 1 — Remove friction to starting (highest leverage, build first)

The core ADHD problem this app fights is *getting started*, not staying
motivated once rolling. These changes touch existing files only; no new stats
engine, no new screens.

### 1.1 True resume-exactly-where-you-left-off

**Current state:** `FileManager.create_new_file()` / opening a file from the
browser always starts the editor at row 0, col 0, scroll_offset 0.

**Change:** Persist `(cursor_row, cursor_col, scroll_offset)` per file. Simplest
storage: a sidecar `.inkwriter_state.json` in the documents dir, keyed by
relative file path, written on every save (`Editor._save()`) and read in
`Editor._load()`.

Files touched: `editor.py` (`_load`, `_save`), `file_manager.py` (new
`load_cursor_state(path)` / `save_cursor_state(path, row, col, scroll)`
helpers so the JSON format lives in one place).

Edge cases: file edited outside Inkwriter (line count changed) — clamp
cursor_row to `len(self.lines) - 1` on load instead of trusting the stored
value blindly.

### 1.2 Skip the "new file" dialog entirely

**Current state:** need to check `file_browser.py` / `file_manager.py` for
whether `create_new_file()` already prompts for a name or just generates one
(e.g. timestamp-based) — if it already auto-names, this item is already done
and can be removed from the plan during kickoff.

**Change (if needed):** `create_new_file()` should generate a name
(`untitled-YYYYMMDD-HHMMSS.txt` or similar) and drop straight into the editor
with an empty buffer, no text-entry prompt. Renaming happens later via the
existing Ctrl+R rename shortcut, which the docstring in `editor.py` already
lists as a keybinding — so this is a matter of confirming the rename path
still works when the file started with an auto-generated name.

### 1.3 "Today's words" in the status bar

**Current state:** status bar already shows `Ln / Col / Words` (total words in
current file) — see `editor.py` `_draw()` status line construction.

**Change:** add a running "today" counter: words written today across all
files, computed as `today_total = max(0, current_word_count_at_session_start_per_file... )`.
Simplify: track a single integer `words_written_today` in the same state file
from 1.1, incremented by the delta each time `_get_content()` word count goes
up between saves (never decremented — deleting text you wrote earlier today
shouldn't claw back your count, that's punitive). Reset when the stored date
rolls over to a new day.

Files touched: `editor.py` (status bar string, delta tracking on save),
`file_manager.py` (state file already extended in 1.1, add `words_today` /
`words_today_date` fields to the same JSON).

### Phase 1 config additions (`config.py`)

```
[editor]
remember_cursor_position = true
show_today_word_count = true
```

Both default `true` — this phase is pure friction removal, no reason to hide
it behind opt-in.

---

## Phase 2 — Gentle reward / growth system

Only build this after phase 1 has been used for real. Everything here is
**additive, non-punitive, and fully toggleable** via one master config flag —
if it turns out streaks/growth stuff is more annoying than motivating in
practice, `show_growth_features = false` turns all of it off and the app goes
back to exactly phase 1 behavior.

### 2.1 Lifetime stats store

New file: `inkwriter/stats.py`. Owns a single JSON file
(`~/.config/inkwriter/stats.json`) tracking:

```json
{
  "lifetime_words": 0,
  "sessions": 0,
  "milestones_seen": [1000, 10000],
  "streak_days": 0,
  "streak_last_active": "2026-07-15",
  "streak_frozen_until": null
}
```

`streak_days` increments once per calendar day with any writing activity.
Missing a day does **not** reset it to zero — it sets `streak_frozen_until`
to "resume within 3 days to keep it" or similar grace window, framed in the
UI as a pause, never a loss. No animation, no red warning color (there is no
color) — just plain text like `streak: 12 days (paused, write today to
resume)`.

### 2.2 Session summary on exit

When leaving the editor back to the browser (not on every save — that would
be noisy), show a single status-bar-height line for ~3 seconds (reusing the
existing `self.message` / `self.message_time` mechanism already in
`editor.py` — no new UI primitive needed):

`"847 words today · 12 day streak · nice session"`

No popup, no keypress required to dismiss — it just uses the same
timed-message mechanic the status bar already has for save confirmations.

### 2.3 Milestone flourish

At lifetime word milestones (1k, 5k, 10k, 25k, 50k, 100k...), show one
full-screen static message on the *next* app boot (not mid-sentence while
writing — never interrupt active writing) using the same box-drawing-border
treatment planned for visual polish. Dismiss on any keypress. Recorded in
`milestones_seen` so it only ever fires once per milestone.

### Phase 2 config additions

```
[growth]
show_growth_features = true
show_session_summary = true
show_milestones = true
streak_grace_days = 3
```

---

## Phase 3 — Shutdown screen

Depends on phase 2's stats store for content (word count / growth visual) but
the mechanism itself doesn't — build the plumbing anytime, wire up content
once phase 2 exists.

### 3.1 Mechanism

E-ink holds its last image with zero power draw, so "shutdown screen" really
means: draw one final full image before `display.sleep()`, then never
refresh again until `wake()`. Hook point: `main.py`'s `finally: display.sleep()`
becomes `finally: display.show_shutdown_screen(); display.sleep()`.

New method on `Display` (`display.py`): `show_shutdown_screen()` — builds a
full-panel PIL image (reusing `self._font`, the existing Spleen bitmap font)
and does one full refresh (never partial — this image needs to persist
cleanly, no ghosting from a partial-refresh region).

### 3.2 Content options (config-selectable, pick one)

- `quote` — pull a random line ≥40 chars from the user's own writing
  (scan documents_dir, pick one file, pick one line). Personal, no
  external dependency.
- `stats` — lifetime words, streak, session count, plain text, centered.
- `growth` — simple ASCII-art scene (tree/garden) whose complexity scales
  with `lifetime_words` from the stats store (e.g. a handful of growth
  stages, not procedurally generated — a fixed set of 5-6 ASCII scenes
  swapped at word-count thresholds is far easier to make actually look
  good than anything generative).
- `off` — just sleep, no shutdown image (for anyone who'd rather the
  screen go properly blank/last-editor-state).

### Phase 3 config additions

```
[display]
shutdown_screen = "quote"   # quote | stats | growth | off
```

---

## What's deliberately out of scope for this pass

- Anything resembling point/currency systems (gold, XP shop) — adds
  complexity disproportionate to a single-user offline device and drifts
  toward the "gamification as pressure" pattern the autism-friendly
  research flagged.
- Sound/haptics — hardware doesn't have them, not worth scoping.
- Any streak mechanic that *resets to zero* — grace-window pause only.
- Anything that pops up while actively typing — all feedback is either in
  the status bar (existing mechanic) or shown at natural transition points
  (file close, app boot), never mid-sentence.

## Build order recap

1. Phase 1 (1.1 → 1.2 → 1.3), ship, use it for a few days.
2. Phase 2 (2.1 → 2.2 → 2.3), ship, use it, decide if growth features earn
   their keep or should stay off by default.
3. Phase 3, wire to whichever phase 2 content ended up feeling right.
