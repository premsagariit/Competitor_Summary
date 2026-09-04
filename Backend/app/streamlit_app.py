"""
UI over the competitor_analysis.cli module, with a human-in-the-loop gate between
Phase 1 and Phase 2:

  setup  -> pick FY/Quarter, optionally run Phase 1 (download)
  review -> see which insurer PDFs / GIC.xlsx actually landed, hand-upload
            any that failed, then decide whether to continue
  done   -> download the generated report and Data Engine file

Phase 1 downloads are best-effort per insurer, so stopping here matters: it
lets a partial download be topped up manually instead of silently producing
a report with companies missing. Continuing without the missing files is
always allowed - Phase 2 just skips whatever isn't there.

Each stage runs the competitor_analysis.cli module as a subprocess (not an in-process
import) because Streamlit re-executes this script on every interaction within
the same process, and the orchestrator's dependent modules compute
FY/Quarter-derived path constants once at first import - a second run for a
different period in the same process would silently reuse the first period's
paths. A fresh subprocess per stage avoids that entirely, and also means the
build stage imports those modules only after any manual uploads are in place.

pipeline_config is safe to import here (every function takes an explicit
fy/quarter), and is the single source of truth for expected filenames.

Usage:
    streamlit run app/streamlit_app.py
"""
import os
import subprocess
import sys
from pathlib import Path

# `streamlit run` executes this file as a script, so the package is only
# importable if it has been installed (`pip install -e .`). Fall back to
# putting src/ on the path so the dashboard also runs straight from a fresh
# checkout - and so the "module not found" failure mode can't come back.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st

from competitor_analysis import config as cfg
from competitor_analysis import paths

BACKEND_DIR = paths.BACKEND_ROOT
FY_OPTIONS = cfg.fy_options(back=4, forward=1)
QUARTER_OPTIONS = ["Q1", "Q2", "Q3", "Q4"]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.set_page_config(page_title="Competition Analysis Pipeline", layout="centered")
st.title("Competition Analysis Pipeline")

ss = st.session_state
ss.setdefault("stage", "setup")      # setup -> review -> done
ss.setdefault("fy", cfg.fy_options(back=0, forward=0)[0])
ss.setdefault("quarter", "Q3")
ss.setdefault("phase1_log", [])
ss.setdefault("build_log", [])
ss.setdefault("build_rc", None)
ss.setdefault("saved_uploads", set())


def stream_stage(stage: str, fy: str, quarter: str, log_key: str, label: str) -> int:
    """Runs one orchestrator stage, streaming its output into a live status
    panel and persisting the log in session state so it survives the reruns
    that follow. Returns the process exit code.

    "-u" forces the child's stdout to be unbuffered - without it Python
    block-buffers output that isn't going to a real terminal, so nothing
    reaches this UI until the process exits and the pipeline looks frozen
    for minutes at a time even though it's working.
    """
    cmd = [
        sys.executable, "-u", "-m", "competitor_analysis.cli",
        "--fy", fy, "--quarter", quarter, "--stage", stage,
    ]
    lines = []
    ss[log_key] = lines
    with st.status(label, expanded=True) as status:
        box = st.empty()
        # Hand the child the same src/ fallback this process used, so the
        # subprocess resolves competitor_analysis whether or not the package
        # is installed in the environment.
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (f"{_SRC}{os.pathsep}{existing}"
                             if existing else str(_SRC))
        proc = subprocess.Popen(
            cmd, cwd=str(BACKEND_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            box.code("\n".join(lines[-200:]), language=None)
            # Mirror stage headers into the status label so progress is
            # visible even once the log panel is collapsed.
            if line.startswith(("---", "===")):
                status.update(label=line.strip("- =") or label)
        proc.wait()
        ok = proc.returncode == 0
        status.update(
            label=f"{label} - {'complete' if ok else 'failed'}",
            state="complete" if ok else "error",
            expanded=not ok,
        )
    return proc.returncode


def persist_upload(dest: str, uploaded, key: str) -> bool:
    """Writes an uploaded file to its canonical destination exactly once.
    The guard matters: a file_uploader keeps returning its file on every
    rerun, so without it the save + st.rerun() pair would loop forever."""
    if uploaded is None or key in ss.saved_uploads:
        return False
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(uploaded.getbuffer())
    ss.saved_uploads.add(key)
    return True


def reset_to_setup():
    ss.stage = "setup"
    ss.saved_uploads = set()
    ss.phase1_log = []
    ss.build_log = []
    ss.build_rc = None


# ---------------------------------------------------------------- setup ----
if ss.stage == "setup":
    col1, col2 = st.columns(2)
    fy = col1.selectbox("Financial Year", FY_OPTIONS, index=FY_OPTIONS.index(ss.fy))
    quarter = col2.selectbox("Quarter", QUARTER_OPTIONS, index=QUARTER_OPTIONS.index(ss.quarter))

    download_choice = st.radio(
        "Download source files (insurer disclosures + GIC data) from public portals?",
        ["No - use what's already in downloads/", "Yes - fetch them first (Phase 1)"],
        index=0,
    )

    if st.button("Start", type="primary"):
        ss.fy, ss.quarter = fy, quarter
        ss.build_log, ss.build_rc = [], None
        if download_choice.startswith("Yes"):
            stream_stage("download", fy, quarter, "phase1_log",
                         f"Phase 1: downloading source files for {fy} {quarter}")
        else:
            ss.phase1_log = ["Phase 1 skipped - using the files already in downloads/."]
        ss.stage = "review"
        st.rerun()

# --------------------------------------------------------------- review ----
elif ss.stage == "review":
    fy, quarter = ss.fy, ss.quarter
    st.subheader(f"Source files for {fy} {quarter}")

    if ss.phase1_log:
        with st.expander("Phase 1 log", expanded=False):
            st.code("\n".join(ss.phase1_log[-300:]), language=None)

    avail = cfg.source_availability(fy, quarter, root="")
    found, missing = avail["companies_found"], avail["companies_missing"]
    n_total = len(cfg.COMPANY_PDF_FILENAMES)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Available ({len(found)}/{n_total} insurers)**")
        for c in sorted(found):
            st.markdown(f"- :green[OK] {c}")
        st.markdown(
            f"- {':green[OK]' if avail['gic_found'] else ':red[MISSING]'} GIC.xlsx (industry data)"
        )
    with col2:
        st.markdown(f"**Failed / missing ({len(missing)} insurers)**")
        if missing:
            for c in sorted(missing):
                st.markdown(f"- :red[MISSING] {c}")
        else:
            st.markdown("- _none_")

    if missing or not avail["gic_found"]:
        st.divider()
        st.markdown(
            "**Upload the missing files** (optional) - each is saved to "
            f"`{cfg.download_dir(fy, quarter)}` under the exact filename Phase 2 looks for."
        )
        if not avail["gic_found"]:
            up = st.file_uploader("GIC industry data (.xlsx)", type=["xlsx"], key="up_gic")
            if persist_upload(avail["gic_path"], up, "up_gic"):
                st.rerun()
        for c in sorted(missing):
            key = f"up_{c}"
            up = st.file_uploader(f"{c} - quarterly disclosure (.pdf)", type=["pdf"], key=key)
            if persist_upload(cfg.expected_pdf_path(c, fy, quarter, root=""), up, key):
                st.rerun()

    st.divider()
    can_build = bool(found) or avail["gic_found"]
    if missing or not avail["gic_found"]:
        st.info(
            "Continuing now will build the report from the available sources only - "
            "anything still missing is skipped, not faked."
        )
    if not can_build:
        st.warning(
            "Nothing to build from: no insurer PDFs and no GIC.xlsx. "
            "Upload at least one file, or go back and run Phase 1."
        )

    b1, b2 = st.columns([2, 1])
    if b1.button("Continue to Phase 2 & 3", type="primary", disabled=not can_build):
        ss.build_rc = stream_stage("build", fy, quarter, "build_log",
                                    f"Phase 2 & 3: building report for {fy} {quarter}")
        ss.stage = "done"
        st.rerun()
    if b2.button("Start over"):
        reset_to_setup()
        st.rerun()

# ----------------------------------------------------------------- done ----
else:
    fy, quarter = ss.fy, ss.quarter
    ok = ss.build_rc == 0
    if ok:
        st.success(f"Report generated for {fy} {quarter}.")
    else:
        st.error(f"Pipeline failed for {fy} {quarter} - see the log below for details.")

    if ss.build_log:
        with st.expander("Phase 2 & 3 log", expanded=not ok):
            st.code("\n".join(ss.build_log[-400:]), language=None)

    pdf_path = Path(cfg.output_pdf_path(fy, quarter))
    if pdf_path.exists():
        st.download_button("Download Report (PDF)", data=pdf_path.read_bytes(),
                            file_name=pdf_path.name, mime="application/pdf")

    engines = sorted(paths.OUTPUT_DIR.glob(f"Data_Engine_{fy}_{quarter}_*.xlsx"))
    if engines:
        latest = engines[-1]
        st.download_button(f"Download Data Engine ({latest.name})", data=latest.read_bytes(),
                            file_name=latest.name, mime=XLSX_MIME)

    if st.button("Run another period"):
        reset_to_setup()
        st.rerun()
