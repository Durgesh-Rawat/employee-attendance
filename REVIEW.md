# Starter code review

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses a same-day shift start even for overnight shifts and relies on naive/local datetimes. | Punch-in shortly after midnight for a 22:00 shift becomes negative/incorrect. | Convert instants to IST and calculate against the applicable shift date. |
| 2 | `compute_late_minutes` | Grace-period boundary must be strictly greater than 10 minutes, but the starter's whole-minute truncation can hide seconds after the boundary. | 09:40:01 for a 09:30 shift. | Compare elapsed seconds to 600, then floor whole minutes. |
| 3 | `compute_overtime` | Always puts shift end on `date_str`, so overnight shifts use the wrong day. | 22:00–06:00 shift punches out next morning. | Compute the actual shift-end instant including overnight rollover. |
| 4 | `compute_overtime` | Counts any positive overtime, despite the 30-minute minimum. | Punch-out 18:35 for an 18:30 end. | Return zero unless overtime is at least 1,800 seconds. |
| 5 | `compute_work_hours` | Python's built-in `round` uses ties-to-even, not decimal half-up. | A duration exactly at a half-cent rounding boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 6 | Mongo client / `/health` | Health always returns OK without checking MongoDB. | Stop MongoDB and call `/health`. | Issue a ping and return 503 on database failure. |
| 7 | `EmployeeIn` | Missing contract-level format/range checks and allows identical shift start/end. | Bad employee code, invalid date/time or equal shift times. | Validate codes, email, dates, shift formats, lengths and differing shift times. |
| 8 | `create_employee` | Check-then-insert is race-prone and no unique index is created. | Send concurrent creates with the same code. | Unique index and catch `DuplicateKeyError`. |
| 9 | `created_at` | Uses naive local `datetime.now()`, not a UTC instant. | Inspect BSON timestamp on a host with another timezone. | Store UTC BSON datetimes and expose epoch milliseconds. |
| 10 | `list_employees` | `skip = page * page_size` skips page 1 and total ignores department filter. | Page 1 omits first page; department filter has inflated total. | Use `(page-1)*page_size` and count the same filter. |
| 11 | `list_employees` | No validation for page/page_size or deterministic sort. | Page 0, size 0/101, repeated pages. | Validate pagination and sort by `emp_code`. |
| 12 | `punch_in` | Does not return 404 for unknown employee before dereferencing it. | Punch in using unknown code causes a 500. | Check employee and return 404. |
| 13 | `punch_in` | Converts epoch seconds/local time instead of validating epoch milliseconds and UTC. | Send seconds, float, string or run in another timezone. | Validate integer milliseconds, truncate to whole seconds and convert at the boundary. |
| 14 | `punch_in` | Check-then-insert alone is unsafe under concurrency and status is unrestricted. | Parallel identical requests or `status=ABSENT`. | Unique `(emp_code,date)` index and restrict status to presence statuses. |
| 15 | `punch_in` | Attendance date ignores overnight shift rule; timestamps aren't truncated. | Early-morning punch for overnight shift or milliseconds in timestamp. | Apply R1 to determine date and store whole seconds. |
| 16 | `list_attendance` | Fetches every matching document and sorts/slices in Python. | Run against 100k records and monitor memory/latency. | Count and paginate in MongoDB using indexed query and stable sort. |
| 17 | `list_attendance` | No validation of date formats/order and pagination. | Reversed date range or page size above 100. | Validate inclusive date filters and bounds. |
| 18 | `list_attendance` | Leaks `_id` and returns BSON datetimes rather than epoch milliseconds. | Inspect response for ObjectId/datetime serialization. | Remove internal ID and convert all API instants. |
| 19 | Overall | Missing punch-out, regularization, all analytics and explain endpoints. | Request each documented route. | Implement the remaining contract endpoints. |
| 20 | Overall | No indexes are created at startup. | Explain a list query on the seeded 100k dataset. | Create unique natural-key and query-supporting indexes idempotently. |

**Reviewed and retained:** using `emp_code` as business identity and storing calendar dates as `YYYY-MM-DD` strings match the data model. The date range in attendance listing is inclusive, as required. MongoDB's `_id` remains internal and is not used as an API identifier.
