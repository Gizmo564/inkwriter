# Ops / infra follow-ups

Not user-facing features (see ADHD_FEATURES_PLAN.md for those) -- things to
build into install.sh / the deployment tooling itself, based on real
problems hit while running this on actual hardware.

## Debug/rescue service with broader sudo access

**Why:** During the SKU 26843 / Rev2.3 Driver HAT hardware fault (panel
init appears to brown out the whole Pi), the only way to disable
non-essential services (bt-reconnect, inkwriter-hid-setup) automatically
via a GitHub-delivered update was blocked by the inkwriter user's sudo
access being scoped to exactly `systemctl poweroff` -- correct as a
default (least privilege), but it meant every rescue action needed a
live, uninterrupted SSH window on a Pi that was only reachable for ~35
seconds at a time between reboots. That's a bad position to be in during
an actual hardware emergency.

**Proposed fix:** Have install.sh set up a separate, narrowly-scoped
systemd service + sudoers rule specifically for remote rescue/debug
actions, so a future git-delivered "safe mode" update can actually act
on it without needing a prior manual SSH session:

- A small standalone script (outside the main `inkwriter/` package, same
  reasoning as `inkwriter-update` living in `/usr/local/bin` -- so a
  `git reset --hard` can't affect it) that reads a plain marker file
  (e.g. `~/.config/inkwriter/rescue_mode`) written by the main app or by
  the updater, and if present, stops/disables a fixed allow-list of
  non-essential services (bt-reconnect, inkwriter-hid-setup, the main
  inkwriter.service itself) and nothing else.
- A dedicated sudoers rule scoped only to that specific script path
  (`NOPASSWD: /usr/local/bin/inkwriter-rescue`), not to `systemctl`
  generally -- keeps the least-privilege property while still being
  useful in an emergency.
- A oneshot systemd unit (`inkwriter-rescue.service`) that runs this
  script early at every boot, before inkwriter.service, so it can react
  even if the main app never gets far enough to run its own code.
- install.sh should set this up by default (not as an opt-in prompt),
  since its entire value is being present *before* you know you'll need
  it.

**Also worth revisiting while building this:** whether `inkwriter-update`
itself should be allowed to touch this same allow-list directly instead
of going through a marker file -- simpler, but reopens the
least-privilege question since the updater already runs unattended at
every boot. Marker-file + separate rescue service keeps the update
mechanism itself dumb and low-risk.

**Status:** not started. Revisit once the current SKU 26843 hardware
issue is resolved and there's room to build/test this properly rather
than firefighting.
