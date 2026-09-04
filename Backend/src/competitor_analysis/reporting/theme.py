"""
Visual constants and company-name resolution for the PDF report.

Colors are plain hex strings for matplotlib. The company-name helpers below
used to live in a python-pptx-based theme module shared with an earlier
PowerPoint generator; that generator has been removed, so they are inlined here
and the project no longer depends on python-pptx at all.
"""
import os

from competitor_analysis import paths

LOGO_DIR = str(paths.LOGO_DIR)

COMPANY_DISPLAY_NAME = {
    "NBHI": "NBHI", "STAR": "STAR", "CARE": "CARE", "CIGNA": "CIGNA",
    "ABHI": "ABHI", "NARAYANA": "Narayana", "GALAXY": "Galaxy",
}
# Company keys that have a real extracted logo image (data/assets/logos).
COMPANIES_WITH_LOGO = {"NBHI", "STAR", "CARE", "CIGNA", "ABHI"}


def canonical_company(raw):
    """Resolves any of the Data Engine's several spellings for a company down
    to one of the 7 short keys, or None if `raw` isn't an individual company at
    all (e.g. 'Industry', 'SAHI', 'Public GI' aggregate labels used as the
    Company or Meric-1 value on several slides)."""
    if not raw:
        return None
    t = str(raw).strip().lower()
    if "niva" in t or t == "nbhi":
        return "NBHI"
    if "star" in t:
        return "STAR"
    if "care" in t:
        return "CARE"
    if "cigna" in t or "manipal" in t:
        return "CIGNA"
    if "aditya" in t or t == "abhi":
        return "ABHI"
    if "narayana" in t or "naryana" in t:  # "Naryana" is a known sheet typo (Slide 5)
        return "NARAYANA"
    if "galaxy" in t:
        return "GALAXY"
    return None


def logo_path(company_key):
    if company_key not in COMPANIES_WITH_LOGO:
        return None
    p = os.path.join(LOGO_DIR, f"{company_key}.png")
    return p if os.path.exists(p) else None

PAGE_SIZE = (8.27, 11.69)  # A4 portrait, inches
DPI = 150

NAVY = "#1F3564"
BLUE = "#2E75B6"
ORANGE = "#ED7D31"
DARK_TEXT = "#333333"
GREY_TEXT = "#808080"
INSIGHT_BG = "#FFF7E6"
INSIGHT_BORDER = "#ED7D31"
GRID_COLOR = "#D9D9D9"

COMPANY_COLORS = {
    "NBHI": "#9DC3E6", "STAR": "#1F4E79", "CARE": "#FFD91A", "CIGNA": "#92D050",
    "ABHI": "#C00000", "NARAYANA": "#2CA6A4", "GALAXY": "#ED7D31",
}
SEGMENT_COLORS = {
    "Private": BLUE, "Pvt GI": BLUE, "Pvt. GI": BLUE,
    "Public": "#FFC000", "Public GI": "#FFC000",
    "SAHI": "#70AD47", "SAHI Market": "#70AD47",
    "Specialized Insurer": NAVY, "Specialised": NAVY, "Industry": BLUE,
}
FALLBACK_SERIES_COLORS = ["#2E75B6", "#ED7D31", "#70AD47", "#FFC000", "#7C3AED",
                          "#264E70", "#C00000", "#00B0B0", "#A9D18E", "#BFBFBF"]


def series_colors_for(names):
    return {name: FALLBACK_SERIES_COLORS[i % len(FALLBACK_SERIES_COLORS)] for i, name in enumerate(names)}


PRIOR_COLOR = "#2E5495"
CURRENT_COLOR = "#A6A6A6"
