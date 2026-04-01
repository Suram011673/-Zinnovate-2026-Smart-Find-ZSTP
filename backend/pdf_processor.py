"""
PDF text extraction with PyMuPDF + optional Tesseract OCR fallback.
Produces text blocks with page index and bounding boxes (PDF points, origin top-left).
"""
from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import fitz  # PyMuPDF
from PIL import Image
from rapidfuzz import fuzz

logger = logging.getLogger("smart_find")


def _handwriting_ocr_mode() -> bool:
    """Stronger OCR for cursive / forms: always run EasyOCR on weak pages, paragraph mode, mild contrast."""
    return os.environ.get("SMART_FIND_HANDWRITING_MODE", "").lower() in ("1", "true", "yes")


def _ocr_boost_for_ink(handwriting_merge: bool) -> bool:
    """Contrast + EasyOCR emphasis: env and/or per-upload ink option."""
    return _handwriting_ocr_mode() or bool(handwriting_merge)


def _merge_mixed_native_ocr(handwriting_merge: bool) -> bool:
    """
    Full-page raster OCR merged into *every* page that already has native text — very slow on long digital PDFs.

    Enable only via SMART_FIND_MIXED_PAGE_OCR=1 (typed forms with ink overlays). ``handwriting_merge`` no longer
    turns this on; it only boosts weak/scanned pages via :func:`_ocr_boost_for_ink`.
    """
    _ = handwriting_merge  # kept for API compatibility; no longer ties to mixed merge
    return os.environ.get("SMART_FIND_MIXED_PAGE_OCR", "").lower() in ("1", "true", "yes")


def _bbox_iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ax0, ay0, ax1, ay1 = (float(a[i]) for i in range(4))
    bx0, by0, bx1, by1 = (float(b[i]) for i in range(4))
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    aa = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    ba = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = aa + ba - inter
    return inter / union if union > 0 else 0.0


def merge_ocr_with_native_blocks(
    ocr_blocks: list[TextBlock],
    native_blocks: list[TextBlock],
    *,
    iou_threshold: float = 0.32,
) -> list[TextBlock]:
    """
    Prefer OCR boxes (covers ink with no text layer); keep native spans that do not overlap OCR
    (avoids dropping odd PDF text that OCR missed).
    """
    if not ocr_blocks:
        return list(native_blocks)
    out: list[TextBlock] = list(ocr_blocks)
    for nb in native_blocks:
        if not any(_bbox_iou(nb.bbox, ob.bbox) >= iou_threshold for ob in ocr_blocks):
            out.append(nb)
    return _blocks_reading_order(out)


try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore

try:
    import easyocr
except ImportError:
    easyocr = None  # type: ignore

# Lazy EasyOCR reader (heavy); created on first use
_easyocr_reader = None
_tesseract_configured = False


def _ensure_tesseract_cmd() -> None:
    """Point pytesseract at tesseract.exe (PATH, TESSERACT_CMD, or common Windows paths)."""
    global _tesseract_configured
    if pytesseract is None or _tesseract_configured:
        return
    import shutil

    env_cmd = os.environ.get("TESSERACT_CMD", "").strip().strip('"')
    candidates: list[str] = []
    if env_cmd:
        candidates.append(env_cmd)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    user_tesseract = (
        os.path.join(local, "Programs", "Tesseract-OCR", "tesseract.exe") if local else ""
    )
    candidates.extend(
        [
            shutil.which("tesseract") or "",
            os.path.join(pf, "Tesseract-OCR", "tesseract.exe"),
            os.path.join(pfx86, "Tesseract-OCR", "tesseract.exe"),
            user_tesseract,
        ]
    )
    for path in candidates:
        if path and os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            logger.info("Tesseract binary: %s", path)
            _tesseract_configured = True
            return
    logger.warning(
        "Tesseract not found. Install from https://github.com/UB-Mannheim/tesseract/wiki "
        "or set TESSERACT_CMD to the full path to tesseract.exe"
    )
    _tesseract_configured = True  # avoid repeated scans


def _get_easyocr_reader():
    """Singleton EasyOCR reader (English); optional dependency."""
    global _easyocr_reader
    if easyocr is None:
        return None
    if _easyocr_reader is None:
        _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _easyocr_reader


@dataclass
class TextBlock:
    text: str
    page: int  # 1-based
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF coords (points)


def _span_to_bbox(span: dict[str, Any]) -> tuple[float, float, float, float]:
    """PyMuPDF span bbox: keys 'bbox' as [x0,y0,x1,y1]."""
    b = span.get("bbox") or span.get("origin")
    if b is None:
        return (0.0, 0.0, 0.0, 0.0)
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    return (0.0, 0.0, 0.0, 0.0)


def extract_text_blocks_pymupdf(pdf_bytes: bytes) -> list[TextBlock]:
    """Extract text spans with bounding boxes from all pages."""
    blocks: list[TextBlock] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_num = page_index + 1
            d = page.get_text("dict")
            for b in d.get("blocks", []):
                if b.get("type") != 0:
                    continue
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        txt = (span.get("text") or "").strip()
                        if not txt:
                            continue
                        bbox = _span_to_bbox(span)
                        blocks.append(TextBlock(text=txt, page=page_num, bbox=bbox))
    finally:
        doc.close()
    return blocks


def page_has_meaningful_text(page: fitz.Page, min_chars: int = 20) -> bool:
    t = page.get_text("text") or ""
    return len(re.sub(r"\s+", "", t)) >= min_chars


# Short tokens from "example" PDFs — not real form content; ignore for "has native text" decisions
_NOISE_NATIVE_TOKENS = frozenset(
    {"EXAMPLE", "SAMPLE", "DRAFT", "COPY", "CONFIDENTIAL", "WATERMARK"}
)


def _filter_noise_native_blocks(blocks: list[TextBlock]) -> list[TextBlock]:
    """Drop watermark-like spans so we do not skip OCR on scanned/handwritten pages."""
    out: list[TextBlock] = []
    for b in blocks:
        t = b.text.strip()
        if not t:
            continue
        if t.upper() in _NOISE_NATIVE_TOKENS and len(t) <= 14:
            continue
        out.append(b)
    return out


def _native_printable_score(blocks: list[TextBlock]) -> int:
    return sum(len(re.sub(r"\s+", "", b.text or "")) for b in blocks)


def _ocr_min_page_chars() -> int:
    try:
        v = int(os.environ.get("SMART_FIND_MIN_PAGE_CHARS", "22"))
    except ValueError:
        v = 22
    return max(8, min(v, 200))


def _ocr_min_native_score() -> int:
    try:
        v = int(os.environ.get("SMART_FIND_MIN_NATIVE_SCORE", "20"))
    except ValueError:
        v = 20
    return max(0, min(v, 500))


def page_needs_full_raster_ocr(
    page: fitz.Page,
    native_filtered: list[TextBlock],
    *,
    min_page_chars: int | None = None,
    min_native_score: int | None = None,
) -> bool:
    """
    True for scanned forms, image-only pages, or "EXAMPLE"-only overlays — run Tesseract + EasyOCR.
    Thresholds are slightly lower by default so lecture scans with sparse embedded junk still rasterize.
    """
    mpc = min_page_chars if min_page_chars is not None else _ocr_min_page_chars()
    mns = min_native_score if min_native_score is not None else _ocr_min_native_score()
    if not page_has_meaningful_text(page, min_chars=mpc):
        return True
    if _native_printable_score(native_filtered) < mns:
        return True
    return False


def ocr_engine_status() -> dict[str, Any]:
    """Diagnostics for /health/ocr — verify Tesseract + EasyOCR before uploading scans."""
    info: dict[str, Any] = {
        "pytesseract_installed": pytesseract is not None,
        "easyocr_installed": easyocr is not None,
        "ocr_image_enhance": (os.environ.get("SMART_FIND_OCR_IMAGE_ENHANCE") or "0").strip(),
        "handwriting_mode_env": _handwriting_ocr_mode(),
    }
    if pytesseract is None:
        return info
    _ensure_tesseract_cmd()
    cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    info["tesseract_cmd"] = cmd
    info["tesseract_binary_found"] = bool(cmd and os.path.isfile(cmd))
    try:
        ver = pytesseract.get_tesseract_version()
        info["tesseract_version"] = (
            ver.decode("utf-8", errors="replace") if isinstance(ver, bytes) else str(ver)
        )
    except Exception as e:
        info["tesseract_error"] = str(e)
    return info


def _pick_best_ocr_layer(
    tesseract_blocks: list[TextBlock],
    easyocr_blocks: list[TextBlock],
) -> list[TextBlock]:
    """Prefer EasyOCR when it extracts more content (typical for handwriting)."""
    st = sum(len(b.text) for b in tesseract_blocks)
    se = sum(len(b.text) for b in easyocr_blocks)
    ne, nt = len(easyocr_blocks), len(tesseract_blocks)
    if ne > 0 and (se >= st * 0.75 or ne >= max(3, nt)):
        return easyocr_blocks
    if tesseract_blocks:
        return tesseract_blocks
    return easyocr_blocks


def _raster_page_rgb(page: fitz.Page, dpi: int) -> tuple[Image.Image, float, float]:
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    scale_x = page.rect.width / pix.width
    scale_y = page.rect.height / pix.height
    return img, scale_x, scale_y


def _ocr_image_preprocessing(img: Image.Image) -> Image.Image:
    """
    Optional computer-vision style cleanup before Tesseract / EasyOCR (prescriptions, scans).

    SMART_FIND_OCR_IMAGE_ENHANCE:
      0 / off — no change (default, fastest)
      1 / light — contrast + mild unsharp mask
      2 / strong — grayscale round-trip + autocontrast + contrast (slower, noisy photos)

    For handwritten lines, also set SMART_FIND_HANDWRITING_MODE=1 and/or Strong OCR in the UI.
    """
    raw = (os.environ.get("SMART_FIND_OCR_IMAGE_ENHANCE") or "0").strip().lower()
    if raw in ("0", "", "false", "no", "off"):
        return img
    try:
        from PIL import ImageEnhance, ImageFilter, ImageOps

        out = img.convert("RGB")
        if raw in ("1", "light", "true", "yes", "on"):
            out = ImageEnhance.Contrast(out).enhance(1.1)
            out = out.filter(ImageFilter.UnsharpMask(radius=1, percent=70, threshold=2))
            return out
        if raw in ("2", "strong"):
            out = out.convert("L").convert("RGB")
            out = ImageOps.autocontrast(out, cutoff=2)
            out = ImageEnhance.Contrast(out).enhance(1.15)
            return out
    except Exception as e:
        logger.debug("OCR image preprocess skipped: %s", e)
    return img


def _tesseract_text_score(blocks: list[TextBlock]) -> int:
    return sum(len(re.sub(r"\s+", "", b.text or "")) for b in blocks)


def _skip_easyocr_after_tesseract(tesseract_blocks: list[TextBlock]) -> bool:
    """
    EasyOCR is very slow on CPU. If Tesseract already returned enough text, skip EasyOCR.
    Set SMART_FIND_TESS_SUFFICIENT_CHARS=0 to always run both (old behavior).
    Set SMART_FIND_OCR_ALWAYS_EASYOCR=1 to always run EasyOCR after Tesseract.
    """
    if os.environ.get("SMART_FIND_OCR_ALWAYS_EASYOCR", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("SMART_FIND_DISABLE_EASYOCR", "").lower() in ("1", "true", "yes"):
        return True
    try:
        # Skip EasyOCR when Tesseract already returned enough text (major CPU save on uploads).
        min_chars = int(os.environ.get("SMART_FIND_TESS_SUFFICIENT_CHARS", "110"))
    except ValueError:
        min_chars = 72
    if min_chars <= 0:
        return False
    return _tesseract_text_score(tesseract_blocks) >= min_chars


def _tesseract_ocr_config() -> str:
    """PSM 6 = single uniform text block (typical notes/slides); override via SMART_FIND_TESSERACT_CONFIG."""
    raw = (os.environ.get("SMART_FIND_TESSERACT_CONFIG") or "").strip()
    if raw:
        return raw
    return "--oem 3 --psm 6"


def _tesseract_blocks_from_image(
    img: Image.Image, page_num: int, scale_x: float, scale_y: float
) -> list[TextBlock]:
    if pytesseract is None:
        return []
    _ensure_tesseract_cmd()
    data = pytesseract.image_to_data(
        img,
        output_type=pytesseract.Output.DICT,
        config=_tesseract_ocr_config(),
    )
    blocks: list[TextBlock] = []
    n = len(data.get("text", []))
    min_conf = 15
    if os.environ.get("SMART_FIND_TESSERACT_STRICT", "").lower() in ("1", "true", "yes"):
        min_conf = 30

    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        conf = int(data["conf"][i])
        if conf < 0:
            continue
        if conf < min_conf:
            continue
        x, y, w, h = (
            data["left"][i],
            data["top"][i],
            data["width"][i],
            data["height"][i],
        )
        blocks.append(
            TextBlock(
                text=word,
                page=page_num,
                bbox=(
                    x * scale_x,
                    y * scale_y,
                    (x + w) * scale_x,
                    (y + h) * scale_y,
                ),
            )
        )
    return blocks


def _easyocr_blocks_from_image(
    img: Image.Image,
    page_num: int,
    scale_x: float,
    scale_y: float,
    *,
    handwriting_boost: bool = False,
) -> list[TextBlock]:
    reader = _get_easyocr_reader()
    if reader is None:
        return []
    import numpy as np

    arr = np.array(img)
    paragraph = (
        handwriting_boost
        or _handwriting_ocr_mode()
        or os.environ.get("SMART_FIND_EASYOCR_PARAGRAPH", "").lower() in ("1", "true", "yes")
    )
    results = reader.readtext(
        arr,
        detail=1,
        paragraph=paragraph,
        width_ths=0.5,
        height_ths=0.5,
    )
    blocks: list[TextBlock] = []
    for item in results:
        if not item or len(item) < 2:
            continue
        box, text, conf = item[0], item[1], (item[2] if len(item) > 2 else 1.0)
        t = (text or "").strip()
        if not t:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        blocks.append(
            TextBlock(
                text=t,
                page=page_num,
                bbox=(
                    x0 * scale_x,
                    y0 * scale_y,
                    x1 * scale_x,
                    y1 * scale_y,
                ),
            )
        )
    return blocks


def ocr_page_blocks(page: fitz.Page, page_num: int, dpi: int = 240) -> list[TextBlock]:
    """
    Rasterize page and run Tesseract. Returns word-level boxes (approximate).
    Uses TESSERACT_CMD or common install paths on Windows if tesseract is not on PATH.
    """
    if pytesseract is None:
        return []
    try:
        img, sx, sy = _raster_page_rgb(page, dpi)
        img = _ocr_image_preprocessing(img)
        return _tesseract_blocks_from_image(img, page_num, sx, sy)
    except Exception as e:
        logger.warning("Tesseract OCR failed on page %s: %s", page_num, e)
        return []


def easyocr_page_blocks(page: fitz.Page, page_num: int, dpi: int = 240) -> list[TextBlock]:
    """
    Rasterize page and run EasyOCR. Returns line/phrase boxes in PDF coordinates.
    """
    reader = _get_easyocr_reader()
    if reader is None:
        return []
    try:
        img, sx, sy = _raster_page_rgb(page, dpi)
        img = _ocr_image_preprocessing(img)
        return _easyocr_blocks_from_image(img, page_num, sx, sy)
    except Exception as e:
        logger.warning("EasyOCR failed on page %s: %s", page_num, e)
        return []


def extract_blocks_with_ocr_fallback(
    pdf_bytes: bytes,
    *,
    aggressive_ocr: bool = True,
    ocr_dpi: int = 220,
    handwriting_merge: bool = False,
) -> list[TextBlock]:
    """
    Extract text blocks: native PyMuPDF when the page has real text; otherwise rasterize
    and OCR (Tesseract + EasyOCR). **aggressive_ocr** treats low-text / watermark-only
    pages as scanned (handwritten forms, image PDFs) and runs full-page OCR instead of
    keeping useless native spans like ``EXAMPLE``.
    """
    all_blocks: list[TextBlock] = []
    try:
        dpi_use = int(os.environ.get("SMART_FIND_OCR_DPI", str(ocr_dpi)))
    except ValueError:
        dpi_use = ocr_dpi
    dpi_use = max(144, min(dpi_use, 400))

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_num = page_index + 1
            hw_boost = _ocr_boost_for_ink(handwriting_merge)
            hw_mix = _merge_mixed_native_ocr(handwriting_merge)
            native = extract_text_blocks_pymupdf_single_page(doc, page_index)

            if not aggressive_ocr:
                if native and page_has_meaningful_text(page):
                    all_blocks.extend(native)
                    continue
                if native:
                    all_blocks.extend(native)
                    continue
                ocr_blocks = ocr_page_blocks(page, page_num, dpi=dpi_use)
                if not ocr_blocks:
                    ocr_blocks = easyocr_page_blocks(page, page_num, dpi=dpi_use)
                if ocr_blocks:
                    all_blocks.extend(ocr_blocks)
                continue

            native_clean = _filter_noise_native_blocks(native)
            weak = page_needs_full_raster_ocr(page, native_clean)

            if weak:
                img = None
                sx = sy = 1.0
                try:
                    img, sx, sy = _raster_page_rgb(page, dpi_use)
                    img = _ocr_image_preprocessing(img)
                except Exception as e:
                    logger.warning("Raster failed on page %s: %s", page_num, e)
                if img is None:
                    chosen: list[TextBlock] = []
                else:
                    if hw_boost:
                        try:
                            from PIL import ImageEnhance

                            img = ImageEnhance.Contrast(img).enhance(1.12)
                        except Exception:
                            pass
                    te = _tesseract_blocks_from_image(img, page_num, sx, sy)
                    if hw_boost or not _skip_easyocr_after_tesseract(te):
                        ez = _easyocr_blocks_from_image(
                            img, page_num, sx, sy, handwriting_boost=hw_boost
                        )
                        chosen = _pick_best_ocr_layer(te, ez)
                    else:
                        chosen = te
                if chosen:
                    all_blocks.extend(chosen)
                elif native_clean:
                    all_blocks.extend(native_clean)
                else:
                    logger.warning(
                        "Page %s looks scanned/handwritten but OCR returned no text. "
                        "Install Tesseract (PATH) and pip install easyocr; check SMART_FIND / docs.",
                        page_num,
                    )
                continue

            if native_clean:
                if aggressive_ocr and hw_mix:
                    try:
                        img_m, sx_m, sy_m = _raster_page_rgb(page, dpi_use)
                        img_m = _ocr_image_preprocessing(img_m)
                    except Exception as e:
                        logger.warning("Raster (mixed handwriting OCR) failed on page %s: %s", page_num, e)
                        all_blocks.extend(native_clean)
                    else:
                        if hw_boost:
                            try:
                                from PIL import ImageEnhance

                                img_m = ImageEnhance.Contrast(img_m).enhance(1.12)
                            except Exception:
                                pass
                        te_m = _tesseract_blocks_from_image(img_m, page_num, sx_m, sy_m)
                        if hw_boost or not _skip_easyocr_after_tesseract(te_m):
                            ez_m = _easyocr_blocks_from_image(
                                img_m, page_num, sx_m, sy_m, handwriting_boost=hw_boost
                            )
                            chosen_m = _pick_best_ocr_layer(te_m, ez_m)
                        else:
                            chosen_m = te_m
                        all_blocks.extend(
                            merge_ocr_with_native_blocks(chosen_m, native_clean)
                        )
                else:
                    all_blocks.extend(native_clean)
            elif native:
                all_blocks.extend(native)
    finally:
        doc.close()
    return all_blocks


def extract_text_blocks_pymupdf_single_page(doc: fitz.Document, page_index: int) -> list[TextBlock]:
    page = doc[page_index]
    page_num = page_index + 1
    blocks: list[TextBlock] = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip()
                if not txt:
                    continue
                bbox = _span_to_bbox(span)
                blocks.append(TextBlock(text=txt, page=page_num, bbox=bbox))
    return blocks


# --- Dynamic field detection (schema-free) ---

_RX_STANDALONE_DATE = re.compile(
    r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s*$|^\s*\d{4}-\d{2}-\d{2}\s*$"
)
# Used only for fuzzy confidence hints (allows short digit runs inside longer lines).
_RX_STANDALONE_ID = re.compile(
    r"^\s*(?:[A-Z]{2,4}[-\s]?)?\d{5,12}\s*$|^\s*(?:SUB|POL)[-\s]?\d{4,12}\s*$",
    re.I,
)
# Standalone *field* candidates: strict so OCR word boxes like "12345" do not flood the queue.
_RX_STANDALONE_ID_FIELD = re.compile(
    r"^\s*(?:SUB|POL)[-\s]?\d{4,12}\s*$"
    r'|^\s*[A-Z]{2,4}[-\s]\d{4,14}\s*$'  # e.g. AB-1234567 (separator required before digits)
    r"|^\s*\d{8,14}\s*$",  # long numeric only (ignores 5–7 digit crumbs)
    re.I,
)


def _include_form_placeholder_fields() -> bool:
    """Label: ____ empty slots — off by default so navigation targets real answers."""
    return os.environ.get("SMART_FIND_INCLUDE_FORM_PLACEHOLDERS", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _max_standalone_values_per_page() -> int:
    """
    Max standalone date/ID fields per page (OCR digit spam guard). Default 8.
    Set SMART_FIND_MAX_STANDALONE_VALUES_PER_PAGE=0 to disable standalone values entirely.
    """
    try:
        return max(0, int(os.environ.get("SMART_FIND_MAX_STANDALONE_VALUES_PER_PAGE", "8")))
    except ValueError:
        return 8


def _looks_like_title_hyphen_chunk(text: str) -> bool:
    """Avoid pairing title fragments (e.g. 'Inter-Agency') with the next OCR line as a 'value'."""
    t = text.strip()
    if len(t) > 48:
        return False
    return bool(re.match(r"^[A-Z][a-z]{2,}-[A-Z][a-z]{2,}$", t))


def slugify_label(label: str, max_len: int = 56) -> str:
    """Turn arbitrary label text into a stable field_id fragment."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] if s else "field")


def _value_looks_empty(v: str) -> bool:
    t = v.strip()
    if len(t) < 1:
        return True
    core = re.sub(r"[\s_\-\.]", "", t)
    return len(core) < 1


def _is_placeholder_style(v: str) -> bool:
    """Underscores / dashes / dots only — empty slot, not plain text."""
    return _value_looks_empty(v)


def _blocks_reading_order(blocks: list[TextBlock]) -> list[TextBlock]:
    return sorted(blocks, key=lambda b: (b.page, b.bbox[1], b.bbox[0]))


def _vertical_gap_px(a: TextBlock, b: TextBlock) -> float:
    return float(b.bbox[1] - a.bbox[3])


def _column_overlap_ok(a: TextBlock, b: TextBlock, min_overlap: float = 40.0) -> bool:
    ax0, ax1 = float(a.bbox[0]), float(a.bbox[2])
    bx0, bx1 = float(b.bbox[0]), float(b.bbox[2])
    overlap = min(ax1, bx1) - max(ax0, bx0)
    return overlap >= min_overlap


def _looks_like_label_line(text: str) -> bool:
    t = text.strip()
    if len(t) < 2 or len(t) > 52:
        return False
    if ":" in t:
        return False
    letters = sum(1 for c in t if c.isalpha())
    if letters < 2:
        return False
    if sum(1 for c in t if c.isdigit()) > len(t) * 0.6:
        return False
    return True


def _looks_like_plain_value(text: str) -> bool:
    t = text.strip()
    if len(t) < 1 or len(t) > 500:
        return False
    if t.count(":") == 1 and len(t.split(":")[0]) < 30:
        return False
    if _looks_like_label_line(t) and len(t) < 18:
        return False
    return bool(re.search(r"[\w\d]", t))


def _stacked_value_text_ok(nv: str) -> bool:
    """
    Value line under a stacked label (handwriting / answers). More permissive than
    _looks_like_plain_value so names like "Peter Jones" are not rejected as "label-like".
    """
    t = nv.strip()
    if len(t) < 1 or len(t) > 500:
        return False
    if _is_placeholder_style(t):
        return True
    if ":" in t[:22] and len(t) < 36:
        return False
    # Single short token that looks like another label (e.g. "Name") — not the filled value
    if " " not in t and len(t) <= 8 and _looks_like_label_line(t):
        return False
    return bool(re.search(r"[\w\d]", t))


_RX_DATE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"
)
_RX_ID = re.compile(
    r"\b(?:[A-Z]{2,4}[-\s]?)?\d{5,12}\b|\b(?:SUB|POL)[-\s]?\d{4,12}\b",
    re.I,
)


def _confidence_label_value(label: str, value: str) -> float:
    base = 0.72
    if len(label) >= 3:
        base += 0.05
    if _RX_STANDALONE_DATE.search(value) or _RX_DATE.search(value):
        base += 0.12
    if _RX_ID.search(value):
        base += 0.1
    if "@" in value:
        base += 0.08
    return round(min(1.0, base), 3)


def _append_candidate(
    candidates: list[dict[str, Any]],
    *,
    field_id: str,
    name: str,
    label: str,
    value: str,
    page: int,
    bbox: list[float],
    confidence: float,
    detection: str,
) -> None:
    candidates.append(
        {
            "field_id": field_id,
            "name": name[:120],
            "label": label[:120],
            "value": value[:500],
            "bbox": bbox,
            "page": page,
            "confidence": round(min(1.0, float(confidence)), 3),
            "detection": detection,
        }
    )


def _union_bbox(a: tuple[float, ...], b: tuple[float, ...]) -> list[float]:
    return [
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    ]


def detect_fields_dynamic_from_blocks(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    """
    Discover fields without a fixed schema.
    - ``Label: plain text`` and optional ``Label: ____`` placeholders (see SMART_FIND_INCLUDE_FORM_PLACEHOLDERS)
    - ``Label:`` with value on the **next** line/block (plain text, no colon required)
    - **Label line** (no colon) + **value** on next line when layout aligns (forms / stacked text)
    - Standalone dates / IDs (strict ID pattern + per-page cap to avoid OCR digit spam)
    """
    if not blocks:
        return []

    candidates: list[dict[str, Any]] = []
    auto_seq: dict[int, int] = {}
    standalone_by_page: dict[int, int] = {}
    max_standalone = _max_standalone_values_per_page()
    ordered = _blocks_reading_order(blocks)
    pending: tuple[str, TextBlock] | None = None
    consumed: set[int] = set()

    for idx, b in enumerate(ordered):
        if id(b) in consumed:
            continue
        line = b.text.strip()

        # Finish pending "Label:" with value on this line (before skipping empty rows)
        if pending is not None:
            lbl, lb = pending
            pre = line.split(":", 1)[0].strip() if ":" in line[:36] else ""
            early_colon = bool(
                pre and len(pre) >= 3 and any(c.isalpha() for c in pre)
            )
            if early_colon:
                pending = None
            elif len(line) < 1:
                pending = None
            else:
                if _looks_like_plain_value(line) or _is_placeholder_style(line):
                    val = line.strip()[:500]
                    if _is_placeholder_style(val) and not _include_form_placeholder_fields():
                        pending = None
                        continue
                    conf = (
                        0.36
                        if _is_placeholder_style(val)
                        else _confidence_label_value(lbl, val) * 0.92
                    )
                    det = "label_next_placeholder" if _is_placeholder_style(val) else "label_next_plain"
                    _append_candidate(
                        candidates,
                        field_id=slugify_label(lbl),
                        name=lbl,
                        label=lbl,
                        value=val,
                        page=b.page,
                        bbox=list(b.bbox),
                        confidence=conf,
                        detection=det,
                    )
                    consumed.add(id(b))
                pending = None
                if id(b) in consumed:
                    continue

        if len(line) < 1:
            continue

        if ":" in line:
            left, right = line.split(":", 1)
            label = left.strip()
            value = right.strip()
            if len(label) < 2:
                continue
            base_id = slugify_label(label)
            if not value:
                pending = (label, b)
                continue
            if _is_placeholder_style(value):
                if _include_form_placeholder_fields():
                    _append_candidate(
                        candidates,
                        field_id=base_id,
                        name=label,
                        label=label,
                        value=value[:500],
                        page=b.page,
                        bbox=list(b.bbox),
                        confidence=0.40,
                        detection="label_placeholder",
                    )
                continue
            conf = _confidence_label_value(label, value)
            _append_candidate(
                candidates,
                field_id=base_id,
                name=label,
                label=label,
                value=value[:500],
                page=b.page,
                bbox=list(b.bbox),
                confidence=conf,
                detection="label_value",
            )
            continue

        # Adjacent: label row (no colon) + plain value row below
        if (
            _looks_like_label_line(line)
            and not _looks_like_title_hyphen_chunk(line)
            and idx + 1 < len(ordered)
        ):
            nxt = ordered[idx + 1]
            if nxt.page != b.page or id(nxt) in consumed:
                pass
            elif _vertical_gap_px(b, nxt) <= 52 and _column_overlap_ok(b, nxt, 35):
                nv = nxt.text.strip()
                if _stacked_value_text_ok(nv) and (":" not in nv[:20] or len(nv) > 25):
                    _append_candidate(
                        candidates,
                        field_id=slugify_label(line),
                        name=line[:120],
                        label=line[:120],
                        value=nv[:500],
                        page=b.page,
                        bbox=list(nxt.bbox),
                        confidence=0.52,
                        detection="stacked_label_plain",
                    )
                    consumed.add(id(nxt))
                    continue

        # Standalone structured value (no label on same line)
        is_date = bool(_RX_STANDALONE_DATE.match(line))
        is_id = bool(_RX_STANDALONE_ID_FIELD.match(line))
        if is_date or is_id:
            if max_standalone == 0:
                continue
            if standalone_by_page.get(b.page, 0) >= max_standalone:
                continue
            standalone_by_page[b.page] = standalone_by_page.get(b.page, 0) + 1
            auto_seq[b.page] = auto_seq.get(b.page, 0) + 1
            n = auto_seq[b.page]
            slug = slugify_label(line[:40])[:40].strip("_") or "value"
            fid = f"detected_{slug}_p{b.page}_{n}"
            conf = 0.58 if is_date else 0.62
            _append_candidate(
                candidates,
                field_id=fid,
                name=f"Detected: {line[:56]}",
                label="",
                value=line.strip()[:500],
                page=b.page,
                bbox=list(b.bbox),
                confidence=conf,
                detection="standalone_pattern",
            )

    # Dedupe (field_id + page): keep best confidence
    best: dict[tuple[str, int], dict[str, Any]] = {}
    for c in candidates:
        key = (str(c["field_id"]), int(c["page"]))
        prev = best.get(key)
        if prev is None or float(c["confidence"]) > float(prev["confidence"]):
            best[key] = c

    return list(best.values())


# --- Field detection (keyword + fuzzy) ---

FIELD_SPECS: list[dict[str, Any]] = [
    {
        "field_id": "policy_number",
        "label_keywords": ("policy", "policy number", "policy #", "policy no", "member id", "subscriber id"),
        "value_pattern": re.compile(
            r"\b(?:[A-Z]{2,3}[-\s]?)?\d{5,12}\b|\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
            re.I,
        ),
        "priority": 1,
    },
    {
        "field_id": "full_name",
        "label_keywords": ("name", "member name", "patient name", "subscriber name", "full name"),
        "value_pattern": re.compile(
            r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4}\b"
        ),
        "priority": 2,
    },
    {
        "field_id": "date_of_birth",
        "label_keywords": ("dob", "date of birth", "birth date", "birthdate", "d.o.b"),
        "value_pattern": re.compile(
            r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b"
        ),
        "priority": 3,
    },
]


def _best_fuzzy_label(line: str, keywords: tuple[str, ...]) -> tuple[str | None, float]:
    """Return best matching keyword and score 0..100."""
    line_lower = line.lower()
    best_score = 0.0
    best_kw = None
    for kw in keywords:
        score = fuzz.partial_ratio(kw.lower(), line_lower)
        if score > best_score:
            best_score = float(score)
            best_kw = kw
    return best_kw, best_score


def detect_fields_from_blocks(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    """
    Find Policy Number, Name, DOB using keywords + fuzzy + regex values.
    Returns list of dicts: field_id, name, value, bbox, page, confidence
    """
    if not blocks:
        return []

    # Page-level joined text for quick scan
    page_lines: dict[int, list[tuple[TextBlock, str]]] = {}
    for b in blocks:
        page_lines.setdefault(b.page, []).append((b, b.text))

    found: dict[str, dict[str, Any]] = {}

    for spec in FIELD_SPECS:
        fid = spec["field_id"]
        keywords = spec["label_keywords"]
        pattern: re.Pattern[str] = spec["value_pattern"]
        best_conf = 0.0
        best_hit: dict[str, Any] | None = None

        for b in blocks:
            line = b.text
            _, label_score = _best_fuzzy_label(line, keywords)

            # Value on same span or next heuristic: search value pattern in line
            m = pattern.search(line)
            value = m.group(0).strip() if m else None

            # If label fuzzy matches but value is on same line after colon
            if value is None and ":" in line:
                after = line.split(":", 1)[1]
                m2 = pattern.search(after)
                value = m2.group(0).strip() if m2 else after.strip()[:80]

            if label_score < 40 and value is None:
                continue

            # Confidence: blend label fuzzy and optional value match
            conf = (label_score / 100.0) * 0.6 + (0.4 if value else 0.0)
            if value and pattern.search(value):
                conf = min(1.0, conf + 0.15)

            if value is None and label_score >= 55:
                # label only — lower confidence
                conf = label_score / 100.0 * 0.5

            if conf < 0.25:
                continue

            if best_hit is None or conf > best_conf:
                best_conf = conf
                best_hit = {
                    "field_id": fid,
                    "name": fid,
                    "value": value or "(see page)",
                    "bbox": list(b.bbox),
                    "page": b.page,
                    "confidence": round(min(1.0, conf), 3),
                }

        # Also scan full-line concatenation per page for isolated values (e.g. name without label)
        if best_hit is None and fid == "full_name":
            for page, items in page_lines.items():
                for tb, text in items:
                    if re.match(r"^[A-Z][a-z]+\s+[A-Z][a-z]+", text.strip()):
                        best_hit = {
                            "field_id": fid,
                            "name": fid,
                            "value": text.strip()[:120],
                            "bbox": list(tb.bbox),
                            "page": page,
                            "confidence": 0.55,
                        }
                        break

        if best_hit:
            # Keep highest confidence per field_id
            prev = found.get(fid)
            if prev is None or best_hit["confidence"] > prev["confidence"]:
                found[fid] = best_hit

    return list(found.values())


def blocks_to_json(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    """Serialize blocks as {text, bbox, page} for APIs / AI prompts."""
    return [
        {
            "text": b.text,
            "bbox": [float(x) for x in b.bbox],
            "page": b.page,
        }
        for b in blocks
    ]


def process_pdf(
    pdf_bytes: bytes,
    use_ocr_fallback: bool = True,
    aggressive_ocr: bool = True,
    handwriting_merge: bool = False,
) -> tuple[list[TextBlock], list[dict[str, Any]]]:
    """
    Extract blocks and detected fields.
    If use_ocr_fallback is False, only PyMuPDF (faster; no tesseract required).
    aggressive_ocr: full-page OCR on low-text / watermark pages (scanned & handwritten forms).
    handwriting_merge: merge full-page OCR with embedded text on mixed print+ink pages.
    """
    if use_ocr_fallback:
        blocks = extract_blocks_with_ocr_fallback(
            pdf_bytes,
            aggressive_ocr=aggressive_ocr,
            handwriting_merge=handwriting_merge,
        )
    else:
        blocks = extract_text_blocks_pymupdf(pdf_bytes)

    fields = detect_fields_from_blocks(blocks)
    return blocks, fields
