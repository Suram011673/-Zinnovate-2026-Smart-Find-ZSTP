import * as pdfjsLib from 'pdfjs-dist';

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString();

/**
 * Map a substring range inside one PDF.js text item to an axis-aligned bbox
 * in the same coordinate system as page.getViewport({ scale: 1 }) — matches HighlightOverlay.
 */
/**
 * @param {{ tight?: boolean }} [opts] tight=true for search hits: highlight the query only, not inflated to line height.
 */
function subStringBBox(item, startIdx, endIdx, viewport, opts = {}) {
  const tight = opts.tight === true;
  const str = item.str || '';
  const n = Math.max(str.length, 1);
  const t = item.transform;
  const m = pdfjsLib.Util.transform(viewport.transform, t);
  const wScale = Math.hypot(m[0], m[1]) || 1;
  const hScale = Math.hypot(m[2], m[3]) || wScale;
  const totalW = item.width * wScale;
  const frac0 = Math.min(1, Math.max(0, startIdx / n));
  const frac1 = Math.min(1, Math.max(0, endIdx / n));
  const xL = m[4] + totalW * frac0;
  const xR = m[4] + totalW * frac1;
  const yTop = m[5] - hScale;
  const yBot = m[5];
  let x0 = Math.min(xL, xR);
  let y0 = Math.min(yTop, yBot);
  let x1 = Math.max(xL, xR);
  let y1 = Math.max(yTop, yBot);
  const ah = Math.abs(hScale);
  /* Tight search: tiny floor only when bbox collapses; avoid full-line-height padding */
  const minW = tight ? Math.max(1, ah * 0.02) : Math.max(2, ah * 0.08);
  const minH = tight ? Math.max(1, ah * 0.18) : Math.max(2, ah * 0.85);
  if (x1 - x0 < minW) {
    const mid = (x0 + x1) / 2;
    x0 = mid - minW / 2;
    x1 = mid + minW / 2;
  }
  if (y1 - y0 < minH) {
    const mid = (y0 + y1) / 2;
    y0 = mid - minH / 2;
    y1 = mid + minH / 2;
  }
  return [x0, y0, x1, y1];
}

function unionBBox(boxes) {
  if (!boxes.length) return [0, 0, 0, 0];
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const b of boxes) {
    if (!b || b.length < 4) continue;
    x0 = Math.min(x0, b[0], b[2]);
    y0 = Math.min(y0, b[1], b[3]);
    x1 = Math.max(x1, b[0], b[2]);
    y1 = Math.max(y1, b[1], b[3]);
  }
  if (!Number.isFinite(x0)) return [0, 0, 0, 0];
  return [x0, y0, x1, y1];
}

/** Baseline Y and left X in viewport space for sorting / line grouping */
function itemXY(item, viewport) {
  const t = item.transform;
  const m = pdfjsLib.Util.transform(viewport.transform, t);
  return { x: m[4], y: m[5], m };
}

/** PDFs with large type / headings need a looser same-line band than body text */
/* Tighter band reduces merging unrelated table rows / labels on nearby baselines */
const LINE_Y_TOLERANCE = 6;

/** Letters, numbers, combining marks — for whole-word boundaries (not ASCII-only). */
function isWordChar(ch) {
  return ch ? /[\p{L}\p{N}\p{M}]/u.test(ch) : false;
}

/**
 * Indices where qLower appears as a whole word (Unicode-aware boundaries).
 */
function wholeWordIndices(fullLow, qLower) {
  const out = [];
  if (!qLower.length || !fullLow.includes(qLower)) return out;
  let pos = 0;
  while (pos <= fullLow.length - qLower.length) {
    const idx = fullLow.indexOf(qLower, pos);
    if (idx < 0) break;
    const before = idx === 0 ? '' : fullLow[idx - 1];
    const after = idx + qLower.length >= fullLow.length ? '' : fullLow[idx + qLower.length];
    const okBefore = !isWordChar(before);
    const okAfter = !isWordChar(after);
    if (okBefore && okAfter) out.push(idx);
    pos = idx + 1;
  }
  return out;
}

/**
 * Group text items into rough reading-order lines (same baseline band), merge text, search.
 */
function findInPageLines(items, pageNum, viewport, qLower, useWholeWord) {
  const matches = [];
  const textItems = items.filter((it) => 'str' in it && it.str && it.str.trim());
  if (!textItems.length) return matches;

  const withMeta = textItems.map((item) => {
    const { x, y } = itemXY(item, viewport);
    return { item, x, y };
  });
  withMeta.sort((a, b) => {
    if (Math.abs(a.y - b.y) < LINE_Y_TOLERANCE) return a.x - b.x;
    return b.y - a.y;
  });

  const lines = [];
  let cur = [];
  let lineY = null;
  for (const row of withMeta) {
    if (!cur.length) {
      cur = [row];
      lineY = row.y;
      continue;
    }
    if (Math.abs(row.y - lineY) < LINE_Y_TOLERANCE) {
      cur.push(row);
    } else {
      lines.push(cur);
      cur = [row];
      lineY = row.y;
    }
  }
  if (cur.length) lines.push(cur);

  for (const line of lines) {
    let full = '';
    const spans = [];
    for (const { item } of line) {
      const s = item.str || '';
      if (full.length) full += ' ';
      const start = full.length;
      full += s;
      spans.push({ item, start, end: full.length });
    }
    const fullLow = full.toLowerCase();

    const recordMatch = (idx, len) => {
      const boxes = [];
      const absStart = idx;
      const absEnd = idx + len;
      for (const sp of spans) {
        const ov0 = Math.max(absStart, sp.start);
        const ov1 = Math.min(absEnd, sp.end);
        if (ov1 <= ov0) continue;
        const i0 = ov0 - sp.start;
        const i1 = ov1 - sp.start;
        boxes.push(subStringBBox(sp.item, i0, i1, viewport, { tight: true }));
      }
      if (!boxes.length) return;
      const bbox = unionBBox(boxes);
      const sn = full.slice(Math.max(0, idx - 12), idx + len + 12);
      matches.push({ page: pageNum, bbox, snippet: sn });
    };

    if (useWholeWord) {
      for (const idx of wholeWordIndices(fullLow, qLower)) {
        recordMatch(idx, qLower.length);
      }
    } else {
      let from = 0;
      while (from < fullLow.length) {
        const idx = fullLow.indexOf(qLower, from);
        if (idx < 0) break;
        recordMatch(idx, qLower.length);
        from = idx + 1;
      }
    }
  }

  return matches;
}

function bboxDedupeKey(page, bbox) {
  if (!bbox || bbox.length < 4) return `${page}|`;
  const q = bbox.map((x) => Math.round(Number(x)));
  return `${page}|${q.join(',')}`;
}

function pushHitUnique(hit, matches, seen) {
  const k = `${bboxDedupeKey(hit.page, hit.bbox)}|${(hit.snippet || '').slice(0, 48)}`;
  if (seen.has(k)) return;
  seen.add(k);
  matches.push(hit);
}

/** Min digit count (after stripping non-digits from query) to run flexible embedded-number match. */
const MIN_DIGIT_QUERY_LEN = 4;

/** Unicode-aware strip to digit characters only (query may include any separators). */
function digitsOnly(s) {
  return (s || '').replace(/\D/gu, '');
}

/**
 * Regex built from the query's digit sequence: between each digit, any run of non-digits is allowed.
 * Boundaries: not immediately inside a longer digit run (lookbehind / lookahead).
 */
function buildFlexibleDigitSequenceRegex(digitOnlyQuery) {
  const qd = digitOnlyQuery;
  if (!qd || qd.length < MIN_DIGIT_QUERY_LEN) return null;
  if (!/^\d+$/u.test(qd)) return null;
  const core = [...qd].join('\\D*');
  return new RegExp(`(?<!\\d)${core}(?!\\d)`, 'gu');
}

function recordMergedCharRange(pageNum, viewport, full, spans, absStart, absEndExcl, seen, matches, matchType) {
  const boxes = [];
  for (const sp of spans) {
    const ov0 = Math.max(absStart, sp.start);
    const ov1 = Math.min(absEndExcl, sp.end);
    if (ov1 <= ov0) continue;
    const i0 = ov0 - sp.start;
    const i1 = ov1 - sp.start;
    boxes.push(subStringBBox(sp.item, i0, i1, viewport, { tight: true }));
  }
  if (!boxes.length) return;
  const bbox = unionBBox(boxes);
  const sn = full.slice(Math.max(0, absStart - 12), absEndExcl + 12);
  pushHitUnique({ page: pageNum, bbox, snippet: sn, match_type: matchType }, matches, seen);
}

function collectDigitFormattedLineMatches(items, pageNum, viewport, flexRe, seen, matches) {
  if (!flexRe) return;
  const textItems = items.filter((it) => 'str' in it && it.str && it.str.trim());
  if (!textItems.length) return;

  const withMeta = textItems.map((item) => {
    const { x, y } = itemXY(item, viewport);
    return { item, x, y };
  });
  withMeta.sort((a, b) => {
    if (Math.abs(a.y - b.y) < LINE_Y_TOLERANCE) return a.x - b.x;
    return b.y - a.y;
  });

  const lines = [];
  let cur = [];
  let lineY = null;
  for (const row of withMeta) {
    if (!cur.length) {
      cur = [row];
      lineY = row.y;
      continue;
    }
    if (Math.abs(row.y - lineY) < LINE_Y_TOLERANCE) {
      cur.push(row);
    } else {
      lines.push(cur);
      cur = [row];
      lineY = row.y;
    }
  }
  if (cur.length) lines.push(cur);

  for (const line of lines) {
    let full = '';
    const spans = [];
    for (const { item } of line) {
      const s = item.str || '';
      if (full.length) full += ' ';
      const start = full.length;
      full += s;
      spans.push({ item, start, end: full.length });
    }
    const re = new RegExp(flexRe.source, flexRe.flags);
    for (const m of full.matchAll(re)) {
      const absStart = m.index;
      const absEndExcl = m.index + m[0].length;
      recordMergedCharRange(
        pageNum,
        viewport,
        full,
        spans,
        absStart,
        absEndExcl,
        seen,
        matches,
        'digit_formatted',
      );
    }
  }
}

function collectDigitFormattedItemMatches(items, pageNum, viewport, flexRe, seen, matches) {
  if (!flexRe) return;
  for (const item of items) {
    if (!('str' in item) || !item.str) continue;
    const text = item.str;
    const re = new RegExp(flexRe.source, flexRe.flags);
    for (const m of text.matchAll(re)) {
      const absStart = m.index;
      const absEndExcl = m.index + m[0].length;
      const bbox = subStringBBox(item, absStart, absEndExcl, viewport, { tight: true });
      const sn = text.slice(Math.max(0, absStart - 12), absEndExcl + 12);
      pushHitUnique({ page: pageNum, bbox, snippet: sn, match_type: 'digit_formatted' }, matches, seen);
    }
  }
}

/** Substring search inside each raw text item (handles glued tokens, odd splits). */
function collectItemSubstringMatches(items, pageNum, viewport, qLower, seen, matches) {
  for (const item of items) {
    if (!('str' in item) || !item.str) continue;
    const low = item.str.toLowerCase();
    let from = 0;
    while (from < low.length) {
      const idx = low.indexOf(qLower, from);
      if (idx < 0) break;
      const bbox = subStringBBox(item, idx, idx + qLower.length, viewport, { tight: true });
      const sn = item.str.slice(Math.max(0, idx - 12), idx + qLower.length + 12);
      pushHitUnique({ page: pageNum, bbox, snippet: sn }, matches, seen);
      from = idx + 1;
    }
  }
}

/**
 * Search embedded PDF text on all pages (selectable / digital text layer).
 * Prefers whole-word matches for long queries, then falls back to substring (lecture PDFs often
 * omit spaces or use encodings where ASCII-only boundaries miss).
 *
 * @returns {Array<{ page: number, bbox: number[], snippet: string }>}
 */
export async function findTextInPdf(pdf, rawQuery) {
  const q = rawQuery.trim();
  const qLower = q.toLowerCase();
  if (!qLower || !pdf) return [];
  const useWholeWord = qLower.length >= 4;
  const matches = [];
  const seen = new Set();

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    const page = await pdf.getPage(pageNum);
    const viewport = page.getViewport({ scale: 1 });
    const { items } = await page.getTextContent({ normalizeWhitespace: true });

    const countStart = matches.length;

    if (useWholeWord) {
      for (const hit of findInPageLines(items, pageNum, viewport, qLower, true)) {
        pushHitUnique(hit, matches, seen);
      }

      if (matches.length === countStart) {
        for (const item of items) {
          if (!('str' in item) || !item.str) continue;
          const low = item.str.toLowerCase();
          for (const idx of wholeWordIndices(low, qLower)) {
            const bbox = subStringBBox(item, idx, idx + qLower.length, viewport, { tight: true });
            const sn = item.str.slice(Math.max(0, idx - 12), idx + qLower.length + 12);
            pushHitUnique({ page: pageNum, bbox, snippet: sn }, matches, seen);
          }
        }
      }

      /* Still nothing: substring on merged lines + per item (COA-style notes, glued words). */
      if (matches.length === countStart) {
        for (const hit of findInPageLines(items, pageNum, viewport, qLower, false)) {
          pushHitUnique(hit, matches, seen);
        }
      }
      if (matches.length === countStart) {
        collectItemSubstringMatches(items, pageNum, viewport, qLower, seen, matches);
      }
    } else {
      for (const hit of findInPageLines(items, pageNum, viewport, qLower, false)) {
        pushHitUnique(hit, matches, seen);
      }
      if (matches.length === countStart) {
        collectItemSubstringMatches(items, pageNum, viewport, qLower, seen, matches);
      }
    }

    const flexRe = buildFlexibleDigitSequenceRegex(digitsOnly(q));
    if (flexRe && matches.length === countStart) {
      collectDigitFormattedLineMatches(items, pageNum, viewport, flexRe, seen, matches);
      if (matches.length === countStart) {
        collectDigitFormattedItemMatches(items, pageNum, viewport, flexRe, seen, matches);
      }
    }
  }
  return matches;
}

/**
 * Embedded text search across several PDF {@link File}s (e.g. all uploaded docs).
 * Each match includes `document_id`, `filename`, `source: 'embedded'`.
 *
 * @param {Array<{ document_id: string, file: File, filename?: string }>} entries
 * @returns {Promise<Array<{ page: number, bbox: number[], snippet: string, document_id: string, filename: string, source: string, match_type: string }>>}
 */
export async function findTextInMultiplePdfs(entries, rawQuery) {
  const q = (rawQuery || '').trim();
  if (!q || !entries?.length) return [];
  const out = [];
  for (const { document_id, file, filename } of entries) {
    if (!file || !document_id) continue;
    let doc;
    try {
      const data = await file.arrayBuffer();
      doc = await pdfjsLib.getDocument({ data }).promise;
    } catch {
      continue;
    }
    try {
      const matches = await findTextInPdf(doc, q);
      const fn = filename || file.name || '';
      for (const m of matches) {
        out.push({
          ...m,
          document_id,
          filename: fn,
          source: 'embedded',
          match_type: m.match_type || 'exact',
        });
      }
    } finally {
      try {
        await doc?.destroy?.();
      } catch {
        /* ignore */
      }
    }
    /* Let the viewer’s PDF.js render finish a frame before opening the next document (same worker). */
    await new Promise((r) => requestAnimationFrame(r));
  }
  return out;
}
