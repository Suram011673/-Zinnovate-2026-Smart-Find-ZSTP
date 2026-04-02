import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PDFViewer from './components/PDFViewer.jsx';
import * as api from './api.js';
import { findTextInMultiplePdfs, compareSearchMatchReadingOrder } from './utils/pdfTextSearch.js';
import brandMarkUrl from './assets/brand-mark.svg';
import zinniaInsuranceLogo from './assets/zinniainsurance_logo.png';

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
  if (mt === 'exact_loose') return digitOnly ? 412 : 292;
  if (mt === 'exact_substring') return digitOnly ? 400 : 285;
  if (mt === 'digit_formatted') return digitOnly ? 418 : 277;
  if (mt === 'line_digits') return digitOnly ? 416 : 217;
  if (mt === 'line' || mt === 'line_merged_word' || mt === 'line_nospace' || mt === 'line_substring')
    return 220;
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

/** Intersection area of two axis-aligned boxes (PDF space). */
function bboxIntersectionArea(a, b) {
  if (!a?.length || !b?.length) return 0;
  const [ax0, ay0, ax1, ay1] = a;
  const [bx0, by0, bx1, by1] = b;
  const ix0 = Math.max(ax0, bx0);
  const iy0 = Math.max(ay0, by0);
  const ix1 = Math.min(ax1, bx1);
  const iy1 = Math.min(ay1, by1);
  return Math.max(0, ix1 - ix0) * Math.max(0, iy1 - iy0);
}

function bboxMinArea(a, b) {
  const aa = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
  const ba = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
  return Math.min(aa, ba, Infinity);
}

/** Share of the smaller box covered by intersection — catches “word inside OCR line” with low IoU. */
function bboxOverlapOfSmaller(a, b) {
  if (!a?.length || !b?.length) return 0;
  const inter = bboxIntersectionArea(a, b);
  const small = bboxMinArea(a, b);
  return small > 1e-6 ? inter / small : 0;
}

function normSearchSnippet(s) {
  return (s || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
    .slice(0, 96);
}

/** Embedded vs OCR often offset a few pt — IoU alone misses duplicates. */
function bboxSameSearchLocation(a, b) {
  if (!a?.length || !b?.length) return false;
  if (bboxIou(a, b) > 0.35) return true;
  const ovSm = bboxOverlapOfSmaller(a, b);
  if (ovSm > 0.52) return true;
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
  if (dist < 38 && ovSm > 0.22 && rW > 0.2 && rH > 0.25) return true;
  if (bboxIou(a, b) > 0.12 && dist < 28) return true;
  return false;
}

function bboxCenterDist(a, b) {
  if (!a?.length || !b?.length) return Infinity;
  const acx = (a[0] + a[2]) / 2;
  const acy = (a[1] + a[3]) / 2;
  const bcx = (b[0] + b[2]) / 2;
  const bcy = (b[1] + b[3]) / 2;
  return Math.hypot(acx - bcx, acy - bcy);
}

/**
 * Merge only true duplicates (same ink span, embedded vs OCR overlay, or duplicate match_types).
 * Handwriting: many OCR boxes on one line — use stricter rules when both hits are OCR so we
 * do not collapse distinct words. Looser rules only when embedded + OCR disagree on the same spot.
 */
function isSameSearchDuplicate(r, m, rawQuery) {
  if (r.document_id !== m.document_id || r.page !== m.page) return false;
  const ra = r.bbox || [];
  const ma = m.bbox || [];
  if (!ra.length || !ma.length) return false;
  const rs = r.source || '';
  const ms = m.source || '';
  const crossLayer = rs !== ms;

  if (bboxSameSearchLocation(ra, ma)) return true;

  if (crossLayer) {
    if (bboxIou(ra, ma) > 0.22) return true;
    if (bboxOverlapOfSmaller(ra, ma) > 0.38) return true;
    const q = (rawQuery || '').trim().toLowerCase();
    if (q.length >= 4) {
      const nsr = normSearchSnippet(r.snippet);
      const nsm = normSearchSnippet(m.snippet);
      if (nsr.includes(q) && nsm.includes(q) && bboxCenterDist(ra, ma) < 44) return true;
    }
    const qn = normSearchSnippet(rawQuery);
    const snippetDedupeMinLen = Math.min(12, Math.max(4, (rawQuery || '').trim().length || 0));
    const nsr = normSearchSnippet(r.snippet);
    const nsm = normSearchSnippet(m.snippet);
    if (
      qn.length >= 4 &&
      nsr.length >= snippetDedupeMinLen &&
      nsr === nsm &&
      (bboxIou(ra, ma) > 0.08 || bboxOverlapOfSmaller(ra, ma) > 0.42)
    ) {
      return true;
    }
    return false;
  }

  /* Both embedded or both OCR: avoid merging adjacent words / separate handwritten fragments */
  if (bboxIou(ra, ma) > 0.48) return true;
  if (bboxOverlapOfSmaller(ra, ma) > 0.58) return true;
  if (bboxIou(ra, ma) > 0.28 && bboxCenterDist(ra, ma) < 20) return true;
  const qn = normSearchSnippet(rawQuery);
  const snippetDedupeMinLen = Math.min(12, Math.max(4, (rawQuery || '').trim().length || 0));
  const nsr = normSearchSnippet(r.snippet);
  const nsm = normSearchSnippet(m.snippet);
  if (
    qn.length >= 4 &&
    nsr.length >= snippetDedupeMinLen &&
    nsr === nsm &&
    (bboxIou(ra, ma) > 0.2 || bboxOverlapOfSmaller(ra, ma) > 0.5)
  ) {
    return true;
  }
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
    const c = compareSearchMatchReadingOrder(a, b, orderIdx);
    if (c !== 0) return c;
    /* Stable tie-break: same priority + position → deterministic order (avoids 5 vs 8 style flips). */
    const sa = `${a.source || ''}\0${a.match_type || ''}`;
    const sb = `${b.source || ''}\0${b.match_type || ''}`;
    if (sa !== sb) return sa < sb ? -1 : 1;
    const ba = (a.bbox || []).map((x) => Math.round(Number(x) * 100) / 100).join(',');
    const bb = (b.bbox || []).map((x) => Math.round(Number(x) * 100) / 100).join(',');
    return ba < bb ? -1 : ba > bb ? 1 : 0;
  });
  const out = [];
  for (const m of merged) {
    const dup = out.some((r) => isSameSearchDuplicate(r, m, rawQuery));
    if (!dup) out.push(m);
  }
  /* Find / Prev / Next: always follow visual top → bottom (then left → right), not match-type or box size. */
  out.sort((a, b) => compareSearchMatchReadingOrder(a, b, orderIdx));
  return out;
}

export default function App() {
  const pdfViewerRef = useRef(null);
  const [pdfFile, setPdfFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [msgErr, setMsgErr] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [searchMatches, setSearchMatches] = useState([]);
  const [searchIndex, setSearchIndex] = useState(0);
  const [searchBusy, setSearchBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  /** 'upload' = sending PDF; 'extract' = server OCR/fields — drives branded Loading UI */
  const [processingPhase, setProcessingPhase] = useState(null);
  const [readableDoc, setReadableDoc] = useState(null);
  const [transcriptBusy, setTranscriptBusy] = useState(false);
  const [documentsList, setDocumentsList] = useState([]);
  const [activeDocumentId, setActiveDocumentId] = useState(null);
  const [docFilesById, setDocFilesById] = useState({});
  /** Last Find: how many session PDFs contain the keyword vs how many were searched */
  const [searchPdfStats, setSearchPdfStats] = useState(null);
  const [headerLogoIdx, setHeaderLogoIdx] = useState(0);
  /** Must be checked to run OCR/extraction after upload and to use Find. Starts unchecked each session until the user opts in. */
  const [readyToSearch, setReadyToSearch] = useState(false);
  /** True after deferred upload until POST /extract-documents completes (or upload had nothing pending). */
  const [extractionPending, setExtractionPending] = useState(false);
  const pendingExtractOptionsRef = useRef(null);
  const searchTextRef = useRef('');
  const onSearchPdfRef = useRef(async () => {});
  const pendingAutoFindAfterOcrRef = useRef(false);
  const docFilesRef = useRef({});
  const activeDocRef = useRef(null);

  const headerLogoSources = useMemo(
    () => [
      zinniaInsuranceLogo,
      `${import.meta.env.BASE_URL}zinniainsurance_logo.png`,
      `${import.meta.env.BASE_URL}zinniainsurance_logo.jpg`,
      `${import.meta.env.BASE_URL}zinniainsurance_logo.svg`,
      brandMarkUrl,
    ],
    [],
  );

  /**
   * PDF overlays are 100% data-driven: each Find uses the current `searchText` and merged API + embedded
   * results in `searchMatches` — no fixed sample phrases. Every match on the active document gets a box;
   * `searchIndex` only picks which box scrolls and shows the stronger “active” ring (Prev/Next).
   */
  const searchOverlayHits = useMemo(() => {
    const q = searchText.trim();
    if (!searchMatches.length || !q) return [];
    let docFilter = activeDocumentId || '';
    if (!docFilter) {
      const ids = [...new Set(searchMatches.map((m) => m.document_id).filter(Boolean))];
      if (ids.length === 1) docFilter = ids[0];
      else return [];
    }
    return searchMatches
      .map((m, globalIdx) => ({ m, globalIdx }))
      .filter(({ m }) => (m.document_id || '') === docFilter)
      .map(({ m, globalIdx }) => {
        if (!m.page || !m.bbox || m.bbox.length < 4) return null;
        const bbKey = (m.bbox || []).map((x) => Math.round(Number(x) * 10) / 10).join('-');
        return {
          page: m.page,
          bbox: m.bbox,
          overlayKey: `s-${docFilter}-${globalIdx}-p${m.page}-${bbKey}`,
          scrollIntoView: globalIdx === searchIndex,
        };
      })
      .filter(Boolean);
  }, [searchMatches, activeDocumentId, searchIndex, searchText]);

  useEffect(() => {
    docFilesRef.current = docFilesById;
  }, [docFilesById]);
  useEffect(() => {
    activeDocRef.current = activeDocumentId;
  }, [activeDocumentId]);

  useEffect(() => {
    searchTextRef.current = searchText;
  }, [searchText]);

  /** Do not clear search when pdfFile changes for multi-PDF viewing (Find / Prev / Next). */
  useEffect(() => {
    if (!pdfFile) setReadableDoc(null);
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

  const refreshDocuments = useCallback(async () => {
    try {
      const data = await api.getDocuments();
      setDocumentsList(data.documents || []);
      setActiveDocumentId(data.active_document_id ?? null);
    } catch {
      setDocumentsList([]);
    }
  }, []);

  /** Restore session document list after reload so Find can use server blocks without local File blobs. */
  useEffect(() => {
    void refreshDocuments();
  }, [refreshDocuments]);

  const clearSearchResults = useCallback(() => {
    setSearchMatches([]);
    setSearchIndex(0);
    setSearchPdfStats(null);
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

    /* New upload replaces the session: clear viewer + maps immediately, then server. */
    clearSearchState();
    setReadableDoc(null);
    setDocFilesById({});
    setDocumentsList([]);
    setActiveDocumentId(null);
    setPdfFile(null);
    docFilesRef.current = {};
    activeDocRef.current = null;
    setReadyToSearch(false);
    setExtractionPending(false);
    pendingExtractOptionsRef.current = null;
    pendingAutoFindAfterOcrRef.current = false;
    try {
      await api.resetSession();
    } catch {
      /* Upload still replaces server session; ignore reset failure (e.g. API down). */
    }

    setBusy(true);
    setUploading(true);
    setProcessingPhase('upload');
    setMsg('');
    setMsgErr(false);
    try {
      setOk('Uploading…');
      const options = {
        ocr: true,
        use_openai: false,
        use_transformers: false,
        handwriting_merge: false,
        defer_extraction: true,
      };
      const data =
        files.length === 1
          ? await api.uploadPdf(files[0], options)
          : await api.uploadPdfBatch(files, options, '', {});
      pendingExtractOptionsRef.current = options;
      setExtractionPending(!!data.extraction_pending);
      const map = {};
      if (files.length === 1) {
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
      // Avoid blocking upload completion on transcript fetch.
      setReadableDoc(null);
      const n = data.documents?.length ?? 0;
      const fn0 = data.documents?.[0]?.filename ?? 'PDF';
      const failNote =
        data.errors?.length > 0
          ? ` (${data.errors.length} file(s) failed)`
          : '';
      if (data.extraction_pending) {
        setOk(
          'PDF stored. Check "Ready to search" below to extract text, detect fields, and enable Find.',
        );
      } else if (!data.fields?.length) {
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
        setOk(`Ready: ${n} PDF(s) — viewing ${fn0}`);
      }
    } catch (err) {
      setErr(formatApiError(err));
    } finally {
      setUploading(false);
      setProcessingPhase(null);
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
          return;
        }
      }
    },
    [],
  );

  const searchBlocked = !readyToSearch || extractionPending;

  const onSearchPdf = async () => {
    const q = searchText.trim();
    if (!q) {
      setErr('Enter text to search.');
      return;
    }
    if (!readyToSearch) {
      setErr('Check "Ready to search" to run Find.');
      return;
    }
    if (extractionPending) {
      setErr('Extraction is still running — wait until it finishes before Find.');
      return;
    }
    setSearchMatches([]);
    setSearchIndex(0);
    setSearchPdfStats(null);

    let searchEntries = (documentsList || [])
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

    let docList = documentsList || [];
    let actId = activeDocumentId;
    if (!searchEntries.length) {
      try {
        const d = await api.getDocuments();
        const dl = d.documents || [];
        if (dl.length) {
          docList = dl;
          actId = d.active_document_id || dl[0]?.document_id || actId;
          setDocumentsList(dl);
          if (d.active_document_id) {
            setActiveDocumentId(d.active_document_id);
          }
        }
      } catch {
        /* no session — handled below */
      }
    }

    const canSearchServer =
      docList.length > 0 && (actId || docList[0]?.document_id);
    if (!searchEntries.length && !canSearchServer) {
      setErr('No files loaded. Upload File(s) first.');
      return;
    }
    if (!actId && docList[0]?.document_id) {
      actId = docList[0].document_id;
    }

    setSearchBusy(true);
    setMsg('');
    setMsgErr(false);
    try {
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      const localTagged =
        searchEntries.length > 0 ? await findTextInMultiplePdfs(searchEntries, q) : [];
      let serverMatches = [];
      try {
        serverMatches = await api.documentSearch(q);
      } catch (se) {
        setSearchPdfStats(null);
        setErr(formatApiError(se));
        return;
      }
      const orderIdx = {};
      const orderSource =
        docList.length > 0 ? docList : searchEntries.map((e) => ({ document_id: e.document_id }));
      orderSource.forEach((d, i) => {
        if (d.document_id) orderIdx[d.document_id] = i;
      });
      const merged = mergeAndDedupeSearchMatches(localTagged, serverMatches, orderIdx, q);
      setSearchMatches(merged);
      const pdfTotal = docList.length || searchEntries.length || 1;
      const docIdsWithHits = new Set(merged.map((m) => m.document_id).filter(Boolean));
      const pdfWithHits = docIdsWithHits.size;
      setSearchPdfStats({
        query: q,
        pdfWithHits,
        pdfTotal,
        matchCount: merged.length,
      });
      if (!merged.length) {
        setErr(
          pdfTotal > 1
            ? `No match found — 0 of ${pdfTotal} PDFs contain “${q}” in embedded text.`
            : `No match found for “${q}” in embedded text.`,
        );
        return;
      }
      setSearchIndex(0);
      await applySearchMatch(merged, 0, q);
      if (pdfTotal > 1) {
        setOk(
          `“${q}” in ${pdfWithHits} of ${pdfTotal} PDFs · ${merged.length} match location${merged.length === 1 ? '' : 'es'}.`,
        );
      } else {
        setOk(
          `Found ${merged.length} match${merged.length === 1 ? '' : 'es'} for “${q}”.`,
        );
      }
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setSearchBusy(false);
    }
  };

  onSearchPdfRef.current = onSearchPdf;

  /** OCR + field extraction: runs only after the user checks "Ready to search" (mandatory opt-in). */
  useEffect(() => {
    if (!readyToSearch || !extractionPending || !pendingExtractOptionsRef.current) return;
    const opts = pendingExtractOptionsRef.current;
    let cancelled = false;
    (async () => {
      setUploading(true);
      setProcessingPhase('extract');
      setBusy(true);
      setMsg('');
      setMsgErr(false);
      try {
        const data = await api.extractDocuments(opts);
        if (cancelled) return;
        setExtractionPending(false);
        setDocumentsList(data.documents || []);
        if (data.active_document_id) {
          setActiveDocumentId(data.active_document_id);
        }
        void refreshDocuments();
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
          setOk(`Ready: ${n} PDF(s) — viewing ${fn0}`);
        }
        pendingAutoFindAfterOcrRef.current = !!(searchTextRef.current || '').trim();
      } catch (extractErr) {
        if (!cancelled) {
          setErr(formatApiError(extractErr));
          setExtractionPending(false);
        }
      } finally {
        if (!cancelled) {
          setUploading(false);
          setProcessingPhase(null);
          setBusy(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [readyToSearch, extractionPending, refreshDocuments]);

  /** After OCR completes, re-run Find once if there was a non-empty query (avoids stale empty server index). */
  useEffect(() => {
    if (extractionPending || !readyToSearch) return;
    if (!pendingAutoFindAfterOcrRef.current) return;
    pendingAutoFindAfterOcrRef.current = false;
    const q = (searchTextRef.current || '').trim();
    if (!q) return;
    void onSearchPdfRef.current();
  }, [extractionPending, readyToSearch]);

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

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-inner">
          <div className="app-header-left">
            <div className="app-header-logo-wrap">
              <img
                className="app-header-zinnia-logo"
                src={headerLogoSources[headerLogoIdx]}
                alt="Zinnia Insurance"
                width={480}
                height={75}
                onError={() =>
                  setHeaderLogoIdx((i) => Math.min(i + 1, headerLogoSources.length - 1))
                }
              />
            </div>
            <span className="app-header-rule" aria-hidden="true" />
            <div className="app-header-product">
              <h1 className="app-header-title">
                <img
                  src={brandMarkUrl}
                  alt=""
                  className="app-header-product-mark"
                  width={29}
                  height={29}
                  decoding="async"
                  aria-hidden="true"
                />
                <span className="app-header-title-text">Smart Find</span>
              </h1>
              <p className="app-header-product-hint">
                Jump to each field on the image and search across files — zero manual scrolling.
              </p>
            </div>
          </div>
          <div className="app-header-badges" role="group" aria-label="Zinnia taglines">
            <span className="app-header-chip">Rewiring Insurance With AI</span>
            <span className="app-header-chip">Zinnia Simplifies Insurance</span>
          </div>
        </div>
      </header>

      <div className="app-body">
        <aside className="app-sidebar">
          <section className="panel">
            <h2>Source</h2>
            <div className="btn-row btn-row--source">
              <label className="btn-primary file-pick file-pick--full">
                Upload File(s)
                <input
                  type="file"
                  accept=".pdf,application/pdf"
                  multiple
                  hidden
                  onChange={onPickPdf}
                />
              </label>
            </div>
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
          </section>

          <section className="panel panel--search">
            <h2>Search</h2>
            <label className="search-ready-label">
              <input
                type="checkbox"
                checked={readyToSearch}
                disabled={uploading || busy}
                onChange={(e) => setReadyToSearch(e.target.checked)}
                aria-required="true"
                aria-describedby="search-ready-explainer"
              />
              <span>Ready to search</span>
              <span className="search-ready-required" aria-hidden="true">
                {' '}
                (required)
              </span>
            </label>
            <p id="search-ready-explainer" className="field-hint muted small search-ready-explainer">
              Checking this box runs text extraction and field detection on your uploaded PDF(s), then unlocks Find.
            </p>
            <p className="field-hint muted small">
              Type any word, number, or phrase — Find runs on that text only. All hits on the open Document
              highlight together; Prev/Next moves focus (accent + scroll).
            </p>
            <input
              type="text"
              className="search-input"
              placeholder="Search words, numbers, or phrases…"
              value={searchText}
              disabled={
                searchBusy || searchBlocked || (!documentsList.length && !pdfFile)
              }
              onChange={(e) => setSearchText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onSearchPdf();
              }}
              aria-label="Search PDFs using your own keywords or numbers"
            />
            <div className="btn-row search-actions">
              <button
                type="button"
                className="btn-primary"
                disabled={
                  searchBusy ||
                  searchBlocked ||
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
                disabled={searchBlocked || !searchMatches.length}
                onClick={onSearchPrev}
              >
                Prev
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={searchBlocked || !searchMatches.length}
                onClick={onSearchNext}
              >
                Next
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={!searchMatches.length}
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

          <div className="app-sidebar-trailing">
            {msg ? (
              <p className={`app-msg${msgErr ? ' app-msg--err' : ''}`} role="status">
                {msg}
              </p>
            ) : null}
          </div>
        </aside>

        <main className="app-main">
          <div className="app-doc-frame" role="region" aria-label="PDF document view">
            <div className="app-doc-frame__chrome">
              <span className="app-doc-frame__title">Document</span>
              <div className="app-doc-frame__chrome-center">
                {pdfFile ? (
                  <span className="app-doc-frame__filename" title={pdfFile.name}>
                    {pdfFile.name}
                  </span>
                ) : (
                  <span className="app-doc-frame__filename app-doc-frame__filename--placeholder">
                    Upload a file to open the transaction document
                  </span>
                )}
              </div>
              {uploading ? (
                <div
                  className="app-doc-frame__process app-doc-frame__process--branded"
                  role="status"
                  aria-live="polite"
                  aria-label={
                    processingPhase === 'upload'
                      ? 'Uploading PDF'
                      : processingPhase === 'extract'
                        ? 'Loading document'
                        : 'Loading'
                  }
                >
                  <img
                    className="app-doc-frame__process-logo"
                    src={zinniaInsuranceLogo}
                    alt=""
                    width={112}
                    height={22}
                    decoding="async"
                  />
                  <span className="app-doc-frame__process-label">Loading</span>
                  <span className="app-doc-frame__process-spinner" aria-hidden="true" />
                </div>
              ) : null}
            </div>
            <div className="app-main-pdf">
              <PDFViewer
                key={activeDocumentId || 'none'}
                ref={pdfViewerRef}
                file={pdfFile}
                highlight={null}
                searchHits={searchOverlayHits}
              />
            </div>
          </div>
          {pdfFile && (
            <details className="ocr-transcript">
              <summary>
                <span>Extracted text (formatted)</span>
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
                  The same extracted text blocks power <strong>Find</strong> on scanned or image-only PDFs. Reading order +
                  paragraph breaks; watermark tokens like EXAMPLE are filtered when alone.
                </p>
                {!readableDoc ? (
                  <p className="muted">Click Refresh to load the transcript from the server.</p>
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
                      ? 'No extracted text in the last upload (0 blocks). Check Tesseract/EasyOCR on the API or SMART_FIND_HANDWRITING_MODE=1.'
                      : 'No paragraphs yet — only noise was extracted (e.g. watermark), or gaps did not form paragraphs.'}
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
