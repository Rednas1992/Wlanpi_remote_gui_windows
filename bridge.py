#!/usr/bin/env python3
"""
WLAN Pi Remote - bridge (runs ON the WLAN Pi, started by app.py over SSH).

- Stops the wlanpi-fpms service and runs FPMS itself in button-emulation
  mode (-e), so key presses arriving over the SSH pty act as button presses.
  The physical buttons and the real display keep working at the same time.
- Hooks fpms' oled.drawImage() and sends every new frame as base64 PNG
  over stdout, wrapped in @@FRAME:...@@ markers.
- On any exit (GUI quit, SSH drop, crash) the wlanpi-fpms service is
  started again, so the device always returns to normal.

Protocol (stdin, single bytes): w x a d s i o p = up down left right
center key1 key2 key3, 'z' = heartbeat, 'Q' = quit.
"""

import base64
import hashlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
import tty

SERVICE = "wlanpi-fpms"
HEARTBEAT_TIMEOUT = 15  # seconds without input from the GUI -> quit

_out_lock = threading.Lock()
_shutdown_lock = threading.RLock()  # re-entrant: a signal may hit mid-shutdown
_last_rx = time.time()
_last_hash = None
_stopping = False


def emit(tag, payload=""):
    with _out_lock:
        try:
            os.write(1, f"\r\n@@{tag}:{payload}@@\r\n".encode())
        except OSError:
            pass


def shutdown(code=0):
    """Restart the normal FPMS service after we are gone, then exit hard."""
    global _stopping
    # SIGHUP and stdin EOF usually arrive together on an SSH drop: only the
    # first caller may proceed, the rest wait for it to exit the process.
    if not _shutdown_lock.acquire(blocking=False):
        while True:
            time.sleep(1)
    _stopping = True
    try:
        # Detached + delayed: our process must release the GPIO lines first.
        subprocess.Popen(
            ["sh", "-c", f"trap '' HUP; sleep 2; systemctl start {SERVICE}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
    emit("BYE")
    os._exit(code)


class RemoteStdin:
    """Replaces sys.stdin for fpms' getch(): filters heartbeats/quit."""

    def __init__(self, real):
        self._real = real
        self._fd = real.fileno()

    def fileno(self):
        return self._fd

    def read(self, n=1):
        global _last_rx
        while True:
            try:
                b = os.read(self._fd, 1)
            except OSError:
                shutdown()
            if not b:
                shutdown()
            _last_rx = time.time()
            ch = b.decode("latin-1")
            if ch == "z":
                continue
            if ch == "Q":
                shutdown()
            return ch

    def __getattr__(self, name):
        return getattr(self._real, name)


class _NoTermios:
    """fpms' getch() toggles raw mode per key; we keep the tty raw instead."""

    error = Exception

    @staticmethod
    def tcgetattr(fd):
        return None

    @staticmethod
    def tcsetattr(fd, when, attrs):
        return None

    TCSADRAIN = 1


class _NoTty:
    @staticmethod
    def setraw(fd, when=None):
        return None


def send_frame(image):
    global _last_hash
    try:
        img = image.convert("RGB")
        h = hashlib.md5(img.tobytes()).digest()
        if h == _last_hash:
            return
        _last_hash = h
        buf = io.BytesIO()
        img.save(buf, "PNG")
        emit("FRAME", base64.b64encode(buf.getvalue()).decode())
    except Exception as e:  # never break fpms because of us
        emit("LOG", f"frame error: {e!r}")


def watchdog():
    while True:
        time.sleep(2)
        if time.time() - _last_rx > HEARTBEAT_TIMEOUT:
            shutdown()


def main():
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: shutdown())

    try:
        tty.setraw(0)
    except Exception:
        pass

    subprocess.run(["systemctl", "stop", SERVICE], check=False)
    time.sleep(0.5)

    from fpms.modules import wlanpi_oled as oled

    orig_draw = oled.drawImage
    orig_sleep = oled.sleep
    orig_wakeup = oled.wakeup

    def draw_image(image):
        orig_draw(image)
        send_frame(image)

    def screen_sleep():
        global _last_hash
        orig_sleep()
        _last_hash = None
        emit("SLEEP")

    def screen_wakeup():
        orig_wakeup()
        emit("WAKE")

    oled.drawImage = draw_image
    oled.sleep = screen_sleep
    oled.wakeup = screen_wakeup

    from fpms import fpms as fpms_main

    fpms_main.termios = _NoTermios
    fpms_main.tty = _NoTty
    sys.stdin = RemoteStdin(sys.stdin)

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        from fpms.modules.constants import BUTTONS_PINS, PLATFORM

        model, buttons = str(PLATFORM), ",".join(BUTTONS_PINS)
    except Exception:
        model, buttons = "", ""
    emit("READY", f"{model}|{buttons}")

    sys.argv = ["fpms", "-e"]
    try:
        fpms_main.main()
    except SystemExit:
        pass
    except Exception as e:
        emit("LOG", f"fpms stopped: {e!r}")
    shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        emit("LOG", f"bridge error: {e!r}")
        shutdown(1)
