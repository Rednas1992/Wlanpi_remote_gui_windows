#!/usr/bin/env python3
"""
WLAN Pi Remote - local GUI that mirrors the WLAN Pi front panel.

Connects over SSH, uploads bridge.py, runs it with sudo, shows every frame
of the FPMS display and sends button presses back.

    pip install -r requirements.txt
    python app.py
"""

import base64
import io
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import paramiko
from PIL import Image, ImageDraw, ImageTk

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
BRIDGE_LOCAL = HERE / "bridge.py"
BRIDGE_REMOTE = "/tmp/wlanpi_remote_bridge.py"

SCREEN_PX = 128
# GUI button -> key understood by fpms' button emulator (-e)
KEYS = {
    "up": "w",
    "down": "x",
    "left": "a",
    "right": "d",
    "center": "s",
    "key1": "i",
    "key2": "o",
    "key3": "p",
}

# colours
BODY = "#2b2d31"
BODY_EDGE = "#1b1c1f"
BEZEL = "#050505"
BTN = "#45484f"
BTN_HI = "#6d9eeb"
BTN_EDGE = "#151618"
TEXT = "#c9ccd1"
ACCENT = "#e8a33d"


# --------------------------------------------------------------------------
# SSH session (runs in background threads, talks to the GUI through a queue)
# --------------------------------------------------------------------------
class PiSession:
    def __init__(self, host, user, password, events):
        self.host = host
        self.user = user
        self.password = password
        self.events = events  # queue of (kind, data)
        self.client = None
        self.chan = None
        self.alive = False
        self._send_lock = threading.Lock()

    # ---- public -----------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def press(self, name):
        self._send(KEYS[name])

    def close(self):
        if self.alive:
            self._send("Q")
            time.sleep(0.3)
        self.alive = False
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass

    # ---- internals ----------------------------------------------------------
    def _send(self, data):
        with self._send_lock:
            try:
                if self.chan and not self.chan.closed:
                    self.chan.send(data.encode())
            except Exception:
                pass

    def _emit(self, kind, data=None):
        self.events.put((self, kind, data))

    def _run(self):
        try:
            self._emit("status", f"Verbinden met {self.host}...")
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            host, _, port = self.host.partition(":")
            self.client.connect(
                host,
                port=int(port or 22),
                username=self.user,
                password=self.password or None,
                timeout=10,
                look_for_keys=True,
                allow_agent=True,
            )

            self._emit("status", "Bridge uploaden...")
            sftp = self.client.open_sftp()
            sftp.put(str(BRIDGE_LOCAL), BRIDGE_REMOTE)
            sftp.close()

            python = self._find_python()
            self._emit("status", f"FPMS starten ({python})...")

            self.chan = self.client.get_transport().open_session()
            self.chan.get_pty(term="dumb", width=200, height=50)
            self.chan.exec_command(
                f"sudo -S -p '@@SUDO@@' {python} -u {BRIDGE_REMOTE}"
            )
            self.alive = True
            self._read_loop()
        except paramiko.AuthenticationException:
            self._emit("error", "Inloggen mislukt: controleer gebruiker/wachtwoord.")
        except Exception as e:
            self._emit("error", f"Verbinding mislukt: {e}")
        finally:
            self.alive = False
            self._emit("closed")

    def _find_python(self):
        cmd = (
            "for p in /opt/wlanpi-fpms/bin/python3 /opt/wlanpi-fpms/bin/python; do "
            '[ -x "$p" ] && echo "$p" && exit 0; done; '
            "f=$(readlink -f /usr/sbin/fpms 2>/dev/null); "
            "[ -n \"$f\" ] && head -1 \"$f\" | sed 's/^#!//'"
        )
        _, out, _ = self.client.exec_command(cmd, timeout=10)
        py = out.read().decode().strip().splitlines()
        if not py:
            raise RuntimeError("FPMS (wlanpi-fpms) niet gevonden op dit apparaat")
        return py[0].strip()

    def _heartbeat(self):
        while self.alive:
            self._send("z")
            time.sleep(2)

    def _read_loop(self):
        buf = ""
        sudo_tries = 0
        ready = False
        while True:
            data = self.chan.recv(65536)
            if not data:
                break
            buf += data.decode("latin-1")
            while True:
                start = buf.find("@@")
                if start < 0:
                    if ready and buf.strip():
                        self._emit("log", buf.strip())
                    buf = buf[-1:] if buf.endswith("@") else ""
                    break
                end = buf.find("@@", start + 2)
                if end < 0:
                    buf = buf[start:]  # wait for the rest of the token
                    break
                if ready and buf[:start].strip():
                    self._emit("log", buf[:start].strip())
                token = buf[start + 2 : end]
                buf = buf[end + 2 :]
                tag, _, payload = token.partition(":")

                if tag == "SUDO":
                    sudo_tries += 1
                    if not self.password:
                        self._emit("error", "sudo vraagt een wachtwoord: vul het wachtwoord in.")
                        self.chan.close()
                        return
                    if sudo_tries > 1:
                        self._emit("error", "sudo-wachtwoord onjuist.")
                        self.chan.close()
                        return
                    self._send(self.password + "\n")
                elif tag == "READY":
                    ready = True
                    # Only now: a heartbeat sent earlier would end up in
                    # sudo's password input ("z" + password -> rejected).
                    threading.Thread(target=self._heartbeat, daemon=True).start()
                    self._emit("ready", payload)
                elif tag == "FRAME":
                    self._emit("frame", payload)
                elif tag == "SLEEP":
                    self._emit("sleep")
                elif tag == "WAKE":
                    self._emit("wake")
                elif tag == "LOG":
                    self._emit("log", payload)
                elif tag == "BYE":
                    self._emit("status", "FPMS-service wordt hersteld.")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WLAN Pi Remote")
        self.configure(bg=BODY_EDGE)
        self.resizable(False, False)

        self.cfg = self._load_cfg()
        self.scale = int(self.cfg.get("scale", 3))
        self.events = queue.Queue()
        self.session = None
        self.frame = None  # last PIL image
        self.sleeping = False
        self.photo = None
        self.buttons = {}  # name -> canvas item id
        self.show_keys = True  # WLAN Pi Pro has no KEY1-3

        self._build_toolbar()
        self.canvas = tk.Canvas(self, bg=BODY_EDGE, highlightthickness=0)
        self.canvas.pack(padx=12, pady=(4, 6))
        self.status = tk.Label(
            self, text="Niet verbonden", anchor="w", bg=BODY_EDGE, fg=TEXT, font=("Segoe UI", 9)
        )
        self.status.pack(fill="x", padx=12, pady=(0, 8))

        self._draw_device()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(30, self._poll)

    # ---- config -----------------------------------------------------------
    def _load_cfg(self):
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {"host": "wlanpi.local", "user": "wlanpi", "scale": 3}

    def _save_cfg(self):
        self.cfg.update(host=self.host_var.get().strip(), user=self.user_var.get().strip(), scale=self.scale)
        try:
            CONFIG_FILE.write_text(json.dumps(self.cfg, indent=2))
        except Exception:
            pass

    # ---- toolbar ------------------------------------------------------------
    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BODY_EDGE)
        bar.pack(fill="x", padx=12, pady=(10, 4))

        def lbl(t):
            tk.Label(bar, text=t, bg=BODY_EDGE, fg=TEXT, font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))

        self.host_var = tk.StringVar(value=self.cfg.get("host", "wlanpi.local"))
        self.user_var = tk.StringVar(value=self.cfg.get("user", "wlanpi"))
        self.pass_var = tk.StringVar()

        lbl("Host")
        self.host_entry = ttk.Entry(bar, textvariable=self.host_var, width=16)
        self.host_entry.pack(side="left", padx=(0, 8))
        lbl("Gebruiker")
        ttk.Entry(bar, textvariable=self.user_var, width=9).pack(side="left", padx=(0, 8))
        lbl("Wachtwoord")
        pw = ttk.Entry(bar, textvariable=self.pass_var, show="•", width=11)
        pw.pack(side="left", padx=(0, 8))
        pw.bind("<Return>", lambda e: self._toggle_connect())

        self.connect_btn = ttk.Button(bar, text="Verbinden", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Screenshot", command=self._save_png).pack(side="left", padx=(0, 6))

        self.scale_var = tk.StringVar(value=f"{self.scale}x")
        zoom = ttk.Combobox(bar, textvariable=self.scale_var, values=["2x", "3x", "4x", "5x"], width=4, state="readonly")
        zoom.pack(side="left")
        zoom.bind("<<ComboboxSelected>>", self._on_zoom)

    # ---- device drawing -------------------------------------------------------
    def _rounded(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _draw_device(self):
        c = self.canvas
        c.delete("all")
        self.buttons.clear()

        s = SCREEN_PX * self.scale
        pad = 28
        side = 170  # width for d-pad / keys area
        w = side + s + side + 2 * pad
        h = s + 2 * pad + 40
        c.config(width=w, height=h)

        self._rounded(2, 2, w - 2, h - 2, 36, fill=BODY, outline=BODY_EDGE, width=2)
        c.create_text(w / 2, h - 22, text="WLAN Pi", fill=TEXT, font=("Segoe UI Semibold", 12))

        # screen + bezel
        sx = pad + side
        sy = pad
        self._rounded(sx - 12, sy - 12, sx + s + 12, sy + s + 12, 14, fill=BEZEL, outline="#000")
        self.screen_item = c.create_image(sx, sy, anchor="nw")
        self.screen_xy = (sx, sy)

        # d-pad (left)
        cx = pad + side / 2
        cy = sy + s / 2
        d = 52  # distance from centre
        r = 22
        c.create_oval(cx - d - r - 8, cy - d - r - 8, cx + d + r + 8, cy + d + r + 8, fill="#232428", outline=BTN_EDGE)
        for name, dx, dy, arrow in (
            ("up", 0, -d, "▲"),
            ("down", 0, d, "▼"),
            ("left", -d, 0, "◀"),
            ("right", d, 0, "▶"),
            ("center", 0, 0, "●"),
        ):
            self._button(name, cx + dx, cy + dy, r, arrow)

        # keys (right)
        kx = sx + s + pad + side / 2
        for i, name in enumerate(("key1", "key2", "key3") if self.show_keys else ()):
            ky = cy + (i - 1) * 70
            self._button(name, kx, ky, 24, str(i + 1))
            c.create_text(kx + 42, ky, text=f"KEY{i + 1}", fill=TEXT, font=("Segoe UI", 8), anchor="w")

        self._render_screen()

    def _button(self, name, x, y, r, label):
        c = self.canvas
        tag = f"btn_{name}"
        oval = c.create_oval(x - r, y - r, x + r, y + r, fill=BTN, outline=BTN_EDGE, width=2, tags=(tag,))
        c.create_text(x, y, text=label, fill=TEXT, font=("Segoe UI", 11, "bold"), tags=(tag,))
        c.tag_bind(tag, "<ButtonPress-1>", lambda e, n=name: self._press(n))
        c.tag_bind(tag, "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda e: c.config(cursor=""))
        self.buttons[name] = oval

    def _flash(self, name):
        item = self.buttons.get(name)
        if item:
            self.canvas.itemconfig(item, fill=BTN_HI)
            self.after(140, lambda: self.canvas.itemconfig(item, fill=BTN))

    def _render_screen(self):
        s = SCREEN_PX * self.scale
        if self.frame is not None and not self.sleeping:
            img = self.frame.resize((s, s), Image.NEAREST)
        else:
            img = Image.new("RGB", (s, s), "black")
            msg = "scherm slaapt" if self.sleeping else ("" if self.session else "niet verbonden")
            if msg:
                ImageDraw.Draw(img).text((s / 2, s / 2), msg, fill="#444", anchor="mm")
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self.screen_item, image=self.photo)

    # ---- input ------------------------------------------------------------------
    def _bind_keys(self):
        mapping = {
            "<Up>": "up", "<Down>": "down", "<Left>": "left", "<Right>": "right",
            "<Return>": "center", "<space>": "center",
            "1": "key1", "2": "key2", "3": "key3",
        }
        for seq, name in mapping.items():
            self.bind(seq, lambda e, n=name: self._key(e, n))

    def _key(self, event, name):
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return  # typing in a field
        self._press(name)

    def _press(self, name):
        self._flash(name)
        if self.session and self.session.alive:
            self.session.press(name)
            if self.sleeping:
                self.sleeping = False
                self._render_screen()

    # ---- actions ----------------------------------------------------------------
    def _toggle_connect(self):
        if self.session:
            self._disconnect()
            return
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        if not host or not user:
            messagebox.showwarning("WLAN Pi Remote", "Vul host en gebruiker in.")
            return
        self._save_cfg()
        self.session = PiSession(host, user, self.pass_var.get(), self.events)
        self.connect_btn.config(text="Verbreken")
        self.session.start()

    def _disconnect(self):
        s, self.session = self.session, None
        if s:
            threading.Thread(target=s.close, daemon=True).start()
        self.connect_btn.config(text="Verbinden")
        self.frame = None
        self.sleeping = False
        self._render_screen()
        self._set_status("Niet verbonden (FPMS-service wordt op de Pi hersteld)")

    def _save_png(self):
        if self.frame is None:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            initialfile=time.strftime("wlanpi_%Y%m%d_%H%M%S.png"),
            filetypes=[("PNG", "*.png")],
        )
        if path:
            self.frame.resize((SCREEN_PX * 4, SCREEN_PX * 4), Image.NEAREST).save(path)
            self._set_status(f"Opgeslagen: {path}")

    def _on_zoom(self, _e=None):
        self.scale = int(self.scale_var.get().rstrip("x"))
        self._save_cfg()
        self._draw_device()

    def _on_close(self):
        if self.session:
            self.session.close()
        self.destroy()

    def _set_status(self, text):
        self.status.config(text=text)

    # ---- event pump ---------------------------------------------------------------
    def _poll(self):
        latest = None
        try:
            while True:
                src, kind, data = self.events.get_nowait()
                if src is not self.session:
                    continue  # event from an old connection
                if kind == "frame":
                    latest = data  # only render the newest frame
                elif kind == "status":
                    self._set_status(data)
                elif kind == "ready":
                    model, _, keys = data.partition("|")
                    has_keys = (not keys) or "key1" in keys.split(",")
                    if has_keys != self.show_keys:
                        self.show_keys = has_keys
                        self._draw_device()
                    hint = "pijltjes, Enter" + (", 1/2/3" if self.show_keys else "")
                    self._set_status(f"Verbonden met {self.host_var.get()}" + (f" ({model})" if model else "") + f"  |  {hint}")
                elif kind == "sleep":
                    self.sleeping = True
                    self._render_screen()
                elif kind == "wake":
                    self.sleeping = False
                    self._render_screen()
                elif kind == "log":
                    self._set_status(data[-160:])
                elif kind == "error":
                    messagebox.showerror("WLAN Pi Remote", data)
                elif kind == "closed":
                    self.session = None
                    self.connect_btn.config(text="Verbinden")
                    self.frame = None
                    self._render_screen()
                    self._set_status("Verbinding gesloten")
        except queue.Empty:
            pass
        if latest:
            try:
                self.frame = Image.open(io.BytesIO(base64.b64decode(latest))).convert("RGB")
                self.sleeping = False
                self._render_screen()
            except Exception:
                pass
        self.after(30, self._poll)


if __name__ == "__main__":
    App().mainloop()
