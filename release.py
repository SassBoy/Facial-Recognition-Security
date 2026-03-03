"""
release.py — Run this AFTER build.py to package the compiled output into a
TUF-signed release bundle ready for upload to GitHub Releases.

Workflow:
  1. python build.py    <- compiles  →  dist/main.dist/  +  dist/version.txt
  2. python release.py  <- signs     →  my-tuf-repo/ metadata & targets
  3. Upload all files from my-tuf-repo/metadata/ and my-tuf-repo/targets/
     as assets on a GitHub Release (use a fixed tag, e.g. "tuf-repo").
"""

import pathlib
import sys

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
