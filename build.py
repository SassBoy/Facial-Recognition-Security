import os
import subprocess
import shutil
import sys  # to ensure its building uses the env

from app_config import APP_NAME, APP_VERSION, MAIN_SCRIPT, ICON_PATH

# Config
OUTPUT_DIR = "dist"
MODELS = "models"
FACES_DB = "faces_db"
SPLASH_ASSETS = "splash_assets"
SETTINGS = "settings.json"

IS_PROD= False

cmd = [
    sys.executable, "-m", "nuitka",
    "--standalone",
    "--enable-plugin=tk-inter",
    "--windows-uac-admin",
    f"--output-dir={OUTPUT_DIR}",
    f"--include-data-dir={MODELS}={MODELS}",
    f"--include-data-dir={FACES_DB}={FACES_DB}",
    f"--include-data-dir={SPLASH_ASSETS}={SPLASH_ASSETS}",
    f"--include-data-file={SETTINGS}={SETTINGS}",
    #this will need to change
    f"--include-data-dir=cache=cache",
    f"--include-data-file=my-tuf-repo/metadata/root.json=cache/metadata/root.json",
    f"--include-package-data=securesystemslib",
    MAIN_SCRIPT
]

if IS_PROD:
    cmd.extend([
        "--windows-disable-console",
        f"--windows-icon-from-ico={ICON_PATH}",
        "--lto=yes",
        "--deployment"
    ])
else:
    cmd.extend(["--show-progress"])

#Execute

print(f"Building the application...")

if os.path.exists(OUTPUT_DIR):
    # Retry rmtree — virtual-camera DLLs (cv2.pyd) may still be locked by a
    # previous run.  Wait up to 10 s for the handle to be released.
    import time as _time
    for _attempt in range(20):
        try:
            shutil.rmtree(OUTPUT_DIR)
            break
        except PermissionError as _e:
            if _attempt == 19:
                raise RuntimeError(
                    f"Cannot delete '{OUTPUT_DIR}' — a file is still locked.\n"
                    "Close any running instance of main.exe and try again.\n"
                    f"({_e})"
                ) from _e
            print(f"[build] dist locked, retrying in 0.5 s … ({_attempt+1}/20)")
            _time.sleep(0.5)

result = subprocess.run(cmd)

if (result.returncode == 0):
    print("\n Build completed successfully!")
else:
    print("\n Build failed!")