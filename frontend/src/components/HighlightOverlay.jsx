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
      const w = Math.max((x1 - x0) * scale, 2);
      const h = Math.max((y1 - y0) * scale, 2);
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
  }, [bbox, canvasW, pdf, pageNumber]);

  /* block: 'nearest' avoids large scroll jumps that resize the viewer (scrollbar) and cancel PDF.js renders. */
  useLayoutEffect(() => {
    if (variant !== 'search' || !rect) return;
    const id = requestAnimationFrame(() => {
      const el = elRef.current;
      if (!el) return;
      el.scrollIntoView({ behavior: 'auto', block: 'nearest', inline: 'nearest' });
    });
    return () => cancelAnimationFrame(id);
  }, [variant, rect, bbox]);

  if (!rect) return null;
  const verify = variant === 'field' && (needsReview || requiresVerification);
  const cls =
    variant === 'search'
      ? 'pdf-highlight pdf-highlight--search'
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
