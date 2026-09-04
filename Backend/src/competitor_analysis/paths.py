"""Filesystem layout for the project, anchored to the repository - not to the
current working directory.

Every path the pipeline reads or writes resolves through here. Previously each
module built its own relative path ("Data_Engine_UI.xlsx", os.path.join("cache",
...)), which meant the pipeline only worked when invoked from inside the
Backend directory and silently created stray cache/output trees anywhere else.

Layout:
    Backend/
      src/competitor_analysis/   importable package (this file lives here)
      app/                       Streamlit UI entrypoint
      tests/
      data/                      INPUTS, version-controlled
        downloads/{FY}/{Q}/      retrieved regulatory filings
        templates/               blank Data Engine workbook
        assets/logos/            company logos used in the report
        source_links.json        disclosure portal roots
      artifacts/                 OUTPUTS, regenerable
        Data_Engine_UI.xlsx      working workbook the pipeline populates
        output/                  generated reports and dated workbooks
        cache/                   PDF-JSON, LLM responses, extraction audits

An env var override (CA_BACKEND_ROOT) exists so a deployment can relocate the
data/artifact trees without editing code.
"""
import os
from pathlib import Path

# .../Backend/src/competitor_analysis/paths.py -> .../Backend
PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
BACKEND_ROOT = Path(os.getenv("CA_BACKEND_ROOT", SRC_DIR.parent))
PROJECT_ROOT = BACKEND_ROOT.parent

# ---- inputs -------------------------------------------------------------
DATA_DIR = BACKEND_ROOT / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
TEMPLATES_DIR = DATA_DIR / "templates"
ASSETS_DIR = DATA_DIR / "assets"
LOGO_DIR = ASSETS_DIR / "logos"
SOURCE_LINKS_FILE = DATA_DIR / "source_links.json"
DATA_ENGINE_TEMPLATE = TEMPLATES_DIR / "Data_Engine_Template.xlsx"

# ---- outputs ------------------------------------------------------------
ARTIFACTS_DIR = BACKEND_ROOT / "artifacts"
DATA_ENGINE_WORKBOOK = ARTIFACTS_DIR / "Data_Engine_UI.xlsx"
OUTPUT_DIR = ARTIFACTS_DIR / "output"
CACHE_DIR = ARTIFACTS_DIR / "cache"
PDF_JSON_CACHE = CACHE_DIR / "pdf_json"
GEMINI_CACHE = CACHE_DIR / "gemini"
EXTRACTION_AUDIT_DIR = CACHE_DIR / "extraction_audit"
URL_PATTERNS_FILE = CACHE_DIR / "url_patterns.json"


def ensure(path):
    """Create `path` as a directory (parents included) and return it. Used at
    the point of writing rather than at import time, so merely importing the
    package never has filesystem side effects."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent(path):
    """Create the parent directory of a file path and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
