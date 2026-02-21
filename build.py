import os
import subprocess
import shutil
import sys #to ensure its building uses the env
#Config
APP_NAME = "Desktop-Monitor"
MAIN_SCRIPT = "main.py"
ICON_PATH = "logo.ico"
VERSION = "1.0.0"
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
    "--include-module=screeninfo",
    "--include-module=PIL",
    "--windows-uac-admin",
    f"--output-dir={OUTPUT_DIR}",
    f"--include-data-dir={MODELS}={MODELS}",
    f"--include-data-dir={FACES_DB}={FACES_DB}",
    f"--include-data-dir={SPLASH_ASSETS}={SPLASH_ASSETS}",
    f"--include-data-file={SETTINGS}={SETTINGS}",
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

if (os.path.exists(OUTPUT_DIR)):
    shutil.rmtree(OUTPUT_DIR)

result = subprocess.run(cmd)

if (result.returncode == 0):
    print("\n Build completed successfully!")
else:
    print("\n Build failed!")