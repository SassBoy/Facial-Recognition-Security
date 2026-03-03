import os
import sys
import pathlib
import shutil

# ---------------------------------------------------------------------------
# Tufup setup
# ---------------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent

APP_INSTALL_DIR   = pathlib.Path(__file__).resolve().parent
REPO_METADATA_DIR = APP_INSTALL_DIR / "my-tuf-repo" / "metadata"
CACHE_DIR         = pathlib.Path(__file__).resolve().parent / "cache"
METADATA_DIR      = pathlib.Path(__file__).resolve().parent / "metadata"
TARGET_DIR        = pathlib.Path(__file__).resolve().parent / "targets"

METADATA_BASE_URL = "https://github.com/SassBoy/Facial-Recognition-Security/releases/download/tuf-repo/"
TARGET_BASE_URL   = "https://github.com/SassBoy/Facial-Recognition-Security/releases/download/tuf-repo/"


def _build_client():
    """Create and return a configured tufup Client.

    Deferred imports — tufup/__init__.py unconditionally imports tufup.repo
    which pulls in setuptools -> jaraco (not always in the venv).  Importing
    here means the chain only fires when an update check actually runs.
    """
    import bsdiff4  # noqa: F401 — required by tufup's patch application
    from tufup.client import Client
    from app_config import APP_NAME, APP_VERSION

    for dir_path in [APP_INSTALL_DIR, METADATA_DIR, TARGET_DIR]:
        dir_path.mkdir(exist_ok=True)

    # Ensure cache/metadata/root.json exists — in the compiled dist it is
    # bundled here directly; in dev we seed it once from the repo copy.
    cached_root = CACHE_DIR / "metadata" / "root.json"
    if not cached_root.exists():
        cached_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_METADATA_DIR / "root.json", cached_root)

    dest_path = METADATA_DIR / "root.json"
    if not dest_path.exists():
        shutil.copy(cached_root, dest_path)

    return Client(
        app_name=APP_NAME,
        app_install_dir=APP_INSTALL_DIR,
        current_version=APP_VERSION,
        metadata_dir=METADATA_DIR,
        metadata_base_url=METADATA_BASE_URL,
        target_dir=TARGET_DIR,
        target_base_url=TARGET_BASE_URL,
        refresh_required=False,
    )


def check_for_updates() -> bool:
    """Return True if a newer version is available, False otherwise.

    Does NOT download or apply anything — safe to call from a background
    thread at startup without affecting the running application.
    Swallows all network / TUF errors and returns False so the app
    continues normally when offline or the repo is unreachable.
    """
    try:
        from packaging.version import Version
        from app_config import APP_VERSION
        client = _build_client()
        latest = client.check_for_updates()
        if not latest:
            return False
        # Explicit guard: only signal an update when remote > current
        if Version(str(latest.version)) <= Version(APP_VERSION):
            print(f"[updater] Remote version {latest.version} is not newer than {APP_VERSION}, skipping.")
            return False
        return True
    except Exception as e:
        print(f"[updater] Update check failed: {e}")
        return False


def apply_update():
    """Download, apply the update, then restart the process.

    Call this only after the recognition pipeline has been fully stopped
    and the caller has confirmed the user wants to update.

    The function never returns — it either restarts via os.execv or raises.
    """
    client = _build_client()
    client.check_for_updates()   # must populate new_targets before downloading
    if not client.updates_available:
        raise RuntimeError("No update available to apply.")
    client.download_and_apply_update(skip_confirmation=True)


def _restart_process():
    """Replace the current process with a fresh copy of itself."""
    exe = sys.executable
    args = sys.argv[:]
    print(f"[updater] Restarting: {exe} {args}")
    try:
        # os.execv replaces the process image — clean restart with no orphans
        os.execv(exe, [exe] + args)
    except OSError:
        # execv not available (e.g. frozen exe edge cases) — fall back to spawn
        import subprocess
        subprocess.Popen([exe] + args)
        sys.exit(0)