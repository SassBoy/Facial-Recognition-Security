import pathlib
import shutil

#Tufup Setup
BASE_DIR = pathlib.Path(__file__).resolve().parent

#change dir to local app data for caching and metadata
APP_INSTALL_DIR = pathlib.Path(__file__).resolve().parent
REPO_METADATA_DIR = APP_INSTALL_DIR / "my-tuf-repo" / "metadata"
CACHE_DIR = pathlib.Path(__file__).resolve().parent / 'cache'
METADATA_DIR = pathlib.Path(__file__).resolve().parent / 'metadata'
TARGET_DIR = pathlib.Path(__file__).resolve().parent / 'targets'

METADATA_BASE_URL = "https://github.com/SassBoy/Facial-Recognition-Security/releases/download/"
TARGET_BASE_URL = "https://github.com/SassBoy/Facial-Recognition-Security/releases/download/"

def check_for_updates():
    # Deferred imports — tufup/__init__.py unconditionally imports tufup.repo
    # which pulls in setuptools -> jaraco (not in the venv). Importing here
    # means the chain only fires when an update check actually runs, not at
    # application startup.
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
    
    client = Client(
        app_name=APP_NAME,
        app_install_dir=APP_INSTALL_DIR,
        current_version=APP_VERSION,
        metadata_dir=METADATA_DIR,
        metadata_base_url=METADATA_BASE_URL,
        target_dir=TARGET_DIR,
        target_base_url=TARGET_BASE_URL,
        refresh_required=False, 
    )

    if client.check_for_updates():
        client.download_and_apply_update()