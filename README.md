# WLAN Pi Remote

A local Windows GUI that mirrors the WLAN Pi front panel: you see exactly
what is on the device's display, live, and you can operate the buttons
(D-pad + KEY1/2/3) with the mouse or keyboard.

## Getting started

Double-click `start.bat`. On first run it creates a `.venv` with `paramiko`
and `Pillow`. Or manually:

```
pip install -r requirements.txt
python app.py
```

Enter the host (e.g. `wlanpi.local`, the IP address, or `ip:port`), the user
(`wlanpi`) and the password, then click **Connect**. SSH keys work too; the
password is then only needed if `sudo` asks for it.

Controls: arrow keys, Enter/Space = center, 1/2/3 = KEY1-3.
**Screenshot** saves the current screen as a PNG (4x upscaled).

## How it works

1. `app.py` connects over SSH and uploads `bridge.py` to `/tmp` on the Pi.
2. `bridge.py` runs with `sudo`, stops the `wlanpi-fpms` service and starts
   FPMS itself in its built-in button emulation mode (`fpms -e`).
3. Every time FPMS draws the screen (`oled.drawImage`), the bridge sends the
   image to the GUI as a PNG. Button presses from the GUI go back to the FPMS
   emulator as key presses.
4. The physical display and buttons keep working at the same time.
5. On exit, on a dropped connection, or after 15 s without a heartbeat, the
   bridge stops and `wlanpi-fpms` starts again automatically.

Nothing is permanently installed or changed on the Pi.
