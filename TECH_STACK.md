# Smart Find — technology stack

Short map of **what we use** and **what it is for**.

---

## Backend (Python)

| Technology | Purpose |
|------------|---------|
| **FastAPI** | HTTP API: uploads, search, documents, session. Auto docs at `/docs`. |
| **Uvicorn** | Runs the FastAPI app (ASGI server). |
| **PyMuPDF** | Read PDFs, extract text, render pages to images for OCR. |
| **Tesseract** (+ **pytesseract**) | OCR when pages are scans or have little selectable text. |
| **EasyOCR** | Extra OCR—often better on handwriting and noisy scans. |
| **Pillow** | Prepare page images before OCR. |
| **RapidFuzz** | Fuzzy matching on text (labels, values, search-related logic). |
| **OpenAI SDK** | Optional GPT pass to structure or validate fields when `OPENAI_API_KEY` is set. |
| **NumPy** | Supporting numeric / array use (OCR / ML stack). |
| **python-multipart** | Parse file uploads (`multipart/form-data`). |

**Optional (separate install):** **PyTorch + Hugging Face Transformers** (`requirements-ml.txt`) — Donut DocVQA on page 1; only if you enable that path.

---

## Frontend

| Technology | Purpose |
|------------|---------|
| **React** | UI: upload, document viewer, search, layout and state. |
| **Vite** | Dev server, fast refresh, production build. |
| **Axios** | JSON calls to the API (search, documents, reset, etc.). |
| **fetch** | Multipart **upload** so the browser sets the correct form boundary. |
| **pdf.js** | Render PDF pages in the browser and support find/highlight on the canvas. |

---

## Runtime versions (typical)

- **Python 3.10+** — backend  
- **Node.js 18+** — frontend tooling (`npm run dev` / `build`)

---

## How it fits together

1. **Upload** — Browser sends PDF(s) → FastAPI → PyMuPDF (+ Tesseract / EasyOCR when needed) → text blocks and fields stored in server memory for this session.  
2. **View** — pdf.js draws the file; highlights come from search hit boxes.  
3. **Find** — API searches OCR/text blocks across the session; UI shows matches and scrolls the viewer.

More setup (ports, Tesseract install, env vars): see **`README.md`**.
