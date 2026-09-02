"""
Visual constants for the matplotlib/PDF report generator. Company-name
resolution and display names are re-used from report_theme.py (pure string/
color logic, not tied to python-pptx); colors here are plain hex strings for
matplotlib rather than pptx RGBColor objects.
"""
from report_theme import canonical_company, COMPANY_DISPLAY_NAME, COMPANIES_WITH_LOGO, logo_path  # noqa: F401

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
