import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")

# scripts/ isn't a package (no __init__.py, deliberately — it's meant to be
# run directly via `python3 scripts/build_data.py`). Put it on sys.path so
# tests can `import build_data` / `import ingest_github_activity` the same
# way those scripts import each other's neighbors, without turning scripts/
# into a package and touching how the GitHub Actions workflows invoke them.
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
