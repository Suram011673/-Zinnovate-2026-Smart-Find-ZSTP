"""
Pick PDFs from a local folder and validate them in one call against the Smart Find API
(same as UI: upload + optional field checklist on every file).

Requires: API running (run-api.bat / uvicorn). Uses only the Python standard library.

Examples:
  python scripts/validate_folder_pdfs.py --dir "C:\\Users\\manugon\\OneDrive - Zinnia\\Desktop\\PDFs" --count 3

  python scripts/validate_folder_pdfs.py --dir "C:\\...\\PDFs" --pick 2,5,9 --checks "Date of birth\\nFirst name"

  python scripts/validate_folder_pdfs.py --dir "C:\\...\\PDFs" --interactive

Env:
  SMART_FIND_API   Base URL (default http://127.0.0.1:8000)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _multipart_body(
    file_paths: list[Path],
    checks: str | None,
) -> tuple[bytes, str]:
    boundary = f"----SmartFind{uuid.uuid4().hex}"
    crlf = b"\r\n"
    chunks: list[bytes] = []

    for p in file_paths:
        data = p.read_bytes()
        disp = (
            f'Content-Disposition: form-data; name="files"; filename="{p.name}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n"
        )
        chunks.append(f"--{boundary}".encode() + crlf)
        chunks.append(disp.encode() + data + crlf)

    if checks is not None and checks.strip():
        disp = 'Content-Disposition: form-data; name="checks"\r\n\r\n'
        chunks.append(f"--{boundary}".encode() + crlf)
        chunks.append(disp.encode() + checks.strip().encode() + crlf)

    chunks.append(f"--{boundary}--".encode() + crlf)
    return b"".join(chunks), boundary


def main() -> int:
    default_dir = os.environ.get(
        "SMART_FIND_PDF_FOLDER",
        r"C:\Users\manugon\OneDrive - Zinnia\Desktop\PDFs",
    )
    ap = argparse.ArgumentParser(description="Validate N PDFs from a folder via Smart Find API")
    ap.add_argument(
        "--dir",
        default=default_dir,
        help=f"Folder containing PDFs (default: SMART_FIND_PDF_FOLDER or your Desktop\\PDFs example)",
    )
    ap.add_argument(
        "--count",
        type=int,
        default=3,
        help="How many PDFs to take when not using --pick or --interactive (default: 3)",
    )
    ap.add_argument(
        "--pick",
        help="Comma-separated 1-based indices after sorting by name, e.g. 1,4,7",
    )
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="List PDFs and type indices to choose (e.g. 2,3,7)",
    )
    ap.add_argument(
        "--checks",
        default="",
        help="Validation lines (use \\n in shell for newlines) or leave empty to skip validation",
    )
    ap.add_argument(
        "--checks-file",
        type=Path,
        help="File with one concept per line (Date of birth, First name, …)",
    )
    ap.add_argument(
        "--api",
        default=os.environ.get("SMART_FIND_API", "http://127.0.0.1:8000").rstrip("/"),
    )
    ap.add_argument("--openai", action="store_true", help="Use OpenAI field merge (needs OPENAI_API_KEY on API)")
    ap.add_argument("--ink-mixed", action="store_true", help="Enable handwriting merge on mixed pages (slower)")
    args = ap.parse_args()

    folder = Path(args.dir).expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 1

    pdfs = sorted(folder.glob("*.pdf"), key=lambda p: p.name.lower())
    if not pdfs:
        print(f"No .pdf files in {folder}", file=sys.stderr)
        return 1

    chosen: list[Path] = []
    if args.interactive:
        print(f"PDFs in {folder}:\n")
        for i, p in enumerate(pdfs, start=1):
            print(f"  [{i:3}] {p.name}")
        raw = input("\nEnter comma-separated numbers to validate (e.g. 1,3,5): ").strip()
        try:
            idxs = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            print("Invalid input.", file=sys.stderr)
            return 1
        for j in idxs:
            if j < 1 or j > len(pdfs):
                print(f"Index out of range: {j}", file=sys.stderr)
                return 1
            chosen.append(pdfs[j - 1])
    elif args.pick:
        try:
            idxs = [int(x.strip()) for x in args.pick.split(",") if x.strip()]
        except ValueError:
            print("--pick must be comma-separated integers", file=sys.stderr)
            return 1
        for j in idxs:
            if j < 1 or j > len(pdfs):
                print(f"Index out of range: {j} (1..{len(pdfs)})", file=sys.stderr)
                return 1
            chosen.append(pdfs[j - 1])
    else:
        n = max(1, args.count)
        chosen = pdfs[:n]

    checks_text: str | None = None
    if args.checks_file:
        checks_text = args.checks_file.read_text(encoding="utf-8", errors="replace")
    elif args.checks:
        checks_text = args.checks.replace("\\n", "\n")

    query = {
        "ocr": "true",
        "aggressive_ocr": "true",
        "handwriting_merge": "true" if args.ink_mixed else "false",
        "dynamic_fields": "true",
        "use_openai": "true" if args.openai else "false",
        "use_gpt_validate": "false",
        "use_transformers": "false",
        "min_match_score": "62",
    }

    body, boundary = _multipart_body(chosen, checks_text)
    url = f"{args.api}/upload-pdf-batch?{urlencode(query)}"
    req = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    print(f"POST {len(chosen)} file(s) -> {url.split('?')[0]}")
    for p in chosen:
        print(f"  - {p.name}")
    if checks_text:
        print("Checks:\n" + "\n".join(f"  - {line}" for line in checks_text.strip().splitlines() if line.strip()))
    else:
        print("(No checks - upload only; batch field verification skipped)")

    try:
        with urlopen(req, timeout=600) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {err_body}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"Connection failed: {e.reason}. Is the API running? ({args.api})", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return 0

    print(json.dumps(data, indent=2)[:8000])
    if len(raw) > 8000:
        print("\n… (truncated)")

    v = data.get("verification") or {}
    summ = v.get("summary")
    if summ:
        print(
            f"\nValidation summary: {summ.get('pdf_count')} PDFs, "
            f"{summ.get('pdfs_with_issues', 0)} with issues, "
            f"{summ.get('issue_lines', 0)} issue lines."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
