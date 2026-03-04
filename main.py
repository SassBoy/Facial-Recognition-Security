"""
Facial Recognition Security System
YuNet (detection) + SFace (recognition) pipeline.

Database:  faces_db/good/<Name>/  and  faces_db/bad/<Name>/
Splash:    splash_assets/  (image, gif, or video)
"""

import os
import sys
import time
import atexit
import signal
import argparse
import ctypes
import numpy as np
import cv2

# Write any unhandled exception (including import errors below) to crash.log
# so the error is captured even when the process exits without a visible console.
def _crash_hook(exc_type, exc_val, exc_tb):
    import traceback
    _log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")
    with open(_log, "w") as _f:
        traceback.print_exception(exc_type, exc_val, exc_tb, file=_f)
    sys.__excepthook__(exc_type, exc_val, exc_tb)
sys.excepthook = _crash_hook


from splash import SplashPlayer, SPLASH_DIR
from input_locker import InputLocker
from camera_enum import enumerate_cameras, get_camera_name
from app_config import APP_VERSION

# ---------------------------------------------------------------------------
# Paths & tunables
# ---------------------------------------------------------------------------

SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
YUNET_MODEL        = os.path.join(SCRIPT_DIR, "models", "face_detection_yunet_2023mar.onnx")
SFACE_MODEL        = os.path.join(SCRIPT_DIR, "models", "face_recognition_sface_2021dec.onnx")
FACES_DB_DIR       = os.path.join(SCRIPT_DIR, "faces_db")

COSINE_THRESHOLD     = 0.50
BAD_SPLASH_THRESHOLD = 0.45
SIZE_MARGIN          = 1.10
SCORE_THRESHOLD      = 0.70
NMS_THRESHOLD        = 0.30

MAX_FRAME_RETRIES  = 15
IMAGE_EXTS         = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
WINDOW_NAME        = f"Facial Recognition Security  v{APP_VERSION}"

_stop_requested    = False
_active_cap        = None          # tracked for emergency cleanup
_LOCK_FILE         = os.path.join(SCRIPT_DIR, ".camera.lock")


# ---------------------------------------------------------------------------
# Win32 icon helper for OpenCV windows
# ---------------------------------------------------------------------------

def _set_cv2_window_icon(window_name: str, ico_path: str):
    """Set a custom .ico on an OpenCV HighGUI window (Windows only)."""
    if sys.platform != "win32" or not os.path.isfile(ico_path):
        return
    try:
        FindWindowW = ctypes.windll.user32.FindWindowW
        SendMessageW = ctypes.windll.user32.SendMessageW
        LoadImageW = ctypes.windll.user32.LoadImageW

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        hwnd = FindWindowW(None, window_name)
        if not hwnd:
            return

        # Load small (16x16) and large (32x32) icons
        ico_small = LoadImageW(
            None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
        )
        ico_big = LoadImageW(
            None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
        )
        if ico_small:
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL, ico_small)
        if ico_big:
            SendMessageW(hwnd, WM_SETICON, ICON_BIG, ico_big)
    except Exception:
        pass  # non-critical — fall back to default icon


# ---------------------------------------------------------------------------
# Single-instance / camera-already-running check
# ---------------------------------------------------------------------------

def _is_already_running():
    """Return True if another instance appears to be running (lock file + live PID)."""
    if not os.path.isfile(_LOCK_FILE):
        return False
    try:
        with open(_LOCK_FILE, "r") as f:
            pid = int(f.read().strip())
        # Check if that PID is still alive
        import ctypes.wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Process doesn't exist any more — stale lock
            return False
        exit_code = ctypes.wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == STILL_ACTIVE
    except Exception:
        return False


def _acquire_lock():
    """Write our PID to the lock file."""
    try:
        with open(_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _release_lock():
    """Remove the lock file."""
    try:
        if os.path.isfile(_LOCK_FILE):
            os.remove(_LOCK_FILE)
    except Exception:
        pass


atexit.register(_release_lock)

# ---------------------------------------------------------------------------
# Emergency camera cleanup
# ---------------------------------------------------------------------------

def _cleanup_camera():
    """Release the camera if still held — called by atexit / signals.

    Virtual cameras (OBS, NVIDIA Broadcast, etc.) hold a COM/MSMF reference
    that keeps cv2.pyd locked until fully drained.  The sequence below forces
    the pipeline to flush before the process exits.
    """
    global _active_cap
    try:
        cv2.destroyAllWindows()
        cv2.waitKey(1)   # drain pending GUI events so windows close cleanly
    except Exception:
        pass
    if _active_cap is not None:
        try:
            _active_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # flush internal buffer
        except Exception:
            pass
        try:
            _active_cap.release()
        except Exception:
            pass
        _active_cap = None
    # Second destroyAllWindows — OpenCV sometimes needs two calls after cap release
    try:
        cv2.destroyAllWindows()
        cv2.waitKey(1)
    except Exception:
        pass
    time.sleep(0.1)  # give MSMF/DShow COM a moment to drop its ref


atexit.register(_cleanup_camera)


def _signal_handler(signum, frame):
    _cleanup_camera()
    sys.exit(1)


for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _signal_handler)
    except (OSError, ValueError):
        pass

# ---------------------------------------------------------------------------
# Admin elevation (required for BlockInput)
# ---------------------------------------------------------------------------

def _is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _elevate(): 
    if _is_admin():
        return
    print("[INFO] Requesting Administrator privileges …")
    # sys.argv[0] is the exe path itself — forwarding it makes argparse treat
    # it as an unknown positional argument in the elevated process and exit(2).
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
    except Exception as e:
        print(f"[WARN] Could not elevate: {e}")
        return
    sys.exit(0)

# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def _open_camera(camera_id, width, height):
    """Open a camera by index, preferring MSMF (Media Foundation) backend.

    Tries CAP_MSMF first, then CAP_DSHOW, then auto-detect.  Prints the
    resolved camera name (from DirectShow COM enumeration) when available.
    """
    cam_name = get_camera_name(camera_id)
    if cam_name:
        print(f"[INFO] Opening camera {camera_id}: {cam_name}")
    else:
        print(f"[INFO] Opening camera {camera_id}")

    backends = [
        (cv2.CAP_MSMF, "MSMF"),
        (cv2.CAP_DSHOW, "DSHOW"),
    ]
    cap = None
    for backend, label in backends:
        try:
            cap = cv2.VideoCapture(camera_id, backend)
            if cap.isOpened():
                print(f"[INFO] Backend: {label}")
                break
            cap.release()
            cap = None
        except Exception:
            cap = None

    if cap is None or not cap.isOpened():
        try:
            cap = cv2.VideoCapture(camera_id)
            if cap.isOpened():
                print("[INFO] Backend: AUTO")
        except Exception:
            cap = None

    if cap is None or not cap.isOpened():
        sys.exit(f"[ERROR] Cannot open camera {camera_id}."
                 + (f" ({cam_name})" if cam_name else ""))

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap

# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------

def build_detector(input_size=(320, 320)):
    if not os.path.isfile(YUNET_MODEL):
        sys.exit(f"[ERROR] YuNet model not found:\n  {YUNET_MODEL}\n"
                 "Run: python main.py --download-models")
    return cv2.FaceDetectorYN.create(
        model=YUNET_MODEL, config="", input_size=input_size,
        score_threshold=SCORE_THRESHOLD, nms_threshold=NMS_THRESHOLD,
        top_k=5000)


def build_recognizer():
    if not os.path.isfile(SFACE_MODEL):
        sys.exit(f"[ERROR] SFace model not found:\n  {SFACE_MODEL}\n"
                 "Run: python main.py --download-models")
    return cv2.FaceRecognizerSF.create(model=SFACE_MODEL, config="")


def align_and_extract(recognizer, image, face):
    aligned = recognizer.alignCrop(image, face)
    return recognizer.feature(aligned)

# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _preprocess_image(img, max_dim=640):
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        s = max_dim / max(h, w)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    if max(img.shape[:2]) < 200:
        s = 200 / max(img.shape[:2])
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    return img


def _enhance_image(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def _detect_face_robust(detector, img):
    for variant in (img, _enhance_image(img)):
        h, w = variant.shape[:2]
        detector.setInputSize((w, h))
        for thr in (0.7, 0.5, 0.3):
            detector.setScoreThreshold(thr)
            _, faces = detector.detect(variant)
            if faces is not None and len(faces) > 0:
                detector.setScoreThreshold(SCORE_THRESHOLD)
                return faces[0], variant
    detector.setScoreThreshold(SCORE_THRESHOLD)
    return None, img

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _load_person_images(detector, recognizer, person_dir, person_name):
    features, skipped = [], 0
    for img_file in os.listdir(person_dir):
        if os.path.splitext(img_file)[1].lower() not in IMAGE_EXTS:
            continue
        img = cv2.imread(os.path.join(person_dir, img_file))
        if img is None:
            print(f"  [WARN] Could not read {img_file}")
            continue
        img = _preprocess_image(img)
        best, used = _detect_face_robust(detector, img)
        if best is None:
            print(f"  [WARN] No face in {img_file}")
            skipped += 1
            continue
        features.append(align_and_extract(recognizer, used, best))
    return features, skipped


def load_database(detector, recognizer):
    db, bad_names, skipped = {}, set(), 0

    if not os.path.isdir(FACES_DB_DIR):
        os.makedirs(os.path.join(FACES_DB_DIR, "good"), exist_ok=True)
        os.makedirs(os.path.join(FACES_DB_DIR, "bad"), exist_ok=True)
        print(f"[INFO] Created {FACES_DB_DIR}/good/ and bad/")
        return db, bad_names, 0

    for category in ("good", "bad"):
        cat_dir = os.path.join(FACES_DB_DIR, category)
        if not os.path.isdir(cat_dir):
            os.makedirs(cat_dir, exist_ok=True)
            continue
        print(f"  [{category.upper()}]")
        for name in sorted(os.listdir(cat_dir)):
            person_dir = os.path.join(cat_dir, name)
            if not os.path.isdir(person_dir):
                continue
            feats, s = _load_person_images(detector, recognizer, person_dir, name)
            skipped += s
            if feats:
                db[name] = feats
                if category == "bad":
                    bad_names.add(name)
                info = f"{len(feats)} loaded"
                if s:
                    info += f", {s} skipped"
                print(f"    {name}: {info}")
            elif s:
                print(f"    {name}: ALL {s} failed detection!")

    if skipped:
        print(f"\n[INFO] {skipped} image(s) had no detectable face.")

    return db, bad_names, sum(len(v) for v in db.values())


# ---------------------------------------------------------------------------
# Vectorised identification — replaces the slow per-ref Python loop
# ---------------------------------------------------------------------------

def build_feature_index(db):
    """Pre-compute a stacked numpy matrix and name list for fast matching.

    Returns ``(matrix, names)`` where *matrix* has shape ``(N, 128)`` and
    *names* is a list of length *N* mapping each row to a person name.
    When the database is empty, *matrix* is ``None``.
    """
    if not db:
        return None, []
    rows, names = [], []
    for name, feat_list in db.items():
        for feat in feat_list:
            rows.append(feat.flatten())
            names.append(name)
    matrix = np.vstack(rows).astype(np.float32)          # (N, 128)
    # L2-normalise each row for cosine similarity via dot product
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms
    return matrix, names


def identify(recognizer, feature, db, _feat_matrix=None, _feat_names=None):
    """Identify a face feature against the database.

    If *_feat_matrix* and *_feat_names* are provided (from
    :func:`build_feature_index`), a single numpy dot-product replaces the
    Python loop — typically 10-50x faster for databases with many images.
    """
    if _feat_matrix is not None and len(_feat_names) > 0:
        query = feature.flatten().astype(np.float32)
        query /= (np.linalg.norm(query) or 1.0)
        scores = _feat_matrix @ query          # (N,) cosine similarities
        best_idx = int(np.argmax(scores))
        best_score = float(scores[best_idx])
        best_name = _feat_names[best_idx]
        if best_score < COSINE_THRESHOLD:
            return "Unknown", best_score
        return best_name, best_score

    # Fallback: original per-ref loop (used when matrix not built)
    best_name, best_score = "Unknown", 0.0
    for name, feat_list in db.items():
        for ref in feat_list:
            score = recognizer.match(feature, ref, cv2.FaceRecognizerSF_FR_COSINE)
            if score > best_score:
                best_score = score
                best_name = name
    if best_score < COSINE_THRESHOLD:
        return "Unknown", best_score
    return best_name, best_score

# ---------------------------------------------------------------------------
# Face metrics
# ---------------------------------------------------------------------------

def _face_area(face):
    return int(face[2]) * int(face[3])


def _face_size_pct(face, frame_h, frame_w):
    return (_face_area(face) / (frame_h * frame_w)) * 100.0

# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

_COLORS = {}


def color_for(name, bad_names=None):
    if name not in _COLORS:
        if name == "Unknown":
            _COLORS[name] = (0, 0, 255)
        elif bad_names and name in bad_names:
            _COLORS[name] = (0, 80, 255)
        else:
            _COLORS[name] = (0, 200, 0)
    return _COLORS[name]


def draw_result(frame, face, name, score, bad_names=None):
    x, y, w, h = int(face[0]), int(face[1]), int(face[2]), int(face[3])
    fh, fw = frame.shape[:2]
    size_pct = _face_size_pct(face, fh, fw)
    color = color_for(name, bad_names)

    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)

    label = f"{name} ({score:.2f})" if name != "Unknown" else "Unknown"
    full = f"{label}  [{size_pct:.1f}%]"
    (tw, th), _ = cv2.getTextSize(full, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
    cv2.rectangle(frame, (x, y - th - 10), (x + tw + 4, y), color, -1)
    cv2.putText(frame, full, (x + 2, y - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)


def draw_hud(frame, fps, pipeline_ms):
    for i, line in enumerate([f"FPS: {fps:.1f}", f"Pipeline: {pipeline_ms:.1f} ms"]):
        cv2.putText(frame, line, (8, 24 + i * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2, cv2.LINE_AA)

# ---------------------------------------------------------------------------
# Model downloader
# ---------------------------------------------------------------------------

def download_models():
    import urllib.request
    urls = {
        YUNET_MODEL: ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                       "face_detection_yunet/face_detection_yunet_2023mar.onnx"),
        SFACE_MODEL: ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                       "face_recognition_sface/face_recognition_sface_2021dec.onnx"),
    }
    os.makedirs(os.path.join(SCRIPT_DIR, "models"), exist_ok=True)
    for dest, url in urls.items():
        if os.path.isfile(dest):
            print(f"[OK] Already exists: {dest}")
            continue
        print(f"[DOWNLOAD] {url}\n        -> {dest}")
        urllib.request.urlretrieve(url, dest)
        print(f"[OK] Saved ({os.path.getsize(dest) / 1e6:.1f} MB)")
    print("\nModels ready.")

# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

def enroll_person(name, category="good", num_shots=5, camera_id=0):
    person_dir = os.path.join(FACES_DB_DIR, category, name)
    os.makedirs(person_dir, exist_ok=True)

    cap = _open_camera(camera_id, 640, 480)
    detector = build_detector()
    saved = 0
    print(f"\n[ENROLL] {num_shots} shots for '{name}' — SPACE=capture  ESC=cancel\n")

    while saved < num_shots:
        ret, frame = cap.read()
        if not ret:
            break
        display = frame.copy()
        detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = detector.detect(frame)

        if faces is not None and len(faces) > 0:
            f = faces[0]
            cv2.rectangle(display,
                          (int(f[0]), int(f[1])),
                          (int(f[0]+f[2]), int(f[1]+f[3])), (0, 255, 0), 2)

        cv2.putText(display, f"Enrolled: {saved}/{num_shots}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow("Enroll", display)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:
            break
        if key == 32 and faces is not None and len(faces) > 0:
            fname = os.path.join(person_dir, f"{saved:03d}.jpg")
            cv2.imwrite(fname, frame)
            saved += 1
            print(f"  Saved {fname}")

    cap.release()
    cv2.destroyAllWindows()
    print(f"[ENROLL] Done — {saved} image(s) saved to {person_dir}")

# ---------------------------------------------------------------------------
# Main recognition loop
# ---------------------------------------------------------------------------

def _esc_unlock(splash, locker):
    if locker.is_locked:
        locker.unlock()
    if splash.is_active:
        splash.dismiss()


def run(camera_id=0, width=640, height=480, headless=False, fps_cap=30,
         detection_delay=0.0, idle_skip_pct=0, alert_sustain=3.0,
         detection_resolution=640, show_splash=True):
    global _stop_requested
    _stop_requested = False
    frame_delay = (1.0 / fps_cap) if fps_cap > 0 else 0.0  # 0 = unlimited

    print("\n=== Facial Recognition Security System ===\n")

    # List available cameras
    try:
        all_cams = enumerate_cameras()
        if all_cams:
            print("[INFO] Available cameras:")
            for c in all_cams:
                tag = " (virtual)" if c["type"] == "virtual" else ""
                print(f"         [{c['index']}] {c['name']}{tag}")
        else:
            print("[WARN] No cameras detected via DirectShow enumeration")
    except Exception:
        pass  # Non-critical — we'll still try to open by index

    # Check if another instance is already running
    if _is_already_running():
        print("[WARN] Another instance is already running (camera in use).")
        print("[WARN] Stop the other instance first, or delete .camera.lock if stale.")
        return
    _acquire_lock()

    detector   = build_detector((width, height))
    recognizer = build_recognizer()

    print("[INFO] Loading face database …")
    db, bad_names, db_count = load_database(detector, recognizer)
    feat_matrix, feat_names = build_feature_index(db)
    if not db:
        print(f"[WARN] Database empty \u2014 add images to {FACES_DB_DIR}/good/ or bad/\n")
    else:
        n_good = len(db) - len(bad_names)
        print(f"[INFO] Loaded: {n_good} good, {len(bad_names)} bad, {db_count} images")
        if feat_matrix is not None:
            print(f"[INFO] Feature index: {feat_matrix.shape[0]} vectors ({feat_matrix.nbytes/1024:.1f} KB)")
        if bad_names:
            print(f"[INFO] Bad list: {', '.join(sorted(bad_names))}")
        print()

    splash = SplashPlayer()
    if os.path.isdir(SPLASH_DIR):
        assets = [f for f in os.listdir(SPLASH_DIR)
                  if os.path.isfile(os.path.join(SPLASH_DIR, f))]
        if assets:
            print(f"[INFO] Splash asset: {assets[0]}")
    else:
        os.makedirs(SPLASH_DIR, exist_ok=True)

    # Tell Windows this app should NOT prevent sleep / display-off.
    # ES_CONTINUOUS = 0x80000000 alone clears any prior "keep awake" flags.
    try:
        ES_CONTINUOUS = 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        print("[INFO] Windows sleep policy: allowed (not blocking sleep)")
    except Exception:
        pass

    locker = InputLocker(on_esc_callback=lambda: _esc_unlock(splash, locker))
    cap = _open_camera(camera_id, width, height)
    global _active_cap
    _active_cap = cap                     # track for emergency cleanup

    fps, pipeline_ms = 0.0, 0.0
    frame_count, fps_start = 0, time.time()
    consecutive_failures = 0

    # --- Performance: frame skipping & detection downscale ----------------
    # Convert skip percentage to interval: 0%=1 (every frame), 50%=2, 67%=3, 75%=4, 90%=10
    _skip_pct   = max(0, min(int(idle_skip_pct), 99))
    SKIP_IDLE   = max(1, round(100 / (100 - _skip_pct))) if _skip_pct > 0 else 0
    DET_MAX_DIM = max(int(detection_resolution), 0)  # 0 = no downscale
    total_frame_num   = 0
    threat_active     = False
    last_threat_time  = 0.0          # when the last bad detection occurred
    bad_first_seen    = 0.0          # monotonic time of first continuous bad sighting
    bad_streak_active = False        # True while bad person is continuously detected
    DETECTION_DELAY   = float(detection_delay)  # seconds of continuous detection before splash
    ALERT_SUSTAIN     = float(alert_sustain)     # seconds to keep full-rate after threat clears

    if not headless:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
        # Set custom icon on the OpenCV window via Win32 API
        _set_cv2_window_icon(WINDOW_NAME, os.path.join(SCRIPT_DIR, "logo.ico"))

    mode = "headless" if headless else "windowed"
    print(f"[INFO] Mode: {mode}")
    print("[INFO] Press 'q' or ESC to dismiss splash. Close the window to quit.\n")

    try:
        while not _stop_requested:
            ret, frame = cap.read()
            if not ret:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FRAME_RETRIES:
                    print(f"[ERROR] {MAX_FRAME_RETRIES} consecutive frame failures — reopening camera …")
                    cap.release()
                    time.sleep(0.5)
                    cap = _open_camera(camera_id, width, height)
                    _active_cap = cap
                    consecutive_failures = 0
                continue
            consecutive_failures = 0

            t0 = time.perf_counter()
            total_frame_num += 1

            # --- Frame skip logic -----------------------------------------
            # While no threat is active (and sustain has expired), skip frames
            now_mono = time.monotonic()
            in_sustain = (now_mono - last_threat_time) < ALERT_SUSTAIN
            skip_interval = 1 if (threat_active or in_sustain or SKIP_IDLE <= 0) else (SKIP_IDLE + 1)
            run_pipeline = (total_frame_num % skip_interval == 0)

            h, w = frame.shape[:2]

            bad_in_frame = False
            max_bad_area, max_good_area = 0, 0
            bad_person_name = None

            if run_pipeline:
                # --- Detection downscale ----------------------------------
                if DET_MAX_DIM > 0 and max(h, w) > DET_MAX_DIM:
                    det_scale = DET_MAX_DIM / max(h, w)
                    det_frame = cv2.resize(frame, None, fx=det_scale, fy=det_scale,
                                           interpolation=cv2.INTER_AREA)
                else:
                    det_scale = 1.0
                    det_frame = frame

                dh, dw = det_frame.shape[:2]
                detector.setInputSize((dw, dh))
                _, faces = detector.detect(det_frame)

                # Scale face coordinates back to original resolution
                if faces is not None and det_scale != 1.0:
                    inv = 1.0 / det_scale
                    for i in range(len(faces)):
                        faces[i][0] *= inv   # x
                        faces[i][1] *= inv   # y
                        faces[i][2] *= inv   # w
                        faces[i][3] *= inv   # h
                        # landmarks (pairs at indices 4-13)
                        for li in range(4, 14):
                            faces[i][li] *= inv

                if faces is not None:
                    for face in faces:
                        feat = align_and_extract(recognizer, frame, face)
                        name, score = identify(recognizer, feat, db,
                                               _feat_matrix=feat_matrix,
                                               _feat_names=feat_names)

                        if name in bad_names and score < BAD_SPLASH_THRESHOLD:
                            name = "Unknown"

                        draw_result(frame, face, name, score, bad_names)
                        area = _face_area(face)

                        if name in bad_names:
                            bad_in_frame = True
                            bad_person_name = name
                            max_bad_area = max(max_bad_area, area)
                        elif name != "Unknown":
                            max_good_area = max(max_good_area, area)

            # --- Detection delay: require continuous detection before splash
            if bad_in_frame:
                last_threat_time = now_mono
                threat_active = True
                if not bad_streak_active:
                    bad_streak_active = True
                    bad_first_seen = now_mono
                streak_elapsed = now_mono - bad_first_seen

                if streak_elapsed >= DETECTION_DELAY and not splash.is_active and not locker.is_locked:
                    if max_bad_area >= max_good_area * SIZE_MARGIN:
                        triggered = False
                        if show_splash:
                            triggered = splash.trigger()
                        else:
                            triggered = True   # skip splash, still lock
                        if triggered:
                            print(f"[ALERT] Bad person detected: {bad_person_name}"
                                  f" (confirmed after {streak_elapsed:.1f}s)"
                                  f"{'' if show_splash else ' [splash disabled]'}")
                            if not locker.is_locked:
                                locker.lock()
            else:
                bad_streak_active = False
                bad_first_seen = 0.0
                if not in_sustain:
                    threat_active = False

            if (locker.is_locked or splash.is_active) and not bad_in_frame:
                if not in_sustain:
                    if locker.is_locked:
                        print("[LOCK] Bad person left — unlocking")
                        locker.unlock()
                    if splash.is_active:
                        print("[ALERT] Sustain expired — dismissing splash")
                        splash.dismiss()

            if splash.is_active and bad_in_frame and max_good_area * SIZE_MARGIN > max_bad_area:
                if locker.is_locked:
                    print("[LOCK] Good person closer — unlocking")
                    locker.unlock()
                splash.dismiss()

            pipeline_ms = (time.perf_counter() - t0) * 1000.0

            frame_count += 1
            elapsed = time.time() - fps_start
            if elapsed >= 1.0:
                fps = frame_count / elapsed
                frame_count = 0
                fps_start = time.time()

            draw_hud(frame, fps, pipeline_ms)

            if not headless:
                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    # Dismiss splash/lock only, don't exit
                    if splash.is_active:
                        splash.dismiss()
                    if locker.is_locked:
                        locker.unlock()
                if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    break
            else:
                time.sleep(0.001)

            # FPS cap — sleep to maintain target frame rate
            if frame_delay > 0:
                frame_elapsed = time.perf_counter() - t0
                sleep_time = frame_delay - frame_elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    finally:
        # Guaranteed cleanup regardless of how we exit
        if locker.is_locked:
            locker.unlock()
        if splash.is_active:
            splash.dismiss()
        cap.release()
        _active_cap = None
        _release_lock()
        cv2.destroyAllWindows()
        print("[INFO] Shut down.")

# ---------------------------------------------------------------------------
# Tunable setters (used by settings GUI)
# ---------------------------------------------------------------------------

def _set_threshold(v):
    global COSINE_THRESHOLD
    COSINE_THRESHOLD = v


def _set_bad_threshold(v):
    global BAD_SPLASH_THRESHOLD
    BAD_SPLASH_THRESHOLD = v


def _set_size_margin(v):
    global SIZE_MARGIN
    SIZE_MARGIN = v

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Facial Recognition Security (YuNet + SFace)")
    p.add_argument("--camera",          type=int,   default=0)
    p.add_argument("--width",           type=int,   default=640)
    p.add_argument("--height",          type=int,   default=480)
    p.add_argument("--threshold",       type=float, default=COSINE_THRESHOLD)
    p.add_argument("--bad-threshold",   type=float, default=BAD_SPLASH_THRESHOLD)
    p.add_argument("--size-margin",     type=float, default=SIZE_MARGIN)
    p.add_argument("--download-models", action="store_true")
    p.add_argument("--enroll",          type=str,   metavar="NAME")
    p.add_argument("--enroll-shots",    type=int,   default=5)
    p.add_argument("--enroll-category", type=str,   default="good", choices=["good", "bad"])
    p.add_argument("--headless",        action="store_true")
    p.add_argument("--fps-cap",         type=int,   default=30,
                   help="Maximum frames per second (0-60, 0=unlimited)")
    p.add_argument("--detection-delay", type=float, default=0.0,
                   help="Seconds of continuous bad detection before splash (0-3)")
    p.add_argument("--idle-skip-pct",   type=int,   default=0,
                   help="Percentage of frames to skip when idle (0-90)")
    p.add_argument("--detection-resolution", type=int, default=640,
                   help="Max dimension for detection downscale (160-1920, 0=no downscale)")
    p.add_argument("--alert-sustain",   type=float, default=3.0,
                   help="Seconds to hold splash/lock and full-rate detection after bad person leaves (prevents flashing)")
    p.add_argument("--no-gui",          action="store_true")
    args = p.parse_args()

    if args.threshold != COSINE_THRESHOLD:
        _set_threshold(args.threshold)
    if args.bad_threshold != BAD_SPLASH_THRESHOLD:
        _set_bad_threshold(args.bad_threshold)
    if args.size_margin != SIZE_MARGIN:
        _set_size_margin(args.size_margin)

    if args.download_models:
        download_models()
        return

    if args.enroll:
        enroll_person(args.enroll, category=args.enroll_category,
                      num_shots=args.enroll_shots, camera_id=args.camera)
        return

    if not args.no_gui:
        from settings_gui import launch_gui
        launch_gui()
        return

    run(camera_id=args.camera, width=args.width, height=args.height,
        headless=args.headless, fps_cap=args.fps_cap,
        detection_delay=args.detection_delay,
        idle_skip_pct=args.idle_skip_pct,
        alert_sustain=args.alert_sustain,
        detection_resolution=args.detection_resolution)



if __name__ == "__main__":
    if not _is_admin():
        pass  # _elevate()
    try:
        
        main()
    except Exception as _exc:
        import traceback
        _log = os.path.join(SCRIPT_DIR, "crash.log")
        with open(_log, "w") as _f:
            traceback.print_exc(file=_f)
        raise
