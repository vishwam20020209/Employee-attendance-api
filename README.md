# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the HROne Employee Attendance & Analytics assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (MongoDB 7 is supported)
- Environment variables `MONGO_URI` and `MONGO_DB`

## Run locally

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your shell or in a local `.env` file. For example, for local MongoDB:

```bash
# Linux/macOS
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
# Windows PowerShell
# $env:MONGO_URI='mongodb://localhost:27017'
# $env:MONGO_DB='attendance_db'
```

Load the provided sample documents if desired:

```bash
python sample_seed.py
```

Then start the API from the repository root:

```bash
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` for the interactive API documentation. `/health` checks MongoDB readiness. Indexes are created automatically at startup.

## Implementation notes

- Instants cross the API boundary as epoch milliseconds; stored datetimes are UTC.
- Calendar attendance dates and shift times follow the assignment's IST rules.
- Analytics use MongoDB aggregation pipelines, including database-side date generation and window functions for department trends and late ranking.
- The database's unique indexes enforce employee-code and attendance-day uniqueness under concurrent requests.

## Submission checklist

Commit `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, `DECISIONS.md`, and the supplied contract/data-model files. Do not commit `.env`, credentials, a virtual environment, or a Dockerfile.
