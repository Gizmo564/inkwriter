# Inkwriter – Raspberry Pi Zero WH Installation Guide

## Fast path: install.sh

Steps 2 through 10 and 13 below (system update, SPI, automatic GitHub
updates, Bluetooth pairing + auto-reconnect, USB gadget mode, packages,
the Waveshare driver, the systemd service, and the shutdown-key sudoers
rule) are automated by `install.sh`, included in this project folder.
After Steps 0, 1, and 8 below (flash the SD card, SSH in, copy the
project folder over):

```bash
cd ~/inkwriter
bash install.sh
```

It's interactive only where a step genuinely needs a human (confirming
the slower `apt full-upgrade`, the Bluetooth pairing handshake itself,
whether to enable USB gadget mode, and the final reboot) and safe to
re-run if something needs fixing. The rest of this document is the
step-by-step manual version of the same process -- useful for
understanding what the script does, doing it by hand, or troubleshooting.

## Hardware requirements

- Raspberry Pi Zero WH (pre-soldered header, built-in Wi-Fi + Bluetooth)
- Waveshare 5.79" e-Paper HAT (792 × 272 px, SPI, dual-controller) — **and/or** an HDMI monitor
  - This build's `config.ini` defaults (`driver = epd5in79`, `width = 792`, `height = 272`)
    are set for this panel. If you're using a different Waveshare panel, change
    `display.driver` to match the driver module name in `waveshare_epd/`
    (e.g. `epd2in13_V4` for the 2.13" HAT) and update `width`/`height` to that
    panel's native resolution.
- Micro-USB cable to the PWR port (left) for power
- Bluetooth keyboard (recommended — keeps the OTG port free for type-out)
  — or a USB keyboard via a micro-USB OTG adapter into the USB port (right)
- 8 GB+ micro-SD card (Class 10 / A1 recommended)

---

## Display auto-detection

Inkwriter picks the best available display at every boot — no config needed:

| Hardware present | Mode used |
|------------------|-----------|
| SPI e-ink HAT only | **e-ink** |
| HDMI monitor only | **hdmi** |
| Both SPI + HDMI | **e-ink** (SPI always wins) |
| Neither | **terminal** (SSH / dev use) |

To force a mode, edit `~/.config/inkwriter/config.ini` after first run:

```ini
[display]
type = auto       # default — recommended
# type = eink     # force SPI e-ink
# type = hdmi     # force HDMI
# type = terminal # SSH / development
```

---

## Step 0 — Flash the SD card

1. Download and open **Raspberry Pi Imager** on your computer.
2. Choose **Raspberry Pi OS Lite (32-bit)** as the OS.
   - Do **not** choose the 64-bit version — the Zero WH is ARMv6.
3. Click the **gear icon** (Advanced options) before writing and set:
   - ✅ Enable SSH → "Use password authentication"
   - ✅ Set username and password (default `volvi` / your chosen password)
   - ✅ Configure Wi-Fi → enter your network name and password
   - ✅ Set locale / timezone
4. Write to your SD card, insert into the Pi, and power on.

---

## Step 1 — SSH into the Pi

Give the Pi about 60 seconds to boot, then from your computer:

```bash
ssh volvi@inkwriter.local
```

If that doesn't resolve, find the Pi's IP from your router's device list
and use that instead:

```bash
ssh volvi@192.168.1.xxx
```

All remaining steps are run over SSH unless noted otherwise.

---

## Step 2 — Expand filesystem and update

```bash
sudo raspi-config --expand-rootfs
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

SSH back in after the reboot before continuing.

---

## Step 3 — Enable SPI (e-ink HAT only — skip if HDMI only)

```bash
sudo raspi-config
```

Navigate: **Interface Options → SPI → Yes → Finish** then reboot.

```bash
sudo reboot
```

After rebooting, confirm:

```bash
ls /dev/spidev0.*
# Expected output: /dev/spidev0.0  /dev/spidev0.1
```

---

## Step 4 — Pair a Bluetooth keyboard

The Zero WH has built-in Bluetooth. A BT keyboard is the best choice
because it leaves the OTG port free for type-out-to-PC mode.

### 4a. Install Bluetooth tools

```bash
sudo apt install -y bluetooth bluez bluez-tools
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

### 4b. Put your keyboard into pairing mode

Every keyboard is different — usually hold a dedicated pairing button or
`Fn + Bluetooth key` until an LED flashes rapidly. Check your keyboard's
manual. Do this right before the next step.

### 4c. Pair interactively

```bash
bluetoothctl
```

You are now in the `bluetoothctl` shell. Run these commands one at a time:

```
power on
agent on
default-agent
scan on
```

Wait for your keyboard to appear — you'll see a line like:

```
[NEW] Device AA:BB:CC:DD:EE:FF MyKeyboard
```

Copy the MAC address (`AA:BB:CC:DD:EE:FF`) then:

```
scan off
pair AA:BB:CC:DD:EE:FF
```

You may be prompted to type a PIN on the keyboard and press Enter.
Once paired:

```
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
quit
```

### 4d. Verify the keyboard is connected

```bash
bluetoothctl devices
# Your keyboard should appear in the list

bluetoothctl info AA:BB:CC:DD:EE:FF
# Look for: Connected: yes
#           Trusted: yes
#           Paired: yes
```

### 4e. Enable auto-reconnect on boot

The `bluetooth` service starts automatically, but we want the Pi to
reconnect to the keyboard without any manual step after power-on.

Create `/etc/bluetooth/reconnect.sh`:

```bash
sudo tee /etc/bluetooth/reconnect.sh << 'EOF'
#!/bin/bash
# Reconnect trusted Bluetooth devices on boot. Retries several times,
# spaced out -- the adapter and/or keyboard often aren't fully ready in
# the first several seconds after boot. Many BT keyboards also won't
# accept an unsolicited connect while asleep; a keypress wakes them, and
# with AutoEnable=true (see 4f below) they'll usually reconnect on their
# own at that point without this script's help. Always exits 0 -- a
# failed attempt here is routine (keyboard asleep/out of range), not a
# real service failure.
sleep 10   # wait for BT stack to fully start
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if bluetoothctl -- connect AA:BB:CC:DD:EE:FF; then
        exit 0
    fi
    sleep 5
done
exit 0
EOF
sudo chmod +x /etc/bluetooth/reconnect.sh
```

Replace `AA:BB:CC:DD:EE:FF` with your keyboard's actual address.

Create a systemd unit for it:

```bash
sudo tee /etc/systemd/system/bt-reconnect.service << 'EOF'
[Unit]
Description=Reconnect Bluetooth keyboard on boot
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=oneshot
ExecStart=/etc/bluetooth/reconnect.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable bt-reconnect
```

### 4f. Bluetooth troubleshooting

| Problem | Fix |
|---------|-----|
| Keyboard not appearing in `scan on` | Put it back into pairing mode; BT discovery windows are short |
| `pair` fails immediately | Run `remove AA:BB:CC:DD:EE:FF` first, then retry |
| Connected but no input | Run `sudo chmod 660 /dev/input/event*` — then test with `evtest` |
| Drops after a few minutes | Add `AutoEnable=true` to `/etc/bluetooth/main.conf` under `[Policy]` |
| Reconnect script not working | Check `journalctl -u bt-reconnect` for errors |
| `power on` fails / `NotReady` in bluetoothctl | The Zero W's Bluetooth is UART-attached, not USB -- check `sudo systemctl status hciuart`, `rfkill list` (unblock if soft-blocked), and that `pi-bluetooth` is installed. See `hciconfig -a` for whether `hci0` shows up at all. |

### 4g. Boot-time "waiting for keyboard" screen

If `install.sh` set up Bluetooth for you, it also wrote the paired
keyboard's MAC into `~/.config/inkwriter/config.ini` under `[bluetooth]`:

```ini
[bluetooth]
keyboard_mac = AA:BB:CC:DD:EE:FF
require_keyboard_at_boot = true
```

With `keyboard_mac` set, Inkwriter checks (independently of
`bt-reconnect.service` above) whether that keyboard is actually connected
*before* starting the editor UI. If it isn't yet, the e-ink panel shows a
"Waiting for keyboard..." screen and keeps polling every 5 seconds,
indefinitely -- landing in an editor you have no way to type into is
worse than a clear wait screen. Two ways out, both usable over SSH
without touching the Pi itself:

- **Connect the keyboard remotely**: `ssh` in and run
  `bluetoothctl connect AA:BB:CC:DD:EE:FF` (or just bring the keyboard
  back in range / wake it with a keypress) -- the next poll (within 5s)
  picks it up and the UI starts.
- **Turn the gate off**: `ssh` in, edit `config.ini`, and set
  `require_keyboard_at_boot = false`. This is re-read live every poll
  cycle, so it takes effect within 5 seconds -- no restart needed. Useful
  if you're troubleshooting without the keyboard on hand, or plan to use
  Inkwriter over SSH/type-out only for a session.

Setting `keyboard_mac` to blank (or leaving it unset if you paired
manually rather than through `install.sh`) skips this gate entirely --
the UI starts immediately regardless of Bluetooth state, same as before
this feature existed.

---

## Step 5 — Enable USB gadget mode (type-out-to-PC)

This is what lets Inkwriter act like a keyboard and type a document into
any computer it's plugged into via USB.

Edit `/boot/config.txt` and add at the bottom:

```bash
sudo nano /boot/config.txt
```

```
dtoverlay=dwc2
```

Edit `/boot/cmdline.txt` — this is a **single line**, append after `rootwait`:

```bash
sudo nano /boot/cmdline.txt
```

Add (space-separated, same line as everything else):

```
modules-load=dwc2,libcomposite
```

Create the HID gadget setup script:

```bash
sudo tee /usr/local/bin/inkwriter-hid-setup << 'EOF'
#!/bin/bash
set -e
modprobe libcomposite
cd /sys/kernel/config/usb_gadget
mkdir -p inkwriter && cd inkwriter

echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "inkwriter" > strings/0x409/manufacturer
echo "Inkwriter HID" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "HID Config" > configs/c.1/strings/0x409/configuration
echo 120 > configs/c.1/MaxPower

mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol
echo 1 > functions/hid.usb0/subclass
echo 8 > functions/hid.usb0/report_length
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
  > functions/hid.usb0/report_desc

ln -sf functions/hid.usb0 configs/c.1/
ls /sys/class/udc > UDC
EOF

sudo chmod +x /usr/local/bin/inkwriter-hid-setup
```

Run it at boot by adding to `/etc/rc.local` before `exit 0`:

```bash
sudo nano /etc/rc.local
```

```bash
/usr/local/bin/inkwriter-hid-setup &
```

Reboot to apply the `config.txt` and `cmdline.txt` changes:

```bash
sudo reboot
```

After rebooting, verify the gadget device exists:

```bash
ls /dev/hidg0
# Expected: /dev/hidg0
```

---

## Step 6 — System packages

```bash
sudo apt update && sudo apt install -y \
    python3-pip \
    python3-pil python3-numpy \
    git libgpiod2 python3-lgpio \
    fonts-dejavu-core
```

---

## Step 7 — Waveshare e-ink driver (skip if HDMI only)

```bash
git clone https://github.com/waveshare/e-Paper.git ~/e-Paper
pip3 install --break-system-packages \
    ~/e-Paper/RaspberryPi_JetsonNano/python/
```

This takes 2–3 minutes on the Zero WH. Alternatively, install from PyPI:

```bash
pip3 install --break-system-packages waveshare-epaper
```

---

## Step 8 — Copy Inkwriter files to the Pi

From your **computer** (not the Pi), copy the project folder over SSH:

```bash
scp -r inkwriter_build/ volvi@inkwriter.local:~/inkwriter
```

Or if you have the files on a USB drive, mount it on the Pi and copy:

```bash
sudo mkdir -p /mnt/usb
sudo mount /dev/sda1 /mnt/usb
cp -r /mnt/usb/inkwriter_build ~/inkwriter
sudo umount /mnt/usb
```

The final layout on the Pi should be:

```
/home/volvi/inkwriter/
  run_inkwriter.py
  inkwriter/
    __init__.py
    main.py
    config.py
    display.py
    editor.py
    file_browser.py
    file_manager.py
    note_manager.py
    progress.py
    shutdown_screen.py
    usb_hid.py
    fonts/
      spleen-8x16.pil       # bundled bitmap font (display.font_name)
      spleen-8x16.pbm
    art/
      logo.png               # boot-animation logo -- see Step 9c
      growth_1_seed.png     # shutdown-screen pixel art, one per growth
      growth_2_sprout.png   # stage -- see display.shutdown_screen in
      growth_3_plant.png    # Step 10. Shipped as static files, not
      growth_4_bush.png     # generated at runtime.
      growth_5_tree.png
      growth_6_blooming.png
      custom/
        README.txt
        *.png / *.jpg       # your own shutdown backgrounds, if any --
                             # see "Custom backgrounds" in Step 10
```

Everything under `fonts/` and `art/` is plain static assets -- copied over
with the rest of the project in the `scp -r` above, nothing extra to
install. If a copy method other than `scp -r` is used (e.g. copying
individual files), double-check `fonts/` and `art/` made it over --
Inkwriter degrades gracefully if they didn't (falls back to a system TTF
font, skips the shutdown image) but you'll be missing pixel-perfect text
and/or the shutdown art without any error being obvious.

Do a quick sanity check:

```bash
cd ~/inkwriter
python3 -c "from inkwriter.main import main; print('OK')"
# Expected: OK
```

---

## Step 8b — Automatic updates from GitHub (optional)

Inkwriter can check a GitHub repo once at every boot, before it starts,
and pull in a newer version automatically -- with a smoke test and
automatic rollback if the update turns out to be broken (see
`/usr/local/bin/inkwriter-update`, written by `install.sh`). This is
opt-in and asked about during `install.sh`.

### Push this project to GitHub (one-time, from your computer)

Claude can't push to GitHub on your behalf -- this part needs your own
GitHub account. From your **computer**, in the project folder:

```bash
cd /Users/tannernelson/Downloads/inkwriter_build
git init
git add .
git commit -m "Initial Inkwriter build"
git branch -M main
```

Then create a new **empty** repository on github.com (no README, no
`.gitignore`, no license -- this project already has its own
`.gitignore`, which keeps your personal shutdown backgrounds and Python
bytecode cruft out of what gets pushed). A public repo is the simplest
option -- the Pi can then check for updates with a plain `git fetch`,
no credentials stored on the device. Then:

```bash
git remote add origin https://github.com/<your-username>/inkwriter.git
git push -u origin main
```

### Enable it on the Pi

If you're running `install.sh`, just answer yes to "Enable automatic
updates from a GitHub repo?" and give it the URL above -- it handles
everything below itself, including converting an existing `scp`'d
`~/inkwriter` folder into a proper git checkout of that repo.

Doing it by hand instead: `~/inkwriter` needs to be a git clone of the
repo (not an `scp`'d copy) for updates to apply. Either clone fresh --

```bash
rm -rf ~/inkwriter   # only if you already scp'd a non-git copy there
git clone https://github.com/<your-username>/inkwriter.git ~/inkwriter
```

-- or convert an existing folder in place:

```bash
cd ~/inkwriter
git init
git remote add origin https://github.com/<your-username>/inkwriter.git
git fetch origin main
git symbolic-ref HEAD refs/heads/main
git reset --hard origin/main
git clean -fd -e inkwriter/art/custom
```

Then write `/usr/local/bin/inkwriter-update` and the
`inkwriter-update.service` systemd unit exactly as `install.sh`'s
"Automatic updates from GitHub" step does (see `install.sh` for the full
script if setting this up manually).

### Shipping an update

From your computer, whenever you have a change ready:

```bash
cd /Users/tannernelson/Downloads/inkwriter_build
git add .
git commit -m "describe the change"
git push
```

The Pi picks it up on its next boot. There's no live/hot update while
Inkwriter is running -- deliberately, so a background update can never
interrupt an active writing session. To apply it without waiting for a
natural reboot, SSH in and run:

```bash
sudo systemctl restart inkwriter-update  # runs the update check now
sudo systemctl restart inkwriter          # picks up whatever it found
```

### What it protects against

- **No network at boot**: the check has a short timeout (6s) and skips
  quietly if GitHub isn't reachable -- boot is never meaningfully
  delayed, and Inkwriter starts with whatever's already on the SD card.
- **A broken push**: before restarting anything, the pulled code has to
  pass `python3 -c "from inkwriter.main import main"`. If that fails
  (syntax error, broken import), the updater automatically resets back
  to the previous commit and logs what happened -- the device never gets
  stuck on code that won't even import. It'll safely retry the broken
  commit on every subsequent boot until you push a fix, rather than ever
  adopting it.
- **Update log**: `~/.config/inkwriter/update.log` on the Pi records
  every check -- what it found, what it applied, and any rollback.
- **Your own data**: documents, notes, `config.ini` (including your
  Bluetooth keyboard MAC), progress stats, and custom art all live in
  `~/.config/inkwriter` and `~/Documents/inkwriter` -- entirely outside
  the git-managed `inkwriter/` checkout the updater resets, so a `git
  reset --hard` during an update structurally cannot touch them. As
  extra defense-in-depth, the updater also takes a timestamped backup of
  `config.ini` and `progress.json` into `~/.config/inkwriter/backups/`
  before every update, keeping the last 5.

---

## Step 9 — Auto-start on boot (systemd)

Create the service file:

```bash
sudo tee /etc/systemd/system/inkwriter.service << 'EOF'
[Unit]
Description=Inkwriter writing device
After=multi-user.target bt-reconnect.service
Wants=bt-reconnect.service

[Service]
User=volvi
WorkingDirectory=/home/volvi/inkwriter
ExecStart=/usr/bin/python3 run_inkwriter.py
Restart=on-failure
RestartSec=5
StandardInput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable inkwriter
sudo systemctl start inkwriter
```

Free up tty1 so Inkwriter owns the console:

```bash
sudo systemctl disable getty@tty1
```

Check it started cleanly:

```bash
sudo systemctl status inkwriter
# Should show: Active: active (running)

# Or check the log:
tail -f ~/.config/inkwriter/inkwriter.log
```

---

## Step 9b — Shutdown key (Ctrl+P)

Ctrl+P (press twice within 5 seconds to confirm — no accidental power-offs
from a stray keypress) shows the pixel-art shutdown screen and then powers
the Pi off. For the app to actually be able to run `poweroff`, the service
user needs a passwordless sudo rule for exactly that one command:

```bash
echo 'volvi ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff' | sudo tee /etc/sudoers.d/inkwriter-shutdown
sudo chmod 440 /etc/sudoers.d/inkwriter-shutdown
```

Replace `volvi` with whatever user you set in the systemd unit above. If
this rule isn't in place, Ctrl+P still shows the shutdown screen and exits
the app cleanly, it just won't be able to cut power — check
`~/.config/inkwriter/inkwriter.log` for a "System poweroff failed" line if
that happens.

---

## Step 9c — Boot animation

At every boot (e-ink only -- a no-op on HDMI/terminal), Inkwriter reveals
`inkwriter/art/logo.png` and then clears it away, all built from the
panel's own native partial-refresh operation rather than any special
animation hardware:

1. **Iris reveal** -- the logo appears from its own center outward, like
   an aperture opening, instead of a flat sweep in one direction.
2. **Hold** on the completed logo for a moment, with one clean full
   refresh to leave a crisp image.
3. **Wipe-clear** -- the whole panel sweeps to blank in vertical strips,
   so the handoff into the file browser is a deliberate wipe rather than
   an abrupt cut.

Deliberately built from only a handful of real hardware refreshes (a
few reveal steps + a few wipe strips, ~12 total, not one per pixel
block) -- each partial/full refresh costs real physical time on e-ink
hardware regardless of how the software is written, so keeping that
count low keeps this from meaningfully slowing down boot. An earlier
version of this feature used ~85 hardware refreshes and added well
over a minute to every boot; if boot ever feels slow again, this
animation (and the `require_keyboard_at_boot` wait screen, which also
does hardware refreshes) are the first things worth checking.

Want to see it before it's on the actual hardware?
`tools/preview_boot_animation.py` renders the same reveal/hold/wipe
sequence to an animated GIF you can open on any computer -- run
`python3 tools/preview_boot_animation.py` from the repo root (needs
Pillow: `pip install pillow`). The GIF has extra in-between frames for
smooth playback; the real device does far fewer actual refreshes than
the frame count implies.

To use your own logo: replace `inkwriter/art/logo.png` with any
PNG/JPG -- it's letterboxed to fit the panel and dithered to 1-bit
automatically, same as the shutdown-screen custom backgrounds. Turn it
off entirely with:

```ini
[display]
boot_animation = false
```

If `logo.png` is missing, this skips itself silently rather than
erroring -- boot proceeds straight to the keyboard-wait gate (Step 4g) or
file browser.

---

## Step 10 — Configuration

Auto-created on first run at `~/.config/inkwriter/config.ini`.

```ini
[display]
type = auto             # auto | eink | hdmi | terminal

[storage]
auto_save_interval = 30
backup_on_save = true

[usb_hid]
device = /dev/hidg0
chars_per_second = 60

[editor]
word_wrap = true
autosave = true
tab_size = 4
remember_cursor_position = true
show_today_word_count = true

[growth]
show_growth_features = true
show_session_summary = true
show_milestones = true
streak_grace_days = 3

[bluetooth]
keyboard_mac =
require_keyboard_at_boot = true

[safe_mode]
enabled = false
```

**`[safe_mode] enabled`** -- emergency kill switch. When `true`, Inkwriter
skips almost everything at startup (file manager, notes, display, curses)
and just idles, logging a warning -- useful if a hardware fault is making
the normal app unstable and you want the Pi itself to stay reachable
while you sort it out. Re-read fresh on restart, so flipping it over SSH
takes effect with just `sudo systemctl restart inkwriter`, no reinstall.
See `OPS_TODO.md` for the fuller story and `inkwriter-rescue.service`
(below) for a more severe version of the same idea that doesn't need SSH
access at all.

`display.shutdown_screen` (in the `[display]` block above) picks what's
shown before power-off: `growth` (pixel-art growth stage + word count,
default), `quote` (growth art + a random line from your own writing),
`stats` (growth art + lifetime words/streak), `custom` (a full-panel
background image of your own choosing -- see below), or `off`.

**Custom backgrounds:** drop PNG/JPG files into `inkwriter/art/custom/`
and set `shutdown_screen = custom`. Each image is cover-fit to the panel
and dithered to 1-bit automatically -- no pre-processing needed on your
end. If there's more than one image in that folder, one is picked at
random each shutdown; if the folder's empty, it quietly falls back to
`growth` mode.

---

## Step 11 — E-ink display wiring

The WH header is pre-soldered — seat the HAT directly onto the 40-pin header.

| HAT label | GPIO pin | Function  |
|-----------|----------|-----------|
| VCC       | 3.3V     | Power     |
| GND       | GND      | Ground    |
| DIN       | MOSI     | SPI data  |
| CLK       | SCLK     | SPI clock |
| CS        | CE0      | Chip sel  |
| DC        | GPIO 25  | Data/cmd  |
| RST       | GPIO 17  | Reset     |
| BUSY      | GPIO 24  | Busy out  |

---

## Step 12 — E-ink refresh notes

- **Partial refresh** — only the changed character cell is redrawn after each
  keystroke, keeping flicker to a minimum.
- **Full refresh** — runs every 10 partial refreshes by default to clear
  accumulated ghosting. Tune with `refresh_full_interval` in `config.ini`.
  Lower it (e.g. `5`) if you see ghosting; raise it (e.g. `20`) to reduce
  the brief full-refresh pause during writing.

---

## Step 13 — Switching displays

Inkwriter detects hardware once at startup. To switch modes, restart the
service (it re-probes on every start):

```bash
sudo systemctl restart inkwriter
```

To lock a mode permanently regardless of what's plugged in, set `type`
explicitly in `config.ini`.

---

## Step 14 — Keyboard summary

| Keyboard type | Connection | Notes |
|---------------|------------|-------|
| Bluetooth | Built-in BT | Recommended — OTG port stays free for type-out |
| USB wireless (dongle) | OTG adapter → USB port | Can't use type-out at the same time |
| USB wired | OTG adapter → USB port | Same limitation as above |

For type-out mode you need the OTG port free (USB cable to host PC). If you
use a USB keyboard, you'll need to unplug it and plug in the host PC cable
each time you want to type out a document. A Bluetooth keyboard avoids this
entirely.

---

## Step 15 — Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ssh: Could not resolve hostname inkwriter.local` | Check router for Pi's IP; use that instead |
| Pi visible on network but SSH refuses | Confirm SSH was enabled in Imager advanced options |
| Inkwriter starts in terminal mode | Check log; SPI may not be enabled or HAT not seated |
| SPI HAT ignored, HDMI used | Run `ls /dev/spidev0.*` — if missing, re-enable SPI in raspi-config |
| Bluetooth keyboard not reconnecting | Check `journalctl -u bt-reconnect`; re-trust the device |
| BT keyboard connects but no input | `sudo chmod 660 /dev/input/event*` |
| `/dev/hidg0` missing | Run `inkwriter-hid-setup` manually; check `dwc2` in `config.txt` |
| Type-out does nothing | `sudo chmod 660 /dev/hidg0` |
| Ghosting on e-ink | Lower `refresh_full_interval` to 5 in config |
| Inkwriter service won't start | `journalctl -u inkwriter -n 50` for full error log |
| Log file location | `~/.config/inkwriter/inkwriter.log` |

---

## Updating Inkwriter — replacing files over SSH

Use these commands whenever you have a new version to deploy. Run them from
your **computer**, not the Pi.

### Copy new files to the Pi

```bash
# Copy the whole project folder (run from the directory containing inkwriter_build/)
scp -r inkwriter_build/ volvi@inkwriter.local:~/inkwriter_new
```

### Stop the service, swap files, restart

SSH in and run:

```bash
ssh volvi@inkwriter.local
```

```bash
# Stop Inkwriter
sudo systemctl stop inkwriter

# Back up current installation just in case
cp -r ~/inkwriter ~/inkwriter_backup_$(date +%Y%m%d)

# Replace only the Python files (preserve your config and documents)
cp ~/inkwriter_new/run_inkwriter.py ~/inkwriter/run_inkwriter.py
cp ~/inkwriter_new/inkwriter/*.py ~/inkwriter/inkwriter/

# Restart
sudo systemctl start inkwriter
sudo systemctl status inkwriter
```

### Replace individual files only

If you only changed one or two files, you can copy just those:

```bash
# Example: only usb_hid.py and editor.py changed
scp inkwriter_build/inkwriter/usb_hid.py volvi@inkwriter.local:~/inkwriter/inkwriter/
scp inkwriter_build/inkwriter/editor.py  volvi@inkwriter.local:~/inkwriter/inkwriter/

# Then restart the service
ssh volvi@inkwriter.local "sudo systemctl restart inkwriter"
```

### One-liner: copy all + restart in a single command

```bash
scp -r inkwriter_build/inkwriter/*.py volvi@inkwriter.local:~/inkwriter/inkwriter/ && \
scp inkwriter_build/run_inkwriter.py  volvi@inkwriter.local:~/inkwriter/ && \
ssh volvi@inkwriter.local "sudo systemctl restart inkwriter && sudo systemctl status inkwriter"
```

### Roll back if something breaks

```bash
ssh volvi@inkwriter.local

sudo systemctl stop inkwriter
rm -rf ~/inkwriter
mv ~/inkwriter_backup_YYYYMMDD ~/inkwriter   # use the actual date from your backup
sudo systemctl start inkwriter
```

### Updating this version (v1.4)

For the v1.4 fixes specifically (type-out permissions + Ctrl+E rename):

```bash
# On the Pi — fix hidg0 permissions permanently
sudo chmod 660 /dev/hidg0
sudo chown root:input /dev/hidg0
sudo usermod -aG input volvi
sudo sed -i 's|ls /sys/class/udc > UDC|ls /sys/class/udc > UDC\nchmod 660 /dev/hidg0\nchown root:input /dev/hidg0|' \
    /usr/local/bin/inkwriter-hid-setup

# Copy just the two changed files from your computer
scp inkwriter_build/inkwriter/usb_hid.py volvi@inkwriter.local:~/inkwriter/inkwriter/
scp inkwriter_build/inkwriter/editor.py  volvi@inkwriter.local:~/inkwriter/inkwriter/

# Log out and back in for the group change to take effect
exit
ssh volvi@inkwriter.local
sudo systemctl restart inkwriter
```
