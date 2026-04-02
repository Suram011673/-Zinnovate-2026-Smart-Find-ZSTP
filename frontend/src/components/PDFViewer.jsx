import {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import * as pdfjsLib from 'pdfjs-dist';
import HighlightOverlay from './HighlightOverlay.jsx';
import { findTextInPdf } from '../utils/pdfTextSearch.js';

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString();

function highlightPropsEqual(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  if (a.page !== b.page || a.isSearch !== b.isSearch) return false;
  const ba = a.bbox;
  const bb = b.bbox;
  if (!ba || !bb || ba.length !== 4 || bb.length !== 4) return false;
  return ba[0] === bb[0] && ba[1] === bb[1] && ba[2] === bb[2] && ba[3] === bb[3];
}

function bboxEqual4(a, b) {
  if (!a?.length || !b?.length || a.length < 4 || b.length < 4) return false;
  return a[0] === b[0] && a[1] === b[1] && a[2] === b[2] && a[3] === b[3];
}

function searchHitsForPageEqual(prev, next) {
  if (prev === next) return true;
  if (!prev?.length && !next?.length) return true;
  if (!prev || !next || prev.length !== next.length) return false;
  for (let i = 0; i < prev.length; i += 1) {
    if (prev[i].scrollIntoView !== next[i].scrollIntoView) return false;
    if (!bboxEqual4(prev[i].bbox, next[i].bbox)) return false;
  }
  return true;
}

function pageCanvasPropsEqual(prev, next) {
  return (
    prev.pdf === next.pdf &&
    prev.pageNumber === next.pageNumber &&
    prev.maxPageCssPx === next.maxPageCssPx &&
    prev.scrollTargetsRef === next.scrollTargetsRef &&
    highlightPropsEqual(prev.highlight, next.highlight) &&
    searchHitsForPageEqual(prev.searchHits, next.searchHits)
  );
}

/**
 * Canvas + PDF.js render only. Memoized so Find overlays / scroll do not re-run this subtree
 * and cancel in-flight page.render (blank pages).
 */
const PdfPageCanvasCore = memo(
  function PdfPageCanvasCore({ pdf, pageNumber, maxPageCssPx, onMetrics }) {
    const canvasRef = useRef(null);
    const renderTaskRef = useRef(null);
    const renderRetryRef = useRef(0);
    const [viewportSize, setViewportSize] = useState({ w: 0, h: 0 });
    const [canvasLayoutW, setCanvasLayoutW] = useState(0);
    const [canvasLayoutH, setCanvasLayoutH] = useState(0);
    const [renderNonce, setRenderNonce] = useState(0);

    useEffect(() => {
      renderRetryRef.current = 0;
      setRenderNonce(0);
    }, [pdf, pageNumber, maxPageCssPx]);

    useEffect(() => {
      let cancelled = false;
      let retryTimer = null;
      (async () => {
        try {
          const page = await pdf.getPage(pageNumber);
          if (cancelled) return;
          const base = page.getViewport({ scale: 1 });
          const cap = Math.max(280, Number(maxPageCssPx) || 920);
          const scale = Math.min(1.4, cap / base.width);
          const viewport = page.getViewport({ scale });
          const canvas = canvasRef.current;
          if (!canvas) return;
          const ctx = canvas.getContext('2d', { alpha: false });
          if (!ctx) return;
          if (renderTaskRef.current) {
            try {
              renderTaskRef.current.cancel();
            } catch {
              /* ignore */
            }
            renderTaskRef.current = null;
          }
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          setViewportSize({ w: viewport.width, h: viewport.height });
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, viewport.width, viewport.height);

          const task = page.render({
            canvasContext: ctx,
            viewport,
            intent: 'display',
            background: 'rgb(255,255,255)',
          });
          renderTaskRef.current = task;
          await task.promise;
          renderRetryRef.current = 0;
        } catch (e) {
          const name = e && typeof e === 'object' ? e.name : '';
          if (!cancelled && renderRetryRef.current < 3) {
            renderRetryRef.current += 1;
            retryTimer = setTimeout(() => setRenderNonce((n) => n + 1), 120);
            return;
          }
          if (name !== 'RenderingCancelledException') {
            setViewportSize({ w: 0, h: 0 });
          }
        } finally {
          renderTaskRef.current = null;
        }
      })();
      return () => {
        cancelled = true;
        if (retryTimer) clearTimeout(retryTimer);
        if (renderTaskRef.current) {
          try {
            renderTaskRef.current.cancel();
          } catch {
            /* ignore */
          }
          renderTaskRef.current = null;
        }
      };
    }, [pdf, pageNumber, maxPageCssPx, renderNonce]);

    useLayoutEffect(() => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const sync = () => {
        const r = canvas.getBoundingClientRect();
        if (r.width > 0) setCanvasLayoutW(r.width);
        if (r.height > 0) setCanvasLayoutH(r.height);
      };
      sync();
      const ro = new ResizeObserver(() => sync());
      ro.observe(canvas);
      return () => ro.disconnect();
    }, [pdf, pageNumber, maxPageCssPx, renderNonce, viewportSize.w, viewportSize.h]);

    useEffect(() => {
      const vw = viewportSize.w;
      const vh = viewportSize.h;
      const dw = canvasLayoutW > 0 ? canvasLayoutW : vw;
      const dh = canvasLayoutH > 0 ? canvasLayoutH : vh;
      onMetrics({ viewportW: vw, viewportH: vh, displayW: dw, displayH: dh });
    }, [onMetrics, viewportSize.w, viewportSize.h, canvasLayoutW, canvasLayoutH]);

    return <canvas ref={canvasRef} />;
  },
  (prev, next) =>
    prev.pdf === next.pdf &&
    prev.pageNumber === next.pageNumber &&
    prev.maxPageCssPx === next.maxPageCssPx,
);

function PageCanvasInner({ pdf, pageNumber, scrollTargetsRef, highlight, searchHits, maxPageCssPx }) {
  const wrapRef = useRef(null);
  const [metrics, setMetrics] = useState({
    viewportW: 0,
    viewportH: 0,
    displayW: 0,
    displayH: 0,
  });
  const onMetrics = useCallback((m) => {
    setMetrics((prev) => {
      if (
        prev.viewportW === m.viewportW &&
        prev.viewportH === m.viewportH &&
        prev.displayW === m.displayW &&
        prev.displayH === m.displayH
      ) {
        return prev;
      }
      return m;
    });
  }, []);

  /** Map PDF user-space bbox → pixels using the same box the canvas uses on screen (fixes offset / floating highlights). */
  const mapW = metrics.displayW > 0 ? metrics.displayW : metrics.viewportW;
  const mapH = metrics.displayH > 0 ? metrics.displayH : metrics.viewportH;
  const canvasPainted = metrics.viewportW > 0;

  return (
    <div
      className="pdf-page-wrap"
      ref={(el) => {
        wrapRef.current = el;
        if (scrollTargetsRef) {
          if (el) scrollTargetsRef.current[pageNumber] = el;
          else delete scrollTargetsRef.current[pageNumber];
        }
      }}
    >
      <span className="pdf-page-label">Page {pageNumber}</span>
      <div className="pdf-page-inner">
        <div className="pdf-page-canvas-stack">
          <PdfPageCanvasCore
            pdf={pdf}
            pageNumber={pageNumber}
            maxPageCssPx={maxPageCssPx}
            onMetrics={onMetrics}
          />
          {highlight?.bbox && mapW > 0 && mapH > 0 && (
            <HighlightOverlay
              bbox={highlight.bbox}
              mapWidth={mapW}
              mapHeight={mapH}
              pdf={pdf}
              pageNumber={pageNumber}
              needsReview={highlight.needs_review}
              requiresVerification={highlight.requires_verification}
              variant={highlight.isSearch ? 'search' : 'field'}
            />
          )}
          {(searchHits || []).map((hit) =>
            hit.bbox && mapW > 0 && mapH > 0 ? (
              <HighlightOverlay
                key={hit.overlayKey}
                bbox={hit.bbox}
                mapWidth={mapW}
                mapHeight={mapH}
                pdf={pdf}
                pageNumber={pageNumber}
                needsReview={false}
                requiresVerification={false}
                variant="search"
                scrollIntoView={Boolean(hit.scrollIntoView)}
                searchActive={Boolean(hit.scrollIntoView)}
                deferScrollUntilPainted={canvasPainted}
              />
            ) : null,
          )}
        </div>
      </div>
    </div>
  );
}

const PageCanvas = memo(PageCanvasInner, pageCanvasPropsEqual);

/**
 * Renders PDF from File; scrolls to highlight.page; draws bbox overlay (PDF points → canvas scale).
 * ref.findMatches(query) → Promise<matches[]> for full-document text layer search.
 */
const PDFViewer = forwardRef(function PDFViewer({ file, highlight, searchHits = [] }, ref) {
  const containerRef = useRef(null);
  const pageRefs = useRef({});
  const loadingTaskRef = useRef(null);
  const loadSeqRef = useRef(0);
  const [pdf, setPdf] = useState(null);
  const [numPages, setNumPages] = useState(0);
  const [error, setError] = useState(null);
  /** Max CSS width for a page (points); scales render so PDF never forces horizontal page scroll */
  const [maxPageCssPx, setMaxPageCssPx] = useState(920);

  const resizeDebounceRef = useRef(null);
  /** Ignore sub-pixel / scrollbar flicker so we do not cancel in-flight page renders. */
  const lastLayoutWidthRef = useRef(0);

  const measureMaxWidth = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const w = el.clientWidth;
    if (w <= 0) return;
    const raw = Math.max(280, w - 8);
    /* Wider buckets → fewer width flips when Find scroll shows/hides scrollbars (avoids blank pages). */
    const next = Math.round(raw / 128) * 128;
    const prev = lastLayoutWidthRef.current;
    if (prev > 0 && Math.abs(next - prev) < 128) return;
    lastLayoutWidthRef.current = next;
    setMaxPageCssPx(next);
  }, []);

  const measureMaxWidthDebounced = useCallback(() => {
    if (resizeDebounceRef.current) clearTimeout(resizeDebounceRef.current);
    resizeDebounceRef.current = setTimeout(() => {
      resizeDebounceRef.current = null;
      measureMaxWidth();
    }, 280);
  }, [measureMaxWidth]);

  useLayoutEffect(() => {
    lastLayoutWidthRef.current = 0;
    measureMaxWidth();
  }, [measureMaxWidth, file, pdf]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => measureMaxWidthDebounced());
    ro.observe(el);
    const onWin = () => measureMaxWidthDebounced();
    window.addEventListener('resize', onWin);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', onWin);
      if (resizeDebounceRef.current) clearTimeout(resizeDebounceRef.current);
    };
  }, [measureMaxWidthDebounced, file, pdf]);

  useImperativeHandle(
    ref,
    () => ({
      findMatches(query) {
        if (!pdf) return Promise.resolve([]);
        return findTextInPdf(pdf, query);
      },
      getPdf() {
        return pdf;
      },
    }),
    [pdf],
  );

  useEffect(() => {
    if (!file) {
      if (loadingTaskRef.current) {
        try {
          loadingTaskRef.current.destroy();
        } catch {
          /* ignore */
        }
        loadingTaskRef.current = null;
      }
      setPdf(null);
      setNumPages(0);
      setError(null);
      return;
    }
    let cancelled = false;
    const seq = ++loadSeqRef.current;
    setPdf(null);
    setNumPages(0);
    setError(null);
    file.arrayBuffer().then((data) => {
      if (cancelled) return;
      if (loadingTaskRef.current) {
        try {
          loadingTaskRef.current.destroy();
        } catch {
          /* ignore */
        }
      }
      const task = pdfjsLib.getDocument({ data });
      loadingTaskRef.current = task;
      task.promise
        .then((doc) => {
          if (!cancelled && loadSeqRef.current === seq) {
            setPdf(doc);
            setNumPages(doc.numPages);
            setError(null);
          }
        })
        .catch((e) => {
          if (!cancelled && loadSeqRef.current === seq) setError(e?.message || String(e));
        });
    });
    return () => {
      cancelled = true;
      if (loadingTaskRef.current) {
        try {
          loadingTaskRef.current.destroy();
        } catch {
          /* ignore */
        }
        loadingTaskRef.current = null;
      }
    };
  }, [file]);

  useEffect(() => {
    if (!highlight?.page) return;
    /* Search: HighlightOverlay scrolls the match rect; scrolling the whole page wrap misses in-page position */
    if (highlight.isSearch && highlight.bbox?.length === 4) return;
    const el = pageRefs.current[highlight.page];
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [highlight]);

  if (!file) {
    return (
      <div className="pdf-viewer pdf-viewer--empty" ref={containerRef}>
        <p className="muted">Upload a file to preview.</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="pdf-viewer pdf-viewer--empty" ref={containerRef}>
        <p style={{ color: 'var(--danger)' }}>{error}</p>
      </div>
    );
  }

  if (!pdf) {
    return (
      <div className="pdf-viewer pdf-viewer--empty" ref={containerRef}>
        <p className="muted">Loading file…</p>
      </div>
    );
  }

  return (
    <div className="pdf-viewer" ref={containerRef}>
      {Array.from({ length: numPages }, (_, i) => (
        <PageCanvas
          key={i + 1}
          pdf={pdf}
          pageNumber={i + 1}
          maxPageCssPx={maxPageCssPx}
          scrollTargetsRef={pageRefs}
          highlight={highlight?.page === i + 1 ? highlight : null}
          searchHits={(searchHits || []).filter((h) => h.page === i + 1)}
        />
      ))}
    </div>
  );
});

export default PDFViewer;
