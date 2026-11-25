"""KB Loan PDF importer. Render + Flask + Firebase Firestore (multi-branch).

Upload window has two options:
  1. Borrower List Uploader      -> LoanMonthlyData (+ MetaData/Loan Data Update)
  2. All Loan Balance Uploader   -> LoanInfo        (+ MetaData/Loan Balance Update)

Document ID rule (both collections): programme code + loan case no,
where the loan case no has its LEADING ZEROS removed (03 -> 3, 01 -> 1, 0101.1 -> 101.1).
Example: 0308 + 03 -> 03083, 0309 + 01 -> 03091.
"""
from __future__ import annotations

import hmac
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
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.utils import secure_filename

COLLECTION = "LoanMonthlyData"
META_COLLECTION = "MetaData"
META_DOCUMENT = "Loan Data Update"
# --- All Loan Balance uploader (second upload option) ---
LOAN_INFO_COLLECTION = "LoanInfo"
LOAN_BALANCE_META_DOCUMENT = "Loan Balance Update"   # separate from "Loan Data Update" (borrower list)
BATCH_SIZE = 400  # Firestore supports up to 500 writes per batch.
MAX_UPLOAD_MB = 40

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# One Render environment variable PER BRANCH:  FIREBASE_KEY_0536 = <service-account JSON of branch 0536>
#                                              FIREBASE_KEY_0522 = <service-account JSON of branch 0522> ...
# (Optional old style also works: FIREBASE_KEYS = {"0536": {...}, "0522": {...}})
firebase_keys: dict = {}
raw_keys = os.environ.get("FIREBASE_KEYS", "").strip()
if raw_keys:
    try:
        parsed = json.loads(raw_keys)
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        firebase_keys.update({str(k): v for k, v in parsed.items()})
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("FIREBASE_KEYS must be valid JSON: {\"0536\": {...}, \"0522\": {...}}") from exc
for _name, _value in os.environ.items():
    if _name.startswith("FIREBASE_KEY_") and _value.strip():
        _code = _name[len("FIREBASE_KEY_"):].strip()
        try:
            firebase_keys[_code] = json.loads(_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{_name} must contain the complete Firebase service-account JSON.") from exc
if not firebase_keys:
    raise RuntimeError("No Firebase key found. Add one environment variable per branch, e.g. FIREBASE_KEY_0536 = service-account JSON.")

CLIENTS: dict[str, "firestore.Client"] = {}
for _code, _cfg in firebase_keys.items():
    _fb_app = firebase_admin.initialize_app(credentials.Certificate(_cfg), name=str(_code))
    CLIENTS[str(_code)] = firestore.client(_fb_app)

# Who may upload: Admin, or any active user whose "permissions" list contains "loan-upload"
# (granted in the web app's Permission Management). The web app sends GPF + password; they are checked
# against the Users collection of the chosen branch, the same way the web app signs in.
UPLOAD_PERMISSION = "loan-upload"
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*").strip()
CORS(app, origins=[o.strip().rstrip("/") for o in ALLOWED_ORIGIN.split(",") if o.strip()] or "*")

# The processing code below uses the name `db`. It now points to the client of the branch that
# the current background thread is working for (set in run_job).
_ctx = threading.local()


class _DbProxy:
    def __getattr__(self, name):
        client = getattr(_ctx, "client", None)
        if client is None:
            raise RuntimeError("No branch selected for this job.")
        return getattr(client, name)


db = _DbProxy()

# Simple brute-force guard: 5 wrong passwords -> 10 minutes lock for that branch + GPF.
FAILS: dict[str, list] = {}
FAILS_LOCK = threading.Lock()


def check_uploader(branch, gpf, password):
    """Return (user_dict, None) when allowed, else (None, (message, http_status))."""
    branch, gpf, password = str(branch or "").strip(), str(gpf or "").strip(), str(password or "")
    client = CLIENTS.get(branch)
    if not client:
        return None, ("Unknown branch.", 400)
    if not gpf or not password:
        return None, ("GPF number and password are required.", 401)
    key = f"{branch}:{gpf}"
    now = time.time()
    with FAILS_LOCK:
        count, last = FAILS.get(key, [0, 0])
        if count >= 5 and now - last < 600:
            return None, ("Too many wrong attempts. Try again after 10 minutes.", 429)
        if now - last >= 600:
            count = 0
    snap = client.collection("Users").document(gpf).get()
    user = snap.to_dict() if snap.exists else None
    ok = bool(user) and hmac.compare_digest(str(user.get("password", "")), password)
    if not ok:
        with FAILS_LOCK:
            FAILS[key] = [count + 1, now]
        return None, ("Wrong GPF number or password.", 401)
    with FAILS_LOCK:
        FAILS.pop(key, None)
    if user.get("status") != "active":
        return None, ("This account is not active.", 403)
    perms = user.get("permissions")
    if user.get("role") != "Admin" and not (isinstance(perms, list) and UPLOAD_PERMISSION in perms):
        return None, ("You do not have permission to upload. Ask the Admin to give you Loan Upload access.", 403)
    return user, None


# In-process job status. A job continues after the upload HTTP response, while this Render instance
# remains alive. A restart stops in-memory jobs, so run gunicorn with ONE worker (see Start Command in SETUP-STEPS.md).
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def clean_cell(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_case_no(value):
    """Remove leading zeros from the loan case no. The decimal part is kept as is.
    03 -> 3, 01 -> 1, 007 -> 7, 0101.1 -> 101.1, 00 -> 0, 342.1 -> 342.1"""
    text = clean_cell(value)
    whole, dot, frac = text.partition(".")
    whole = whole.lstrip("0") or "0"
    return whole + dot + frac


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
    case_no = normalize_case_no(row[1])
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
        "dueNo": parse_number(row[14]),
        "due": parse_number(row[15]),
        "rescheduleAmount": None,  # set below from currentBalance when rescheduled

        "rescheduleNo": parse_number(row[16]),
        "rescheduleDate": normalize_date(row[17]),
        "lastReceivedAmount": parse_number(row[18]),
        "lastReceivedDate": normalize_date(row[19]),
        "notPaidMonths": parse_number(row[20]),
    }
    if record["rescheduleNo"] and record["rescheduleDate"]:
        record["rescheduleAmount"] = record["currentBalance"]
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
        # Leading zeros removed so the document id matches LoanInfo (03 -> 3).
        case_no = normalize_case_no(row[1])
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
            "rescheduleAmount": None,  # set below from currentBalance when rescheduled

            "rescheduleNo": parse_number(row[16]),
            "rescheduleDate": normalize_date(row[17]),
            "lastReceivedAmount": parse_number(row[18]),
            "lastReceivedDate": normalize_date(row[19]),
            "notPaidMonths": parse_number(row[20]),
        }
        # Rule: if rescheduleNo and rescheduleDate both exist, rescheduleAmount = currentBalance.
        if record["rescheduleNo"] and record["rescheduleDate"]:
            record["rescheduleAmount"] = record["currentBalance"]
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

# =====================================================================================
#  ALL LOAN BALANCE UPLOADER  ->  Firestore collection "LoanInfo"
#  Document id = programme code + loan case no (leading zeros removed)
#  (e.g. 0307126, 0101342.1, 0308 + 03 -> 03083)
#  Programme code is NOT printed as a column: it is the middle 4 digits of "Loan Sl. No."
#  (0522-0101-00436 -> 0101).
# =====================================================================================
LB_SERIAL_RE = re.compile(r"^(\d{4})-(\d{4})-(\d{5})$")
LB_CASE_RE = re.compile(r"^\d+(?:\.\d+)?$")
# Column x-boundaries (PDF points, landscape A4 842 wide) of the "Loan Balance" report.
LB_BOUNDS = [("sl", 0, 52), ("case", 52, 78), ("serial", 78, 148), ("name", 148, 256), ("phone", 256, 320),
             ("start", 320, 363), ("duration", 363, 395), ("end", 395, 441),
             ("debit_p", 441, 476), ("debit_i", 476, 521), ("debit_t", 521, 558),
             ("credit_p", 558, 594), ("credit_i", 594, 639), ("credit_t", 639, 676),
             ("bal_p", 676, 712), ("bal_i", 712, 766), ("bal_t", 766, 801), ("class", 801, 10000)]


def lb_column(word):
    center = (word["x0"] + word["x1"]) / 2
    return next((name for name, lo, hi in LB_BOUNDS if lo <= center < hi), None)


def lb_money(cells, key):
    """Blank money cell (e.g. interest not yet charged) is 0 in this report."""
    value = parse_number(cells.get(key, ""))
    return 0 if value is None else value


def parse_loan_balance_pdf(pdf_path):
    """Read the 'Loan Balance' PDF. Raises ValueError unless every row and every total reconciles."""
    rows, warnings, totals = [], [], []
    group_sum = [0, 0, 0]
    report_date = branch = None
    program_context = None

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("PDF contains no pages.")
        page_count = len(pdf.pages)
        for page_number, page in enumerate(pdf.pages, start=1):
            for line in group_page_words(page):
                text = line["text"]
                if page_number == 1:
                    m = re.search(r"\bDate\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text)
                    if m and not report_date:
                        report_date = normalize_date(m.group(1))
                    m = re.match(r"^(.+?)\s+Branch$", text)
                    if m and not branch:
                        branch = m.group(1).strip()
                m = re.search(r"\bLoan Program\s*:\s*(\d{4})", text)
                if m:
                    program_context = m.group(1)
                m = re.match(r"^Total:\s*(\d+)\s+(\d+)\s+(\d+)\s*$", text)
                if m:
                    totals.append({"page": page_number, "values": [int(x) for x in m.groups()],
                                   "rows_since_last": group_sum[:]})
                    group_sum = [0, 0, 0]
                    continue

                serial_word = next((w for w in line["words"] if LB_SERIAL_RE.match(w["text"])
                                    and lb_column(w) == "serial"), None)
                if not serial_word:
                    continue
                cells = {}
                for word in line["words"]:
                    column = lb_column(word)
                    if column:
                        cells[column] = (cells.get(column, "") + " " + word["text"]).strip()
                serial = serial_word["text"]
                branch_code, program_code, running_no = LB_SERIAL_RE.match(serial).groups()
                # Leading zeros removed so the document id matches LoanMonthlyData (03 -> 3).
                case_no = normalize_case_no(cells.get("case", ""))
                where = f"page {page_number}, Loan Sl. No. {serial}"
                if not LB_CASE_RE.match(case_no):
                    warnings.append({"page": page_number, "reason": f"Invalid loan case number at {where}"})
                    continue
                if program_context and program_context != program_code:
                    warnings.append({"page": page_number,
                                     "reason": f"Programme heading {program_context} differs from Loan Sl. No. {serial}"})
                money = {k: lb_money(cells, k) for k in ("debit_p", "debit_i", "debit_t", "credit_p", "credit_i",
                                                          "credit_t", "bal_p", "bal_i", "bal_t")}
                if (money["debit_p"] + money["debit_i"] != money["debit_t"]
                        or money["credit_p"] + money["credit_i"] != money["credit_t"]
                        or money["bal_p"] + money["bal_i"] != money["bal_t"]
                        or money["debit_p"] - money["credit_p"] != money["bal_p"]
                        or money["debit_i"] - money["credit_i"] != money["bal_i"]):
                    warnings.append({"page": page_number, "reason": f"Amounts do not add up at {where}"})
                    continue
                loan_class = blank_to_none(cells.get("class"))
                if loan_class is not None:
                    loan_class = loan_class.upper()
                    if not re.fullmatch(r"[A-Z]{1,4}", loan_class):
                        warnings.append({"page": page_number, "reason": f"Unexpected Class value {loan_class!r} at {where}"})
                        continue
                start_date = normalize_date(cells.get("start"))
                duration = parse_number(cells.get("duration", ""))
                if not start_date or duration is None:
                    warnings.append({"page": page_number, "reason": f"Missing start date or duration at {where}"})
                    continue
                rows.append({
                    "document_id": f"{program_code}{case_no}",
                    "page": page_number,
                    "record": {
                        "loanCaseNo": case_no,
                        "loanSlNo": serial,
                        "loanProgrammeCode": program_code,
                        "loanStartDate": start_date,
                        "loanDurationMonths": duration,
                        "balancePrincipal": money["bal_p"],
                        "balanceInterest": money["bal_i"],
                        "loanClass": loan_class,          # UC / BL ... ; null when the Class column is blank
                    },
                })
                group_sum = [group_sum[0] + money["bal_p"], group_sum[1] + money["bal_i"], group_sum[2] + money["bal_t"]]

    if not rows:
        raise ValueError("No loan rows were parsed. Is this the 'Loan Balance' report? Existing Firestore data was not changed.")
    if warnings:
        raise ValueError(f"{len(warnings)} loan rows could not be read safely. Example: {warnings[:4]}. "
                         "Existing Firestore data was not changed.")
    if len(totals) < 2:
        raise ValueError("The PDF totals were not found (group Total rows and the Grand Total). "
                         "Existing Firestore data was not changed.")

    # Every group "Total:" and the Grand Total printed in the PDF must match what was read.
    grand = totals[-1]["values"]
    group_totals = totals[:-1]
    for item in group_totals:
        if item["rows_since_last"][0] != item["values"][0] or item["rows_since_last"][1] != item["values"][1] \
                or item["rows_since_last"][2] != item["values"][2]:
            raise ValueError(f"Group total mismatch on page {item['page']}: PDF says {item['values']}, "
                             f"rows add up to {item['rows_since_last']}. Existing Firestore data was not changed.")
    read_sum = [sum(r["record"]["balancePrincipal"] for r in rows),
                sum(r["record"]["balanceInterest"] for r in rows)]
    read_sum.append(read_sum[0] + read_sum[1])
    if read_sum != grand:
        raise ValueError(f"Grand total mismatch: PDF says {grand}, rows add up to {read_sum}. "
                         "Existing Firestore data was not changed.")

    seen, duplicates = set(), []
    for item in rows:
        if item["document_id"] in seen:
            duplicates.append(item["document_id"])
        seen.add(item["document_id"])
    if duplicates:
        raise ValueError(f"Duplicate document IDs detected ({len(duplicates)}): {', '.join(duplicates[:10])}. "
                         "Existing Firestore data was not changed.")

    return rows, {"pages": page_count, "records": len(rows), "groups": len(group_totals), "grand": grand,
                  "report_date": report_date, "branch": branch}


def process_loan_balance_file(path, job_id):
    """Validate the whole PDF first (nothing is touched if anything is wrong), then make LoanInfo
    exactly equal to this PDF: new/changed rows are written, old rows that are not in the PDF are deleted."""
    started = time.monotonic()
    try:
        set_job(job_id, status="processing", phase="Parsing PDF")
        records, stats = parse_loan_balance_pdf(path)
        report = [["PDF pages", stats["pages"]], ["Loan rows read", stats["records"]],
                  ["Groups checked", stats["groups"]],
                  ["Balance total (PDF = rows)", f"{stats['grand'][2]:,}"],
                  ["Report date", stats["report_date"] or "—"], ["Branch", stats["branch"] or "—"]]
        set_job(job_id, phase="Validation complete", totalPages=stats["pages"], totalRecords=stats["records"], stats=report)
        col = db.collection(LOAN_INFO_COLLECTION)
        set_job(job_id, phase="Uploading Loan Info", deleted=0, inserted=0)
        inserted = 0
        for start in range(0, len(records), BATCH_SIZE):
            part = records[start:start + BATCH_SIZE]
            batch = db.batch()
            for item in part:
                batch.set(col.document(item["document_id"]), item["record"])     # replaces the whole document
            batch.commit()
            inserted += len(part)
            set_job(job_id, phase="Uploading Loan Info", inserted=inserted, totalRecords=len(records))

        # Old Loan Info that is not in this PDF any more (closed / removed loans) is deleted automatically.
        set_job(job_id, phase="Removing old Loan Info")
        keep = {item["document_id"] for item in records}
        stale, deleted = [], 0
        for doc in col.stream():
            if doc.reference.id not in keep:
                stale.append(doc.reference)
        for start in range(0, len(stale), BATCH_SIZE):
            batch = db.batch()
            for ref in stale[start:start + BATCH_SIZE]:
                batch.delete(ref)
            batch.commit()
            deleted += len(stale[start:start + BATCH_SIZE])
            set_job(job_id, phase="Removing old Loan Info", deleted=deleted)

        db.collection(META_COLLECTION).document(LOAN_BALANCE_META_DOCUMENT).set({
            "updateTime": firestore.SERVER_TIMESTAMP,
            "sourceFile": JOBS[job_id].get("filename"),
            "totalRecords": inserted,
            "totalPages": stats["pages"],
            "reportDate": stats["report_date"],
            "branch": stats["branch"],
        }, merge=True)
        report += [["Loan Info inserted / updated", inserted], ["Old Loan Info deleted (not in this PDF)", deleted]]
        set_job(job_id, status="completed", phase="Completed", deleted=deleted, inserted=inserted, stats=report,
                elapsedSeconds=round(time.monotonic() - started, 2),
                finishedAt=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    except Exception as exc:
        app.logger.exception("Loan balance import job %s failed", job_id)
        set_job(job_id, status="failed", phase="Failed", error=str(exc),
                elapsedSeconds=round(time.monotonic() - started, 2))
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


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


def run_job(target, path, job_id, branch):
    """Runs one import in a background thread, bound to the chosen branch's Firestore."""
    _ctx.client = CLIENTS[branch]
    target(path, job_id)


@app.get("/")
def home():
    return jsonify({"service": "KB Loan PDF importer", "status": "running", "branches": sorted(CLIENTS)})


@app.post("/upload")
def upload_api():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Please choose a PDF file."}), 400
    if Path(file.filename).suffix.lower() != ".pdf":
        return jsonify({"error": "Only PDF files are allowed."}), 400
    branch = str(request.form.get("branch", "")).strip()
    _user, denied = check_uploader(branch, request.form.get("gpf"), request.form.get("password"))
    if denied:
        return jsonify({"error": denied[0]}), denied[1]
    kind = "balance" if request.form.get("kind") == "balance" else "borrower"
    job_id = uuid.uuid4().hex
    safe_name = secure_filename(file.filename) or ("loan-balance.pdf" if kind == "balance" else "borrower-list.pdf")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="kb-loan-", suffix=".pdf", delete=False) as tmp:
            temp_path = tmp.name
            file.save(tmp)
        with JOBS_LOCK:
            JOBS[job_id] = {
                "jobId": job_id, "filename": safe_name, "kind": kind, "branch": branch, "status": "queued", "phase": "Queued",
                "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "updatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "totalPages": 0, "totalRecords": 0, "deleted": 0, "inserted": 0,
            }
        target = process_loan_balance_file if kind == "balance" else process_file
        thread = threading.Thread(target=run_job, args=(target, temp_path, job_id, branch), daemon=True)
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
            return jsonify({"error": "Job not found. The Render service may have restarted, or the job ID is invalid."}), 404
        result = dict(job)
    # Do not expose internal error tracebacks or credentials.
    return jsonify(result)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": f"PDF is too large. Maximum upload size is {MAX_UPLOAD_MB} MB."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
