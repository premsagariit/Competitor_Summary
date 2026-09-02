"""
One-time asset extraction: pulls the 5 competitor logos that appear in
Backend/Competition Summary FY25 1.pdf's Income Statement table (page 19)
into standalone PNGs under assets/logos/, so report generation doesn't
depend on re-parsing the reference PDF every run.

Run once: python extract_logos.py
"""
import pdfplumber

SOURCE_PDF = "Competition Summary FY25 1.pdf"
OUT_DIR = "assets/logos"

# (pdfplumber page index (0-based), image "name" on that page) -> company key.
# Identified by x0 ordering on page 19 (0-indexed page 18), matching the
# table's left-to-right column order: NBHI, STAR, CARE, CIGNA, ABHI.
LOGO_MAP = {
    "NBHI": (18, "Image247"),
    "STAR": (18, "Image243"),
    "CARE": (18, "Image244"),
    "CIGNA": (18, "Image245"),
    "ABHI": (18, "Image246"),
}


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    with pdfplumber.open(SOURCE_PDF) as pdf:
        for company, (page_idx, image_name) in LOGO_MAP.items():
            page = pdf.pages[page_idx]
            match = next(im for im in page.images if im["name"] == image_name)
            bbox = (match["x0"], match["top"], match["x1"], match["bottom"])
            cropped = page.crop(bbox)
            pic = cropped.to_image(resolution=300)
            out_path = f"{OUT_DIR}/{company}.png"
            pic.save(out_path)
            print(f"  {company}: saved {out_path} ({match['width']:.0f}x{match['height']:.0f}pt source)")


if __name__ == "__main__":
    main()
