# Prerequisites — Smart Find

What you **actually** need, split by **must install** vs **runtime / optional**.

---

## 1. Must have on the computer

| Item | Why |
|------|-----|
| **Python 3.10+** | Runs the API (`backend/`). Needs **`pip`**. On Windows use **`python`** or **`py -3`** if one works. |
| **Node.js 18+** + **npm** | Installs and runs the React app (`frontend/`). |

---

## 2. Backend — Python packages (`pip`)

From **`backend/`** (ideally inside a **venv**):

```text
pip install -r requirements.txt
```

That **installs** (among others): **FastAPI**, **Uvicorn**, **python-multipart**, **PyMuPDF**, **Pillow**, **NumPy**, **RapidFuzz**, **pytesseract** (Python wrapper only), **EasyOCR**, **OpenAI SDK** (library only — no key required).

- **Size / first run:** **EasyOCR** depends on **PyTorch** (large download). The first time OCR runs, EasyOCR may **download models** — needs **internet** once.
- **OpenAI:** The app only calls the API if **`OPENAI_API_KEY`** is set; no key = no cloud cost.

---

## 3. Tesseract — separate from `pip`

| | |
|--|--|
| **What** | **Tesseract** = the actual **OCR program** (`.exe` on Windows). |
| **Not in** | **`requirements.txt`** — you install it like a normal app. |
| **Why needed** | Default upload uses **OCR on weak/scanned pages**. Without Tesseract, **digital PDFs** may still work (PyMuPDF text); **scans / handwriting** often get **no or poor text** / Find won’t work well. |
| **Install** | [Windows build](https://github.com/UB-Mannheim/tesseract/wiki) (or your OS package manager). |
| **Point the app at it** | **`TESSERACT_CMD`** = full path to `tesseract.exe`, or PATH. **`backend\run-api.bat`** tries common Windows paths automatically. |
| **Check** | With API up: **`GET http://127.0.0.1:8000/health/ocr`** → **`tesseract_binary_found: true`**. |

---

## 4. Frontend — npm packages

From **`frontend/`**:

```text
npm install
```

Installs **React 18**, **Vite 5**, **Axios**, **pdf.js**, dev tooling. **No global installs** required.

---

## 5. Run (two processes)

| Part | Command | Default URL |
|------|---------|-------------|
| **API** | `cd backend` → activate venv → **`python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000`** | `http://127.0.0.1:8000` |
| **UI** | `cd frontend` → **`npm run dev`** | Usually `http://localhost:5173` |

- Dev UI calls **`/api/...`** on the same host; **Vite proxies** `/api` → **`http://127.0.0.1:8000`** (override with **`VITE_BACKEND_PORT`** / **`VITE_API_URL`** if needed).
- **Do not** rely on a global **`uvicorn`** command — use **`python -m uvicorn`** inside the **activated `.venv`**, or **`backend\run-api.bat`**.

**One-shot (Windows):** repo root **`start-dev.bat`** or **`start-dev.ps1`** (API + Vite).

---

## 6. Optional (not required for core demo)

| Item | Purpose |
|------|---------|
| **`OPENAI_API_KEY`** | GPT-assisted field extraction / validation |
| **`backend/requirements-ml.txt`** | PyTorch + Transformers for **Donut** (heavy; GPU helps) |
| **Git** | Clone/update the repo only |

---

## 7. Quick troubleshooting

| Problem | Likely fix |
|---------|------------|
| **`uvicorn` is not recognized** | Activate **`.venv`**, run **`python -m uvicorn ...`** |
| **UI: cannot reach API** | Start backend **first**; port **8000** free; match **`VITE_BACKEND_PORT`** if API is not on 8000 |
| **Upload works but Find empty on scans** | Install **Tesseract**, set **`TESSERACT_CMD`**, confirm **`/health/ocr`** |
| **First OCR very slow** | EasyOCR model download / CPU inference — normal |

---

More behavior and tuning: **`README.md`**. API reference: **`http://127.0.0.1:8000/docs`** when the server is running.
