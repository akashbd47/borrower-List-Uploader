"""KB Loan PDF importer. Render + Flask + Firebase Firestore."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pdfplumber
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, jsonify, request, render_template_string
from werkzeug.utils import secure_filename

COLLECTION = "LoanMonthlyData"
META_COLLECTION = "MetaData"
META_DOCUMENT = "Loan Data Update"
BATCH_SIZE = 400  # Firestore supports up to 500 writes per batch.
MAX_UPLOAD_MB = 40

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Render Environment Variable: FIREBASE_KEY = complete Firebase service-account JSON.
firebase_json = os.environ.get("FIREBASE_KEY", "").strip()
if not firebase_json:
    raise RuntimeError("FIREBASE_KEY environment variable is missing. Add the Firebase service-account JSON in Render Environment.")
try:
    firebase_dict = json.loads(firebase_json)
except json.JSONDecodeError as exc:
    raise RuntimeError("FIREBASE_KEY must contain valid JSON for the Firebase service account.") from exc

if not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(firebase_dict))
db = firestore.client()

# In-process job status. A job continues after the upload HTTP response, while this Render
# instance remains alive. A process restart stops in-memory jobs; see README for this limit.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def clean_cell(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def blank_to_none(value):
    value = clean_cell(value)
    if not value or value.upper() in {"N/A", "NA", "NIL", "NONE", "-", "—"}:
        return None
    return value


def parse_number(value):
    """Return int/float when valid; preserve blank/N/A as None."""
    value = blank_to_none(value)
    if value is None:
        return None
    value = value.replace(",", "").strip()
    if value.startswith("."):
        value = "0" + value
    if value.startswith("-."):
        value = value.replace("-.", "-0.", 1)
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return None
    number = float(value) if "." in value else int(value)
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return number


def normalize_date(value):
    value = blank_to_none(value)
    if value is None:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%m-%Y")
        except ValueError:
            pass
    return value  # Do not fabricate a date if the source format is unusual.


def normalize_bd_phone(value):
    """Return valid Bangladesh mobile as local 11 digits; never guess invalid numbers."""
    raw = clean_cell(value)
    if not raw:
        return None, False
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00880"):
        digits = digits[2:]
    if digits.startswith("8801") and len(digits) == 13:
        digits = "0" + digits[3:]
    elif digits.startswith("01") and len(digits) == 11:
        pass
    elif len(digits) == 11 and digits.startswith("1"):
        digits = "0" + digits
    else:
        return None, True
    if re.fullmatch(r"01[3-9]\d{8}", digits):
        return digits, False
    return None, True


def _text_lines_with_y(page):
    """Group PDF words by visual line so program/status context is tied to each table."""
    words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines = []
    for word in words:
        if not lines or abs(word["top"] - lines[-1]["top"]) > 2.5:
            lines.append({"top": word["top"], "words": [word]})
        else:
            lines[-1]["words"].append(word)
    for line in lines:
        line["text"] = " ".join(w["text"] for w in sorted(line["words"], key=lambda w: w["x0"]))
    return lines


def context_for_table(lines, table_top, previous_program, previous_program_name, previous_status, previous_account):
    program = previous_program
    program_name = previous_program_name
    status = previous_status
    account = previous_account
    for line in lines:
        if line["top"] >= table_top:
            break
        text = line["text"]
        # Program heading is visually on one line in this report, but tolerate extra spaces.
        if re.search(r"\bProgram\b", text, re.I):
            match = re.search(r"\b(\d{4})\s+(.+)$", text)
            if match:
                program = match.group(1).strip()
                program_name = clean_cell(match.group(2))
        # Account code heading, e.g. Overdue( 100103 ) or Regular( 100101 ).
        match = re.search(r"\b(Overdue|Regular)\s*\(\s*(\d{6})\s*\)", text, re.I)
        if match:
            status = match.group(1).title()
            account = match.group(2)
    return program, program_name, status, account


def is_header_row(row):
    joined = " ".join(clean_cell(v).lower() for v in row if v is not None)
    return "case no" in joined or ("borrower name" in joined and "phone" in joined)


def row_to_record(row, program_code, program_name, loan_status, account_code, page_number):
    # This PDF's extract_tables layout has 21 columns. Road/Block/House (6) and Ward No (8)
    # are deliberately never read or written.
    if len(row) < 21:
        return None, "wrong_column_count"
    sl_no = clean_cell(row[0])
    case_no = clean_cell(row[1])
    customer_name = clean_cell(row[2])
    if not re.fullmatch(r"\d+", sl_no) or not re.fullmatch(r"\d+(?:\.\d+)?", case_no):
        return None, "not_data_row"
    if not program_code or not account_code or not customer_name:
        return None, "missing_required_context_or_name"

    phone, phone_invalid = normalize_bd_phone(row[9])
    record = {
        "customerName": customer_name,
        "loanCaseNo": case_no,
        "loanProgrammeCode": program_code,
        "loanProgrammeName": program_name or None,
        "accountCode": account_code,
        "loanStatus": loan_status,
        "fatherName": blank_to_none(row[3]),
        "spouseName": blank_to_none(row[4]),
        "village": blank_to_none(row[5]),
        "union": blank_to_none(row[7]),
        "overdueDate": normalize_date(row[10]),
        "installmentAmount": parse_number(row[11]),
        "receivedAmount": parse_number(row[12]),
        "currentBalance": parse_number(row[13]),
        "due": parse_number(row[14]),
        "rescheduleAmount": parse_number(row[15]),
        "rescheduleNo": parse_number(row[16]),
        "rescheduleDate": normalize_date(row[17]),
        "lastReceivedAmount": parse_number(row[18]),
        "lastReceivedDate": normalize_date(row[19]),
        "notPaidMonths": parse_number(row[20]),
    }
    # Keep phone field absent when source phone is invalid/unfinished; do not invent digits.
    if phone:
        record["phone"] = phone
    return {"record": record, "phone_invalid": phone_invalid, "page": page_number, "sl_no": sl_no}, None


# Column boundaries measured from the source report's fixed table layout.
# roadBlockHouse and wardNo are intentionally excluded from the output record.
PDF_COLUMN_BOUNDS = [13, 45, 81, 148.5, 220.5, 292.5, 351, 414, 477, 499.5,
                     562.5, 612, 652.5, 693, 742.5, 775, 810, 835, 882, 920, 967, 1005]


def group_page_words(page):
    words = sorted(page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False),
                   key=lambda word: (word["top"], word["x0"]))
    lines = []
    for word in words:
        if not lines or abs(word["top"] - lines[-1]["top"]) > 2.5:
            lines.append({"top": word["top"], "words": [word]})
        else:
            lines[-1]["words"].append(word)
    for line in lines:
        line["words"].sort(key=lambda word: word["x0"])
        line["text"] = " ".join(word["text"] for word in line["words"])
    return lines


def detect_row_starts(lines):
    """Find each real borrower row by the Sl. No and Loan Case No x positions."""
    starts = []
    for line_index, line in enumerate(lines):
        sl_no = case_no = None
        for word in line["words"]:
            center = (word["x0"] + word["x1"]) / 2
            if center < 45 and re.fullmatch(r"\d+", word["text"]):
                sl_no = word["text"]
            elif 45 <= center < 81 and re.fullmatch(r"\d+(?:\.\d+)?", word["text"]):
                case_no = word["text"]
        if sl_no is not None and case_no is not None and not re.search(
                r"Loan Program|Program Wise Total|Account Code Wise Total", line["text"], re.I):
            starts.append({"line_index": line_index, "top": line["top"],
                           "sl_no": sl_no, "case_no": case_no})
    return starts


# A word that fills (almost) the whole cell width and continues on the next line with a
# lowercase fragment was broken in the middle by the report (e.g. "Norosingahp" + "ur").
MID_WORD_EDGE = 6


def column_for_word(word):
    center = (word["x0"] + word["x1"]) / 2
    return next((idx for idx in range(21)
                 if PDF_COLUMN_BOUNDS[idx] <= center < PDF_COLUMN_BOUNDS[idx + 1]), None)


def add_line_to_cells(col_lines, line):
    """Put the words of one visual line into their column buckets (one fragment per line)."""
    per_column = {}
    for word in line["words"]:
        column = column_for_word(word)
        if column is not None:
            per_column.setdefault(column, []).append(word)
    for column, words in per_column.items():
        col_lines[column].append(words)


def join_cell(fragments, column):
    """Join wrapped lines of one cell. No space is added when a word was split mid-word."""
    if not fragments:
        return ""
    right_edge = PDF_COLUMN_BOUNDS[column + 1]
    text = " ".join(w["text"] for w in fragments[0])
    for prev, cur in zip(fragments, fragments[1:]):
        glue = " "
        if len(prev) == 1 and prev[0]["x1"] >= right_edge - MID_WORD_EDGE and cur[0]["text"][:1].islower():
            glue = ""
        text += glue + " ".join(w["text"] for w in cur)
    return text.strip()


def extract_row_lines(lines, start, next_start_top):
    """Collect all wrapped lines for one borrower, bucketed by the PDF's fixed column x-coordinates."""
    end_top = next_start_top
    start_index = start["line_index"]
    for line in lines[start_index + 1:]:
        if line["top"] >= end_top:
            break
        if re.search(r"Account Code Wise Total|Loan Program Wise Total|Loan Program\s+\d{4}|"
                     r"Loan Status & Account Code|Software Developed", line["text"], re.I):
            end_top = min(end_top, line["top"])
            break
    col_lines = [[] for _ in range(21)]
    for line in lines[start_index:]:
        if line["top"] >= end_top:
            break
        add_line_to_cells(col_lines, line)
    return col_lines


def row_cells(col_lines, start):
    cells = [join_cell(col_lines[idx], idx) for idx in range(21)]
    cells[0] = start["sl_no"]
    cells[1] = start["case_no"]
    return cells


def extract_row_cells(lines, start, next_start_top):
    return row_cells(extract_row_lines(lines, start, next_start_top), start)


def continuation_lines(lines, limit_index):
    """Lines printed under the table header but before the first borrower row of a page.
    They are the wrapped remainder of the last borrower row of the previous page."""
    header_index = None
    for idx in range(limit_index):
        if re.search(r"Case No", lines[idx]["text"]):
            header_index = idx
    if header_index is None:
        return []
    skip = re.compile(r"Software Developed|Page \d+ of \d+|Loan Program|Loan Status|"
                      r"Account Code Wise Total|Print On|Supervisor Name", re.I)
    return [ln for ln in lines[header_index + 1:limit_index] if not skip.search(ln["text"])]


def parse_pdf(pdf_path):
    records = []
    parse_warnings = []
    invalid_phones = 0
    candidate_count = 0
    raw_rows = []  # one entry per borrower row, in PDF order, across all pages

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("PDF contains no pages.")
        for page_number, page in enumerate(pdf.pages, start=1):
            lines = group_page_words(page)
            starts = detect_row_starts(lines)
            candidate_count += len(starts)

            # Wrapped text of a row that was split across the page break belongs to the previous page's last row.
            lead = continuation_lines(lines, starts[0]["line_index"] if starts else len(lines))
            if lead and raw_rows:
                for line in lead:
                    add_line_to_cells(raw_rows[-1]["col_lines"], line)

            program_code = program_name = loan_status = account_code = None
            line_cursor = 0
            for row_index, start in enumerate(starts):
                # Carry the latest programme/status context forward until the PDF changes it.
                for line in lines[line_cursor:start["line_index"] + 1]:
                    text = line["text"]
                    match = re.search(r"\bLoan Program\s+(\d{4})\s*(.*)$", text, re.I)
                    if match:
                        program_code = match.group(1)
                        program_name = clean_cell(match.group(2)) or None
                    match = re.search(r"\b(Overdue|Regular)\s*\(\s*(\d{6})\s*\)", text, re.I)
                    if match:
                        loan_status = match.group(1).title()
                        account_code = match.group(2)
                line_cursor = start["line_index"] + 1
                next_top = starts[row_index + 1]["top"] if row_index + 1 < len(starts) else float("inf")
                raw_rows.append({
                    "col_lines": extract_row_lines(lines, start, next_top),
                    "start": start, "page": page_number,
                    "program_code": program_code, "program_name": program_name,
                    "loan_status": loan_status, "account_code": account_code,
                })

    for raw in raw_rows:
        start = raw["start"]
        page_number = raw["page"]
        program_code, program_name = raw["program_code"], raw["program_name"]
        loan_status, account_code = raw["loan_status"], raw["account_code"]
        row = row_cells(raw["col_lines"], start)
        case_no = clean_cell(row[1])
        customer_name = clean_cell(row[2])
        if not re.fullmatch(r"\d+(?:\.\d+)?", case_no):
            parse_warnings.append({"page": page_number, "reason": f"Invalid loan case number at Sl. {start['sl_no']}"})
            continue
        if not (program_code and account_code and customer_name):
            parse_warnings.append({"page": page_number, "reason": f"Missing programme/account/name at Sl. {start['sl_no']} Case {case_no}"})
            continue

        phone, phone_invalid = normalize_bd_phone(row[9])
        record = {
            "customerName": customer_name,
            "loanCaseNo": case_no,
            "loanProgrammeCode": program_code,
            "loanProgrammeName": program_name,
            "accountCode": account_code,
            "loanStatus": loan_status,
            "fatherName": blank_to_none(row[3]),
            "spouseName": blank_to_none(row[4]),
            "village": blank_to_none(row[5]),
            "union": blank_to_none(row[7]),
            "overdueDate": normalize_date(row[10]),
            "installmentAmount": parse_number(row[11]),
            "receivedAmount": parse_number(row[12]),
            "currentBalance": parse_number(row[13]),
            "dueNo": parse_number(row[14]),
            "due": parse_number(row[15]),
            "rescheduleAmount": parse_number(row[15]),
            "rescheduleNo": parse_number(row[16]),
            "rescheduleDate": normalize_date(row[17]),
            "lastReceivedAmount": parse_number(row[18]),
            "lastReceivedDate": normalize_date(row[19]),
            "notPaidMonths": parse_number(row[20]),
        }
        if phone:
            record["phone"] = phone
        elif phone_invalid:
            invalid_phones += 1
        records.append({"record": record, "phone_invalid": phone_invalid,
                        "page": page_number, "sl_no": start["sl_no"],
                        "document_id": f"{program_code}{case_no}"})

    if not records:
        raise ValueError("No borrower rows were parsed. Existing Firestore data was not changed.")
    # Never delete existing Firestore data if any detected borrower row was lost or malformed.
    if len(records) != candidate_count:
        raise ValueError(f"Parser found {candidate_count} borrower-row starts but built {len(records)} complete records. "
                         "Existing Firestore data was not changed. Check the PDF layout or parser logs.")

    seen = set()
    duplicate_ids = []
    for item in records:
        doc_id = item["document_id"]
        if doc_id in seen:
            duplicate_ids.append(doc_id)
        seen.add(doc_id)
    if duplicate_ids:
        sample = ", ".join(duplicate_ids[:10])
        raise ValueError(f"Duplicate document IDs detected ({len(duplicate_ids)}): {sample}. Existing Firestore data was not changed.")

    missing_balance = sum(1 for item in records if item["record"].get("currentBalance") is None)
    if missing_balance > max(3, int(len(records) * 0.05)):
        raise ValueError(f"Too many records have no Current Balance ({missing_balance}/{len(records)}). Existing Firestore data was not changed.")
    if parse_warnings:
        raise ValueError(f"{len(parse_warnings)} borrower rows have missing or invalid required fields. "
                         f"Example: {parse_warnings[:5]}. Existing Firestore data was not changed.")

    return records, {"pages": len(pdf.pages), "records": len(records), "candidate_rows": candidate_count,
                     "invalid_phones": invalid_phones, "parse_warnings": parse_warnings,
                     "missing_balance": missing_balance}

def set_job(job_id, **changes):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(changes)
            JOBS[job_id]["updatedAt"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"


def delete_existing_documents(col, job_id):
    deleted = 0
    # Stream in chunks and commit no more than 400 writes per batch.
    refs = []
    for doc in col.stream():
        refs.append(doc.reference)
        if len(refs) >= BATCH_SIZE:
            batch = db.batch()
            for ref in refs:
                batch.delete(ref)
            batch.commit()
            deleted += len(refs)
            refs.clear()
            set_job(job_id, phase="Deleting old data", deleted=deleted)
    if refs:
        batch = db.batch()
        for ref in refs:
            batch.delete(ref)
        batch.commit()
        deleted += len(refs)
    return deleted


def insert_records(records, job_id):
    col = db.collection(COLLECTION)
    inserted = 0
    for start in range(0, len(records), BATCH_SIZE):
        batch_records = records[start:start + BATCH_SIZE]
        batch = db.batch()
        for item in batch_records:
            batch.set(col.document(item["document_id"]), item["record"])
        batch.commit()
        inserted += len(batch_records)
        set_job(job_id, phase="Uploading new data", inserted=inserted, totalRecords=len(records))
    return inserted


def process_file(path, job_id):
    started = time.monotonic()
    try:
        set_job(job_id, status="processing", phase="Parsing PDF")
        records, stats = parse_pdf(path)
        set_job(job_id, phase="Validation complete", totalPages=stats["pages"], totalRecords=stats["records"],
                candidateRows=stats["candidate_rows"], invalidPhones=stats["invalid_phones"], parseWarnings=stats["parse_warnings"],
                missingBalance=stats["missing_balance"])
        # Preserve the existing Delete -> Insert behavior, but only after parsing and validation succeed.
        set_job(job_id, phase="Deleting old data", deleted=0, inserted=0)
        deleted = delete_existing_documents(db.collection(COLLECTION), job_id)
        set_job(job_id, phase="Uploading new data", deleted=deleted, inserted=0)
        inserted = insert_records(records, job_id)
        db.collection(META_COLLECTION).document(META_DOCUMENT).set({
            "updateTime": firestore.SERVER_TIMESTAMP,
            "sourceFile": JOBS[job_id].get("filename"),
            "totalRecords": inserted,
            "totalPages": stats["pages"],
        }, merge=True)
        set_job(job_id, status="completed", phase="Completed", deleted=deleted, inserted=inserted,
                elapsedSeconds=round(time.monotonic() - started, 2), finishedAt=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    except Exception as exc:
        app.logger.exception("PDF import job %s failed", job_id)
        set_job(job_id, status="failed", phase="Failed", error=str(exc),
                elapsedSeconds=round(time.monotonic() - started, 2))
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/")
def home():
    return render_template_string(PAGE_HTML)


@app.post("/upload")
def upload_api():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Please choose a PDF file."}), 400
    if Path(file.filename).suffix.lower() != ".pdf":
        return jsonify({"error": "Only PDF files are allowed."}), 400
    job_id = uuid.uuid4().hex
    safe_name = secure_filename(file.filename) or "borrower-list.pdf"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="kb-loan-", suffix=".pdf", delete=False) as tmp:
            temp_path = tmp.name
            file.save(tmp)
        with JOBS_LOCK:
            JOBS[job_id] = {
                "jobId": job_id, "filename": safe_name, "status": "queued", "phase": "Queued",
                "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "updatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "totalPages": 0, "totalRecords": 0, "deleted": 0, "inserted": 0,
            }
        thread = threading.Thread(target=process_file, args=(temp_path, job_id), daemon=True)
        thread.start()
        return jsonify({"jobId": job_id, "status": "queued", "message": "PDF uploaded. Background processing started."}), 202
    except Exception as exc:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
        app.logger.exception("Could not start PDF import")
        return jsonify({"error": "Upload failed: " + str(exc)}), 500


@app.get("/status/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found. The Render process may have restarted, or the job ID is invalid."}), 404
        result = dict(job)
    # Do not expose internal error tracebacks or credentials.
    return jsonify(result)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": f"PDF is too large. Maximum upload size is {MAX_UPLOAD_MB} MB."}), 413


PAGE_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KB Loan PDF Importer</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#182230;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Arial}.wrap{max-width:650px;margin:32px auto;padding:16px}.card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:24px;box-shadow:0 8px 28px #1822300d}h1{font-size:24px;margin:0 0 6px}.muted{color:#667085}.file{display:block;width:100%;margin:20px 0;padding:12px;border:1px dashed #98a2b3;border-radius:10px;background:#fafbff}button{width:100%;padding:13px;border:0;border-radius:10px;background:#2457d6;color:white;font-weight:700;font-size:16px}button:disabled{opacity:.6}.panel{display:none;margin-top:20px;padding:16px;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0}.status{font-weight:700;margin-bottom:8px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.stat{padding:12px;background:white;border:1px solid #e2e8f0;border-radius:10px}.stat small{display:block;color:#667085}.stat b{font-size:20px;overflow-wrap:anywhere}.error{color:#b42318}.success{color:#067647}.warning{color:#9a6700}.note{font-size:13px;margin-top:18px;color:#667085}@media(max-width:450px){.wrap{margin:10px auto}.card{padding:18px}.grid{grid-template-columns:1fr 1fr}}
</style></head><body><main class="wrap"><section class="card"><h1>KB Loan PDF Importer</h1><div class="muted">Upload borrower list PDF. Parsing and Firestore import run in the background.</div>
<form id="form"><input class="file" id="file" type="file" accept=".pdf,application/pdf" required><button id="btn" type="submit">Upload PDF & Start Import</button></form>
<div class="panel" id="panel"><div class="status" id="status">Waiting...</div><div class="muted" id="phase"></div><div class="grid" id="stats"></div><div id="details" class="note"></div></div>
<div class="note">Do not upload the same PDF again while a job is running. Existing LoanMonthlyData is replaced only after parsing and validation pass. Keep the Render service running until the job completes.</div></section></main>
<script>
const form=document.getElementById('form'),btn=document.getElementById('btn'),panel=document.getElementById('panel');let pollTimer=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function stat(label,value){return `<div class="stat"><small>${esc(label)}</small><b>${esc(value??0)}</b></div>`}
form.addEventListener('submit',async e=>{e.preventDefault();if(pollTimer)clearTimeout(pollTimer);const f=document.getElementById('file').files[0];if(!f){alert('Please select a PDF.');return}btn.disabled=true;panel.style.display='block';document.getElementById('status').textContent='Uploading PDF…';document.getElementById('phase').textContent='Please keep this page open to see the live status.';document.getElementById('stats').innerHTML='';document.getElementById('details').textContent='';try{const fd=new FormData();fd.append('file',f);const res=await fetch('/upload',{method:'POST',body:fd});const data=await res.json();if(!res.ok)throw new Error(data.error||'Upload failed');document.getElementById('status').textContent='Upload complete';document.getElementById('phase').textContent='Background job started. You may wait here for the final report.';poll(data.jobId)}catch(err){document.getElementById('status').innerHTML='<span class="error">Upload failed</span>';document.getElementById('details').textContent=err.message;btn.disabled=false}});
async function poll(id){try{const res=await fetch('/status/'+encodeURIComponent(id),{cache:'no-store'});const j=await res.json();if(!res.ok)throw new Error(j.error||'Status unavailable');document.getElementById('status').innerHTML=j.status==='completed'?'<span class="success">Import completed</span>':j.status==='failed'?'<span class="error">Import failed</span>':'Processing…';document.getElementById('phase').textContent='Current step: '+(j.phase||j.status);document.getElementById('stats').innerHTML=stat('PDF pages',j.totalPages)+stat('Parsed records',j.totalRecords)+stat('Old records deleted',j.deleted)+stat('New records inserted',j.inserted)+stat('Invalid phones',j.invalidPhones)+stat('Missing balances',j.missingBalance)+stat('Parse warnings',(j.parseWarnings||[]).length)+stat('Elapsed seconds',j.elapsedSeconds??'—');let details='File: '+(j.filename||'')+(j.error?'\nError: '+j.error:'');if(j.parseWarnings&&j.parseWarnings.length)details+='\nWarnings (first 8): '+j.parseWarnings.slice(0,8).map(x=>'Page '+x.page+': '+x.reason).join('; ');document.getElementById('details').textContent=details;if(j.status==='completed'||j.status==='failed'){btn.disabled=false;btn.textContent='Upload Another PDF';return}}catch(err){document.getElementById('status').innerHTML='<span class="error">Status check failed</span>';document.getElementById('details').textContent=err.message;btn.disabled=false;return}pollTimer=setTimeout(()=>poll(id),2000)}
</script></body></html>'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
