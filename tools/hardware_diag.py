#!/usr/bin/env python3
"""
Staged hardware diagnostic for the Waveshare 5.79" e-Paper HAT (SKU 26843,
Rev2.3) -- NOT part of Inkwriter itself. Written to isolate exactly which
electrical step (power-enable, reset pulse, first busy-wait, or full SPI
init) is triggering the Pi-wide crashes/reboots seen when the panel is
driven, since every previous test (Inkwriter's own code, Waveshare's own
unmodified demo, and a from-scratch minimal script) has failed at the
same point with no logs surviving afterward.

WHY THIS IS DIFFERENT FROM EARLIER TESTS
-----------------------------------------
Every crash so far has wiped its own evidence -- even a *verified
working* persistent systemd journal didn't survive one of these events,
which points at an abrupt power interruption rather than a clean kernel
panic (a real panic or graceful reboot leaves fsync'd disk writes
intact; a hard power cut doesn't). Ordinary logging can't out-run that,
so this script:

  1. Breaks the panel bring-up sequence into small, individually-logged
     stages (power pin -> reset pulse -> first busy-wait -> full init),
     instead of one opaque call -- so whichever stage is last logged is
     the one that caused it.
  2. Writes every log line to disk with an immediate flush() + fsync()
     -- not buffered, not batched -- so the log up through the last
     completed step survives even a hard power cut a fraction of a
     second later.
  3. Runs a background thread polling `vcgencmd get_throttled` (and
     voltage, if supported) every ~150ms into that same fsync'd log, in
     case a brief undervoltage flag appears in the instant before a
     harder failure.
  4. Pauses for you to press Enter between stages, so whatever's on
     your screen (or in a video recording) when it locks up tells you
     exactly which stage was in progress.

You mentioned you don't think it's a short, but this is exactly the
test that would show one: if merely enabling the HAT's power pin (stage
2, no SPI, no reset, nothing else) alone is enough to crash the Pi,
that's a strong sign of a short or excessive current draw right at
power-on -- rather than something specific to SPI communication or the
reset pulse.

HOW TO RUN
----------
Stop the real services first so nothing else is holding the GPIO/SPI
pins:

    sudo systemctl stop inkwriter bt-reconnect inkwriter-hid-setup
    cd ~/inkwriter
    python3 tools/hardware_diag.py

Follow the prompts. If it locks up or the Pi reboots, log back in and
run it again with --show-last to see exactly how far it got last time:

    python3 tools/hardware_diag.py --show-last

Send the contents of hardware_diag.log (in this same tools/ directory)
back along with whatever stage it stopped at -- that's the single most
useful thing you can hand to Waveshare support alongside the video.
"""
import os
import sys
import time
import threading
import subprocess
import importlib
from datetime import datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hardware_diag.log")
DRIVER = "epd5in79"

_log_lock = threading.Lock()


def log(msg: str):
    """Write one line to both stdout and the log file, flushed and
    fsync'd immediately so it survives an abrupt power loss a moment
    later. This immediacy is the entire point of this script -- do not
    change this to buffered/batched writes."""
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}"
    with _log_lock:
        print(line, flush=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def show_last():
    if not os.path.exists(LOG_PATH):
        print("No hardware_diag.log yet -- nothing has been run.")
        return
    with open(LOG_PATH) as f:
        lines = f.readlines()
    print(f"Last {min(25, len(lines))} lines of {LOG_PATH}:\n")
    for line in lines[-25:]:
        print(line.rstrip())
    print(
        "\nWhatever line is LAST above is the last thing that completed "
        "before this run ended (crash, reboot, or clean exit) -- if it's "
        "a 'starting stage N' line with no matching 'stage N done', stage "
        "N is what caused it."
    )


def voltage_watchdog(stop_event: threading.Event):
    """Runs continuously in the background for the whole session, polling
    undervoltage/voltage status frequently. If a brief undervoltage flag
    or voltage sag appears right before a harder failure, this is our
    best chance of catching it on disk."""
    while not stop_event.is_set():
        try:
            throttled = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
        except Exception as exc:
            throttled = f"<error: {exc}>"
        try:
            volts = subprocess.run(
                ["vcgencmd", "measure_volts", "core"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
        except Exception:
            volts = "<unavailable>"
        log(f"[watchdog] {throttled}  {volts}")
        stop_event.wait(0.15)


def pin_state(bcm_pin: int) -> str:
    """Read a pin's current level via pinctrl (works without touching
    the driver at all, so it's safe to call before anything else is
    imported)."""
    try:
        out = subprocess.run(
            ["pinctrl", "get", str(bcm_pin)], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        return out
    except Exception as exc:
        return f"<pinctrl unavailable: {exc}>"


def confirm(prompt: str):
    input(f"\n>>> {prompt} [press Enter to continue, Ctrl+C to stop] ")


def main():
    if "--show-last" in sys.argv:
        show_last()
        return

    log("=" * 70)
    log("Starting new hardware_diag.py run")

    # ---- Stage 0: baseline environment info, no hardware touched -------
    log("Stage 0: baseline environment info (nothing touched yet)")
    try:
        model = open("/proc/device-tree/model").read().strip("\x00")
    except Exception:
        model = "<unknown>"
    log(f"  Pi model: {model}")
    throttled = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout.strip()
    log(f"  {throttled}  (0x0 = no undervoltage recorded since last boot)")
    log(f"  /dev/spidev0.0 exists: {os.path.exists('/dev/spidev0.0')}")

    epdconfig = importlib.import_module(f"waveshare_epd.epdconfig")
    for pin_name in ("RST_PIN", "DC_PIN", "CS_PIN", "BUSY_PIN", "PWR_PIN"):
        val = getattr(epdconfig, pin_name, None)
        log(f"  {pin_name} = {val}")
    log("Stage 0 done\n")

    # ---- Stage 1: read-only pin states before touching anything --------
    log("Stage 1: reading current pin states (read-only, no GPIO setup yet)")
    for pin_name in ("RST_PIN", "DC_PIN", "CS_PIN", "BUSY_PIN", "PWR_PIN"):
        val = getattr(epdconfig, pin_name, None)
        if isinstance(val, int):
            log(f"  pin {pin_name} (BCM{val}): {pin_state(val)}")
    log("Stage 1 done\n")

    # Start the background voltage/throttle watchdog now, before any risky step
    stop_event = threading.Event()
    watchdog = threading.Thread(target=voltage_watchdog, args=(stop_event,), daemon=True)
    watchdog.start()
    log("Background voltage watchdog started -- will log every ~150ms for the rest of this run")

    try:
        confirm(
            "Stage 2 will call epdconfig.module_init() -- this sets up the "
            "GPIO/SPI handles but does NOT yet touch the panel's power or "
            "reset pins. If this alone crashes it, that points at the "
            "GPIO/SPI setup itself rather than the panel."
        )
        log("Stage 2: starting epdconfig.module_init()")
        rc = epdconfig.module_init()
        log(f"Stage 2 done -- module_init() returned {rc}")

        has_pwr_pin = getattr(epdconfig, "PWR_PIN", None) is not None
        if has_pwr_pin:
            confirm(
                "Stage 3 will assert the HAT's PWR pin HIGH and hold it for "
                "3 seconds -- no SPI, no reset pulse, nothing else. This is "
                "the most direct test for a short: if merely enabling power "
                "crashes the Pi, that's a strong sign of a short or excess "
                "current draw right at power-on."
            )
            log("Stage 3: asserting PWR_PIN high")
            epdconfig.digital_write(epdconfig.PWR_PIN, 1)
            for i in range(6):
                time.sleep(0.5)
                log(f"  ({(i+1)*0.5:.1f}s) PWR_PIN still asserted, pin reads: {pin_state(epdconfig.PWR_PIN)}")
            log("Stage 3 done -- PWR_PIN held high for 3s with no crash")
        else:
            log("Stage 3 skipped -- this driver version has no separate PWR_PIN")

        confirm(
            "Stage 4 will perform a single reset pulse (epd.reset()) -- "
            "still no SPI commands sent yet. If this is where it crashes, "
            "that points at the reset circuit specifically."
        )
        epd_module = importlib.import_module(f"waveshare_epd.{DRIVER}")
        epd = epd_module.EPD()
        log("Stage 4: starting epd.reset()")
        epd.reset()
        log("Stage 4 done -- reset() returned OK")

        confirm(
            "Stage 5 will call ReadBusy() for the first time -- this is "
            "exactly where the very first hang was originally found months "
            "ago. If it crashes/hangs here, that's the clearest possible "
            "confirmation of where the fault lives."
        )
        log("Stage 5: starting first ReadBusy() call")
        epd.ReadBusy()
        log("Stage 5 done -- ReadBusy() returned OK")

        confirm(
            "Stage 6 will run the full, normal epd.init() -- the complete "
            "sequence, for comparison against the isolated steps above."
        )
        log("Stage 6: starting full epd.init()")
        epd.init()
        log("Stage 6 done -- init() returned OK")

        log("All stages completed with no crash. Panel is at least")
        log("electrically responding to the full init sequence in this run.")

    except KeyboardInterrupt:
        log("Stopped by user (Ctrl+C) -- not a crash, just an intentional stop.")
    finally:
        stop_event.set()
        log("Run ending, watchdog stopping.")


if __name__ == "__main__":
    main()
