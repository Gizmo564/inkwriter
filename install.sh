#!/bin/bash
# ============================================================================
# Inkwriter installer -- run this ON the Raspberry Pi after copying the
# project folder over (see INSTALL.md Step 8). It applies everything from
# INSTALL.md Steps 2-10 and 13 automatically: system update, SPI, Bluetooth
# keyboard pairing + auto-reconnect, USB gadget (type-out) mode, system
# packages, the Waveshare e-ink driver, the systemd service, and the
# shutdown-key sudoers rule.
#
# Usage:
#   cd ~/inkwriter        # wherever you copied the project folder to
#   bash install.sh
#
# Run as your normal user (NOT with sudo) -- the script calls sudo itself
# for the specific commands that need it, and will prompt for your password
# when it does. Safe to re-run: every step checks whether it's already done
# before making a change.
#
# What it asks you for (only when it can't be automated):
#   - Whether to run the slower `apt full-upgrade` (optional, can take a
#     while on a Zero W).
#   - Whether to set up a Bluetooth keyboard, and if so, the actual
#     pairing handshake (putting the keyboard in pairing mode, typing a
#     PIN on the keyboard) -- that step fundamentally needs a human at the
#     keyboard, so the script hands you an interactive `bluetoothctl`
#     shell for it, then automates the reconnect-on-boot plumbing once
#     you tell it the MAC address you paired.
#   - Whether to enable USB gadget "type-out" mode.
#   - Confirmation before the final reboot (SPI + gadget mode need one).
# ============================================================================

set -uo pipefail

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

BOLD="$(tput bold 2>/dev/null || true)"
DIM="$(tput dim 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"
GREEN="$(tput setaf 2 2>/dev/null || true)"
YELLOW="$(tput setaf 3 2>/dev/null || true)"
RED="$(tput setaf 1 2>/dev/null || true)"

step()  { echo -e "\n${BOLD}==> $*${RESET}"; }
ok()    { echo -e "${GREEN}  OK${RESET}  $*"; }
warn()  { echo -e "${YELLOW}  WARN${RESET}  $*"; }
fail()  { echo -e "${RED}  FAIL${RESET}  $*"; }
info()  { echo -e "${DIM}  $*${RESET}"; }

ask_yn() {
    # ask_yn "question" default(Y|N)  -->  returns 0 for yes, 1 for no
    local prompt="$1" default="${2:-Y}" reply
    if [[ "$default" == "Y" ]]; then
        read -rp "$prompt [Y/n] " reply
        reply="${reply:-Y}"
    else
        read -rp "$prompt [y/N] " reply
        reply="${reply:-N}"
    fi
    [[ "$reply" =~ ^[Yy] ]]
}

REBOOT_NEEDED=0

# ----------------------------------------------------------------------------
# 0. Sanity checks
# ----------------------------------------------------------------------------

if [[ "$EUID" -eq 0 ]]; then
    fail "Don't run this with sudo / as root -- run it as your normal user"
    info "(it calls sudo itself for the specific commands that need it)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/run_inkwriter.py" ]]; then
    fail "run_inkwriter.py not found next to this script."
    info "Run install.sh from inside the copied inkwriter project folder."
    exit 1
fi
INKWRITER_DIR="$SCRIPT_DIR"
INKWRITER_USER="$(id -un)"

if ! grep -qi "raspberry pi" /proc/cpuinfo 2>/dev/null && [[ ! -f /proc/device-tree/model ]]; then
    warn "This doesn't look like a Raspberry Pi -- continuing anyway,"
    warn "but SPI/GPIO/USB-gadget steps will likely no-op or fail."
fi

BOOT_DIR="/boot"
[[ -d /boot/firmware ]] && BOOT_DIR="/boot/firmware"   # newer OS layout

echo "${BOLD}Inkwriter installer${RESET}"
echo "  Project dir : $INKWRITER_DIR"
echo "  Running as  : $INKWRITER_USER"
echo "  Boot dir    : $BOOT_DIR"
echo
if ! ask_yn "Proceed?" Y; then
    exit 0
fi

# ----------------------------------------------------------------------------
# 1. System update
# ----------------------------------------------------------------------------

step "System update"
sudo apt-get update -qq && ok "apt update" || warn "apt update failed -- continuing"

if ask_yn "Run 'apt full-upgrade' too? (slower, recommended on a fresh SD card)" N; then
    sudo apt-get full-upgrade -y && ok "apt full-upgrade" || warn "full-upgrade failed -- continuing"
fi

# ----------------------------------------------------------------------------
# 2. SPI (for the e-ink HAT)
# ----------------------------------------------------------------------------

step "Enable SPI (e-ink HAT)"
if command -v raspi-config >/dev/null 2>&1; then
    CURRENT_SPI="$(sudo raspi-config nonint get_spi 2>/dev/null || echo 1)"
    if [[ "$CURRENT_SPI" == "0" ]]; then
        ok "SPI already enabled"
    else
        sudo raspi-config nonint do_spi 0 && ok "SPI enabled (takes effect after reboot)" \
            || warn "Could not enable SPI automatically -- enable via raspi-config manually"
        REBOOT_NEEDED=1
    fi
else
    warn "raspi-config not found -- skipping (not on Raspberry Pi OS?)"
fi

# ----------------------------------------------------------------------------
# 3. System packages
# ----------------------------------------------------------------------------

step "System packages"
sudo apt-get install -y -qq \
    python3-pip python3-pil python3-numpy \
    git libgpiod2 python3-lgpio fonts-dejavu-core \
    && ok "Installed python3-pil, python3-numpy, git, libgpiod2, python3-lgpio, fonts-dejavu-core" \
    || warn "Some packages failed to install -- check apt output above"

# ----------------------------------------------------------------------------
# 4. Waveshare e-ink driver
# ----------------------------------------------------------------------------

step "Waveshare e-ink driver"
if python3 -c "import waveshare_epd" >/dev/null 2>&1; then
    ok "waveshare_epd already importable"
else
    if [[ ! -d "$HOME/e-Paper" ]]; then
        git clone --depth 1 https://github.com/waveshare/e-Paper.git "$HOME/e-Paper" \
            && ok "Cloned waveshare e-Paper repo" \
            || warn "git clone failed -- check network"
    fi
    if [[ -d "$HOME/e-Paper/RaspberryPi_JetsonNano/python" ]]; then
        pip3 install --break-system-packages -q "$HOME/e-Paper/RaspberryPi_JetsonNano/python/" \
            && ok "Installed waveshare_epd from cloned repo" \
            || warn "pip install from repo failed -- trying PyPI fallback"
    fi
    if ! python3 -c "import waveshare_epd" >/dev/null 2>&1; then
        pip3 install --break-system-packages -q waveshare-epaper \
            && ok "Installed waveshare-epaper from PyPI" \
            || warn "Could not install the Waveshare driver -- e-ink mode won't work until this is fixed"
    fi
fi

# ----------------------------------------------------------------------------
# 4b. Automatic updates from GitHub
# ----------------------------------------------------------------------------

step "Automatic updates from GitHub"
info "Checks your GitHub repo once at every boot (before Inkwriter starts),"
info "and if there's a newer commit, pulls it in and smoke-tests it before"
info "launching. Never blocks boot waiting on a slow/missing network --"
info "if GitHub isn't reachable within a few seconds, it just skips the"
info "check and starts whatever's already on disk."
echo
if ask_yn "Enable automatic updates from a GitHub repo?" N; then
    read -rp "Repo URL (e.g. https://github.com/you/inkwriter.git): " REPO_URL
    read -rp "Branch [main]: " REPO_BRANCH
    REPO_BRANCH="${REPO_BRANCH:-main}"

    if [[ -z "$REPO_URL" ]]; then
        warn "No URL given -- skipping automatic updates"
    else
        if [[ ! -d "$INKWRITER_DIR/.git" ]]; then
            info "$INKWRITER_DIR isn't a git checkout yet -- converting it to"
            info "track $REPO_URL. This assumes you've already pushed this"
            info "exact project there (see INSTALL.md's 'Automatic updates'"
            info "section) -- if you haven't yet, do that first, then re-run"
            info "install.sh."
            git -C "$INKWRITER_DIR" init -q
            git -C "$INKWRITER_DIR" remote add origin "$REPO_URL" 2>/dev/null \
                || git -C "$INKWRITER_DIR" remote set-url origin "$REPO_URL"
            if git -C "$INKWRITER_DIR" fetch origin "$REPO_BRANCH" -q; then
                git -C "$INKWRITER_DIR" symbolic-ref HEAD "refs/heads/$REPO_BRANCH"
                git -C "$INKWRITER_DIR" reset --hard "origin/$REPO_BRANCH" -q
                # -e keeps your own custom shutdown backgrounds even though
                # they're untracked (gitignored) -- clean would otherwise
                # sweep up any other stray local files too.
                git -C "$INKWRITER_DIR" clean -fd -e "inkwriter/art/custom" -q
                ok "Converted $INKWRITER_DIR to a git checkout of $REPO_URL ($REPO_BRANCH)"
            else
                fail "Could not fetch $REPO_URL -- push the project there first, then re-run install.sh"
                REPO_URL=""
            fi
        else
            git -C "$INKWRITER_DIR" remote set-url origin "$REPO_URL" 2>/dev/null \
                || git -C "$INKWRITER_DIR" remote add origin "$REPO_URL"
            ok "$INKWRITER_DIR already a git checkout -- pointed origin at $REPO_URL"
        fi

        if [[ -n "$REPO_URL" ]]; then
            # Lives in /usr/local/bin, *not* inside the repo it updates --
            # a script can't safely rewrite its own file mid-execution via
            # `git reset --hard`, so the updater has to sit outside what it
            # updates.
            sudo tee /usr/local/bin/inkwriter-update > /dev/null << EOF
#!/bin/bash
# Inkwriter self-updater (written by install.sh). Checks $REPO_URL for a
# newer commit on $REPO_BRANCH, pulls it in, smoke-tests it, and rolls
# back automatically if the new version doesn't even import cleanly.
# Runs once at boot via inkwriter-update.service, before inkwriter.service
# starts -- whatever this script decides to keep is what actually launches.

set -uo pipefail

INKWRITER_DIR="$INKWRITER_DIR"
BRANCH="$REPO_BRANCH"
DATA_DIR="\$HOME/.config/inkwriter"
LOG_FILE="\$DATA_DIR/update.log"
BACKUP_DIR="\$DATA_DIR/update_backups"
# Kept short deliberately -- this runs Before=inkwriter.service, so it's
# on the critical boot path every time. Two checks (reachability +
# fetch) each bounded by this, so worst case is ~2x this value added to
# boot when a real update is found; typically far less (either no
# network yet -> fails fast, or already up to date -> one quick check).
NETWORK_TIMEOUT=4

mkdir -p "\$DATA_DIR"
log() { echo "\$(date '+%Y-%m-%d %H:%M:%S') \$*" >> "\$LOG_FILE"; }

# Everything this update mechanism actually needs to protect --
# config.ini, progress.json (word counts/streaks), and every document in
# ~/Documents/inkwriter -- already lives outside \$INKWRITER_DIR entirely,
# so `git reset --hard`/`git clean` below structurally can't touch any of
# it (git only ever operates inside the repo's own working tree). Custom
# shutdown backgrounds under inkwriter/art/custom/ are inside the repo
# but gitignored, and git never removes untracked files on a plain reset,
# plus `clean` below explicitly excludes that path.
#
# This backup of config.ini + progress.json is pure defense-in-depth on
# top of that -- protects against a future mistake (this script or the
# repo's .gitignore ever changing to no longer guarantee the above), not
# something the current design actually depends on. Keeps the last 5.
backup_local_data() {
    mkdir -p "\$BACKUP_DIR"
    local ts stamp
    ts="\$(date '+%Y%m%d_%H%M%S')"
    stamp="\$BACKUP_DIR/\$ts"
    mkdir -p "\$stamp"
    [[ -f "\$DATA_DIR/config.ini" ]] && cp "\$DATA_DIR/config.ini" "\$stamp/"
    [[ -f "\$DATA_DIR/progress.json" ]] && cp "\$DATA_DIR/progress.json" "\$stamp/"
    ls -1d "\$BACKUP_DIR"/*/ 2>/dev/null | sort | head -n -5 | xargs -r rm -rf
}

cd "\$INKWRITER_DIR" || { log "ERROR: \$INKWRITER_DIR not found"; exit 0; }

if [[ ! -d .git ]]; then
    log "SKIP: not a git checkout -- run install.sh's update setup again"
    exit 0
fi

# Quick, bounded reachability check -- never block boot on a flaky network.
if ! timeout "\$NETWORK_TIMEOUT" git ls-remote --exit-code origin "\$BRANCH" > /tmp/inkwriter_ls_remote 2>&1; then
    log "SKIP: could not reach origin within \${NETWORK_TIMEOUT}s (no network yet, or GitHub unreachable)"
    exit 0
fi

REMOTE_HEAD="\$(awk '{print \$1}' /tmp/inkwriter_ls_remote | head -1)"
LOCAL_HEAD="\$(git rev-parse HEAD 2>/dev/null || echo "")"

if [[ -z "\$REMOTE_HEAD" || "\$REMOTE_HEAD" == "\$LOCAL_HEAD" ]]; then
    log "OK: already up to date (\$LOCAL_HEAD)"
    exit 0
fi

log "UPDATE FOUND: \$LOCAL_HEAD -> \$REMOTE_HEAD"
PREV_HEAD="\$LOCAL_HEAD"
backup_local_data

if ! timeout "\$NETWORK_TIMEOUT" git fetch origin "\$BRANCH" >> "\$LOG_FILE" 2>&1; then
    log "ERROR: git fetch failed -- keeping current version"
    exit 0
fi

git reset --hard "origin/\$BRANCH" >> "\$LOG_FILE" 2>&1
git clean -fd -e "inkwriter/art/custom" -q

# Smoke test: does the new code even import? Cheap, catches syntax errors
# and obviously broken pushes before they ever reach a running instance.
if python3 -c "from inkwriter.main import main" 2>>"\$LOG_FILE"; then
    log "OK: update applied and smoke-tested clean (\$REMOTE_HEAD)"
else
    log "ERROR: new version failed smoke test -- rolling back to \${PREV_HEAD:-<none>}"
    if [[ -n "\$PREV_HEAD" ]]; then
        git reset --hard "\$PREV_HEAD" >> "\$LOG_FILE" 2>&1
    fi
fi
EOF
            sudo chmod +x /usr/local/bin/inkwriter-update
            ok "Wrote /usr/local/bin/inkwriter-update"

            sudo tee /etc/systemd/system/inkwriter-update.service > /dev/null << EOF
[Unit]
Description=Check for and apply Inkwriter updates from GitHub
Before=inkwriter.service
# Best-effort network wait, but never blocks boot indefinitely -- the
# script itself has its own short internal timeout as the real guard.
After=network.target

[Service]
Type=oneshot
TimeoutStartSec=30
# Must run as the project's owner, not root -- the script writes to that
# user's ~/.config/inkwriter/update.log (systemd only auto-exports \$HOME
# for a unit when User= is set; without it \$HOME is unbound under
# 'set -u' and the whole thing fails immediately), and running git as
# root against a directory owned by another user leaves root-owned files
# behind that later break normal (non-sudo) git/file operations there.
User=$INKWRITER_USER
ExecStart=/usr/local/bin/inkwriter-update

[Install]
WantedBy=multi-user.target
EOF
            sudo systemctl daemon-reload
            sudo systemctl enable inkwriter-update >/dev/null 2>&1
            AUTO_UPDATE_ENABLED=1
            ok "inkwriter-update.service enabled -- checks $REPO_URL on every boot"
        fi
    fi
else
    info "Skipped -- see 'Automatic updates from GitHub' in INSTALL.md to add this later"
fi
AUTO_UPDATE_ENABLED="${AUTO_UPDATE_ENABLED:-0}"

# ----------------------------------------------------------------------------
# 5. Bluetooth keyboard
# ----------------------------------------------------------------------------

step "Bluetooth keyboard"
BT_MAC=""
if ask_yn "Set up a Bluetooth keyboard now?" Y; then
    sudo apt-get install -y -qq bluetooth bluez bluez-tools \
        && ok "Bluetooth tools installed" || warn "Bluetooth package install failed"
    sudo systemctl enable bluetooth >/dev/null 2>&1
    sudo systemctl start bluetooth >/dev/null 2>&1

    echo
    info "Put your keyboard into pairing mode now (hold its pairing button"
    info "or Fn+Bluetooth key until the LED flashes -- check its manual)."
    echo
    info "You're about to get an interactive bluetoothctl shell. Run:"
    info "    power on"
    info "    agent on"
    info "    default-agent"
    info "    scan on"
    info "  ...wait for your keyboard to appear (a line like"
    info "  '[NEW] Device AA:BB:CC:DD:EE:FF MyKeyboard'), then:"
    info "    scan off"
    info "    pair AA:BB:CC:DD:EE:FF"
    info "  (type any PIN it asks for on the keyboard itself, press Enter)"
    info "    trust AA:BB:CC:DD:EE:FF"
    info "    connect AA:BB:CC:DD:EE:FF"
    info "    quit"
    echo
    read -rp "Press Enter to open bluetoothctl... "
    bluetoothctl || true

    echo
    read -rp "Paste the MAC address you just paired (AA:BB:CC:DD:EE:FF), or leave blank to skip: " BT_MAC
    BT_MAC="$(echo "$BT_MAC" | tr 'a-f' 'A-F' | tr -d '[:space:]')"

    if [[ -n "$BT_MAC" ]]; then
        sudo tee /etc/bluetooth/reconnect.sh > /dev/null << EOF
#!/bin/bash
# Reconnect trusted Bluetooth devices on boot (written by install.sh).
#
# Retries a few times, spaced out -- the adapter and/or keyboard often
# aren't fully ready in the first several seconds after boot, so a single
# attempt fails more often than it should. Many BT keyboards also won't
# accept an unsolicited connect while asleep; a keypress wakes them, and
# with AutoEnable=true (set in /etc/bluetooth/main.conf) they'll usually
# reconnect on their own at that point without this script's help --
# this is a second line of defense, not the only path to reconnecting.
#
# Always exits 0: a failed reconnect attempt here is a routine, expected
# condition (keyboard asleep/out of range), not a real service failure,
# so this doesn't report as "failed" on every boot for something normal.
sleep 10
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if bluetoothctl -- connect $BT_MAC; then
        exit 0
    fi
    sleep 5
done
exit 0
EOF
        sudo chmod +x /etc/bluetooth/reconnect.sh

        sudo tee /etc/systemd/system/bt-reconnect.service > /dev/null << 'EOF'
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
        sudo systemctl enable bt-reconnect >/dev/null 2>&1
        ok "Auto-reconnect configured for $BT_MAC"

        # AutoEnable makes the adapter itself come back up after a reboot
        # without needing a manual `power on` -- belt-and-suspenders with
        # bt-reconnect.service, and harmless if already set.
        if [[ -f /etc/bluetooth/main.conf ]] && ! grep -q "^AutoEnable" /etc/bluetooth/main.conf; then
            if grep -q "^\[Policy\]" /etc/bluetooth/main.conf; then
                sudo sed -i '/^\[Policy\]/a AutoEnable=true' /etc/bluetooth/main.conf
            else
                printf '\n[Policy]\nAutoEnable=true\n' | sudo tee -a /etc/bluetooth/main.conf > /dev/null
            fi
            ok "AutoEnable=true set in /etc/bluetooth/main.conf"
        fi

        # Also record the MAC in Inkwriter's own config, not just the
        # systemd reconnect plumbing above -- this is what lets the app
        # itself show a "waiting for keyboard" screen at boot and block
        # entering the UI until that specific keyboard is connected,
        # rather than silently landing in an editor with no way to type.
        if (cd "$INKWRITER_DIR" && python3 -c "
from inkwriter.config import Config
c = Config()
c._cfg.set('bluetooth', 'keyboard_mac', '$BT_MAC')
c.save()
" 2>/dev/null); then
            ok "Saved keyboard MAC to config.ini (enables the boot-time keyboard-wait screen)"
        else
            warn "Could not write keyboard_mac to config.ini -- set it manually later if needed"
        fi
    else
        warn "No MAC provided -- skipping auto-reconnect setup"
        info "Re-run this script, or follow INSTALL.md Step 4e, once you've paired one"
    fi
else
    info "Skipped -- see INSTALL.md Step 4 if you want to add one later"
fi

# ----------------------------------------------------------------------------
# 6. USB gadget mode (type-out-to-PC)
# ----------------------------------------------------------------------------

step "USB gadget mode (type-out-to-PC)"
if ask_yn "Enable USB gadget mode so Ctrl+T can type documents into another computer?" Y; then
    CONFIG_TXT="$BOOT_DIR/config.txt"
    CMDLINE_TXT="$BOOT_DIR/cmdline.txt"

    # Explicit dr_mode=peripheral, not plain "dtoverlay=dwc2" -- without an
    # ID pin wired (true of the Zero W's USB port), OTG auto-detect can
    # land in host mode, which never creates a UDC at /sys/class/udc and
    # silently means /dev/hidg0 (and type-out) can never work. Some stock
    # Raspberry Pi OS images even ship an explicit
    # "dtoverlay=dwc2,dr_mode=host" line by default (host mode, for a USB
    # peripheral plugged into the Pi) -- if that's already there, it has
    # to be flipped to peripheral, not just left alone, since gadget mode
    # and host mode are mutually exclusive for the same controller.
    if grep -q "^dtoverlay=dwc2,dr_mode=host" "$CONFIG_TXT" 2>/dev/null; then
        sudo sed -i 's/^dtoverlay=dwc2,dr_mode=host/dtoverlay=dwc2,dr_mode=peripheral/' "$CONFIG_TXT"
        ok "Found dtoverlay=dwc2 already set to host mode -- switched to peripheral mode in $CONFIG_TXT"
        REBOOT_NEEDED=1
    elif ! grep -q "^dtoverlay=dwc2,dr_mode=peripheral" "$CONFIG_TXT" 2>/dev/null; then
        echo "dtoverlay=dwc2,dr_mode=peripheral" | sudo tee -a "$CONFIG_TXT" > /dev/null
        ok "Added dtoverlay=dwc2,dr_mode=peripheral to $CONFIG_TXT"
        REBOOT_NEEDED=1
    else
        ok "dtoverlay=dwc2,dr_mode=peripheral already present"
    fi

    if [[ -f "$CMDLINE_TXT" ]] && ! grep -q "modules-load=dwc2,libcomposite" "$CMDLINE_TXT"; then
        sudo sed -i 's/\(rootwait\)/\1 modules-load=dwc2,libcomposite/' "$CMDLINE_TXT"
        ok "Added modules-load=dwc2,libcomposite to $CMDLINE_TXT"
        REBOOT_NEEDED=1
    else
        ok "modules-load=dwc2,libcomposite already present (or cmdline.txt missing)"
    fi

    sudo tee /usr/local/bin/inkwriter-hid-setup > /dev/null << 'EOF'
#!/bin/bash
set -e
# Belt-and-suspenders: cmdline.txt's modules-load=dwc2,libcomposite
# should load this at early boot already, but explicitly modprobing here
# too is a harmless no-op if it's already loaded, and a real fix if that
# early-boot load ever didn't happen for some reason.
modprobe dwc2 2>/dev/null || true
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

# Let a normal (non-root) user write to the gadget without sudo.
chmod 660 /dev/hidg0
chown root:input /dev/hidg0
EOF
    sudo chmod +x /usr/local/bin/inkwriter-hid-setup
    ok "Wrote /usr/local/bin/inkwriter-hid-setup"

    sudo usermod -aG input "$INKWRITER_USER"
    ok "Added $INKWRITER_USER to the 'input' group (for /dev/hidg0 access)"

    # Run the setup script at boot. Prefer a systemd oneshot service --
    # more robust than rc.local, and works whether or not rc.local exists
    # on this OS image (Bookworm dropped it; Bullseye still has it).
    sudo tee /etc/systemd/system/inkwriter-hid-setup.service > /dev/null << 'EOF'
[Unit]
Description=Set up Inkwriter USB HID gadget
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/inkwriter-hid-setup
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable inkwriter-hid-setup >/dev/null 2>&1
    ok "inkwriter-hid-setup.service enabled (runs the gadget setup every boot)"
else
    info "Skipped -- Ctrl+T type-out won't work until this is enabled later"
fi

# ----------------------------------------------------------------------------
# 7. Sanity-check the project itself
# ----------------------------------------------------------------------------

step "Inkwriter project sanity check"
if (cd "$INKWRITER_DIR" && python3 -c "from inkwriter.main import main" 2>/tmp/inkwriter_import_err); then
    ok "inkwriter.main imports cleanly"
else
    fail "inkwriter.main failed to import:"
    sed 's/^/    /' /tmp/inkwriter_import_err
    warn "Continuing anyway -- fix this before expecting the service to start"
fi

for asset in fonts/spleen-8x16.pil art/growth_1_seed.png; do
    if [[ -f "$INKWRITER_DIR/inkwriter/$asset" ]]; then
        ok "Found inkwriter/$asset"
    else
        warn "Missing inkwriter/$asset -- font/shutdown-art will fall back to plainer defaults"
    fi
done

# ----------------------------------------------------------------------------
# 8. systemd service
# ----------------------------------------------------------------------------

step "Inkwriter systemd service"
sudo tee /etc/systemd/system/inkwriter.service > /dev/null << EOF
[Unit]
Description=Inkwriter writing device
# inkwriter-update.service only exists if automatic updates were enabled
# above -- systemd treats a reference to a unit that isn't installed as
# already-satisfied, so this line is harmless either way.
#
# Deliberately NOT ordered After=bt-reconnect.service: that service
# retries the OS-level Bluetooth connection up to 10 times, 5s apart
# (worst case ~60s), and Type=oneshot means anything After= it would
# block until it finishes. Inkwriter has its own keyboard-wait screen
# (_wait_for_keyboard in main.py) that polls connection status and
# shows a clear "waiting" screen if needed, so it doesn't need to wait
# for bt-reconnect.service to finish first -- Wants= still starts them
# together, they just run in parallel instead of serially, which was
# previously adding up to a minute of blank-screen delay to every boot.
After=multi-user.target inkwriter-update.service
Wants=bt-reconnect.service

[Service]
User=$INKWRITER_USER
WorkingDirectory=$INKWRITER_DIR
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
sudo systemctl daemon-reload
sudo systemctl enable inkwriter >/dev/null 2>&1
ok "inkwriter.service created and enabled (User=$INKWRITER_USER, WorkingDirectory=$INKWRITER_DIR)"

if systemctl is-enabled getty@tty1 >/dev/null 2>&1; then
    sudo systemctl disable getty@tty1 >/dev/null 2>&1
    ok "Disabled getty@tty1 so Inkwriter owns the console"
else
    ok "getty@tty1 already disabled"
fi

# ----------------------------------------------------------------------------
# 9. Shutdown-key sudoers rule
# ----------------------------------------------------------------------------

step "Shutdown key (Ctrl+P) sudoers rule"
SUDOERS_FILE="/etc/sudoers.d/inkwriter-shutdown"
SUDOERS_LINE="$INKWRITER_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff"
if sudo test -f "$SUDOERS_FILE" && sudo grep -qF "$SUDOERS_LINE" "$SUDOERS_FILE"; then
    ok "Sudoers rule already in place"
else
    echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 440 "$SUDOERS_FILE"
    # Validate before trusting it -- a malformed sudoers file can lock out sudo.
    if sudo visudo -c -f "$SUDOERS_FILE" >/dev/null 2>&1; then
        ok "Sudoers rule written and validated ($SUDOERS_FILE)"
    else
        fail "Sudoers rule failed validation -- removing it"
        sudo rm -f "$SUDOERS_FILE"
    fi
fi

# ----------------------------------------------------------------------------
# 10. Start it up
# ----------------------------------------------------------------------------

step "Starting Inkwriter"
if [[ "$REBOOT_NEEDED" -eq 1 ]]; then
    warn "SPI and/or USB gadget mode need a reboot to activate."
    info "Inkwriter will auto-start on that reboot (service is enabled)."
    info "Starting it now too, but it may fall back to terminal/HDMI mode"
    info "until you reboot."
fi
sudo systemctl restart inkwriter
sleep 2
if systemctl is-active --quiet inkwriter; then
    ok "inkwriter.service is running"
else
    warn "inkwriter.service did not start cleanly -- check: journalctl -u inkwriter -n 50"
fi

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------

echo
echo "${BOLD}================  Summary  ================${RESET}"
echo "  Project dir      : $INKWRITER_DIR"
echo "  Service          : inkwriter.service (enabled, User=$INKWRITER_USER)"
echo "  Config file      : ~/.config/inkwriter/config.ini (created on first run)"
echo "  Log file         : ~/.config/inkwriter/inkwriter.log"
[[ -n "$BT_MAC" ]] && echo "  Bluetooth keyboard: $BT_MAC (auto-reconnect enabled)"
[[ "$AUTO_UPDATE_ENABLED" -eq 1 ]] && echo "  Auto-update      : checks $REPO_URL ($REPO_BRANCH) at every boot"
echo "=============================================="
echo

if [[ "$REBOOT_NEEDED" -eq 1 ]]; then
    if ask_yn "A reboot is needed to finish SPI/USB-gadget setup. Reboot now?" Y; then
        sudo reboot
    else
        info "Remember to reboot before relying on the e-ink panel or type-out mode."
    fi
else
    echo "All done -- no reboot required."
fi
