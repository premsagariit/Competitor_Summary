# Project Overview
Competitor Analysis and Report Generation pipeline for Indian Health Insurance companies (SAHI and Industry-wide GIC statistics). 

## Project Architecture & Phasing
* **Phase 1: Automated Document Retrieval**
  * Uses Parallel AI agents (`AsyncParallel`) to navigate complex, dynamic public disclosure portals.
  * Handles calendar-to-quarter mapping (e.g., Q3 maps to December ending closing reports) and downloads PDFs/XLSX files into local structure: `Backend/downloads/{FY}/{Quarter}/`.
* **Phase 2: Intelligent Data Extraction**
  * Parses multi-page financial tables from downloaded documents and populates the `Data_Engine_UI.xlsx` data engine.
* **Phase 3: Analytical Report Generation**
  * Aggregates and analyzes populated metrics using `pandas` to generate structured competitor insights.
* **Phase 4: Dashboard & HITL Interface**
  * Reactive UI for selecting reporting parameters and integrating Human-In-The-Loop validation.

## Directory Structure
CS/
├── Backend/
│   ├── downloads/            # Target folder for downloaded regulatory files
│   ├── scraper.py            # Phase 1 Parallel AI orchestration & async downloader
│   └── source_links.json     # Root public disclosure URLs for insurers & GIC
└── Frontend/                 # UI dashboard layer

## Key Guidelines & Conventions
* **Asynchronous Operations:** All web extraction and file network calls in `scraper.py` must use asynchronous `httpx` and `AsyncParallel` clients.
* **Environment Variables:** Ensure `PARALLEL_API_KEY` is configured in the environment before triggering extraction tasks.
* **WAF Handling:** All direct document downloads must incorporate custom browser `User-Agent` headers (`REQ_HEADERS`) to prevent HTTP 403/406 firewall rejections.