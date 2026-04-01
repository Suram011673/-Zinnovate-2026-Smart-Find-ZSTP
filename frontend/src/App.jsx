import { useCallback, useEffect, useRef, useState } from 'react';
import PDFViewer from './components/PDFViewer.jsx';
import * as api from './api.js';
import { findTextInMultiplePdfs } from './utils/pdfTextSearch.js';

/** FastAPI: detail string, or Pydantic validation list, or fetch/axios network errors */
function formatApiError(err) {
  const raw = (err && err.message) || String(err || 'Unknown error');

  if (err?.name === 'AbortError' || /abort|timed out/i.test(raw)) {
    return raw;
  }

  if (!err?.response) {
    const isFetchNet =
      err?.code === 'ERR_NETWORK' ||
      raw === 'Network Error' ||
      /failed to fetch/i.test(raw) ||
      raw === 'Load failed' ||
      raw === 'The network connection was lost.' ||
      (err?.name === 'TypeError' && /fetch|network|load failed/i.test(raw));

    if (isFetchNet) {
      return [
        `Cannot reach the API (${api.describeApiTarget()}).`,
        'Start the backend (backend/run-api.bat or uvicorn).',
        'Use npm run dev so /api proxies to the API; for production/preview set VITE_API_URL to your API base URL.',
        'If the port is not 8000, set VITE_BACKEND_PORT (and SMART_FIND_API_PORT on the server) to match.',
      ].join(' ');
    }
    return raw || 'Request failed';
  }

  const d = err.response.data?.detail;
  if (d == null) {
    const msg = err.response.data && typeof err.response.data === 'object' && err.response.data.message;
    if (typeof msg === 'string' && msg.trim()) return msg;
    return err.message || `Request failed (${err.response.status})`;
  }
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d
      .map((x) => (x && typeof x === 'object' && x.msg ? x.msg : JSON.stringify(x)))
      .join('; ');
  }
  if (typeof d === 'object') return JSON.stringify(d);
  return String(d);
}

/** True for queries like phone fragments / IDs where scans are trustworthy only in OCR, not a stale text layer. */
function isDigitOnlyQuery(rawQuery) {
  const q = (rawQuery || '').trim();
  return q.length >= 4 && /^\d+$/.test(q);
}

/**
 * Prefer embedded exact over OCR for normal text; for digit-only queries prefer OCR exact over embedded
 * (scanned forms often have a misaligned or invisible text layer that still contains the digits).
 */
function searchMatchPriority(m, rawQuery) {
  const digitOnly = isDigitOnlyQuery(rawQuery);
  if (m.source === 'embedded') {
    const emt = m.match_type || 'exact';
    if (emt === 'digit_formatted') {
      return digitOnly ? 308 : 275;
    }
    if (digitOnly) {
      return emt === 'exact' ? 310 : 280;
    }
    return emt === 'exact' ? 400 : 350;
  }
  const mt = m.match_type || 'exact';
  if (mt === 'exact') return digitOnly ? 420 : 300;
  if (mt === 'exact_substring') return digitOnly ? 400 : 285;
  if (mt === 'digit_formatted') return digitOnly ? 418 : 277;
  if (mt === 'line_digits') return digitOnly ? 416 : 217;
  if (mt === 'line' || mt === 'line_nospace' || mt === 'line_substring') return 220;
  if (mt === 'fuzzy_block') return 120;
  if (mt === 'fuzzy_line' || mt === 'fuzzy_line_tokens') return 60;
  return 200;
}

function bboxArea(m) {
  const b = m?.bbox;
  if (!b || b.length < 4) return Infinity;
  const w = Math.abs(b[2] - b[0]);
  const h = Math.abs(b[3] - b[1]);
  const a = w * h;
  return Number.isFinite(a) && a > 0 ? a : Infinity;
}

function bboxIou(a, b) {
  if (!a?.length || !b?.length) return 0;
  const [ax0, ay0, ax1, ay1] = a;
  const [bx0, by0, bx1, by1] = b;
  const ix0 = Math.max(ax0, bx0);
  const iy0 = Math.max(ay0, by0);
  const ix1 = Math.min(ax1, bx1);
  const iy1 = Math.min(ay1, by1);
  const iw = Math.max(0, ix1 - ix0);
  const ih = Math.max(0, iy1 - iy0);
  const inter = iw * ih;
  if (inter <= 0) return 0;
  const aa = Math.max(0, ax1 - ax0) * Math.max(0, ay1 - ay0);
  const ba = Math.max(0, bx1 - bx0) * Math.max(0, by1 - by0);
  return inter / (aa + ba - inter + 1e-9);
}

/** Embedded vs OCR often offset a few pt — IoU alone misses duplicates. */
function bboxSameSearchLocation(a, b) {
  if (!a?.length || !b?.length) return false;
  if (bboxIou(a, b) > 0.4) return true;
  const [ax0, ay0, ax1, ay1] = a;
  const [bx0, by0, bx1, by1] = b;
  const acx = (ax0 + ax1) / 2;
  const acy = (ay0 + ay1) / 2;
  const bcx = (bx0 + bx1) / 2;
  const bcy = (by0 + by1) / 2;
  const dist = Math.hypot(acx - bcx, acy - bcy);
  const aw = Math.max(0, ax1 - ax0);
  const ah = Math.max(0, ay1 - ay0);
  const bw = Math.max(0, bx1 - bx0);
  const bh = Math.max(0, by1 - by0);
  const rW = aw > 1e-6 && bw > 1e-6 ? Math.min(aw, bw) / Math.max(aw, bw) : 0;
  const rH = ah > 1e-6 && bh > 1e-6 ? Math.min(ah, bh) / Math.max(ah, bh) : 0;
  if (dist < 22 && rW > 0.45 && rH > 0.45) return true;
  return false;
}

function mergeAndDedupeSearchMatches(localTagged, serverMatches, orderIdx, rawQuery) {
  const merged = [...localTagged, ...serverMatches];
  merged.sort((a, b) => {
    const pb = searchMatchPriority(b, rawQuery);
    const pa = searchMatchPriority(a, rawQuery);
    if (pb !== pa) return pb - pa;
    const da = orderIdx[a.document_id] ?? 999;
    const db = orderIdx[b.document_id] ?? 999;
    if (da !== db) return da - db;
    if (a.page !== b.page) return a.page - b.page;
    const aa = bboxArea(a);
    const ba = bboxArea(b);
    if (aa !== ba) return aa - ba;
    return (a.bbox?.[1] || 0) - (b.bbox?.[1] || 0);
  });
  const out = [];
  for (const m of merged) {
    const dup = out.some(
      (r) =>
        r.document_id === m.document_id &&
        r.page === m.page &&
        bboxSameSearchLocation(r.bbox || [], m.bbox || []),
    );
    if (!dup) out.push(m);
  }
  return out;
}

export default function App() {
  const pdfViewerRef = useRef(null);
  const [pdfFile, setPdfFile] = useState(null);
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [msgErr, setMsgErr] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [searchMatches, setSearchMatches] = useState([]);
  const [searchIndex, setSearchIndex] = useState(0);
  const [searchHighlight, setSearchHighlight] = useState(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadingText, setUploadingText] = useState('');
  const [readableDoc, setReadableDoc] = useState(null);
  const [transcriptBusy, setTranscriptBusy] = useState(false);
  const [useDonut, setUseDonut] = useState(false);
  const [useOpenAi, setUseOpenAi] = useState(false);
  /** Strong OCR: extra EasyOCR on scanned pages (slower). Digital PDFs stay fast when off. */
  const [handwritingMerge, setHandwritingMerge] = useState(false);
  const [documentsList, setDocumentsList] = useState([]);
  const [activeDocumentId, setActiveDocumentId] = useState(null);
  const [docFilesById, setDocFilesById] = useState({});
  const [uploadVerifyText, setUploadVerifyText] = useState('');
  const [workflow, setWorkflow] = useState(null);
  const [reviewConfirmChecked, setReviewConfirmChecked] = useState(false);
  const [lastVerifyReport, setLastVerifyReport] = useState(null);
  /** Last Find: how many session PDFs contain the keyword vs how many were searched */
  const [searchPdfStats, setSearchPdfStats] = useState(null);
  const pendingSearchRef = useRef(null);
  const docFilesRef = useRef({});
  const activeDocRef = useRef(null);

  /** Field navigation/search remain locked until review is acknowledged. */
  const opsLocked = Boolean(workflow?.has_uploaded_pdfs && !workflow?.navigation_allowed);

  useEffect(() => {
    docFilesRef.current = docFilesById;
  }, [docFilesById]);
  useEffect(() => {
    activeDocRef.current = activeDocumentId;
  }, [activeDocumentId]);

  /** Do not clear search when pdfFile changes for multi-PDF viewing (Find / Prev / Next). */
  useEffect(() => {
    if (!pdfFile) setReadableDoc(null);
  }, [pdfFile]);

  /** After switching the viewed File (e.g. search navigates to another PDF), apply pending highlight once the viewer loads. */
  useEffect(() => {
    const pending = pendingSearchRef.current;
    if (!pending || !pdfFile) return;
    pendingSearchRef.current = null;
    const q = pending.query || '';
    setSearchHighlight({
      page: pending.page,
      bbox: pending.bbox,
      field_id: 'pdf_search',
      name: 'Search',
      label: 'Search',
      value: q,
      confidence: 1,
      needs_review: false,
      requires_verification: false,
      isSearch: true,
    });
  }, [pdfFile]);

  const refreshReadableText = useCallback(async () => {
    if (!pdfFile) return;
    setTranscriptBusy(true);
    try {
      const doc = await api.getReadableText();
      setReadableDoc(doc);
    } catch {
      setReadableDoc(null);
    } finally {
      setTranscriptBusy(false);
    }
  }, [pdfFile]);

  const setOk = (text) => {
    setMsg(text);
    setMsgErr(false);
  };
  const setErr = (text) => {
    setMsg(text);
    setMsgErr(true);
  };

  const refreshFields = useCallback(async () => {
    const data = await api.getFields();
    setLog(data.navigation_log || []);
  }, []);

  const refreshDocuments = useCallback(async () => {
    try {
      const data = await api.getDocuments();
      setDocumentsList(data.documents || []);
      setActiveDocumentId(data.active_document_id ?? null);
    } catch {
      setDocumentsList([]);
    }
  }, []);

  const refreshWorkflow = useCallback(async () => {
    const w = await api.getSessionWorkflowStatus();
    setWorkflow(w && typeof w === 'object' ? w : null);
  }, []);

  const clearSearchResults = useCallback(() => {
    setSearchMatches([]);
    setSearchIndex(0);
    setSearchHighlight(null);
    setSearchPdfStats(null);
    pendingSearchRef.current = null;
  }, []);

  const clearSearchState = useCallback(() => {
    setSearchText('');
    clearSearchResults();
  }, [clearSearchResults]);

  const onPickPdf = async (e) => {
    const picked = e.target.files;
    const files = picked?.length ? Array.from(picked) : [];
    e.target.value = '';
    if (!files.length) return;
    setBusy(true);
    setUploading(true);
    setUploadingText(
      files.length === 1
        ? 'Uploading and extracting fields for your PDF...'
        : `Uploading ${files.length} PDFs and extracting fields...`,
    );
    setMsg('');
    setMsgErr(false);
    clearSearchState();
    setReadableDoc(null);
    setLastVerifyReport(null);
    setReviewConfirmChecked(false);
    try {
      setOk('Uploading…');
      const options = {
        ocr: true,
        use_openai: useOpenAi,
        use_transformers: useDonut,
        handwriting_merge: handwritingMerge,
      };
      const canUseSingleFastPath =
        files.length === 1 && !uploadVerifyText.trim();
      const data = canUseSingleFastPath
        ? await api.uploadPdf(files[0], options)
        : await api.uploadPdfBatch(files, options, uploadVerifyText.trim(), {});
      const map = {};
      if (canUseSingleFastPath) {
        const oneId = data.active_document_id || data.document_id;
        if (oneId) {
          map[oneId] = files[0];
        }
      } else {
        for (const d of data.documents || []) {
          if (typeof d.upload_index === 'number' && files[d.upload_index]) {
            map[d.document_id] = files[d.upload_index];
          }
        }
      }
      if (!Object.keys(map).length && data.active_document_id && files[0]) {
        map[data.active_document_id] = files[0];
      }
      setDocFilesById(map);
      setDocumentsList(data.documents || []);
      setActiveDocumentId(data.active_document_id || null);
      const firstFile = map[data.active_document_id];
      setPdfFile(firstFile || null);
      if (data.verification) {
        setLastVerifyReport(data.verification);
      }
      // Avoid blocking upload completion on transcript fetch.
      setReadableDoc(null);
      const n = data.documents?.length ?? 0;
      const fn0 = data.documents?.[0]?.filename ?? 'PDF';
      const failNote =
        data.errors?.length > 0
          ? ` (${data.errors.length} file(s) failed)`
          : '';
      if (!data.fields?.length) {
        setErr(
          (data.hint ||
            `No fields from ${fn0} (${data.block_count ?? 0} text blocks). Try OpenAI or clearer scan.`) +
            failNote,
        );
      } else if (data.errors?.length) {
        setOk(
          `Ready: ${n} PDF(s) — viewing ${fn0}${failNote}. Check API errors for skipped files.`,
        );
      } else {
        let line = `Ready: ${n} PDF(s) — viewing ${fn0}`;
        if (data.verification?.all_messages?.length) {
          line += ` · verify: ${data.verification.all_messages.length} line(s)`;
        }
        setOk(line);
      }
      await refreshWorkflow();
    } catch (err) {
      setLastVerifyReport(null);
      setErr(formatApiError(err));
    } finally {
      setUploading(false);
      setUploadingText('');
      setBusy(false);
    }
  };

  const onReset = async () => {
    setBusy(true);
    clearSearchState();
    try {
      await api.resetState();
      setPdfFile(null);
      setDocFilesById({});
      setDocumentsList([]);
      setActiveDocumentId(null);
      setLastVerifyReport(null);
      setReadableDoc(null);
      setWorkflow(null);
      setReviewConfirmChecked(false);
      try {
        await refreshWorkflow();
      } catch {
        /* session empty */
      }
      setOk('Reset.');
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setBusy(false);
    }
  };

  const applySearchMatch = useCallback(
    async (matches, idx, query) => {
      const m = matches[idx];
      if (!m) return;
      const q = (query || '').trim();
      const curActive = activeDocRef.current;
      const filesMap = docFilesRef.current;
      if (m.document_id && m.document_id !== curActive) {
        const f = filesMap[m.document_id];
        if (f) {
          try {
            await api.setActiveDocument(m.document_id);
          } catch (err) {
            setErr(formatApiError(err));
            return;
          }
          setActiveDocumentId(m.document_id);
          setPdfFile(f);
          await refreshFields();
          pendingSearchRef.current = { page: m.page, bbox: m.bbox, query: q };
          return;
        }
      }
      setSearchHighlight({
        page: m.page,
        bbox: m.bbox,
        field_id: 'pdf_search',
        name: 'Search',
        label: 'Search',
        value: q,
        confidence: 1,
        needs_review: false,
        requires_verification: false,
        isSearch: true,
      });
    },
    [refreshFields],
  );

  const onSearchPdf = async () => {
    const q = searchText.trim();
    if (!q) {
      setErr('Enter text to search.');
      return;
    }
    const searchEntries = (documentsList || [])
      .map((d) => ({
        document_id: d.document_id,
        file: docFilesById[d.document_id],
        filename: d.filename,
      }))
      .filter((e) => e.file);
    if (!searchEntries.length && pdfFile && activeDocumentId) {
      searchEntries.push({
        document_id: activeDocumentId,
        file: pdfFile,
        filename: pdfFile.name,
      });
    }
    if (!searchEntries.length) {
      setErr('No PDF files loaded. Upload PDF(s) first.');
      return;
    }
    setSearchBusy(true);
    setMsg('');
    setMsgErr(false);
    try {
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      const localTagged = await findTextInMultiplePdfs(searchEntries, q);
      let serverMatches = [];
      try {
        serverMatches = await api.documentSearch(q);
      } catch (se) {
        setSearchHighlight(null);
        setSearchPdfStats(null);
        setErr(formatApiError(se));
        return;
      }
      const orderIdx = {};
      (documentsList.length ? documentsList : searchEntries.map((e) => ({ document_id: e.document_id }))).forEach(
        (d, i) => {
          orderIdx[d.document_id] = i;
        },
      );
      const merged = mergeAndDedupeSearchMatches(localTagged, serverMatches, orderIdx, q);
      setSearchMatches(merged);
      const pdfTotal = searchEntries.length;
      const docIdsWithHits = new Set(merged.map((m) => m.document_id).filter(Boolean));
      const pdfWithHits = docIdsWithHits.size;
      setSearchPdfStats({
        query: q,
        pdfWithHits,
        pdfTotal,
        matchCount: merged.length,
      });
      if (!merged.length) {
        setSearchHighlight(null);
        setErr(
          pdfTotal > 1
            ? `No matches — 0 of ${pdfTotal} PDFs contain “${q}” (embedded text + OCR). Try a shorter word or different spelling.`
            : `No matches for “${q}” in embedded text or OCR. Try a shorter word or re-upload.`,
        );
        return;
      }
      setSearchIndex(0);
      await applySearchMatch(merged, 0, q);
      if (pdfTotal > 1) {
        setOk(
          `“${q}” in ${pdfWithHits} of ${pdfTotal} PDFs · ${merged.length} match location${merged.length === 1 ? '' : 'es'} (embedded + OCR).`,
        );
      } else {
        setOk(
          `Found ${merged.length} match${merged.length === 1 ? '' : 'es'} for “${q}” (embedded + OCR).`,
        );
      }
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setSearchBusy(false);
    }
  };

  const onSearchPrev = () => {
    if (!searchMatches.length) return;
    const next = (searchIndex - 1 + searchMatches.length) % searchMatches.length;
    setSearchIndex(next);
    void applySearchMatch(searchMatches, next, searchText.trim());
  };

  const onSearchNext = () => {
    if (!searchMatches.length) return;
    const next = (searchIndex + 1) % searchMatches.length;
    setSearchIndex(next);
    void applySearchMatch(searchMatches, next, searchText.trim());
  };

  const onSelectDocument = async (documentId) => {
    if (!documentId || documentId === activeDocumentId) return;
    const f = docFilesById[documentId];
    if (!f) {
      setErr('No local file for that document in the browser session.');
      return;
    }
    setBusy(true);
    setMsg('');
    setMsgErr(false);
    try {
      await api.setActiveDocument(documentId);
      setActiveDocumentId(documentId);
      setPdfFile(f);
      await refreshFields();
      try {
        setReadableDoc(await api.getReadableText());
      } catch {
        setReadableDoc(null);
      }

      const q = searchText.trim();
      const matches = searchMatches;
      if (q && matches.length > 0) {
        const idx = matches.findIndex((m) => (m.document_id || '') === documentId);
        if (idx >= 0) {
          setSearchIndex(idx);
          activeDocRef.current = documentId;
          await applySearchMatch(matches, idx, q);
          setOk(`Viewing: ${f.name} · match ${idx + 1} of ${matches.length} for “${q}”`);
        } else {
          setSearchHighlight(null);
          pendingSearchRef.current = null;
          setOk(
            `Viewing: ${f.name} — no matches for “${q}” in this file. Use Prev/Next to move across PDFs.`,
          );
        }
      } else {
        setOk(`Viewing: ${f.name}`);
      }
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setBusy(false);
    }
  };

  const onClearSearch = () => {
    clearSearchState();
    setMsg('');
    setMsgErr(false);
  };

  const onAcknowledgeReview = async () => {
    if (!reviewConfirmChecked) return;
    setBusy(true);
    setMsg('');
    setMsgErr(false);
    try {
      const data = await api.acknowledgeSessionReview();
      await refreshWorkflow();
      if (data.navigation_allowed) {
        setOk('Review acknowledged — Find and search are enabled.');
      } else {
        setOk(
          data.navigation_blocked_reason ||
            'Review saved. Complete the required step to unlock Find.',
        );
      }
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-brand">
          <p className="app-header-eyebrow">Zinnia workflow</p>
          <h1>Smart PDF Navigator</h1>
          <p className="app-header-tagline">
            Jump to each field on the PDF, validate batches on upload, search across files — less manual scrolling.
          </p>
        </div>
      </header>

      <div className="app-body">
        <aside className="app-sidebar">
          <section className="panel">
            <h2>Source</h2>
            <div className="btn-row btn-row--source">
              <label className="btn-primary file-pick file-pick--full">
                Upload PDF(s)
                <input
                  type="file"
                  accept=".pdf,application/pdf"
                  multiple
                  hidden
                  onChange={onPickPdf}
                />
              </label>
            </div>
            <p className="muted small source-hint">Multi-select PDFs · large batches take longer.</p>
            <details className="panel-tech-stack muted small">
              <summary>Extraction pipeline (OCR &amp; NLP)</summary>
              <ul className="tech-stack-list">
                <li>
                  <strong>Computer vision</strong> — Each scanned page is rasterized; optional server env{' '}
                  <code>SMART_FIND_OCR_IMAGE_ENHANCE</code> (1=light, 2=strong) applies contrast and sharpening before
                  OCR, similar to prescription image cleanup.
                </li>
                <li>
                  <strong>OCR</strong> — <strong>Tesseract</strong> (open source) reads words from pixels;{' '}
                  <strong>EasyOCR</strong> (deep learning) improves lines and handwriting when enabled.
                </li>
                <li>
                  <strong>Handwriting</strong> — Use <strong>Strong OCR</strong> under Advanced extraction and/or server{' '}
                  <code>SMART_FIND_HANDWRITING_MODE=1</code> for harder cursive (slower).
                </li>
                <li>
                  <strong>NLP / structuring</strong> — Detected fields come from layout + text; optional <strong>OpenAI</strong>{' '}
                  can further interpret labels and values. Domain-specific medical models can be added server-side the same
                  way.
                </li>
              </ul>
              <p className="tech-stack-foot muted small">
                Google Cloud Vision / Azure OCR are not bundled; you can plug them into the API and feed the same block
                format if needed.
              </p>
            </details>
            {documentsList.length > 0 && (
              <p className="muted small" style={{ marginTop: 6, marginBottom: 0 }}>
                <strong>{documentsList.length}</strong> PDF{documentsList.length === 1 ? '' : 's'} in this session
              </p>
            )}
            {documentsList.length > 1 && (
              <label className="muted small" style={{ display: 'block', marginTop: 8 }}>
                Viewing
                <select
                  className="search-input"
                  style={{ marginTop: 4 }}
                  value={activeDocumentId || ''}
                  disabled={busy}
                  onChange={(e) => void onSelectDocument(e.target.value)}
                >
                  {documentsList.map((d) => (
                    <option key={d.document_id} value={d.document_id}>
                      {d.filename}
                    </option>
                  ))}
                </select>
              </label>
            )}

            <div className="validate-once-block">
              <h3 className="validate-once-block__title">Validate on upload</h3>
              <p className="field-hint muted small">
                One concept per line — checked when you upload (empty = skip). Results appear under{' '}
                <strong>Validation</strong> below.
              </p>
              <textarea
                className="search-input batch-checks-input"
                rows={3}
                aria-label="Concepts to validate on every PDF when you upload"
                placeholder={'Date of birth\nPolicy number'}
                value={uploadVerifyText}
                disabled={busy}
                onChange={(e) => setUploadVerifyText(e.target.value)}
                spellCheck={false}
              />
            </div>
            <details className="panel-advanced muted small">
              <summary>Advanced extraction</summary>
              <p className="field-hint muted small" style={{ marginTop: 0 }}>
                Uploads are much faster for normal PDFs: only <em>scanned</em> pages run full raster OCR. Enable{' '}
                <strong>Strong OCR</strong> for hard scans or cursive. For ink on top of typed forms server-wide, set{' '}
                <code className="small">SMART_FIND_MIXED_PAGE_OCR=1</code> (slow).
              </p>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={useOpenAi}
                  disabled={busy}
                  onChange={(e) => setUseOpenAi(e.target.checked)}
                />
                <span>OpenAI (needs API key)</span>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={handwritingMerge}
                  disabled={busy}
                  onChange={(e) => setHandwritingMerge(e.target.checked)}
                />
                <span>Strong OCR (scans, handwriting, mixed pages)</span>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={useDonut}
                  disabled={busy}
                  onChange={(e) => setUseDonut(e.target.checked)}
                />
                <span>Donut (page 1, ML deps)</span>
              </label>
            </details>
            <button type="button" className="btn-secondary btn-full" disabled={busy} onClick={onReset}>
              Reset all
            </button>
            {lastVerifyReport?.summary && (
              <div className="verify-results" id="validation-report" style={{ marginTop: 10 }}>
                <p className="muted small" style={{ marginBottom: 6 }}>
                  <strong>Validation</strong> — {Number(lastVerifyReport.summary.pdf_count) || 0} PDF(s),{' '}
                  {Number(lastVerifyReport.summary.concepts_count) || 0} concept(s).{' '}
                  {Number(lastVerifyReport.summary.pdfs_all_ok) ===
                  Number(lastVerifyReport.summary.pdf_count) ? (
                    <span>All PDFs passed.</span>
                  ) : (
                    <span>
                      {Number(lastVerifyReport.summary.pdfs_with_issues) || 0} PDF(s) with missing/unmatched
                      fields.
                    </span>
                  )}
                </p>
                {Array.isArray(lastVerifyReport.all_messages) && lastVerifyReport.all_messages.length > 0 ? (
                  <pre className="nav-log batch-output">
                    {lastVerifyReport.all_messages.filter(Boolean).join('\n')}
                  </pre>
                ) : (
                  <p className="muted small">No issue lines — all listed concepts satisfied.</p>
                )}
              </div>
            )}
          </section>

          {workflow?.has_uploaded_pdfs && (
            <section className="panel panel--preops">
              <h2>Before field operations</h2>
              {workflow.navigation_allowed ? (
                <p className="preops-unlocked">
                  <span className="preops-unlocked__icon" aria-hidden="true">
                    ✓
                  </span>
                  <span className="preops-unlocked__text">
                    Ready — use <strong>Find</strong> and the document viewer.
                  </span>
                </p>
              ) : (
                <>
                  <p className="preops-lead">
                    Review every uploaded PDF in the viewer, then confirm below. Find stays locked until you
                    complete this step.
                  </p>
                  <div className="preops-track" aria-hidden="true">
                    <span
                      className={
                        reviewConfirmChecked
                          ? 'preops-track__pill preops-track__pill--done'
                          : 'preops-track__pill preops-track__pill--pending'
                      }
                    >
                      Review confirmed
                    </span>
                    <span className="preops-track__arrow">→</span>
                    <span className="preops-track__pill preops-track__pill--pending">Unlock</span>
                  </div>

                  <div className="preops-divider" />

                  <label className="preops-checkbox">
                    <input
                      type="checkbox"
                      checked={reviewConfirmChecked}
                      disabled={busy}
                      onChange={(e) => setReviewConfirmChecked(e.target.checked)}
                    />
                    <span>I have reviewed all uploaded PDF(s) in the viewer.</span>
                  </label>
                  <button
                    type="button"
                    className="btn-primary btn-full preops-continue-btn"
                    disabled={busy || !reviewConfirmChecked}
                    onClick={() => void onAcknowledgeReview()}
                  >
                    Continue to Find
                  </button>
                </>
              )}
            </section>
          )}

          <section className="panel panel--search">
            <h2>Search</h2>
            <p className="field-hint muted small">All session PDFs · Prev/Next through matches.</p>
            <input
              type="text"
              className="search-input"
              placeholder="Type text to find…"
              value={searchText}
              disabled={
                opsLocked || searchBusy || (!documentsList.length && !pdfFile)
              }
              onChange={(e) => setSearchText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onSearchPdf();
              }}
              aria-label="Search text in PDF"
            />
            <div className="btn-row search-actions">
              <button
                type="button"
                className="btn-primary"
                disabled={
                  opsLocked ||
                  searchBusy ||
                  !searchText.trim() ||
                  (!documentsList.length && !pdfFile)
                }
                onClick={onSearchPdf}
              >
                {searchBusy ? 'Searching…' : 'Find'}
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={opsLocked || !searchMatches.length}
                onClick={onSearchPrev}
              >
                Prev
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={opsLocked || !searchMatches.length}
                onClick={onSearchNext}
              >
                Next
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={opsLocked || !searchHighlight}
                onClick={onClearSearch}
              >
                Clear
              </button>
            </div>
            {searchPdfStats && (
              <p className="search-pdf-hit-count" role="status" aria-live="polite">
                <strong>{searchPdfStats.pdfWithHits}</strong> of <strong>{searchPdfStats.pdfTotal}</strong> PDF
                {searchPdfStats.pdfTotal === 1 ? '' : 's'} contain{' '}
                <q className="search-query-quoted">{searchPdfStats.query}</q>
                {searchPdfStats.matchCount > 0 && (
                  <span className="muted">
                    {' '}
                    · {searchPdfStats.matchCount} match location{searchPdfStats.matchCount === 1 ? '' : 's'}
                  </span>
                )}
              </p>
            )}
            {searchMatches.length > 0 && (
              <p className="muted small search-status">
                Match {searchIndex + 1} of {searchMatches.length}
                {searchMatches[searchIndex]?.filename ? (
                  <span> · {searchMatches[searchIndex].filename}</span>
                ) : null}
                {searchMatches[searchIndex]?.snippet && (
                  <span className="search-snippet"> — “{searchMatches[searchIndex].snippet}”</span>
                )}
              </p>
            )}
          </section>

          {msg && (
            <p className={`app-msg${msgErr ? ' app-msg--err' : ''}`}>{msg}</p>
          )}

          <details className="panel panel--log panel--collapsible">
            <summary>Activity log</summary>
            <pre className="nav-log">{log.slice(-12).join('\n')}</pre>
          </details>
        </aside>

        <main className="app-main">
          <div className="app-doc-frame" role="region" aria-label="PDF document view">
            <div className="app-doc-frame__chrome">
              <span className="app-doc-frame__title">Document</span>
              {pdfFile ? (
                <span className="app-doc-frame__filename" title={pdfFile.name}>
                  {pdfFile.name}
                </span>
              ) : (
                <span className="app-doc-frame__filename app-doc-frame__filename--placeholder">
                  Upload a PDF to open the transaction document
                </span>
              )}
            </div>
            <div className="app-main-pdf">
              {uploading && (
                <div className="pdf-upload-loader" role="status" aria-live="polite">
                  <div className="pdf-upload-loader__spinner" aria-hidden="true" />
                  <p className="pdf-upload-loader__title">Processing PDF</p>
                  <p className="pdf-upload-loader__text">
                    {uploadingText || 'This can take longer for scanned or handwritten files.'}
                  </p>
                </div>
              )}
              {opsLocked && (
                <div className="pdf-mandatory-banner" role="status">
                  <strong>Document review is mandatory</strong> — View the PDF(s) below, complete{' '}
                  <strong>Before field operations</strong> in the sidebar, then confirm you have reviewed. Find
                  stays locked until then.
                </div>
              )}
              <PDFViewer
                key={activeDocumentId || 'none'}
                ref={pdfViewerRef}
                file={pdfFile}
                highlight={searchHighlight}
              />
            </div>
          </div>
          {pdfFile && (
            <details className="ocr-transcript">
              <summary>
                <span>OCR text (formatted)</span>
                <button
                  type="button"
                  className="btn-secondary"
                  style={{ padding: '4px 10px', fontSize: '0.75rem' }}
                  disabled={transcriptBusy}
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    refreshReadableText();
                  }}
                >
                  {transcriptBusy ? '…' : 'Refresh'}
                </button>
              </summary>
              <div className="ocr-transcript-body">
                <p className="muted small ocr-meta">
                  Same OCR blocks power <strong>Find</strong> on scanned or image-only PDFs. Reading order + paragraph
                  breaks; watermark tokens like EXAMPLE are filtered when alone.
                </p>
                {!readableDoc ? (
                  <p className="muted">Click Refresh to load the OCR transcript from the server.</p>
                ) : readableDoc.paragraph_count > 0 ? (
                  readableDoc.pages?.map((p) => (
                    <div key={p.page} className="ocr-page">
                      <h3>Page {p.page}</h3>
                      {p.paragraphs?.map((para, i) => (
                        <p key={i} className="ocr-para">
                          {para}
                        </p>
                      ))}
                    </div>
                  ))
                ) : (
                  <p className="muted">
                    {readableDoc.line_count === 0
                      ? 'No OCR text in the last upload (0 blocks). Check Tesseract/EasyOCR and try SMART_FIND_HANDWRITING_MODE=1 on the API.'
                      : 'No paragraphs yet — OCR only returned noise (e.g. watermark), or gaps did not form paragraphs.'}
                  </p>
                )}
              </div>
            </details>
          )}
        </main>
      </div>
    </div>
  );
}
