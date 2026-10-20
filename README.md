# KB Loan PDF Importer

A Flask web app for importing Karmasangsthan Bank borrower-list PDFs into Firebase Firestore. It uses PDF word coordinates (rather than relying only on PDF table extraction), so records continue to be found when a table is split across multiple fragments or borrower names/addresses wrap onto later lines.

## Files

- `app.py` — upload UI, coordinate-based PDF parser, validation, background processing, Firestore delete/insert, job status and final report.
- `requirements.txt` — Python dependencies.
- `README.md` — deployment instructions.

## Render deployment

1. Upload only these three files to a GitHub repository.
2. Create a Render **Python Web Service** connected to that repository.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app --workers 1 --threads 4 --timeout 120`
5. In Render Environment, add `FIREBASE_KEY` containing the complete Firebase service-account JSON. Never commit the service-account key to GitHub.
6. Open the Render service URL and upload the PDF.

## Firestore behavior

- Replaces `LoanMonthlyData` only after parsing and validation complete.
- Deletes old documents in batches and inserts new records in batches of up to 400.
- Updates `MetaData/Loan Data Update` with `updateTime`, `sourceFile`, `totalRecords`, and `totalPages`.
- Document ID is the exact string concatenation of `loanProgrammeCode + loanCaseNo`, e.g. `0101` + `57.1` = `010157.1`.
- Required integration fields retain the exact names `customerName`, `loanCaseNo`, and `loanProgrammeCode`.
- `roadBlockHouse` and `wardNo` are not saved.
- Valid Bangladesh mobile numbers are normalized to local 11-digit format. Invalid numbers are omitted from `phone` and counted in the report; digits are never guessed.
- Dates are formatted as `DD-MM-YYYY` when the source date is parseable.

## Parser validation

The supplied `16.04.2026..pdf` was tested locally with this parser: 38 pages, **543 borrower rows detected and 543 records built**, no missing required programme/account/name fields, no duplicate document IDs, and no missing `currentBalance` values. Eleven phone values were identified as invalid/incomplete and are not stored as `phone` values. This is a local PDF parsing test; it is not proof of a successful live Firestore upload. Review the report after deployment before relying on the data.

Before replacing Firestore data, the app checks that every detected row start becomes a record, required fields are present, IDs are unique, and `currentBalance` is populated within the configured tolerance. If these checks fail, the old collection is not deleted.

## Background processing limitations

The upload endpoint returns a Job ID and runs parsing/upload in a background thread; the page polls `/status/<jobId>` for progress. This is an in-process thread, not a durable queue. If Render restarts/replaces the process during a job, the job may stop and its in-memory status will be lost. Keep the service running until the import completes, and do not submit the same PDF twice while a job is running. A durable queue/worker and persistent job store are needed for restart-safe jobs.

## Security and production note

The Firebase service account must have read/write permission for the target collections. Keep the key secret. Test first with a non-production Firebase project; a successful parser test does not guarantee every value in a different PDF layout will be interpreted correctly.
