"""
release.py — Run this AFTER build.py to package the compiled output into a
TUF-signed release bundle ready for upload to GitHub Releases.

Workflow:
  1. python build.py    <- compiles  →  dist/main.dist/  +  dist/version.txt
  2. python release.py  <- signs     →  my-tuf-repo/ metadata & targets
  3. Upload all files from my-tuf-repo/metadata/ and my-tuf-repo/targets/
     as assets on a GitHub Release (use a fixed tag, e.g. "tuf-repo").
"""

import argparse
import os
import pathlib
import sys

# ---------------------------------------------------------------------------
# CLI argument parsing — lets CI bypass the interactive confirmation prompt
#   python release.py --yes
# ---------------------------------------------------------------------------

_rparser = argparse.ArgumentParser(add_help=False)
_rparser.add_argument("--yes", "-y", dest="auto_confirm", action="store_true",
                      default=False, help="Skip confirmation prompt")
_rcli, _ = _rparser.parse_known_args()

# ---------------------------------------------------------------------------
# Load saved key passwords (if any) from release_keys.py
# ---------------------------------------------------------------------------

_KEY_PASSWORDS = {}   # role_name  →  password  (only non-empty entries)
_CI = os.environ.get("CI", "").lower() in ("true", "1")  # True when running in GitHub Actions

try:
    from release_keys import (
        ROOT_PASSWORD, TARGETS_PASSWORD, SNAPSHOT_PASSWORD, TIMESTAMP_PASSWORD,
    )
    for _role, _pw in [
        ("root", ROOT_PASSWORD),
        ("targets", TARGETS_PASSWORD),
        ("snapshot", SNAPSHOT_PASSWORD),
        ("timestamp", TIMESTAMP_PASSWORD),
    ]:
        if _pw:
            _KEY_PASSWORDS[_role] = _pw
        elif _CI:
            print(f"[release] ERROR: Password for '{_role}' key is empty. "
                  f"Set the TUF_PWD_{_role.upper()} secret in GitHub.")
            sys.exit(1)
except ImportError:
    if _CI:
        print("[release] ERROR: release_keys.py not found in CI — "
              "check the 'Write key passwords file' workflow step.")
        sys.exit(1)
    # Local run — user will be prompted interactively
    pass

# ---------------------------------------------------------------------------
# Monkey-patch tufup's sign_role so saved passwords are injected
# automatically.  Keys whose password is not saved still fall through
# to the normal interactive prompt.
# ---------------------------------------------------------------------------

if _KEY_PASSWORDS:
    import securesystemslib.interface as _ssi
    _original_import_key = _ssi.import_ed25519_privatekey_from_file

    def _patched_import_key(filepath, password=None, prompt=False,
                            storage_backend=None):
        if password is None and prompt:
            # Determine which role this key belongs to from the filename
            key_name = pathlib.Path(filepath).stem   # e.g. "root", "targets"
            saved = _KEY_PASSWORDS.get(key_name)
            if saved:
                return _original_import_key(
                    filepath, password=saved, prompt=False,
                    storage_backend=storage_backend,
                )
        return _original_import_key(
            filepath, password=password, prompt=prompt,
            storage_backend=storage_backend,
        )

    _ssi.import_ed25519_privatekey_from_file = _patched_import_key

# ---------------------------------------------------------------------------
# Locate dist and read the version that was actually compiled
# ---------------------------------------------------------------------------

DIST_DIR     = pathlib.Path("dist/main.dist")
VERSION_STAMP = pathlib.Path("dist/version.txt")
REPO_DIR     = pathlib.Path("my-tuf-repo")
KEYS_DIR     = pathlib.Path("Keys")

from app_config import APP_NAME

if not DIST_DIR.exists():
    print(f"[release] ERROR: '{DIST_DIR}' not found — run build.py first.")
    sys.exit(1)

if not VERSION_STAMP.exists():
    print(f"[release] ERROR: '{VERSION_STAMP}' not found — run build.py first.")
    sys.exit(1)

compiled_version = VERSION_STAMP.read_text().strip()
if not compiled_version:
    print("[release] ERROR: version stamp is empty — run build.py again.")
    sys.exit(1)

print(f"[release] Compiled version detected: {compiled_version}")
print(f"[release] App name : {APP_NAME}")
print(f"[release] Dist dir : {DIST_DIR}")
print(f"[release] Repo dir : {REPO_DIR}")
print()

# ---------------------------------------------------------------------------
# Confirm before proceeding (gives user a chance to abort)
# ---------------------------------------------------------------------------

if _rcli.auto_confirm:
    print(f"[release] Auto-confirmed (--yes flag).")
else:
    confirm = input(f"Package version {compiled_version} for release? [y/N] ").strip().lower()
    if confirm != "y":
        print("[release] Aborted.")
        sys.exit(0)

# ---------------------------------------------------------------------------
# Build the TUF-signed bundle
# ---------------------------------------------------------------------------

try:
    from tufup.repo import Repository
except ImportError:
    print("[release] ERROR: tufup is not installed in this environment.")
    sys.exit(1)

if not KEYS_DIR.exists():
    print(f"[release] ERROR: Keys directory '{KEYS_DIR}' not found.")
    sys.exit(1)

print("\n[release] Loading repository from config...")

repo = Repository.from_config()

repo.add_bundle(
    new_bundle_dir=DIST_DIR,
    new_version=compiled_version,
)

repo.publish_changes(private_key_dirs=[KEYS_DIR])

# ---------------------------------------------------------------------------
# Print upload instructions
# ---------------------------------------------------------------------------

print("\n[release] Bundle created successfully.")
print("=" * 60)
print("Upload the following files to your GitHub Release")
print("(use a fixed tag such as 'tuf-repo', replacing old assets):")
print()
print(f"  Metadata files (from {REPO_DIR / 'metadata'}):")
for f in sorted((REPO_DIR / "metadata").iterdir()):
    print(f"    {f.name}")

print()
print(f"  Target files (from {REPO_DIR / 'targets'}):")
for f in sorted((REPO_DIR / "targets").iterdir()):
    print(f"    {f.name}")

print("=" * 60)
