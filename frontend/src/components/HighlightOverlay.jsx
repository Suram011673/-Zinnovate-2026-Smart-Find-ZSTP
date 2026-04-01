import { useEffect, useLayoutEffect, useRef, useState } from 'react';

/**
 * Absolute-positioned highlight over a rendered PDF page canvas.
 * bbox: PDF points [x0,y0,x1,y1]; canvasW matches rendered canvas width for that page.
 * Search variant: scrolls the highlight into the PDF scroll parent (not the whole page card).
 */
export default function HighlightOverlay({
  pdf,
  pageNumber,
  bbox,
  canvasW,
  needsReview,
  requiresVerification,
  variant = 'field',
  /** When multiple search hits are shown, only the active match scrolls into view. */
  scrollIntoView: shouldScrollIntoView = true,
  /** Stronger ring for the current Prev/Next match when multiple overlays are visible. */
  searchActive = false,
  /**
   * After Find, wait until the page canvas has painted before scrolling — avoids scrollbar/layout
   * churn that cancels PDF.js render and leaves blank pages.
   */
  deferScrollUntilPainted = false,
}) {
  const [rect, setRect] = useState(null);
  const elRef = useRef(null);

  useEffect(() => {
    if (!bbox || !pdf || !canvasW) {
      setRect(null);
      return;
    }
    let cancelled = false;
    pdf.getPage(pageNumber).then((page) => {
      const v1 = page.getViewport({ scale: 1 });
      const scale = canvasW / v1.width;
      const [x0, y0, x1, y1] = bbox;
      if (cancelled) return;
      /* Search: no 2px floor — avoids a fat box around short tokens; field nav keeps a visible minimum. */
      const minPx = variant === 'search' ? 0.35 : 2;
      const w = Math.max((x1 - x0) * scale, minPx);
      const h = Math.max((y1 - y0) * scale, minPx);
      setRect({
        left: x0 * scale,
        top: y0 * scale,
        width: w,
        height: h,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [bbox, canvasW, pdf, pageNumber, variant]);

  /* block: 'nearest' avoids large scroll jumps that resize the viewer (scrollbar) and cancel PDF.js renders. */
  useLayoutEffect(() => {
    if (variant !== 'search' || !rect || !shouldScrollIntoView) return;
    if (deferScrollUntilPainted && canvasW <= 0) return;

    let cancelled = false;
    let t = null;
    let raf1 = null;
    let raf2 = null;

    const runScroll = () => {
      if (cancelled) return;
      const el = elRef.current;
      if (el) el.scrollIntoView({ behavior: 'auto', block: 'nearest', inline: 'nearest' });
    };

    const chain = () => {
      if (cancelled) return;
      raf1 = requestAnimationFrame(() => {
        if (cancelled) return;
        raf2 = requestAnimationFrame(runScroll);
      });
    };

    if (deferScrollUntilPainted) {
      t = window.setTimeout(chain, 64);
    } else {
      chain();
    }

    return () => {
      cancelled = true;
      if (t != null) window.clearTimeout(t);
      if (raf1 != null) cancelAnimationFrame(raf1);
      if (raf2 != null) cancelAnimationFrame(raf2);
    };
  }, [variant, rect, bbox, shouldScrollIntoView, deferScrollUntilPainted, canvasW]);

  if (!rect) return null;
  const verify = variant === 'field' && (needsReview || requiresVerification);
  const cls =
    variant === 'search'
      ? `pdf-highlight pdf-highlight--search${searchActive ? ' pdf-highlight--search-active' : ''}`
      : `pdf-highlight ${verify ? 'pdf-highlight--review' : ''}`;
  return (
    <div
      ref={elRef}
      className={cls}
      style={{
        position: 'absolute',
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        pointerEvents: 'none',
        scrollMargin: variant === 'search' ? '72px 0' : undefined,
      }}
    />
  );
}
