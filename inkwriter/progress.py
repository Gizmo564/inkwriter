"""
Shared progress store: per-file cursor position, today's word count,
lifetime word count, and a non-punitive writing streak.

Single JSON file (~/.config/inkwriter/progress.json), one instance created
once in main.py and passed down to Editor and to the shutdown-screen
builder -- avoids re-reading the file from disk every time a new Editor is
opened in the same run, which matters on SD-card storage.

Design rule carried over from the planning doc: nothing here can go
backwards in a way that feels like a loss. Word counts only ever add
(deleting text you wrote earlier doesn't claw back credit), and the streak
has a grace window instead of resetting to zero on a missed day.
"""

import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Lifetime word-count thresholds that trigger a one-time milestone screen
# on the next app boot. Deliberately not configurable -- keeping this list
# fixed means milestones_seen (below) stays simple to reason about.
_MILESTONES = [1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000]


class Progress:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.config_dir) / "progress.json"
        self._data = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning(f"Progress file unreadable ({exc}); starting fresh")
        return {
            "lifetime_words": 0,
            "sessions": 0,
            "milestones_seen": [],
            "pending_milestone": None,
            "streak_days": 0,
            "streak_last_active": None,
            "words_today": 0,
            "words_today_date": None,
            "cursors": {},
        }

    def save(self):
        """Atomic write (tmp file + rename) so a crash mid-write can't leave
        progress.json corrupted -- this file is read on every boot."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:
            log.warning(f"Progress save failed: {exc}")

    @property
    def data(self):
        """Read-only-by-convention access for the shutdown-screen builder."""
        return self._data

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def record_session(self):
        self._data["sessions"] = self._data.get("sessions", 0) + 1

    # ------------------------------------------------------------------
    # Cursor position (Phase 1.1)
    # ------------------------------------------------------------------

    def get_cursor(self, filepath):
        entry = self._data.get("cursors", {}).get(str(Path(filepath)))
        if not entry:
            return None
        return entry.get("row", 0), entry.get("col", 0), entry.get("scroll", 0)

    def set_cursor(self, filepath, row, col, scroll):
        cursors = self._data.setdefault("cursors", {})
        cursors[str(Path(filepath))] = {"row": row, "col": col, "scroll": scroll}
        # Cap how many files we remember, so progress.json doesn't grow
        # forever on an SD card over months of use.
        if len(cursors) > 200:
            for key in list(cursors.keys())[: len(cursors) - 200]:
                del cursors[key]

    # ------------------------------------------------------------------
    # Word tracking / streak / milestones (Phase 1.3, 2.1)
    # ------------------------------------------------------------------

    @property
    def words_today(self):
        if self._data.get("words_today_date") != date.today().isoformat():
            return 0
        return self._data.get("words_today", 0)

    @property
    def streak_days(self):
        return self._data.get("streak_days", 0)

    def record_words(self, delta):
        """
        Register newly-written words (delta must be > 0 -- callers only call
        this when a save's word count went up). Updates lifetime total,
        today's total, and the streak, and returns a milestone value if one
        was just crossed (else None).
        """
        if delta <= 0:
            return None
        self._data["lifetime_words"] = self._data.get("lifetime_words", 0) + delta
        self._touch_today(delta)
        self._touch_streak()
        return self._check_milestone()

    def _touch_today(self, delta):
        today_str = date.today().isoformat()
        if self._data.get("words_today_date") != today_str:
            self._data["words_today_date"] = today_str
            self._data["words_today"] = 0
        self._data["words_today"] = self._data.get("words_today", 0) + delta

    def _touch_streak(self):
        today = date.today()
        today_str = today.isoformat()
        last = self._data.get("streak_last_active")

        if last == today_str:
            return  # already counted today, nothing to do

        if last:
            gap = (today - date.fromisoformat(last)).days
            grace = getattr(self.config, "streak_grace_days", 3)
            if gap <= 1 + grace:
                # Same day, next day, or within the grace window -- streak
                # continues. A multi-day gap inside the grace window still
                # only adds 1, not 1-per-missed-day.
                self._data["streak_days"] = self._data.get("streak_days", 0) + 1
            else:
                # Grace window passed. Quietly restart at 1 -- never framed
                # to the user as "you lost your streak", just resumes.
                self._data["streak_days"] = 1
        else:
            self._data["streak_days"] = 1

        self._data["streak_last_active"] = today_str

    def _check_milestone(self):
        lifetime = self._data.get("lifetime_words", 0)
        seen = set(self._data.get("milestones_seen", []))
        for m in _MILESTONES:
            if lifetime >= m and m not in seen:
                seen.add(m)
                self._data["milestones_seen"] = sorted(seen)
                self._data["pending_milestone"] = m
                return m
        return None

    def pop_pending_milestone(self):
        """Called once at boot. Returns and clears any milestone earned
        since the last boot, so it only ever displays once."""
        m = self._data.get("pending_milestone")
        if m is not None:
            self._data["pending_milestone"] = None
            self.save()
        return m

    # ------------------------------------------------------------------
    # Session summary text (Phase 2.2)
    # ------------------------------------------------------------------

    def session_summary_text(self):
        words = self.words_today
        if words <= 0:
            return ""
        parts = [f"{words} words today"]
        if self.streak_days > 0:
            parts.append(f"{self.streak_days} day streak")
        return " · ".join(parts) + " -- nice session"
