# Smart PDF Navigator (Smart Find)

Full-stack app: **PyMuPDF** text + **Tesseract** and **EasyOCR** for scans, then a **dynamic field detector** (`detect_fields_dynamic_from_blocks`) that finds **plain text** and **placeholder** values (`Label: text`, `Label: ____`, `Label:` with value on the next line, stacked label + value rows, plus standalone dates/IDs). Optional **OpenAI** / **Donut** merge on top. **Reading-order priorities** (page, top, left) drive navigation unless you turn off `dynamic_fields` on upload. The **decision engine** still sorts pending fields at runtime (priority ↑, confidence ↓, section tie-break). React + PDF.js + `HighlightOverlay`.

## Prerequisites

- **Python 3.10+** (with `pip`). On Windows, **`py -3`** (Python Launcher) works if `python` is not on PATH; batch helpers use **`py-or-python.bat`** (tries `py -3`, then `python`).
- **Node.js 18+** and npm
- **Tesseract OCR** on your PATH when Tesseract is used (Windows: [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki)).
- **EasyOCR** installs with `requirements.txt` (first run may download models).

### Install OCR on Windows (one-time)

1. **Python libs:** from repo root run **`install-ocr-deps.bat`** (or `pip install -r backend/requirements.txt` inside the venv).
2. **Tesseract engine:** if `winget` fails with 403, run **`install-tesseract-windows.ps1`** (PowerShell) or install manually from [UB Mannheim Tesseract](https://github.com/UB-Mannheim/tesseract/wiki). Typical paths: `C:\Program Files\Tesseract-OCR\tesseract.exe` or per-user `%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe` (default for many installers).
3. **`backend\run-api.bat`** sets **`TESSERACT_CMD`** when that path exists and **`SMART_FIND_OCR_DPI=260`** by default.
4. With the API running, open **`http://127.0.0.1:8000/health/ocr`** — you should see `tesseract_binary_found: true` and a version string.

### Scanned & handwritten forms (e.g. concern / safeguarding PDFs)

Many “good example” PDFs are **image-based**: almost no selectable text, only a small **EXAMPLE** watermark. The backend enables **`aggressive_ocr` by default**: those pages are treated as weak-text, watermarks are ignored, and **full-page Tesseract + EasyOCR** run (EasyOCR is preferred when it returns more text — better for some handwriting). Upload query flag: `aggressive_ocr=true` (default). Set `aggressive_ocr=false` only for fast tests on digital PDFs. Large multi-page scans can take several minutes on CPU. Optional: **`SMART_FIND_OCR_DPI`** (default 220, max 400) for harder scans.

Place your PDF anywhere (e.g. `C:\CursurAI\Handwritten-Concern-Form-reporting-Domestic-Abuse-Good-Example.pdf`) and upload it in the app once **Tesseract** and **EasyOCR** are working — the pipeline will OCR each weak page instead of stopping at the `EXAMPLE` stamp.
- **OpenAI** (optional): set `OPENAI_API_KEY`; override model with `OPENAI_MODEL` (default `gpt-4o-mini`).
- **Donut** (optional): `pip install -r requirements-ml.txt`, then `SMART_FIND_TRANSFORMERS=1` (see below).

## 1. Backend

```powershell
cd backend
py -3 -m venv .venv
REM or: python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Optional GPU/CPU torch + Donut:
# pip install -r requirements-ml.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### Environment (optional AI)

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Enables GPT extraction + optional validation pass |
| `OPENAI_MODEL` | e.g. `gpt-4o`, `gpt-4o-mini` |
| `SMART_FIND_TRANSFORMERS` | `1` / `true` to allow Donut path on upload |
| `SMART_FIND_DONUT_MODEL` | HuggingFace id (default `naver-clova-ix/donut-base-finetuned-docvqa`) |

### LayoutLMv3 vs Donut (can we use these?)

| | **Donut** | **LayoutLMv3** |
|---|-----------|----------------|
| **In this repo** | ✅ Optional path in `ai_field_extractor.py` (DocVQA questions on **page 1** image). Install `pip install -r requirements-ml.txt`, then either tick **Donut** on upload or set env `SMART_FIND_TRANSFORMERS=1` so every upload runs it. | ❌ Not wired in. You’d add a new module: run OCR/PDF words → align tokens to boxes → `LayoutLMv3Processor` → model forward → map predictions to fields. |
| **Weights** | Uses public Hugging Face checkpoints (default `naver-clova-ix/donut-base-finetuned-docvqa`). | **Base models** are generic; **good form extraction** usually needs a **fine-tuned** checkpoint on your form type. |
| **Hardware** | Runs **locally** (GPU strongly recommended; CPU is slow). ~2–4 GB+ VRAM typical. | Same class: GPU recommended; batching full pages is heavier than Donut DocVQA-only. |
| **API** | None required if local. | None required if local. |

**Practical note:** Donut here answers a few fixed DocVQA-style questions (policy/name/DOB) and merges with classical + GPT — it does **not** replace full-page OCR. LayoutLMv3 is the right family for **token + layout** joint modeling across a page, but it’s a **separate integration** (and usually **fine-tuning**) project, not a flip of a switch.

- Health: `GET http://127.0.0.1:8000/health`
- Seed mock fields (3–4 demo fields, multi-page): `POST http://127.0.0.1:8000/seed-mock`
- API docs: `http://127.0.0.1:8000/docs`

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/fields` | All fields with status |
| GET | `/next-field` | Next pending field (priority ↑, confidence ↓) |
| POST | `/complete-field` | Body: `{"field_id": "policy_number"}` |
| POST | `/reset` | Clear fields and navigation log |
| POST | `/seed-mock` | Load in-memory mock data |
| POST | `/upload-pdf` | Multipart `file`; query: `ocr`, **`aggressive_ocr`** (default `true` for scanned forms), `dynamic_fields`, `use_openai`, … |

Navigation order is logged on the server (see console / `_navigation_log`).

## 2. Frontend

**If you see `ECONNREFUSED 127.0.0.1:8000` or “Cannot reach API”, the backend is not running.** Easiest fix — from `smart-find-navigator` run **one** of:

```powershell
.\start-dev.bat
```

```powershell
.\start-dev.ps1
```

That opens **uvicorn** on port **8000** in a second window, then **Vite** in the current window.

Or start them yourself in **two** terminals:

```powershell
cd frontend
npm install
npm run dev
```

Open the URL Vite prints (usually `http://localhost:5173`).

Optional `.env` in `frontend`:

```env
VITE_API_URL=http://127.0.0.1:8000
```

If unset during **`npm run dev`**, the app uses **`/api`**, which Vite proxies to `http://127.0.0.1:8000` (same origin, fewer connection issues). Production build defaults to `http://127.0.0.1:8000` unless you set `VITE_API_URL`.

**Windows `WinError 10013` on port 8000:** Something else is already listening on that port (often another **uvicorn** you left open). Close that terminal, or end **`python.exe`** in Task Manager, or pick a free port: `set SMART_FIND_API_PORT=8001` before `run-api.bat`, and set **`VITE_BACKEND_PORT=8001`** in `frontend/.env.local`. **`run-api.bat`** now checks the port first and prints the blocking PID when possible.

## 3. Typical flow

1. Start backend, then frontend.
2. Click **Load mock fields** for a demo queue, or **upload a PDF** (real uploads no longer substitute mock data if OCR finds nothing — you’ll see a hint instead).
3. Click **Next field** → calls `GET /next-field`, scrolls PDF, highlights bbox.
4. Click **Mark complete** → `POST /complete-field`, then loads the next field automatically.
5. Fields with confidence below 0.7 show **Verify this field** and use the amber highlight style.
6. Each field includes a **section** label (e.g. `page1_header`) for grouping.

## Current flow contract (do not break)

If you change this project, keep these behaviors stable so existing users are not impacted:

1. **Upload/reset semantics stay predictable**
   - `POST /upload-pdf` replaces the session with one active document.
   - `POST /upload-pdf-batch` replaces session content with the uploaded batch and sets the first document as active.
   - `POST /reset` clears session docs, review/email gate state, and navigation log.
2. **Pre-operations gate remains enforced**
   - Find/search/navigation endpoints must stay blocked until review is acknowledged.
   - If policy requires email, sending email must remain a prerequisite.
3. **Navigation ordering remains dynamic**
   - `GET /next-field` must continue using pending-only fields with runtime ordering (priority, confidence, section tie-breaks).
4. **Search scope remains session-wide**
   - Frontend search must continue to combine local embedded-text search + backend OCR block search across session PDFs.
5. **UI lock behavior remains consistent**
   - When workflow says navigation is blocked, Find/Prev/Next/Clear must remain disabled and the pre-ops guidance must be shown.

### Safe change zones

These are low-risk areas for enhancement without changing the user flow:

- Error/help text wording in `frontend/src/App.jsx` and `frontend/src/api.js`
- README docs and onboarding scripts
- Additional telemetry/log lines that do not alter API contract or control flow
- New optional endpoints/features that do not modify existing endpoint response shapes

## Testing

- Generate a digital sample PDF:  
  `py -3 backend/scripts/gen_sample_pdf.py` (or `python …`) → `backend/scripts/sample_digital_form.pdf`
- Upload it in the UI (with backend running). For scans, use a scanned PDF; OCR order is Tesseract then EasyOCR on weak pages.
- Exercise **Next field** / **Mark complete** and watch the server log for navigation order.

## Project layout

```
smart-find-navigator/
  backend/
    main.py
    pdf_processor.py      # PyMuPDF, Tesseract, EasyOCR, blocks → JSON
    ai_field_extractor.py # Merge classical + GPT + optional Donut + sections
    gpt_validator.py      # OpenAI extract / validate
    decision_engine.py    # Pending, sort, next field, sections
    requirements.txt
    requirements-ml.txt
    scripts/gen_sample_pdf.py
  frontend/
    src/
      App.jsx
      components/PDFViewer.jsx
      components/HighlightOverlay.jsx
      api.js
```

## Troubleshooting

- **`python` not found**: Install Python from [python.org](https://www.python.org/downloads/) or use the Microsoft Store build; ensure “Add to PATH” is checked.
- **OCR errors**: Install Tesseract and verify `tesseract --version` in a terminal.
- **CORS**: Backend allows all origins in dev; for production, restrict `allow_origins` in `main.py`.
