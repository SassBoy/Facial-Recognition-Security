"""
Splash-screen player – displays full-screen media on ALL monitors when
triggered by a bad-person detection.

Supported media in  splash_assets/ :
  - Static images : .jpg .jpeg .png .bmp .webp
  - Animated GIFs : .gif  (loops until dismissed)
  - Videos        : .mp4 .avi .mkv .mov .webm  (loops until dismissed)

The splash fades in, then stays visible until dismiss() is called
(typically when the person leaves the camera frame or ESC is pressed).
"""

import os
import time
import threading
import cv2
import numpy as np

try:
    from screeninfo import get_monitors
except ImportError:
    get_monitors = None

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SPLASH_DIR = os.path.join(SCRIPT_DIR, "splash_assets")

# Defaults
FADE_DURATION = 0.4        # seconds
FADE_STEPS    = 15         # number of opacity steps during fade

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
GIF_EXTS   = {".gif"}
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_all_monitors():
    """Return list of (x, y, width, height) for every monitor."""
    if get_monitors is None:
        return [(0, 0, 1920, 1080)]
    monitors = []
    for m in get_monitors():
        monitors.append((m.x, m.y, m.width, m.height))
    return monitors


def _find_splash_asset():
    """
    Look for the first supported media file in splash_assets/.
    Returns (file_path, media_type) or (None, None).
    """
    if not os.path.isdir(SPLASH_DIR):
        return None, None

    for fname in sorted(os.listdir(SPLASH_DIR)):
        fpath = os.path.join(SPLASH_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext in IMAGE_EXTS:
            return fpath, "image"
        if ext in GIF_EXTS:
            return fpath, "gif"
        if ext in VIDEO_EXTS:
            return fpath, "video"

    return None, None


def _load_gif_frames(gif_path):
    """Load all frames of a GIF as a list of (BGR numpy array, duration_ms)."""
    if PILImage is None:
        print("[WARN] Pillow not installed - cannot decode GIFs.")
        return []

    pil = PILImage.open(gif_path)
    frames = []
    try:
        while True:
            duration = pil.info.get("duration", 100)
            rgba = pil.convert("RGBA")
            arr = np.array(rgba)
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
            frames.append((bgr, duration))
            pil.seek(pil.tell() + 1)
    except EOFError:
        pass
    return frames


def _resize_to_monitor(img, mon_w, mon_h):
    """Resize image to fill the monitor, keeping aspect ratio (center crop)."""
    ih, iw = img.shape[:2]
    scale = max(mon_w / iw, mon_h / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    x0 = (new_w - mon_w) // 2
    y0 = (new_h - mon_h) // 2
    return resized[y0:y0 + mon_h, x0:x0 + mon_w]


def _apply_alpha(frame, alpha):
    """Blend frame with black at given alpha (0.0 = black, 1.0 = full)."""
    return cv2.convertScaleAbs(frame, alpha=alpha, beta=0)


# ---------------------------------------------------------------------------
# Splash player
# ---------------------------------------------------------------------------
class SplashPlayer:
    """
    Manages full-screen splash windows on all monitors.
    The splash stays visible until dismiss() is called externally.
    Thread-safe.
    """

    def __init__(self, fade_duration=FADE_DURATION):
        self.fade_duration = fade_duration
        self._active = False
        self._dismiss_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def is_active(self):
        return self._active

    def trigger(self):
        """
        Start the splash screen if an asset exists.
        Non-blocking (launches a thread).
        Returns True if splash was started.
        """
        with self._lock:
            if self._active:
                return False

            asset_path, media_type = _find_splash_asset()
            if asset_path is None:
                return False

            self._active = True
            self._dismiss_event.clear()

        t = threading.Thread(target=self._play,
                             args=(asset_path, media_type),
                             daemon=True)
        t.start()
        return True

    def dismiss(self):
        """Signal the splash to fade out and close."""
        self._dismiss_event.set()

    def _should_stop(self):
        """Check if we should stop playback."""
        return self._dismiss_event.is_set()

    # ---- internal playback ------------------------------------------------

    def _play(self, asset_path, media_type):
        """Play the splash on every monitor (runs in a worker thread)."""
        win_names = []
        try:
            monitors = _get_all_monitors()

            for i, (mx, my, mw, mh) in enumerate(monitors):
                wname = f"_splash_{i}"
                win_names.append((wname, mx, my, mw, mh))
                cv2.namedWindow(wname, cv2.WINDOW_NORMAL)
                cv2.setWindowProperty(wname, cv2.WND_PROP_FULLSCREEN,
                                      cv2.WINDOW_FULLSCREEN)
                cv2.moveWindow(wname, mx, my)
                cv2.resizeWindow(wname, mw, mh)
                cv2.imshow(wname, np.zeros((mh, mw, 3), dtype=np.uint8))

            cv2.waitKey(1)

            if media_type == "image":
                self._play_image(asset_path, win_names)
            elif media_type == "gif":
                self._play_gif(asset_path, win_names)
            elif media_type == "video":
                self._play_video(asset_path, win_names)

        except Exception as e:
            print(f"[SPLASH] Error: {e}")
        finally:
            for wname, *_ in win_names:
                try:
                    cv2.destroyWindow(wname)
                except Exception:
                    pass
            cv2.waitKey(1)
            with self._lock:
                self._active = False
                self._dismiss_event.clear()

    # -- Fade helpers -------------------------------------------------------

    def _fade_in(self, frame_per_monitor, win_names):
        """Fade from black to full brightness. Returns False if dismissed."""
        steps = FADE_STEPS
        dt = self.fade_duration / steps
        for s in range(1, steps + 1):
            if self._should_stop():
                return False
            alpha = s / steps
            for (wname, mx, my, mw, mh), frame in zip(win_names,
                                                        frame_per_monitor):
                cv2.imshow(wname, _apply_alpha(frame, alpha))
            cv2.waitKey(max(1, int(dt * 1000)))
        return True

    def _fade_out(self, frame_per_monitor, win_names):
        """Fade from full brightness to black."""
        steps = FADE_STEPS
        dt = self.fade_duration / steps
        for s in range(steps - 1, -1, -1):
            alpha = s / steps
            for (wname, mx, my, mw, mh), frame in zip(win_names,
                                                        frame_per_monitor):
                cv2.imshow(wname, _apply_alpha(frame, alpha))
            cv2.waitKey(max(1, int(dt * 1000)))

    def _show_frame_all(self, frame_per_monitor, win_names):
        """Show a frame on every monitor at full brightness."""
        for (wname, mx, my, mw, mh), frame in zip(win_names,
                                                    frame_per_monitor):
            cv2.imshow(wname, frame)

    def _prepare_frame(self, raw_frame, win_names):
        """Resize one source frame to each monitor's resolution."""
        frames = []
        for wname, mx, my, mw, mh in win_names:
            frames.append(_resize_to_monitor(raw_frame, mw, mh))
        return frames

    # -- Media-type players -------------------------------------------------

    def _play_image(self, path, win_names):
        """Static image: fade in, hold until dismissed, close instantly."""
        img = cv2.imread(path)
        if img is None:
            print(f"[SPLASH] Could not read image: {path}")
            return
        frames = self._prepare_frame(img, win_names)
        if not self._fade_in(frames, win_names):
            return

        # Hold until dismissed
        self._show_frame_all(frames, win_names)
        while not self._should_stop():
            cv2.waitKey(50)

    def _play_gif(self, path, win_names):
        """Animated GIF: fade in, loop until dismissed, close instantly."""
        gif_frames = _load_gif_frames(path)
        if not gif_frames:
            self._play_image(path, win_names)
            return

        first_resized = self._prepare_frame(gif_frames[0][0], win_names)
        if not self._fade_in(first_resized, win_names):
            return

        # Loop until dismissed
        while not self._should_stop():
            for raw, dur in gif_frames:
                if self._should_stop():
                    break
                resized = self._prepare_frame(raw, win_names)
                self._show_frame_all(resized, win_names)
                cv2.waitKey(max(1, dur))

    def _play_video(self, path, win_names):
        """Video file: fade in, loop until dismissed, close instantly."""
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            print(f"[SPLASH] Could not open video: {path}")
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        delay = max(1, int(1000 / fps))

        # Read first frame for fade-in
        ret, first = cap.read()
        if not ret:
            cap.release()
            return

        first_resized = self._prepare_frame(first, win_names)
        if not self._fade_in(first_resized, win_names):
            cap.release()
            return

        self._show_frame_all(first_resized, win_names)
        cv2.waitKey(delay)

        # Loop until dismissed
        while not self._should_stop():
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            resized = self._prepare_frame(frame, win_names)
            self._show_frame_all(resized, win_names)
            cv2.waitKey(delay)

        cap.release()
