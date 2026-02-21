"""
Settings GUI — modern dark-themed configuration panel.
"""

import os
import sys
import json
import time
import socket
import threading
import winreg
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import shutil
import ctypes
import cv2
import subprocess
from PIL import Image, ImageTk

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
FACES_DB_DIR  = os.path.join(SCRIPT_DIR, "faces_db")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")

STARTUP_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME  = "FacialRecognitionSecurity"

_IPC_PORT     = 52718          # localhost-only port for single-instance IPC
_IPC_HOST     = "127.0.0.1"

DEFAULTS = {
    "camera_index": 0,
    "resolution_w": 640,
    "resolution_h": 480,
    "fps_cap": 30,
    "detection_delay": 0.0,
    "idle_skip_pct": 0,
    "alert_sustain": 3.0,
    "detection_resolution": 640,
    "cosine_threshold": 0.50,
    "bad_splash_threshold": 0.45,
    "size_margin": 1.10,
    "score_threshold": 0.70,
    "nms_threshold": 0.30,
    "show_splash": True,
    "headless": False,
    "run_in_background": False,
    "start_on_boot": False,
}

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
PAL = {
    "bg":           "#0f0f0f",
    "surface":      "#1a1a1a",
    "card":         "#222222",
    "card_hover":   "#2a2a2a",
    "border":       "#333333",
    "text":         "#e4e4e7",
    "text_dim":     "#71717a",
    "accent":       "#6366f1",
    "accent_hover": "#818cf8",
    "accent_text":  "#ffffff",
    "green":        "#4ade80",
    "green_dim":    "#166534",
    "red":          "#f87171",
    "red_dim":      "#991b1b",
    "input_bg":     "#2a2a2a",
    "input_border": "#3f3f46",
    "slider_track": "#3f3f46",
}

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def load_settings():
    settings = dict(DEFAULTS)
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    settings["start_on_boot"] = _is_autostart_enabled()
    return settings


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

# ---------------------------------------------------------------------------
# Auto-start (Windows Registry)
# ---------------------------------------------------------------------------

def _get_startup_command():
    return f'"{sys.executable}" "{os.path.join(SCRIPT_DIR, "main.py")}" --no-gui --headless'


def _is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, STARTUP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _set_autostart(enable):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, _get_startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[WARN] Could not update startup registry: {e}")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_people():
    people = {"good": [], "bad": []}
    for cat in ("good", "bad"):
        cat_dir = os.path.join(FACES_DB_DIR, cat)
        if not os.path.isdir(cat_dir):
            os.makedirs(cat_dir, exist_ok=True)
            continue
        for name in sorted(os.listdir(cat_dir)):
            if os.path.isdir(os.path.join(cat_dir, name)):
                people[cat].append(name)
    return people


def count_images(category, name):
    person_dir = os.path.join(FACES_DB_DIR, category, name)
    if not os.path.isdir(person_dir):
        return 0
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    return sum(1 for f in os.listdir(person_dir)
               if os.path.splitext(f)[1].lower() in exts)

# ---------------------------------------------------------------------------
# Camera detection
# ---------------------------------------------------------------------------

from camera_enum import enumerate_cameras

COMMON_RESOLUTIONS = [
    (3840, 2160), (2560, 1440), (1920, 1080), (1280, 720),
    (1024, 768), (800, 600), (640, 480), (320, 240),
]

# ---------------------------------------------------------------------------
# Resolution cache — avoids re-probing cameras whose resolutions are known.
# ---------------------------------------------------------------------------

_RESOLUTION_CACHE_FILE = os.path.join(SCRIPT_DIR, "resolution_cache.json")


def _load_resolution_cache():
    """Return ``{device_name: [[w,h], ...], ...}`` from disk, or ``{}``."""
    try:
        with open(_RESOLUTION_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_resolution_cache(cache):
    """Persist the resolution cache to disk."""
    try:
        with open(_RESOLUTION_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def _try_open_camera(idx, quick=False):
    """Try to open a camera by index.

    If *quick* is True, only try the preferred backend (MSMF) and auto-detect
    to minimise time spent on cameras that won't open.  The full sequence
    (MSMF → DSHOW → AUTO) is used when *quick* is False.
    """
    backends = [(cv2.CAP_MSMF, "MSMF")]
    if not quick:
        backends.append((cv2.CAP_DSHOW, "DSHOW"))
    for backend, name in backends:
        try:
            cap = cv2.VideoCapture(idx, backend)
            if cap.isOpened():
                return cap, name
            cap.release()
        except Exception:
            pass
    try:
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            return cap, "AUTO"
        cap.release()
    except Exception:
        pass
    return None, None


def _probe_resolutions(idx):
    """Open *idx* via OpenCV and probe supported resolutions.

    Returns ``(supported_list, max_w, max_h)``.
    ``supported_list`` is empty if the device couldn't be opened.
    Catches all exceptions so that problematic virtual-camera drivers
    (e.g. NVIDIA Broadcast, Meta Quest) never crash the scan.

    No timeout — this is only called for devices that are NOT in the
    resolution cache, and runs in a parallel thread pool so slow cameras
    don't block fast ones.
    """
    # Suppress OpenCV internal warnings during probing
    try:
        cv2.setLogLevel(0)  # LOG_LEVEL_SILENT
    except Exception:
        pass

    try:
        cap, backend = _try_open_camera(idx, quick=True)
        if cap is None:
            return [], 640, 480

        native_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        native_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        supported, seen = [], set()
        if native_w > 0 and native_h > 0:
            supported.append((native_w, native_h))
            seen.add((native_w, native_h))

        for w, h in COMMON_RESOLUTIONS:
            if (w, h) in seen:
                continue
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if aw > 0 and ah > 0 and (aw, ah) not in seen:
                    supported.append((aw, ah))
                    seen.add((aw, ah))
            except Exception:
                continue

        cap.release()
        supported.sort(key=lambda r: r[0] * r[1], reverse=True)
        max_w, max_h = supported[0] if supported else (640, 480)
        return supported, max_w, max_h
    except Exception:
        return [], 640, 480


def probe_cameras(max_index=10):
    """Enumerate cameras by name (DirectShow COM) and probe resolutions.

    Cached devices (by name) are returned instantly from
    ``resolution_cache.json``.  Only devices *not* in the cache are probed
    via OpenCV (in parallel, no hard timeout so every resolution is found).
    Newly probed results are saved back to the cache for future runs.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    old_level = os.environ.get("OPENCV_LOG_LEVEL", "")
    os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
    cameras = []
    cache = _load_resolution_cache()
    cache_dirty = False

    try:
        ds_devices = enumerate_cameras()
    except Exception:
        ds_devices = []

    if ds_devices:
        # Separate cached vs uncached devices
        cached_devs = []
        uncached_devs = []
        for dev in ds_devices:
            if dev["name"] in cache:
                cached_devs.append(dev)
            else:
                uncached_devs.append(dev)

        # Instant results for cached devices
        for dev in cached_devs:
            resolutions = [tuple(r) for r in cache[dev["name"]]]
            if not resolutions:
                resolutions = [(1920, 1080), (1280, 720), (640, 480)]
            max_w, max_h = resolutions[0]

            virt_tag = "  [Virtual]" if dev["type"] == "virtual" else ""
            cameras.append({
                "index": dev["index"],
                "name":  dev["name"],
                "label": f"{dev['name']}{virt_tag}  ({max_w}\u00d7{max_h})",
                "max_w": max_w, "max_h": max_h,
                "resolutions": resolutions,
                "type": dev["type"],
            })

        # Probe uncached devices in parallel (no timeout — let them finish)
        if uncached_devs:
            with ThreadPoolExecutor(max_workers=max(1, len(uncached_devs))) as pool:
                futures = {
                    pool.submit(_probe_resolutions, dev["index"]): dev
                    for dev in uncached_devs
                }
                for fut in as_completed(futures):
                    dev = futures[fut]
                    try:
                        supported, max_w, max_h = fut.result()
                    except Exception:
                        supported, max_w, max_h = [], 640, 480

                    if not supported:
                        supported = [(1920, 1080), (1280, 720), (640, 480)]
                        max_w, max_h = 1920, 1080

                    # Save to cache
                    cache[dev["name"]] = [list(r) for r in supported]
                    cache_dirty = True

                    virt_tag = "  [Virtual]" if dev["type"] == "virtual" else ""
                    cameras.append({
                        "index": dev["index"],
                        "name":  dev["name"],
                        "label": f"{dev['name']}{virt_tag}  ({max_w}\u00d7{max_h})",
                        "max_w": max_w, "max_h": max_h,
                        "resolutions": supported,
                        "type": dev["type"],
                    })

        # Sort by device index so the order is deterministic
        cameras.sort(key=lambda c: c["index"])
    else:
        # Fallback: index-based probing (COM failed or no cameras)
        for idx in range(max_index):
            cam_name = f"Camera {idx}"
            if cam_name in cache:
                resolutions = [tuple(r) for r in cache[cam_name]]
                if not resolutions:
                    continue
                max_w, max_h = resolutions[0]
            else:
                supported, max_w, max_h = _probe_resolutions(idx)
                if not supported:
                    continue
                resolutions = supported
                cache[cam_name] = [list(r) for r in supported]
                cache_dirty = True
            cameras.append({
                "index": idx,
                "name":  cam_name,
                "label": f"{cam_name}  ({max_w}\u00d7{max_h})",
                "max_w": max_w, "max_h": max_h,
                "resolutions": resolutions,
                "type": "physical",
            })

    # Persist newly discovered resolutions for next time
    if cache_dirty:
        _save_resolution_cache(cache)

    if old_level:
        os.environ["OPENCV_LOG_LEVEL"] = old_level
    else:
        os.environ.pop("OPENCV_LOG_LEVEL", None)
    return cameras

# ---------------------------------------------------------------------------
# Rounded-rectangle card (canvas-drawn)
# ---------------------------------------------------------------------------

class RoundedCard(tk.Canvas):
    """A Canvas that draws a rounded-rectangle background.

    Automatically resizes its height to fit the inner frame content.
    """

    def __init__(self, parent, radius=14, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bg", PAL["bg"])
        super().__init__(parent, **kw)
        self._radius = radius
        self._fill = PAL["card"]
        self._border = PAL["border"]
        self._inner = tk.Frame(self, bg=PAL["card"])
        self._inner_id = self.create_window(0, 0, window=self._inner, anchor="nw")
        self.bind("<Configure>", self._redraw)
        # Track inner-frame size changes so the canvas grows to fit
        self._inner.bind("<Configure>", self._on_inner_configure)

    @property
    def inner(self):
        return self._inner

    def _on_inner_configure(self, event=None):
        """Resize the canvas height to match the inner frame's content."""
        self.update_idletasks()
        pad = self._radius // 2
        needed_h = self._inner.winfo_reqheight() + pad * 2
        current_h = self.winfo_height()
        if abs(needed_h - current_h) > 2:
            self.configure(height=needed_h)

    def _redraw(self, event=None):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        r = self._radius
        self.create_rounded_rect(0, 0, w, h, r, fill=self._fill,
                                 outline=self._border, width=1, tags="bg")
        self.tag_lower("bg")
        pad = r // 2
        self.coords(self._inner_id, pad, pad)
        self._inner.configure(width=w - pad * 2, height=0)

    def create_rounded_rect(self, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kw)

# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tw = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if self._tw or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tw = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=PAL["border"])
        inner = tk.Frame(tw, bg=PAL["card"], padx=8, pady=5)
        inner.pack(padx=1, pady=1)
        tk.Label(inner, text=self.text, justify="left",
                 bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 9), wraplength=340).pack()

    def _hide(self, event=None):
        if self._tw:
            self._tw.destroy()
            self._tw = None

# ---------------------------------------------------------------------------
# Modern toggle switch
# ---------------------------------------------------------------------------

class ToggleSwitch(tk.Canvas):
    def __init__(self, parent, variable=None, command=None, width=44, height=24, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bg", PAL["card"])
        super().__init__(parent, width=width, height=height, **kw)
        self._sw_w = width
        self._sw_h = height
        self._var = variable or tk.BooleanVar(value=False)
        self._cmd = command
        self.bind("<Button-1>", self._toggle)
        # Defer initial draw until widget is realized to avoid Tcl errors
        self.after_idle(self._draw)

    def _draw(self):
        self.delete("all")
        on = self._var.get()
        r = self._sw_h // 2
        bg = PAL["accent"] if on else PAL["slider_track"]
        self.create_rounded_rect(0, 0, self._sw_w, self._sw_h, r, fill=bg, outline="")
        knob_r = r - 3
        cx = self._sw_w - r if on else r
        self.create_oval(cx - knob_r, r - knob_r, cx + knob_r, r + knob_r,
                         fill="#ffffff", outline="")

    def create_rounded_rect(self, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kw)

    def _toggle(self, event=None):
        self._var.set(not self._var.get())
        self._draw()
        if self._cmd:
            self._cmd()

# ---------------------------------------------------------------------------
# Main GUI
# ---------------------------------------------------------------------------

class SettingsGUI:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Facial Recognition Security")
        self.root.configure(bg=PAL["bg"])
        self.root.resizable(True, True)
        self.root.minsize(640, 520)

        win_w, win_h = 700, 860
        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        self.root.geometry(f"{win_w}x{win_h}+{(sx-win_w)//2}+{(sy-win_h)//2}")

        self.settings = load_settings()
        self._running = False
        self._cameras = []

        # Hide window instead of quitting on close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_styles()
        self._build_ui()
        self._refresh_people()

        # Start IPC listener so a second launch can reshow this window
        self._start_ipc_server()

    # -- styles -----------------------------------------------------------
    def _build_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=PAL["bg"], foreground=PAL["text"],
                         font=("Segoe UI", 10))
        style.configure("TFrame", background=PAL["bg"])
        style.configure("Card.TFrame", background=PAL["card"])
        style.configure("TLabel", background=PAL["bg"], foreground=PAL["text"])
        style.configure("Card.TLabel", background=PAL["card"],
                         foreground=PAL["text"], font=("Segoe UI", 10))
        style.configure("Dim.TLabel", background=PAL["card"],
                         foreground=PAL["text_dim"], font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=PAL["bg"],
                         foreground=PAL["text"], font=("Segoe UI", 16, "bold"))
        style.configure("Subheader.TLabel", background=PAL["bg"],
                         foreground=PAL["text_dim"], font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=PAL["card"],
                         foreground=PAL["text"], font=("Segoe UI", 11, "bold"))

        style.configure("Accent.TButton", font=("Segoe UI", 10),
                         background=PAL["accent"], foreground=PAL["accent_text"],
                         borderwidth=0, padding=(14, 7))
        style.map("Accent.TButton",
                  background=[("active", PAL["accent_hover"])])

        style.configure("Ghost.TButton", font=("Segoe UI", 9),
                         background=PAL["card"], foreground=PAL["text_dim"],
                         borderwidth=0, padding=(10, 5))
        style.map("Ghost.TButton",
                  background=[("active", PAL["card_hover"])],
                  foreground=[("active", PAL["text"])])

        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"),
                         background=PAL["green"], foreground="#000000",
                         borderwidth=0, padding=(20, 9))
        style.map("Start.TButton",
                  background=[("active", "#34d399")])

        style.configure("Stop.TButton", font=("Segoe UI", 11, "bold"),
                         background=PAL["red"], foreground="#000000",
                         borderwidth=0, padding=(20, 9))
        style.map("Stop.TButton",
                  background=[("active", "#fca5a5")])

        style.configure("TCombobox",
                         fieldbackground=PAL["input_bg"],
                         background=PAL["card"],
                         foreground=PAL["text"],
                         arrowcolor=PAL["text"],
                         borderwidth=1,
                         padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PAL["input_bg"]),
                                   ("disabled", PAL["surface"])],
                  foreground=[("readonly", PAL["text"]),
                              ("disabled", PAL["text_dim"])],
                  selectbackground=[("readonly", PAL["input_bg"])],
                  selectforeground=[("readonly", PAL["text"])],
                  background=[("readonly", PAL["card"])])
        # Dropdown list colours (Tk Listbox inside the ttk Combobox popdown)
        self.root.option_add("*TCombobox*Listbox.background", PAL["input_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", PAL["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", PAL["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", PAL["accent_text"])
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

        style.configure("Vertical.TScrollbar",
                         background=PAL["surface"], troughcolor=PAL["bg"],
                         borderwidth=0, arrowsize=0)

    @staticmethod
    def _style_combobox_popdown(combo):
        """Force dark colours on a ttk.Combobox's dropdown Listbox."""
        try:
            popdown = combo.tk.call("ttk::combobox::PopdownWindow", combo)
            lb = f"{popdown}.f.l"
            combo.tk.call(lb, "configure",
                          "-background", PAL["input_bg"],
                          "-foreground", PAL["text"],
                          "-selectbackground", PAL["accent"],
                          "-selectforeground", PAL["accent_text"],
                          "-font", ("Segoe UI", 10),
                          "-borderwidth", 0,
                          "-highlightthickness", 0)
        except Exception:
            pass

    # -- main layout ------------------------------------------------------
    def _build_ui(self):
        container = tk.Frame(self.root, bg=PAL["bg"])
        container.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(container, bg=PAL["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        outer = tk.Frame(self._canvas, bg=PAL["bg"])
        self._cw = self._canvas.create_window((0, 0), window=outer, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._cw, width=e.width))
        outer.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self.root.bind_all("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        pad = tk.Frame(outer, bg=PAL["bg"])
        pad.pack(fill="both", expand=True, padx=24, pady=20)

        # -- Header --
        ttk.Label(pad, text="Facial Recognition Security",
                  style="Header.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Configure detection, camera, and security settings",
                  style="Subheader.TLabel").pack(anchor="w", pady=(2, 16))

        # -- Detection card --
        self._build_detection_card(pad)

        # -- Camera card --
        self._build_camera_card(pad)

        # -- People card --
        self._build_people_card(pad)

        # -- Splash card --
        self._build_splash_card(pad)

        # -- Options card --
        self._build_options_card(pad)

        # -- Launch row --
        self._build_launch_row(pad)

    # -- Card builder helpers ---------------------------------------------
    def _make_card(self, parent, title, subtitle=None):
        card = RoundedCard(parent, radius=14)
        card.pack(fill="x", pady=(0, 12))

        title_row = tk.Frame(card.inner, bg=PAL["card"])
        title_row.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(title_row, text=title, bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        if subtitle:
            tk.Label(title_row, text=subtitle, bg=PAL["card"], fg=PAL["text_dim"],
                     font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

        body = tk.Frame(card.inner, bg=PAL["card"])
        body.pack(fill="x", padx=8, pady=(0, 8))

        card.bind("<Configure>", lambda e: card._redraw())
        return card, body

    def _auto_save(self, *_args):
        """Persist current settings to disk whenever any value changes."""
        try:
            settings = self._gather_settings()
            save_settings(settings)
            self.settings = settings
        except Exception:
            pass

    def _make_slider(self, parent, label, key, lo, hi, step, row, tooltip="",
                     fmt="{:.2f}"):
        tk.Label(parent, text=label, bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 10)).grid(row=row, column=0, sticky="w",
                 padx=(0, 8), pady=5)

        var = tk.DoubleVar(value=self.settings[key])
        setattr(self, f"_{key}_var", var)

        val_lbl = tk.Label(parent, text=fmt.format(var.get()), bg=PAL["card"],
                           fg=PAL["accent"], font=("Segoe UI", 10, "bold"), width=5)
        val_lbl.grid(row=row, column=2, padx=(8, 0), pady=5)

        def _on(v):
            val_lbl.configure(text=fmt.format(float(v)))
            self._auto_save()

        s = tk.Scale(parent, from_=lo, to=hi, resolution=step, orient="horizontal",
                     variable=var, command=_on, showvalue=False, length=240,
                     bg=PAL["card"], fg=PAL["text"], troughcolor=PAL["slider_track"],
                     highlightthickness=0, activebackground=PAL["accent"],
                     sliderrelief="flat")
        s.grid(row=row, column=1, sticky="ew", padx=4, pady=5)

        if tooltip:
            lbl_w = parent.grid_slaves(row=row, column=0)
            if lbl_w:
                ToolTip(lbl_w[0], tooltip)
            ToolTip(s, tooltip)

    def _make_toggle_row(self, parent, text, var, row, command=None):
        tk.Label(parent, text=text, bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 10)).grid(row=row, column=0, sticky="w",
                 padx=(0, 12), pady=6)

        def _wrap():
            if command:
                command()
            self._auto_save()

        sw = ToggleSwitch(parent, variable=var, command=_wrap)
        sw.grid(row=row, column=1, sticky="e", pady=6)

    # -- Detection card ---------------------------------------------------
    def _build_detection_card(self, parent):
        _, body = self._make_card(parent, "Detection & Recognition",
                                  "Tune face matching sensitivity")
        body.columnconfigure(1, weight=1)

        self._make_slider(body, "Recognition Threshold", "cosine_threshold",
            0.10, 1.00, 0.01, 0,
            tooltip="How similar a face must be to a database entry to be recognized.\n"
                    "Higher = stricter, fewer false positives.\n"
                    "Lower = looser, more matches but more mistakes.")

        self._make_slider(body, "Bad Splash Threshold", "bad_splash_threshold",
            0.10, 1.00, 0.01, 1,
            tooltip="Minimum confidence to trigger splash + input lock for a bad person.\n"
                    "Below this, the person is treated as Unknown.")

        # Convert stored multiplier to percentage offset for the slider
        # e.g. 1.10 → +10, 0.80 → -20
        size_margin_pct = round((self.settings["size_margin"] - 1.0) * 100)
        self.settings["size_margin"] = size_margin_pct  # temp override for _make_slider

        self._make_slider(body, "Size Advantage", "size_margin",
            -50, 50, 1, 2, fmt="{:+.0f}%",
            tooltip="How much larger a bad face must be vs a good face to trigger.\n"
                    "+ = bad face must be bigger (closer).  − = bad face can be smaller.")

        self._make_slider(body, "Detection Score", "score_threshold",
            0.10, 1.00, 0.05, 3,
            tooltip="YuNet detector confidence threshold.\n"
                    "Higher = only clear faces. Lower = more detections including noise.")

        self._make_slider(body, "NMS Threshold", "nms_threshold",
            0.10, 1.00, 0.05, 4,
            tooltip="Non-Maximum Suppression overlap threshold.\n"
                    "Controls merging of overlapping face detections.")

        self._make_slider(body, "Detection Delay", "detection_delay",
            0.0, 3.0, 0.1, 5, fmt="{:.1f}s",
            tooltip="Seconds of continuous bad-person detection required\n"
                    "before the splash screen triggers.\n"
                    "0 = immediate (no delay).\n"
                    "Increase to avoid false alarms from brief detections.")

        self._make_slider(body, "Idle Skip %", "idle_skip_pct",
            0, 90, 5, 6, fmt="{:.0f}%",
            tooltip="Percentage of frames to skip when no bad person is present.\n"
                    "Scales with FPS cap — e.g. 50% at 30 FPS = 15 detections/sec.\n"
                    "0% = process every frame (highest accuracy).\n"
                    "Higher = lower CPU usage when idle.")

        self._make_slider(body, "Detection Res", "detection_resolution",
            160, 1920, 10, 7, fmt="{:.0f}px",
            tooltip="Maximum dimension (width or height) of the frame\n"
                    "used for face detection. Lower = faster but may\n"
                    "miss small/distant faces. Higher = more accurate\n"
                    "but slower. 640 is a good balance.")

        self._make_slider(body, "Alert Hold", "alert_sustain",
            0.0, 10.0, 0.5, 8, fmt="{:.1f}s",
            tooltip="How long to keep the splash screen and input lock\n"
                    "active after a bad person leaves the frame.\n"
                    "Also maintains full-rate detection during this window.\n"
                    "Prevents the splash from flashing on/off if the\n"
                    "person briefly moves out of view and returns.")

    # -- Camera card ------------------------------------------------------
    def _build_camera_card(self, parent):
        _, body = self._make_card(parent, "Camera", "Select input device and resolution")
        body.columnconfigure(1, weight=1)

        tk.Label(body, text="Device", bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", pady=5)

        self.cam_var = tk.IntVar(value=self.settings["camera_index"])
        self._cam_label_var = tk.StringVar(value="Scanning\u2026")
        self.cam_combo = ttk.Combobox(body, textvariable=self._cam_label_var,
                                      width=34, state="readonly")
        self.cam_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=5)
        self._style_combobox_popdown(self.cam_combo)
        self.cam_combo.bind("<<ComboboxSelected>>", self._on_camera_selected)
        self.cam_combo.bind("<<ComboboxSelected>>", lambda e: self._auto_save(), add="+")

        ttk.Button(body, text="\u21bb  Refresh", style="Ghost.TButton",
                   command=self._scan_cameras).grid(row=0, column=2, padx=(0, 4), pady=5)

        tk.Label(body, text="Resolution", bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", pady=5)
        current_res = f"{self.settings['resolution_w']}\u00d7{self.settings['resolution_h']}"
        self.res_var = tk.StringVar(value=current_res)
        self.res_combo = ttk.Combobox(body, textvariable=self.res_var,
                                      values=[current_res], width=14, state="readonly")
        self.res_combo.grid(row=1, column=1, sticky="w", padx=8, pady=5)
        self._style_combobox_popdown(self.res_combo)
        self.res_combo.bind("<<ComboboxSelected>>", lambda e: self._auto_save())

        # FPS cap slider
        self._make_slider(body, "FPS Cap", "fps_cap",
            0, 60, 1, 2, fmt="{:.0f}",
            tooltip="Maximum frames per second for the recognition loop.\n"
                    "0 = unlimited (as fast as possible).\n"
                    "Lower values reduce CPU / GPU usage.")

        # Camera preview
        preview_frame = tk.Frame(body, bg=PAL["card"])
        preview_frame.grid(row=3, column=0, columnspan=3, sticky="ew",
                           padx=(4, 4), pady=(10, 6))

        # Container for the preview image with a subtle border
        preview_box = tk.Frame(preview_frame, bg=PAL["border"],
                               padx=1, pady=1)
        preview_box.pack(fill="x", padx=4, pady=(0, 6))
        self._preview_label = tk.Label(preview_box, bg="#000000",
                                       text="", anchor="center")
        self._preview_label.pack(fill="both", expand=True)
        # Set a minimum height when no image is shown
        self._preview_label.configure(height=8)

        preview_btn_row = tk.Frame(preview_frame, bg=PAL["card"])
        preview_btn_row.pack(fill="x", padx=4, pady=(0, 2))

        self._preview_running = False
        self._preview_cap = None
        self._preview_after_id = None
        self._preview_photo = None  # prevent GC

        self._preview_toggle_btn = ttk.Button(
            preview_btn_row, text="\u25b6  Preview", style="Ghost.TButton",
            command=self._toggle_camera_preview)
        self._preview_toggle_btn.pack(side="left", padx=(0, 8))

        self._preview_status = tk.Label(preview_btn_row, text="No preview",
                                        bg=PAL["card"], fg=PAL["text_dim"],
                                        font=("Segoe UI", 9))
        self._preview_status.pack(side="left")

        self._scan_cameras()

    # -- People card ------------------------------------------------------
    def _build_people_card(self, parent):
        _, body = self._make_card(parent, "People Database",
                                  "Manage enrolled faces")

        list_frame = tk.Frame(body, bg=PAL["card"])
        list_frame.pack(fill="x", pady=(0, 6))

        self.people_canvas = tk.Canvas(list_frame, bg=PAL["card"],
                                       highlightthickness=0, height=110)
        self.people_inner = tk.Frame(self.people_canvas, bg=PAL["card"])
        self.people_inner.bind("<Configure>",
            lambda e: self.people_canvas.configure(
                scrollregion=self.people_canvas.bbox("all")))
        self.people_canvas.create_window((0, 0), window=self.people_inner, anchor="nw")
        self.people_canvas.pack(fill="x", expand=True)

        btn_row = tk.Frame(body, bg=PAL["card"])
        btn_row.pack(fill="x", pady=(2, 0))

        ttk.Button(btn_row, text="+  New Person", style="Accent.TButton",
                   command=self._create_person).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Add Photos", style="Ghost.TButton",
                   command=self._add_photos).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Quick Capture", style="Ghost.TButton",
                   command=self._enroll_webcam).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="\U0001f4c2  Library", style="Ghost.TButton",
                   command=self._open_library).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Download Models", style="Ghost.TButton",
                   command=self._download_models).pack(side="right")

    # -- Splash card ------------------------------------------------------
    def _build_splash_card(self, parent):
        _, body = self._make_card(parent, "Splash Screen",
                                  "Full-screen alert for bad detections")

        # Preview + info row
        preview_row = tk.Frame(body, bg=PAL["card"])
        preview_row.pack(fill="x", pady=(0, 6))

        self._splash_preview_label = tk.Label(
            preview_row, bg="#000000", relief="flat", borderwidth=0)
        self._splash_preview_label.pack(side="left", padx=(0, 10))
        self._splash_preview_photo = None  # prevent GC

        info_col = tk.Frame(preview_row, bg=PAL["card"])
        info_col.pack(side="left", fill="x", expand=True, anchor="w")

        self.splash_label_var = tk.StringVar(value=self._get_splash_info())
        tk.Label(info_col, textvariable=self.splash_label_var, bg=PAL["card"],
                 fg=PAL["text_dim"], font=("Segoe UI", 9)).pack(anchor="w")

        self._update_splash_preview()

        # Toggle: show splash image on alert
        toggle_row = tk.Frame(body, bg=PAL["card"])
        toggle_row.pack(fill="x", pady=(0, 6))
        toggle_row.columnconfigure(0, weight=1)
        self.show_splash_var = tk.BooleanVar(
            value=self.settings.get("show_splash", True))
        tk.Label(toggle_row, text="Show splash image on alert",
                 bg=PAL["card"], fg=PAL["text"],
                 font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w")
        sw = ToggleSwitch(toggle_row, variable=self.show_splash_var,
                          command=self._auto_save)
        sw.grid(row=0, column=1, sticky="e")
        tk.Label(toggle_row,
                 text="When off, only keyboard & mouse are locked (no overlay)",
                 bg=PAL["card"], fg=PAL["text_dim"],
                 font=("Segoe UI", 8)).grid(row=1, column=0, columnspan=2,
                                            sticky="w", pady=(0, 2))

        btn_row = tk.Frame(body, bg=PAL["card"])
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Change Asset\u2026", style="Accent.TButton",
                   command=self._change_splash).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Clear", style="Ghost.TButton",
                   command=self._clear_splash).pack(side="left")

    # -- Options card -----------------------------------------------------
    def _build_options_card(self, parent):
        _, body = self._make_card(parent, "Options", "System behaviour")
        body.columnconfigure(1, weight=1)

        self.headless_var = tk.BooleanVar(value=self.settings["headless"])
        self._make_toggle_row(body, "Headless mode (no preview window)",
                              self.headless_var, 0)

        self.bg_var = tk.BooleanVar(value=self.settings["run_in_background"])
        self._make_toggle_row(body, "Run in background",
                              self.bg_var, 1)

        self.autostart_var = tk.BooleanVar(value=self.settings["start_on_boot"])
        self._make_toggle_row(body, "Start on Windows boot",
                              self.autostart_var, 2,
                              command=self._on_autostart_toggled)

    # -- Launch row -------------------------------------------------------
    def _build_launch_row(self, parent):
        row = tk.Frame(parent, bg=PAL["bg"])
        row.pack(fill="x", pady=(4, 8))

        self.start_btn = ttk.Button(row, text="\u25b6  Start", style="Start.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(row, text="\u25a0  Stop", style="Stop.TButton",
                                   command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 12))

        self.quit_btn = ttk.Button(row, text="\u2715  Quit", style="Ghost.TButton",
                                   command=self._quit)
        self.quit_btn.pack(side="right")

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(row, textvariable=self.status_var, bg=PAL["bg"],
                 fg=PAL["text_dim"], font=("Segoe UI", 10)).pack(side="left")

    # -- Camera scanning --------------------------------------------------
    def _scan_cameras(self):
        self._cam_label_var.set("Scanning\u2026")
        self.cam_combo.configure(state="disabled")
        self.root.update()
        def _probe():
            cams = probe_cameras()
            self.root.after(0, lambda: self._apply_cameras(cams))
        threading.Thread(target=_probe, daemon=True).start()

    def _apply_cameras(self, cameras):
        self._cameras = cameras
        if not cameras:
            self.cam_combo.configure(values=["No cameras found"], state="readonly")
            self._cam_label_var.set("No cameras found")
            return
        labels = [c["label"] for c in cameras]
        self.cam_combo.configure(values=labels, state="readonly")
        saved_idx = self.settings["camera_index"]
        match = next((i for i, c in enumerate(cameras) if c["index"] == saved_idx), 0)
        self._cam_label_var.set(labels[match])
        self.cam_var.set(cameras[match]["index"])
        self._update_resolutions(cameras[match])

    def _on_camera_selected(self, event=None):
        sel = self.cam_combo.current()
        if sel < 0 or sel >= len(self._cameras):
            return
        cam = self._cameras[sel]
        self.cam_var.set(cam["index"])
        self._update_resolutions(cam)
        # Stop preview when camera changes — user can restart it
        if self._preview_running:
            self._stop_camera_preview()

    def _update_resolutions(self, cam_info):
        res_labels = [f"{w}\u00d7{h}" for w, h in cam_info["resolutions"]]
        if not res_labels:
            res_labels = ["640\u00d7480"]
        self.res_combo.configure(values=res_labels)
        self.res_var.set(res_labels[0])

    # -- Camera preview ---------------------------------------------------
    def _toggle_camera_preview(self):
        if self._preview_running:
            self._stop_camera_preview()
        else:
            self._start_camera_preview()

    def _start_camera_preview(self):
        if self._preview_running:
            return
        cam_idx = self.cam_var.get()
        self._preview_status.configure(text="Opening camera\u2026", fg=PAL["text_dim"])
        self.root.update()

        def _open():
            cap = None
            try:
                # Try MSMF first, then DSHOW, then auto
                for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW):
                    try:
                        cap = cv2.VideoCapture(cam_idx, backend)
                        if cap.isOpened():
                            break
                        cap.release()
                        cap = None
                    except Exception:
                        cap = None
                if cap is None or not cap.isOpened():
                    try:
                        cap = cv2.VideoCapture(cam_idx)
                    except Exception:
                        cap = None

                if cap is not None and cap.isOpened():
                    # Use a small resolution for preview
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    # Try to grab a test frame to confirm it works
                    ret, _ = cap.read()
                    if ret:
                        self.root.after(0, lambda: self._preview_opened(cap))
                    else:
                        cap.release()
                        self.root.after(0, lambda: self._preview_error(
                            "Camera opened but no frames received"))
                else:
                    if cap is not None:
                        cap.release()
                    self.root.after(0, lambda: self._preview_error(
                        "Camera unavailable"))
            except Exception as e:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                self.root.after(0, lambda: self._preview_error(str(e)))

        threading.Thread(target=_open, daemon=True).start()

    def _preview_opened(self, cap):
        self._preview_cap = cap
        self._preview_running = True
        self._preview_toggle_btn.configure(text="\u25a0  Stop Preview")
        self._preview_status.configure(text="Live", fg=PAL["green"])
        self._preview_tick()

    def _preview_error(self, msg):
        self._preview_status.configure(
            text=f"\u26a0  {msg}", fg=PAL["red"])
        self._preview_running = False
        self._preview_toggle_btn.configure(text="\u25b6  Preview")

    def _preview_tick(self):
        if not self._preview_running or self._preview_cap is None:
            return
        try:
            ret, frame = self._preview_cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                # Scale to fit the card width (use the label's current width)
                label_w = self._preview_label.winfo_width()
                if label_w < 100:
                    label_w = 400  # sensible default before first layout
                h, w = frame.shape[:2]
                scale = min(label_w / w, 300 / h)  # max 300px tall
                new_w, new_h = int(w * scale), int(h * scale)
                frame = cv2.resize(frame, (new_w, new_h),
                                   interpolation=cv2.INTER_AREA)
                img = Image.fromarray(frame)
                photo = ImageTk.PhotoImage(image=img)
                self._preview_photo = photo  # prevent GC
                self._preview_label.configure(image=photo, height=new_h)
            else:
                self._preview_status.configure(
                    text="\u26a0  Frame read failed", fg=PAL["red"])
        except Exception:
            self._preview_status.configure(
                text="\u26a0  Preview error", fg=PAL["red"])
        self._preview_after_id = self.root.after(66, self._preview_tick)  # ~15fps

    def _stop_camera_preview(self):
        self._preview_running = False
        if self._preview_after_id is not None:
            self.root.after_cancel(self._preview_after_id)
            self._preview_after_id = None
        if self._preview_cap is not None:
            try:
                self._preview_cap.release()
            except Exception:
                pass
            self._preview_cap = None
        self._preview_photo = None
        self._preview_label.configure(image="", height=8)
        self._preview_status.configure(text="No preview", fg=PAL["text_dim"])
        self._preview_toggle_btn.configure(text="\u25b6  Preview")

    # -- People list ------------------------------------------------------
    def _refresh_people(self):
        for w in self.people_inner.winfo_children():
            w.destroy()
        people = get_people()
        if not people["good"] and not people["bad"]:
            tk.Label(self.people_inner, text="No people enrolled yet",
                     bg=PAL["card"], fg=PAL["text_dim"],
                     font=("Segoe UI", 9, "italic")).pack(anchor="w", padx=4, pady=6)
            return
        for cat in ("good", "bad"):
            for name in people[cat]:
                n = count_images(cat, name)
                rf = tk.Frame(self.people_inner, bg=PAL["card"])
                rf.pack(fill="x", pady=2)
                clr = PAL["green"] if cat == "good" else PAL["red"]
                tk.Label(rf, text=f"\u25cf", bg=PAL["card"], fg=clr,
                         font=("Segoe UI", 10)).pack(side="left", padx=(2, 6))
                tk.Label(rf, text=name, bg=PAL["card"], fg=PAL["text"],
                         font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))
                tk.Label(rf, text=f"{n} photo{'s' if n != 1 else ''}",
                         bg=PAL["card"], fg=PAL["text_dim"],
                         font=("Segoe UI", 9)).pack(side="left")
                tk.Button(rf, text="\u2715", width=2, font=("Segoe UI", 8),
                          bg=PAL["card"], fg=PAL["red"], relief="flat",
                          activebackground=PAL["card_hover"], cursor="hand2",
                          command=lambda c=cat, nm=name: self._delete_person(c, nm)
                          ).pack(side="right", padx=4)

    # -- Actions ----------------------------------------------------------
    def _create_person(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("New Person")
        dlg.configure(bg=PAL["bg"])
        dlg.resizable(False, False)
        dlg.geometry("360x200")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.update_idletasks()
        dlg.geometry(f"+{self.root.winfo_x()+(self.root.winfo_width()-360)//2}"
                     f"+{self.root.winfo_y()+(self.root.winfo_height()-200)//2}")

        tk.Label(dlg, text="Name", bg=PAL["bg"], fg=PAL["text"],
                 font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(18, 4))
        name_var = tk.StringVar()
        e = tk.Entry(dlg, textvariable=name_var, font=("Segoe UI", 11),
                     bg=PAL["input_bg"], fg=PAL["text"], insertbackground=PAL["text"],
                     relief="flat", highlightthickness=1,
                     highlightcolor=PAL["accent"], highlightbackground=PAL["input_border"])
        e.pack(fill="x", padx=24)
        e.focus_set()

        tk.Label(dlg, text="Category", bg=PAL["bg"], fg=PAL["text"],
                 font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(10, 4))
        cat_var = tk.StringVar(value="good")
        cf = tk.Frame(dlg, bg=PAL["bg"])
        cf.pack(anchor="w", padx=24)
        for v, l in [("good", "Good"), ("bad", "Bad")]:
            tk.Radiobutton(cf, text=l, variable=cat_var, value=v,
                           font=("Segoe UI", 10), bg=PAL["bg"], fg=PAL["text"],
                           selectcolor=PAL["input_bg"], activebackground=PAL["bg"],
                           activeforeground=PAL["text"]).pack(side="left", padx=(0, 16))

        def _go(event=None):
            n = name_var.get().strip()
            if not n:
                return
            d = os.path.join(FACES_DB_DIR, cat_var.get(), n)
            os.makedirs(d, exist_ok=True)
            dlg.destroy()
            self._refresh_people()
        e.bind("<Return>", _go)
        ttk.Button(dlg, text="Create", style="Accent.TButton",
                   command=_go).pack(pady=(12, 0))

    def _add_photos(self):
        people = get_people()
        all_p = [(c, n) for c in ("good", "bad") for n in people[c]]
        if not all_p:
            messagebox.showinfo("No People", "Create a person first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Add Photos")
        dlg.configure(bg=PAL["bg"])
        dlg.resizable(False, False)
        dlg.geometry("360x170")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.update_idletasks()
        dlg.geometry(f"+{self.root.winfo_x()+(self.root.winfo_width()-360)//2}"
                     f"+{self.root.winfo_y()+(self.root.winfo_height()-170)//2}")
        labels = [f"{'Good' if c == 'good' else 'Bad'}  \u2014  {n}" for c, n in all_p]
        sel = tk.StringVar(value=labels[0])
        tk.Label(dlg, text="Select person", bg=PAL["bg"], fg=PAL["text"],
                 font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(18, 4))
        cb = ttk.Combobox(dlg, textvariable=sel, values=labels,
                          state="readonly", width=32)
        cb.pack(padx=24)
        self._style_combobox_popdown(cb)
        def _pick():
            idx = labels.index(sel.get()) if sel.get() in labels else -1
            if idx < 0:
                return
            cat, name = all_p[idx]
            dlg.destroy()
            files = filedialog.askopenfilenames(title=f"Photos for {name}",
                filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp")])
            if not files:
                return
            dest = os.path.join(FACES_DB_DIR, cat, name)
            ct = 0
            for fp in files:
                try:
                    shutil.copy2(fp, os.path.join(dest, os.path.basename(fp)))
                    ct += 1
                except Exception:
                    pass
            self._refresh_people()
            messagebox.showinfo("Done", f"Added {ct} photo(s).")
        ttk.Button(dlg, text="Select Photos\u2026", style="Accent.TButton",
                   command=_pick).pack(pady=(14, 0))

    def _enroll_webcam(self):
        people = get_people()
        all_p = [(c, n) for c in ("good", "bad") for n in people[c]]
        if not all_p:
            messagebox.showinfo("No People", "Create a person first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Quick Capture")
        dlg.configure(bg=PAL["bg"])
        dlg.resizable(False, False)
        dlg.geometry("360x220")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.update_idletasks()
        dlg.geometry(f"+{self.root.winfo_x()+(self.root.winfo_width()-360)//2}"
                     f"+{self.root.winfo_y()+(self.root.winfo_height()-220)//2}")
        labels = [f"{'Good' if c == 'good' else 'Bad'}  \u2014  {n}" for c, n in all_p]
        sel = tk.StringVar(value=labels[0])
        tk.Label(dlg, text="Person", bg=PAL["bg"], fg=PAL["text"],
                 font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(18, 4))
        cb2 = ttk.Combobox(dlg, textvariable=sel, values=labels,
                           state="readonly", width=32)
        cb2.pack(padx=24)
        self._style_combobox_popdown(cb2)
        tk.Label(dlg, text="Shots", bg=PAL["bg"], fg=PAL["text"],
                 font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(10, 4))
        shots = tk.IntVar(value=5)
        tk.Spinbox(dlg, from_=1, to=50, width=5, textvariable=shots,
                   font=("Segoe UI", 10), bg=PAL["input_bg"], fg=PAL["text"],
                   insertbackground=PAL["text"], relief="flat").pack(anchor="w", padx=24)
        def _go():
            idx = labels.index(sel.get()) if sel.get() in labels else -1
            if idx < 0:
                return
            cat, name = all_p[idx]
            dlg.destroy()
            # Stop camera preview if running (need exclusive camera access)
            if self._preview_running:
                self._stop_camera_preview()
            self._open_capture_window(name, cat, shots.get())
        ttk.Button(dlg, text="Start Enrollment", style="Accent.TButton",
                   command=_go).pack(pady=(14, 0))

    def _open_capture_window(self, name, category, num_shots):
        """Open a tkinter-based capture window with live preview, Capture
        and Stop buttons, instead of the raw OpenCV enrollment window."""
        import main as m

        person_dir = os.path.join(FACES_DB_DIR, category, name)
        os.makedirs(person_dir, exist_ok=True)

        cam_idx = self.cam_var.get()
        cap = None

        # Try to open camera
        for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW):
            try:
                cap = cv2.VideoCapture(cam_idx, backend)
                if cap.isOpened():
                    break
                cap.release()
                cap = None
            except Exception:
                cap = None
        if cap is None or not cap.isOpened():
            try:
                cap = cv2.VideoCapture(cam_idx)
            except Exception:
                cap = None
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            messagebox.showerror("Camera Error",
                                 f"Could not open camera {cam_idx}.")
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        detector = m.build_detector()

        # -- Build capture window --
        win = tk.Toplevel(self.root)
        win.title(f"Quick Capture \u2014 {name}")
        win.configure(bg=PAL["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: _stop())

        # Camera feed
        feed_frame = tk.Frame(win, bg=PAL["border"], padx=1, pady=1)
        feed_frame.pack(padx=16, pady=(16, 8))
        feed_label = tk.Label(feed_frame, bg="#000000")
        feed_label.pack()

        # Status line
        saved_count = [0]
        status_var = tk.StringVar(
            value=f"Captured: 0 / {num_shots}  \u2014  Position your face and click Capture")
        tk.Label(win, textvariable=status_var, bg=PAL["bg"], fg=PAL["text"],
                 font=("Segoe UI", 10)).pack(pady=(4, 8))

        # Buttons
        btn_row = tk.Frame(win, bg=PAL["bg"])
        btn_row.pack(pady=(0, 16))

        face_detected = [False]
        last_frame = [None]
        last_faces = [None]
        running = [True]
        after_id = [None]
        photo_ref = [None]  # prevent GC

        def _capture():
            if last_frame[0] is None or last_faces[0] is None:
                return
            if not face_detected[0]:
                return
            fname = os.path.join(person_dir,
                                 f"{saved_count[0]:03d}.jpg")
            cv2.imwrite(fname, last_frame[0])
            saved_count[0] += 1
            status_var.set(
                f"Captured: {saved_count[0]} / {num_shots}  \u2014  Saved!")
            if saved_count[0] >= num_shots:
                _finish()

        def _stop():
            running[0] = False
            if after_id[0] is not None:
                win.after_cancel(after_id[0])
                after_id[0] = None
            try:
                cap.release()
            except Exception:
                pass
            win.destroy()
            self._refresh_people()
            total = saved_count[0]
            if total > 0:
                self.status_var.set("Ready")
                messagebox.showinfo("Done",
                                    f"Captured {total} photo(s) for '{name}'.")
            else:
                self.status_var.set("Ready")

        def _finish():
            running[0] = False
            if after_id[0] is not None:
                win.after_cancel(after_id[0])
                after_id[0] = None
            try:
                cap.release()
            except Exception:
                pass
            win.destroy()
            self._refresh_people()
            self.status_var.set("Ready")
            messagebox.showinfo("Done",
                                f"Captured {saved_count[0]} photo(s) for '{name}'.")

        capture_btn = ttk.Button(btn_row, text="\U0001f4f7  Capture",
                                 style="Accent.TButton", command=_capture)
        capture_btn.pack(side="left", padx=(0, 12))

        stop_btn = ttk.Button(btn_row, text="\u25a0  Stop",
                              style="Stop.TButton", command=_stop)
        stop_btn.pack(side="left")

        def _tick():
            if not running[0]:
                return
            try:
                ret, frame = cap.read()
                if ret:
                    last_frame[0] = frame.copy()
                    display = frame.copy()
                    h_f, w_f = frame.shape[:2]
                    detector.setInputSize((w_f, h_f))
                    _, faces = detector.detect(frame)
                    last_faces[0] = faces

                    if faces is not None and len(faces) > 0:
                        face_detected[0] = True
                        f = faces[0]
                        cv2.rectangle(
                            display,
                            (int(f[0]), int(f[1])),
                            (int(f[0] + f[2]), int(f[1] + f[3])),
                            (0, 255, 0), 2)
                        # Show counter on frame
                        cv2.putText(
                            display,
                            f"{saved_count[0]}/{num_shots}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (0, 255, 0), 2, cv2.LINE_AA)
                        if saved_count[0] < num_shots:
                            status_var.set(
                                f"Captured: {saved_count[0]} / {num_shots}"
                                f"  \u2014  Face detected, click Capture")
                    else:
                        face_detected[0] = False
                        status_var.set(
                            f"Captured: {saved_count[0]} / {num_shots}"
                            f"  \u2014  No face detected")

                    # Convert for tkinter display
                    rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
                    # Scale to fit (max 480px wide)
                    h, w = rgb.shape[:2]
                    scale = min(480 / w, 360 / h)
                    new_w, new_h = int(w * scale), int(h * scale)
                    rgb = cv2.resize(rgb, (new_w, new_h),
                                     interpolation=cv2.INTER_AREA)
                    img = Image.fromarray(rgb)
                    photo = ImageTk.PhotoImage(image=img)
                    photo_ref[0] = photo
                    feed_label.configure(image=photo)
            except Exception:
                pass
            after_id[0] = win.after(33, _tick)  # ~30fps

        # Center the window on screen
        win.update_idletasks()
        ww = win.winfo_reqwidth()
        wh = win.winfo_reqheight()
        sx = win.winfo_screenwidth()
        sy = win.winfo_screenheight()
        win.geometry(f"+{(sx - ww) // 2}+{(sy - wh) // 2}")

        self.status_var.set(f"Enrolling {name}\u2026")
        _tick()

    def _delete_person(self, category, name):
        if not messagebox.askyesno("Delete", f"Delete '{name}' ({category})?"):
            return
        try:
            shutil.rmtree(os.path.join(FACES_DB_DIR, category, name))
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self._refresh_people()

    def _open_library(self):
        """Open the faces database folder in Windows Explorer."""
        path = FACES_DB_DIR
        os.makedirs(path, exist_ok=True)
        try:
            subprocess.Popen(["explorer", os.path.normpath(path)])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder:\n{e}")

    def _download_models(self):
        self.status_var.set("Downloading models\u2026")
        self.root.update()
        def _dl():
            from main import download_models
            download_models()
            self.root.after(0, lambda: self.status_var.set("Models ready"))
            self.root.after(0, lambda: messagebox.showinfo("Done", "Models downloaded."))
        threading.Thread(target=_dl, daemon=True).start()

    # -- Autostart --------------------------------------------------------
    def _on_autostart_toggled(self):
        _set_autostart(self.autostart_var.get())

    # -- Splash -----------------------------------------------------------
    def _get_splash_info(self):
        d = os.path.join(SCRIPT_DIR, "splash_assets")
        if not os.path.isdir(d):
            return "No splash asset"
        exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
                ".mp4", ".avi", ".mkv", ".mov", ".webm"}
        for f in sorted(os.listdir(d)):
            if os.path.splitext(f)[1].lower() in exts:
                kb = os.path.getsize(os.path.join(d, f)) / 1024
                return f"{f}  ({kb:.0f} KB)"
        return "No splash asset"

    def _change_splash(self):
        fp = filedialog.askopenfilename(title="Select splash asset",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp"),
                       ("GIFs", "*.gif"), ("Videos", "*.mp4 *.avi *.mkv *.mov *.webm")])
        if not fp:
            return
        d = os.path.join(SCRIPT_DIR, "splash_assets")
        os.makedirs(d, exist_ok=True)
        exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
                ".mp4", ".avi", ".mkv", ".mov", ".webm"}
        for f in os.listdir(d):
            if os.path.splitext(f)[1].lower() in exts:
                try:
                    os.remove(os.path.join(d, f))
                except Exception:
                    pass
        try:
            shutil.copy2(fp, os.path.join(d, os.path.basename(fp)))
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.splash_label_var.set(self._get_splash_info())
        self._update_splash_preview()

    def _clear_splash(self):
        d = os.path.join(SCRIPT_DIR, "splash_assets")
        if not os.path.isdir(d):
            return
        exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
                ".mp4", ".avi", ".mkv", ".mov", ".webm"}
        for f in os.listdir(d):
            if os.path.splitext(f)[1].lower() in exts:
                try:
                    os.remove(os.path.join(d, f))
                except Exception:
                    pass
        self.splash_label_var.set(self._get_splash_info())
        self._update_splash_preview()

    def _update_splash_preview(self):
        """Load the current splash asset and show a thumbnail preview."""
        self._splash_preview_photo = None
        self._splash_preview_label.configure(image="", width=0, height=0)
        d = os.path.join(SCRIPT_DIR, "splash_assets")
        if not os.path.isdir(d):
            return
        img_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
        vid_exts = {".mp4", ".avi", ".mkv", ".mov", ".webm"}
        for f in sorted(os.listdir(d)):
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(d, f)
            if ext in img_exts:
                try:
                    img = Image.open(full)
                    img.thumbnail((160, 120), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    self._splash_preview_photo = photo
                    self._splash_preview_label.configure(
                        image=photo, width=photo.width(), height=photo.height())
                except Exception:
                    pass
                return
            elif ext in vid_exts:
                try:
                    cap = cv2.VideoCapture(full)
                    ret, frame = cap.read()
                    cap.release()
                    if ret:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        img = Image.fromarray(frame)
                        img.thumbnail((160, 120), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img)
                        self._splash_preview_photo = photo
                        self._splash_preview_label.configure(
                            image=photo, width=photo.width(), height=photo.height())
                except Exception:
                    pass
                return

    # -- Launch / Stop ----------------------------------------------------
    def _gather_settings(self):
        res = self.res_var.get().replace("\u00d7", "x").split("x")
        return {
            "camera_index": self.cam_var.get(),
            "resolution_w": int(res[0]),
            "resolution_h": int(res[1]),
            "fps_cap": int(self._fps_cap_var.get()),
            "detection_delay": round(self._detection_delay_var.get(), 1),
            "idle_skip_pct": int(self._idle_skip_pct_var.get()),
            "alert_sustain": round(self._alert_sustain_var.get(), 1),
            "detection_resolution": int(self._detection_resolution_var.get()),
            "cosine_threshold": round(self._cosine_threshold_var.get(), 2),
            "bad_splash_threshold": round(self._bad_splash_threshold_var.get(), 2),
            "size_margin": round(1.0 + self._size_margin_var.get() / 100.0, 2),
            "score_threshold": round(self._score_threshold_var.get(), 2),
            "nms_threshold": round(self._nms_threshold_var.get(), 2),
            "show_splash": self.show_splash_var.get(),
            "headless": self.headless_var.get(),
            "run_in_background": self.bg_var.get(),
            "start_on_boot": self.autostart_var.get(),
        }

    def _start(self):
        if self._running:
            return

        # Stop camera preview — recognition needs exclusive camera access
        if self._preview_running:
            self._stop_camera_preview()

        import main as m
        if m._is_already_running():
            messagebox.showwarning("Already Running",
                "Another instance is already using the camera.\n"
                "Stop it first or delete .camera.lock if stale.")
            return

        settings = self._gather_settings()
        save_settings(settings)
        self.settings = settings

        m.COSINE_THRESHOLD = settings["cosine_threshold"]
        m.BAD_SPLASH_THRESHOLD = settings["bad_splash_threshold"]
        m.SIZE_MARGIN = settings["size_margin"]
        m.SCORE_THRESHOLD = settings["score_threshold"]
        m.NMS_THRESHOLD = settings["nms_threshold"]

        self._running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running\u2026")

        if settings["run_in_background"]:
            self.root.withdraw()
            # Hide the console window too
            try:
                ctypes.windll.user32.ShowWindow(
                    ctypes.windll.kernel32.GetConsoleWindow(), 0)  # SW_HIDE
            except Exception:
                pass

        def _run():
            try:
                m.run(camera_id=settings["camera_index"],
                      width=settings["resolution_w"],
                      height=settings["resolution_h"],
                      headless=settings["headless"],
                      fps_cap=settings.get("fps_cap", 30),
                      detection_delay=settings.get("detection_delay", 0.0),
                      idle_skip_pct=settings.get("idle_skip_pct", 0),
                      alert_sustain=settings.get("alert_sustain", 3.0),
                      detection_resolution=settings.get("detection_resolution", 640),
                      show_splash=settings.get("show_splash", True))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                self.root.after(0, self._on_stopped)

        threading.Thread(target=_run, daemon=True).start()

    def _stop(self):
        if not self._running:
            return
        import main as m
        m._stop_requested = True
        self.status_var.set("Stopping\u2026")

    def _on_stopped(self):
        import main as m
        m._stop_requested = False
        self._running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopped")
        self.root.deiconify()
        self.root.lift()
        # Restore the console window
        try:
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 5)  # SW_SHOW
        except Exception:
            pass

    # -- Close / Quit -----------------------------------------------------
    def _on_close(self):
        """Hide the window instead of quitting — recognition keeps running."""
        if self._preview_running:
            self._stop_camera_preview()
        self.root.withdraw()

    def _quit(self):
        """Fully stop recognition (if running) and exit the application."""
        if self._preview_running:
            self._stop_camera_preview()
        if self._running:
            import main as m
            m._stop_requested = True
            self.status_var.set("Quitting\u2026")
            # Wait for the run thread to finish, then destroy
            def _wait_and_quit():
                # Poll until stopped
                if self._running:
                    self.root.after(100, _wait_and_quit)
                else:
                    self.root.destroy()
            _wait_and_quit()
        else:
            self.root.destroy()

    # -- IPC server -------------------------------------------------------
    def _start_ipc_server(self):
        """Listen on a localhost port so a second launch can signal 'show'."""
        def _serve():
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((_IPC_HOST, _IPC_PORT))
                srv.settimeout(1.0)
                srv.listen(1)
            except OSError:
                return  # port in use — should not happen if we're the first
            while True:
                try:
                    conn, _ = srv.accept()
                    data = conn.recv(64)
                    conn.close()
                    if data == b"show":
                        self.root.after(0, self._reshow)
                except socket.timeout:
                    # Check if root still exists
                    try:
                        self.root.winfo_exists()
                    except Exception:
                        break
                except OSError:
                    break
            try:
                srv.close()
            except Exception:
                pass
        threading.Thread(target=_serve, daemon=True).start()

    def _reshow(self):
        """Bring the GUI window back to the foreground."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        # Also bring to front via Win32 for reliability
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            ctypes.windll.user32.SetForegroundWindow(
                self.root.winfo_id())
        except Exception:
            pass

    # -- Run --------------------------------------------------------------
    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Single-instance check (signal existing GUI to reshow)
# ---------------------------------------------------------------------------

def _signal_existing_instance():
    """Try to connect to an existing instance and tell it to show.
    Returns True if an existing instance was found and signalled."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect((_IPC_HOST, _IPC_PORT))
        s.sendall(b"show")
        s.close()
        return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def launch_gui():
    # If another instance is already running, just reshow it and exit
    if _signal_existing_instance():
        print("[INFO] Existing instance found — bringing GUI to front.")
        return
    gui = SettingsGUI()
    gui.run()


if __name__ == "__main__":
    launch_gui()
