"""Competitor analysis pipeline for Indian health insurers (SAHI + industry GIC).

Phases:
    1. ingestion  - retrieve regulatory filings from public disclosure portals
    2. extraction - parse filings into the Data Engine workbook
    3. reporting  - render the analytical PDF report
    4. app/       - Streamlit pipeline dashboard (outside the package)
"""
__all__ = ["config", "paths"]
