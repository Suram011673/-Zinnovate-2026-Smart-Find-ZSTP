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
  const ah = Math.abs(hScale);
  const emSize = Math.max(Math.abs(wScale), ah, 1e-6);
  /*
   * item.width is often the horizontal advance for the whole TJ run; on many PDFs it does not match str.length.
   * Uniform char fractions against a bad totalW shift highlights (e.g. "name" → "me"). Fall back to ~0.52 em/glyph.
   */
  const declaredW = Math.abs((item.width ?? 0) * wScale);
  const perCharDecl = declaredW / n;
  const lo = emSize * 0.22;
  const hi = emSize * 1.28;
  const totalW =
    declaredW > 0 && perCharDecl >= lo && perCharDecl <= hi ? declaredW : emSize * 0.52 * n;
  const frac0 = Math.min(1, Math.max(0, startIdx / n));
  const frac1 = Math.min(1, Math.max(0, endIdx / n));
  const xL = m[4] + totalW * frac0;
  const xR = m[4] + totalW * frac1;
  /*
   * PDF.js TextLayer uses baseline − fontHeight×ascentRatio (pdf.mjs #appendText), not full em above baseline.
   */
  const ascentRatio = tight ? 0.82 : 1;
  const descentRatio = tight ? 0.2 : 0;
  const yTop = tight ? m[5] - ah * ascentRatio : m[5] - hScale;
  const yBot = tight ? m[5] + ah * descentRatio : m[5];
  let x0 = Math.min(xL, xR);
  let y0 = Math.min(yTop, yBot);
  let x1 = Math.max(xL, xR);
  let y1 = Math.max(yTop, yBot);
  const spanLen = Math.max(0, endIdx - startIdx);

  /*
   * Only clamp when the match covers (almost) the entire item. Partial-word clamp with implausiblyWideGlyph
   * produced wrong left edges on forms (e.g. "name and" → box from "me…").
   */
  if (tight && spanLen >= 1 && spanLen <= 160) {
    const rawW = Math.abs(x1 - x0);
    const em = emSize;
    const estGlyphW = em * 0.58;
    const capW = estGlyphW * spanLen * 1.12;
    const nearFullRun = n > 1 ? spanLen >= n - 1 : true;
    if (rawW > capW * 1.35 && nearFullRun) {
      const left = Math.min(x0, x1);
      const newW = Math.min(rawW, capW);
      x0 = left;
      x1 = left + newW;
    }
  }

  /* Tight search: minimal floors so the box tracks the query, not extra line padding */
  const minW = tight ? Math.max(0.5, ah * 0.015) : Math.max(2, ah * 0.08);
  const minH = tight ? Math.max(0.5, ah * 0.08) : Math.max(2, ah * 0.85);
  if (x1 - x0 < minW) {
    const mid = (x0 + x1) / 2;
    x0 = mid - minW / 2;
    x1 = mid + minW / 2;
  }
  if (!tight && y1 - y0 < minH) {
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

/** PDF viewport: y grows downward — smaller y is higher on the page. */
export function compareBboxReadingOrder(ba, bb) {
  if (!ba?.length || !bb?.length) return 0;
  const ya = Math.min(ba[1], ba[3]);
  const yb = Math.min(bb[1], bb[3]);
  const xa = Math.min(ba[0], ba[2]);
  const xb = Math.min(bb[0], bb[2]);
  const LINE_TOL = 6;
  if (Math.abs(ya - yb) > LINE_TOL) return ya - yb;
  return xa - xb;
}

/**
 * Sidebar document order → page ascending → top-to-bottom → left-to-right.
 */
export function compareSearchMatchReadingOrder(a, b, orderIdx = {}) {
  const da = orderIdx[a.document_id] ?? 999;
  const db = orderIdx[b.document_id] ?? 999;
  if (da !== db) return da - db;
  const pa = a.page ?? 0;
  const pb = b.page ?? 0;
  if (pa !== pb) return pa - pb;
  return compareBboxReadingOrder(a.bbox, b.bbox);
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
 * Indices where qLower appears as a complete keyword or phrase (Unicode-aware word boundaries).
 * Does not match a substring inside a longer word (e.g. "cat" in "catch").
 */
function phraseBoundaryIndices(fullLow, qLower) {
  const out = [];
  if (!qLower.length || !fullLow.includes(qLower)) return out;
  let pos = 0;
  while (pos <= fullLow.length - qLower.length) {
    const idx = fullLow.indexOf(qLower, pos);
    if (idx < 0) break;
    const before = idx === 0 ? '' : fullLow[idx - 1];
    const after = idx + qLower.length >= fullLow.length ? '' : fullLow[idx + qLower.length];
    if (!isWordChar(before) && !isWordChar(after)) out.push(idx);
    pos = idx + 1;
  }
  return out;
}

function levenshtein(a, b) {
  const m = a.length;
  const n = b.length;
  if (!m) return n;
  if (!n) return m;
  const dp = new Array(n + 1);
  for (let j = 0; j <= n; j += 1) dp[j] = j;
  for (let i = 1; i <= m; i += 1) {
    let prev = dp[0];
    dp[0] = i;
    for (let j = 1; j <= n; j += 1) {
      const cur = dp[j];
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      dp[j] = Math.min(dp[j] + 1, dp[j - 1] + 1, prev + cost);
      prev = cur;
    }
  }
  return dp[n];
}

/** 0–100 similarity for typo-tolerant fallback (whole token vs keyword). */
function keywordSimilarityRatio(a, b) {
  const al = (a || '').toLowerCase();
  const bl = (b || '').toLowerCase();
  if (al === bl) return 100;
  const d = levenshtein(al, bl);
  return Math.round(100 * (1 - d / Math.max(al.length, bl.length, 1)));
}

function fuzzyRatioThreshold(qLen) {
  if (qLen <= 3) return 88;
  if (qLen <= 6) return 82;
  return 86;
}

function maxLenDeltaForFuzzy(qLen) {
  return Math.max(2, Math.floor(qLen / 2) + 1);
}

/**
 * Merge PDF text items on one line into a string + span offsets. Inserts a single space between runs only
 * when both sides are word characters and neither side already has whitespace (avoids "foo  bar" index drift).
 */
function mergeLineToSpans(line) {
  let full = '';
  const spans = [];
  for (const { item } of line) {
    const s = item.str || '';
    if (!s) continue;
    if (full.length) {
      const last = full[full.length - 1];
      const first = s[0];
      const needGap =
        isWordChar(last) &&
        isWordChar(first) &&
        !/\s/u.test(last) &&
        !/\s/u.test(first);
      if (needGap) full += ' ';
    }
    const start = full.length;
    full += s;
    spans.push({ item, start, end: full.length });
  }
  return { full, spans };
}

/**
 * Group text items into rough reading-order lines (same baseline band), merge text, search.
 */
/** Merged-line search: full keyword/phrase with word boundaries only. */
function findInPageLinesBoundary(items, pageNum, viewport, qLower) {
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
    const { full, spans } = mergeLineToSpans(line);
    if (!full.length) continue;
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
      matches.push({ page: pageNum, bbox, snippet: sn, match_type: 'exact' });
    };

    for (const idx of phraseBoundaryIndices(fullLow, qLower)) {
      recordMatch(idx, qLower.length);
    }
  }

  return matches;
}

function bboxDedupeKey(page, bbox) {
  if (!bbox || bbox.length < 4) return `${page}|`;
  const q = bbox.map((x) => Math.round(Number(x) / 4));
  return `${page}|${q.join(',')}`;
}

function bboxIouEmbedded(a, b) {
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

function bboxIntersectionAreaEmbedded(a, b) {
  if (!a?.length || !b?.length) return 0;
  const [ax0, ay0, ax1, ay1] = a;
  const [bx0, by0, bx1, by1] = b;
  const ix0 = Math.max(ax0, bx0);
  const iy0 = Math.max(ay0, by0);
  const ix1 = Math.min(ax1, bx1);
  const iy1 = Math.min(ay1, by1);
  return Math.max(0, ix1 - ix0) * Math.max(0, iy1 - iy0);
}

function bboxOverlapOfSmallerEmbedded(a, b) {
  if (!a?.length || !b?.length) return 0;
  const inter = bboxIntersectionAreaEmbedded(a, b);
  const aa = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
  const ba = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
  const small = Math.min(aa, ba);
  return small > 1e-6 ? inter / small : 0;
}

/** Same reading location: tight box vs merged line, or split text items. */
function bboxSameEmbeddedHit(a, b) {
  if (!a?.length || !b?.length) return false;
  if (bboxIouEmbedded(a, b) > 0.32) return true;
  const ov = bboxOverlapOfSmallerEmbedded(a, b);
  if (ov > 0.5) return true;
  const acx = (a[0] + a[2]) / 2;
  const acy = (a[1] + a[3]) / 2;
  const bcx = (b[0] + b[2]) / 2;
  const bcy = (b[1] + b[3]) / 2;
  const dist = Math.hypot(acx - bcx, acy - bcy);
  const aw = Math.max(0, a[2] - a[0]);
  const ah = Math.max(0, a[3] - a[1]);
  const bw = Math.max(0, b[2] - b[0]);
  const bh = Math.max(0, b[3] - b[1]);
  const rW = aw > 1e-6 && bw > 1e-6 ? Math.min(aw, bw) / Math.max(aw, bw) : 0;
  const rH = ah > 1e-6 && bh > 1e-6 ? Math.min(ah, bh) / Math.max(ah, bh) : 0;
  if (dist < 20 && rW > 0.4 && rH > 0.4) return true;
  if (dist < 32 && ov > 0.2 && rW > 0.18 && rH > 0.22) return true;
  if (bboxIouEmbedded(a, b) > 0.1 && dist < 24) return true;
  return false;
}

/**
 * After collecting hits on one page, drop geometry duplicates (keep tighter boxes first).
 */
function dedupeEmbeddedHitsOnPage(hits) {
  if (hits.length < 2) return hits;
  const area = (h) => {
    const b = h.bbox;
    if (!b || b.length < 4) return Infinity;
    const w = Math.abs(b[2] - b[0]);
    const hgt = Math.abs(b[3] - b[1]);
    const a = w * hgt;
    return Number.isFinite(a) && a > 0 ? a : Infinity;
  };
  const sorted = [...hits].sort((x, y) => area(x) - area(y));
  const out = [];
  for (const h of sorted) {
    if (out.some((k) => bboxSameEmbeddedHit(k.bbox, h.bbox))) continue;
    out.push(h);
  }
  out.sort((a, b) => compareBboxReadingOrder(a.bbox, b.bbox));
  return out;
}

function pushHitUnique(hit, matches, seen) {
  const k = `${bboxDedupeKey(hit.page, hit.bbox)}|${(hit.snippet || '').replace(/\s+/g, ' ').trim().slice(0, 48).toLowerCase()}`;
  if (seen.has(k)) return;
  seen.add(k);
  matches.push(hit);
}

/** Min digit count (after stripping non-digits from query) to run flexible embedded-number match. */
const MIN_DIGIT_QUERY_LEN = 3;

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
    const { full, spans } = mergeLineToSpans(line);
    if (!full.length) continue;
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

/** Per text item: boundary-only matches (handles odd PDF splits where line merge missed). */
function collectItemBoundaryMatches(items, pageNum, viewport, qLower, seen, matches) {
  for (const item of items) {
    if (!('str' in item) || !item.str) continue;
    const low = item.str.toLowerCase();
    for (const idx of phraseBoundaryIndices(low, qLower)) {
      const bbox = subStringBBox(item, idx, idx + qLower.length, viewport, { tight: true });
      const sn = item.str.slice(Math.max(0, idx - 12), idx + qLower.length + 12);
      pushHitUnique({ page: pageNum, bbox, snippet: sn, match_type: 'exact' }, matches, seen);
    }
  }
}

/**
 * Fuzzy fallback: single-token query vs whole word tokens in items (typo tolerance), not substrings.
 */
function collectFuzzyTokenMatches(items, pageNum, viewport, rawQuery, seen, matches) {
  const q = (rawQuery || '').trim();
  if (!q || /\s/.test(q) || q.length < 2) return;
  const qLower = q.toLowerCase();
  const th = fuzzyRatioThreshold(q.length);
  const maxDelta = maxLenDeltaForFuzzy(q.length);

  for (const item of items) {
    if (!('str' in item) || !item.str) continue;
    const str = item.str;
    let i = 0;
    const n = str.length;
    while (i < n) {
      while (i < n && !isWordChar(str[i])) i += 1;
      let j = i;
      while (j < n && isWordChar(str[j])) j += 1;
      if (j > i) {
        const tok = str.slice(i, j);
        const tLow = tok.toLowerCase();
        if (tLow.length >= 2 && Math.abs(tLow.length - qLower.length) <= maxDelta) {
          const r = keywordSimilarityRatio(q, tok);
          if (r >= th) {
            const bbox = subStringBBox(item, i, j, viewport, { tight: true });
            const sn = str.slice(Math.max(0, i - 12), j + 12);
            pushHitUnique({ page: pageNum, bbox, snippet: sn, match_type: 'fuzzy_token' }, matches, seen);
          }
        }
      }
      i = j;
    }
  }
}

/**
 * Search embedded PDF text: complete keyword/phrase with word boundaries, then fuzzy whole-token
 * fallback (single-token queries), then formatted-digit match. No partial-word substring hits.
 *
 * @returns {Array<{ page: number, bbox: number[], snippet: string, match_type?: string }>}
 */
export async function findTextInPdf(pdf, rawQuery) {
  const q = rawQuery.trim();
  const qLower = q.toLowerCase();
  if (!qLower || !pdf) return [];
  const matches = [];

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    const page = await pdf.getPage(pageNum);
    const viewport = page.getViewport({ scale: 1 });
    const { items } = await page.getTextContent();

    const pageHits = [];
    const pageSeen = new Set();
    const countStart = 0;

    for (const hit of findInPageLinesBoundary(items, pageNum, viewport, qLower)) {
      pushHitUnique(hit, pageHits, pageSeen);
    }

    if (pageHits.length === countStart) {
      collectItemBoundaryMatches(items, pageNum, viewport, qLower, pageSeen, pageHits);
    }

    if (pageHits.length === countStart) {
      collectFuzzyTokenMatches(items, pageNum, viewport, q, pageSeen, pageHits);
    }

    const flexRe = buildFlexibleDigitSequenceRegex(digitsOnly(q));
    if (flexRe && pageHits.length === countStart) {
      collectDigitFormattedLineMatches(items, pageNum, viewport, flexRe, pageSeen, pageHits);
      if (pageHits.length === countStart) {
        collectDigitFormattedItemMatches(items, pageNum, viewport, flexRe, pageSeen, pageHits);
      }
    }

    matches.push(...dedupeEmbeddedHitsOnPage(pageHits));
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
