import axios from 'axios';

/**
 * In dev, use same-origin `/api` so Vite proxies to FastAPI (avoids some
 * cross-origin / firewall issues with direct http://127.0.0.1:8000).
 * Set VITE_API_URL to override (e.g. production).
 */
const backendPort = import.meta.env.VITE_BACKEND_PORT || '8000';
const BASE =
  (import.meta.env.VITE_API_URL && String(import.meta.env.VITE_API_URL).trim()) ||
  (import.meta.env.DEV ? '/api' : `http://127.0.0.1:${backendPort}`);

/**
 * Human-readable API target for error messages (fetch has no err.response).
 */
export function describeApiTarget() {
  const custom = import.meta.env.VITE_API_URL && String(import.meta.env.VITE_API_URL).trim();
  if (custom) return custom;
  if (import.meta.env.DEV) {
    return `/api (Vite proxy → http://127.0.0.1:${backendPort})`;
  }
  return `http://127.0.0.1:${backendPort}`;
}

/** No default Content-Type: axios would treat FormData as JSON when merged with application/json. */
export const api = axios.create({
  baseURL: BASE,
});

function apiUrl(path) {
  const p = path.startsWith('/') ? path : `/${path}`;
  const b = (BASE || '').replace(/\/$/, '');
  return b ? `${b}${p}` : p;
}

/**
 * Multipart POST via fetch so the browser sets the boundary (axios can stringify FormData if
 * Content-Type is wrong).
 */
async function postMultipart(path, formData, timeoutMs = 600_000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    let res;
    try {
      res = await fetch(apiUrl(path), {
        method: 'POST',
        body: formData,
        signal: ctrl.signal,
      });
    } catch (netErr) {
      if (netErr?.name === 'AbortError') {
        throw new Error(
          `Upload timed out after ${Math.round(timeoutMs / 60_000)} minutes. Try fewer PDFs or turn off Donut / OpenAI / ink merge.`,
        );
      }
      throw netErr;
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      if (!res.ok) {
        const err = new Error(text || res.statusText || 'Request failed');
        err.response = { status: res.status, data: { detail: text } };
        throw err;
      }
      throw new Error('Invalid JSON from API');
    }
    if (!res.ok) {
      const d = data?.detail;
      const msg =
        typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x?.msg || x).join('; ') : JSON.stringify(d ?? data);
      const err = new Error(msg || res.statusText);
      err.response = { status: res.status, data };
      throw err;
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

export async function getFields() {
  const { data } = await api.get('/fields');
  return data;
}

/** OCR/native text blocks for the last uploaded PDF (for search on scans). */
export async function getDocumentBlocks() {
  const { data } = await api.get('/document-blocks');
  return data?.blocks ?? [];
}

/** Handwriting-friendly search over last upload OCR blocks (server: line merge + fuzzy). */
export async function documentSearch(query) {
  const { data } = await api.get('/document-search', {
    params: { q: query },
  });
  return data?.matches ?? [];
}

export async function getDocuments() {
  const { data } = await api.get('/documents');
  return data;
}

/** Pre-ops gate: PDF review + optional required notification email. */
export async function getSessionWorkflowStatus() {
  const { data } = await api.get('/session/workflow-status');
  return data;
}

export async function acknowledgeSessionReview() {
  const { data } = await api.post('/session/acknowledge-review', {});
  return data;
}

/**
 * Email all session PDFs to stakeholders (SMTP required on server).
 * @param {{ to_email: string, cc?: string, public_app_url?: string }} payload
 */
export async function sendSessionEmail(payload) {
  const { data } = await api.post('/send-session-email', {
    to_email: payload.to_email || '',
    cc: payload.cc || '',
    public_api_base: payload.public_api_base || '',
    public_app_url: payload.public_app_url || '',
  });
  return data;
}

export async function setActiveDocument(documentId) {
  const { data } = await api.post('/active-document', { document_id: documentId });
  return data;
}

/** Formatted paragraphs from last upload OCR (handwriting-friendly layout). */
export async function getReadableText() {
  const { data } = await api.get('/readable-text');
  return data;
}

export async function getNextField() {
  const { data } = await api.get('/next-field');
  return data;
}

export async function completeField(fieldId) {
  const { data } = await api.post('/complete-field', { field_id: fieldId });
  return data;
}

export async function seedMock() {
  const { data } = await api.post('/seed-mock');
  return data;
}

export async function uploadPdf(file, options = {}) {
  const {
    ocr = true,
    aggressive_ocr = true,
    handwriting_merge = false,
    dynamic_fields = true,
    use_openai = false,
    use_gpt_validate = false,
    use_transformers = false,
    include_blocks = false,
  } = options;
  const fd = new FormData();
  fd.append('file', file);
  const q = new URLSearchParams({
    ocr: String(ocr),
    aggressive_ocr: String(aggressive_ocr),
    handwriting_merge: String(handwriting_merge),
    dynamic_fields: String(dynamic_fields),
    use_openai: String(use_openai),
    use_gpt_validate: String(use_gpt_validate),
    use_transformers: String(use_transformers),
    include_blocks: String(include_blocks),
  });
  return postMultipart(`/upload-pdf?${q}`, fd);
}

/** Upload multiple PDFs in one session. */
export async function uploadPdfBatch(fileList, options = {}, checksText = '', sessionContext = {}) {
  const {
    ocr = true,
    aggressive_ocr = true,
    handwriting_merge = false,
    dynamic_fields = true,
    use_openai = false,
    use_gpt_validate = false,
    use_transformers = false,
    min_match_score = 62,
  } = options;
  const q = new URLSearchParams({
    ocr: String(ocr),
    aggressive_ocr: String(aggressive_ocr),
    handwriting_merge: String(handwriting_merge),
    dynamic_fields: String(dynamic_fields),
    use_openai: String(use_openai),
    use_gpt_validate: String(use_gpt_validate),
    use_transformers: String(use_transformers),
    min_match_score: String(min_match_score),
  });
  const fd = new FormData();
  for (const f of fileList) {
    fd.append('files', f, f.name || 'document.pdf');
  }
  if (checksText && String(checksText).trim()) {
    fd.append('checks', String(checksText).trim());
  }
  const ctx = sessionContext && typeof sessionContext === 'object' ? sessionContext : {};
  if (ctx.transaction_type) fd.append('transaction_type', String(ctx.transaction_type).slice(0, 200));
  if (ctx.carrier) fd.append('carrier', String(ctx.carrier).slice(0, 200));
  if (ctx.vertical) fd.append('vertical', String(ctx.vertical).slice(0, 200));
  return postMultipart(`/upload-pdf-batch?${q}`, fd);
}
