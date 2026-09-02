"""Compare Data_Engine_UI.xlsx against the ground-truth GT_Data_Engine.xlsx.

Ground-truth cells come in several distinct kinds, not just "numeric or
blank":
  - a real number (int/float)
  - a number stored as text with Indian comma grouping (e.g. "4,00,370") -
    genuinely numeric truth, just typed as text in the sheet
  - "-" (or a bare Unicode dash) - an explicit placeholder, not a real value
  - a formula-error string ("#VALUE!", "#N/A", "#REF!", ...) - an
    unresolved/broken GT formula, not a real value
  - blank (None)
Only the first two count as "numeric GT truth" for accuracy scoring; the
other three are reported separately rather than counted as extraction
failures. FY26_Q3 and FY25_Q3 are scored independently (per value, not
per row), so a row where GT has a real current-quarter figure but a
dash/error prior-year figure doesn't penalize a correctly-extracted
current value.
"""
import re
from collections import Counter, defaultdict

import openpyxl

HEADERS = ["Slide #", "Category", "Company", "Meric 1", "Metric 2",
           "Source Tab", "Link to Source document", "FY26_Q3", "FY25_Q3", "Growth"]
COL = {h: i + 1 for i, h in enumerate(HEADERS)}

_FORMULA_ERRORS = {"#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!"}
_DASHES = {"-", "–", "—", ""}


def load(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    return wb["Data Engine"]


def row_key(ws, r):
    return (
        ws.cell(row=r, column=COL["Slide #"]).value,
        ws.cell(row=r, column=COL["Company"]).value,
        ws.cell(row=r, column=COL["Meric 1"]).value,
        ws.cell(row=r, column=COL["Metric 2"]).value,
    )


def parse_text_number(s):
    """'4,00,370' -> 400370.0 ; '(1,234)' -> -1234.0 ; None if not a number."""
    s = re.sub(r"\s+", "", str(s))
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    core = s.strip("()").replace(",", "")
    if not re.fullmatch(r"-?\d+\.?\d*", core):
        return None
    try:
        v = float(core)
    except ValueError:
        return None
    return -v if neg else v


def classify(v):
    """Returns (kind, numeric_value_or_None). kind is one of 'blank',
    'numeric', 'text_numeric', 'dash', 'formula_error', 'other_text'."""
    if v is None:
        return "blank", None
    if isinstance(v, (int, float)):
        return "numeric", v
    s = str(v).strip()
    if s in _DASHES:
        return "dash", None
    if s.upper() in _FORMULA_ERRORS:
        return "formula_error", None
    n = parse_text_number(s)
    if n is not None:
        return "text_numeric", n
    return "other_text", None


def num_close(a, b, rel=0.01, abs_tol=0.05):
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom <= rel or abs(a - b) <= abs_tol


def main():
    mine = load("Data_Engine_UI.xlsx")
    gt = load("GT_Data_Engine.xlsx")

    gt_rows = {}
    for r in range(2, gt.max_row + 1):
        slide = gt.cell(row=r, column=COL["Slide #"]).value
        if not isinstance(slide, int):
            continue
        gt_rows[row_key(gt, r)] = r

    mine_rows = {}
    for r in range(2, mine.max_row + 1):
        slide = mine.cell(row=r, column=COL["Slide #"]).value
        if not isinstance(slide, int):
            continue
        mine_rows[row_key(mine, r)] = r

    unmatched_keys = set(mine_rows) - set(gt_rows)
    print(f"Row-key alignment: {len(mine_rows)} rows in mine, {len(gt_rows)} in GT, "
          f"{len(set(mine_rows) & set(gt_rows))} keys match, {len(unmatched_keys)} of mine have no GT key match")
    if unmatched_keys:
        print("Sample unmatched keys (first 10):")
        for k in list(unmatched_keys)[:10]:
            print("  ", k)

    correct = wrong = missing = extra = both_blank = 0
    dash_count = error_count = other_text_count = 0
    dash_mine_filled = error_mine_filled = 0
    by_company = defaultdict(Counter)
    wrong_examples = []
    blank_examples = []

    for key, gr in gt_rows.items():
        company = key[1]
        mr = mine_rows.get(key)
        for col, label in (("FY26_Q3", "cur"), ("FY25_Q3", "prior")):
            gt_raw = gt.cell(row=gr, column=COL[col]).value
            gt_kind, gt_val = classify(gt_raw)
            mine_raw = mine.cell(row=mr, column=COL[col]).value if mr else None
            mine_kind, mine_val = classify(mine_raw)
            mine_has = mine_kind in ("numeric", "text_numeric")

            if gt_kind == "dash":
                dash_count += 1
                if mine_has:
                    dash_mine_filled += 1
                continue
            if gt_kind == "formula_error":
                error_count += 1
                if mine_has:
                    error_mine_filled += 1
                continue
            if gt_kind == "other_text":
                other_text_count += 1
                continue
            if gt_kind == "blank":
                if mine_has:
                    extra += 1
                    by_company[company]["extra"] += 1
                else:
                    both_blank += 1
                continue

            # gt_kind in ("numeric", "text_numeric") - real numeric truth
            if not mine_has:
                missing += 1
                by_company[company]["missing"] += 1
                if len(blank_examples) < 30:
                    blank_examples.append((key[0], *key[1:], label, gt_val))
                continue
            if num_close(mine_val, gt_val):
                correct += 1
                by_company[company]["correct"] += 1
            else:
                wrong += 1
                by_company[company]["wrong"] += 1
                if len(wrong_examples) < 40:
                    wrong_examples.append((key[0], *key[1:], label, mine_val, gt_val))

    total_numeric_gt = correct + wrong + missing

    print()
    print("===== SUMMARY (FY26_Q3/FY25_Q3 scored independently) =====")
    print(f"Numeric GT values (denominator): {total_numeric_gt}")
    print(f"  Correctly extracted & matching GT: {correct}")
    print(f"  Filled but WRONG (mismatch vs GT):  {wrong}")
    print(f"  GT has a value, we left blank (missed): {missing}")
    print(f"We filled a GT-blank slot (extra/unverified, source-supported fill): {extra}")
    print(f"Both blank: {both_blank}")
    accuracy = correct / total_numeric_gt * 100 if total_numeric_gt else 0
    coverage = (correct + wrong) / total_numeric_gt * 100 if total_numeric_gt else 0
    print(f"\nAccuracy (correct / numeric-GT): {accuracy:.1f}%")
    print(f"Coverage (attempted / numeric-GT): {coverage:.1f}%")

    print()
    print("===== NON-NUMERIC GT VALUES (excluded from accuracy - not extraction failures) =====")
    print(f"  Dash ('-'): {dash_count}  ({dash_mine_filled} of these we filled with a value anyway)")
    print(f"  Formula error (#VALUE!/#N/A/...): {error_count}  ({error_mine_filled} of these we filled with a value anyway)")
    if other_text_count:
        print(f"  Other non-numeric text: {other_text_count}")

    print()
    print("===== BY COMPANY =====")
    for company, c in sorted(by_company.items(), key=lambda x: -sum(x[1].values())):
        tot = sum(c.values())
        print(f"  {company or '(blank)'}: correct={c['correct']} wrong={c['wrong']} missing={c['missing']} extra={c['extra']}  (n={tot})")

    print()
    print("===== WRONG EXAMPLES (first 40) =====")
    for slide, company, metric1, metric2, label, mine_v, gt_v in wrong_examples:
        print(f"  slide{slide} {company} / {metric1} / {metric2} [{label}]: mine={mine_v} gt={gt_v}")

    print()
    print("===== MISSING EXAMPLES (GT filled, we're blank) (first 30) =====")
    for slide, company, metric1, metric2, label, gt_v in blank_examples:
        print(f"  slide{slide} {company} / {metric1} / {metric2} [{label}]: gt={gt_v}")


if __name__ == "__main__":
    main()
