# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the Employee Attendance & Analytics API contract v2.0.

## Run locally

1. Install Python 3.11+ and MongoDB 6.0+ (local or Atlas).
2. Create and activate a virtual environment, then run `pip install -r requirements.txt`.
3. Set `MONGO_URI` and `MONGO_DB` in your shell (or a local, uncommitted `.env` file).
4. From this directory run `uvicorn app.main:app --port 8000`.
5. Check `GET http://127.0.0.1:8000/health` and explore `/docs`.

Indexes are created automatically at startup. `sample_seed.py` can load the supplied sample data into the configured database; use a disposable database because seeding inserts sample records.

## Notes

- Instants are stored as UTC BSON dates and exposed as epoch milliseconds.
- Calendar dates remain `YYYY-MM-DD` strings.
- `REVIEW.md` documents starter defects; `DECISIONS.md` records design choices.
- Run a MongoDB-backed integration test suite before submitting. The provided sample is not sufficient to prove hidden edge cases or 100k-record query plans.
