import { useEffect, useLayoutEffect, useRef, useState } from 'react';

/**
 * Absolute-positioned highlight over a rendered PDF page canvas.
 * bbox: PDF user space [x0,y0,x1,y1] (same as page.getViewport({ scale: 1 })).
 * mapWidth/mapHeight: on-screen canvas size (getBoundingClientRect) — must match PDF→pixel mapping
 * so highlights sit on the glyphs (not floating in empty margin).
 */
export default function HighlightOverlay({
  pdf,
  pageNumber,
  bbox,
  mapWidth,
  mapHeight,
  needsReview,
  requiresVerification,
  variant = 'field',
  scrollIntoView: shouldScrollIntoView = true,
  searchActive = false,
  deferScrollUntilPainted = false,
}) {
  const [rect, setRect] = useState(null);
  const elRef = useRef(null);

  useEffect(() => {
    if (!bbox || !pdf || !mapWidth || !mapHeight) {
      setRect(null);
      return;
    }
    let cancelled = false;
    pdf.getPage(pageNumber).then((page) => {
      const v1 = page.getViewport({ scale: 1 });
      let scaleX = mapWidth / v1.width;
      let scaleY = mapHeight / v1.height;
      /* Same aspect as page → one scale avoids 1px drift between X/Y from ResizeObserver. */
      const arPdf = v1.width / v1.height;
      const arMap = mapWidth / mapHeight;
      if (Number.isFinite(arPdf) && arPdf > 0 && Math.abs(arMap - arPdf) / arPdf < 0.02) {
        scaleX = scaleY = (scaleX + scaleY) / 2;
      }
      const [x0, y0, x1, y1] = bbox;
      if (cancelled) return;
      const minW = variant === 'search' ? 1 : 2;
      const minH = variant === 'search' ? 1.35 : 2;
      const w = Math.max((x1 - x0) * scaleX, minW);
      const h = Math.max((y1 - y0) * scaleY, minH);
      const left = x0 * scaleX;
      const top = y0 * scaleY;
      setRect({ left, top, width: w, height: h });
    });
    return () => {
      cancelled = true;
    };
  }, [bbox, mapWidth, mapHeight, pdf, pageNumber, variant]);

  useLayoutEffect(() => {
    if (variant !== 'search' || !rect || !shouldScrollIntoView) return;
    if (deferScrollUntilPainted && mapWidth <= 0) return;

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
  }, [variant, rect, bbox, shouldScrollIntoView, deferScrollUntilPainted, mapWidth]);

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
