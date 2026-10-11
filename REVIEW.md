# REVIEW.md

I reviewed the original `app/main.py` before changing it. The line references below refer to the starter file, not this revised version.

| # | Where in starter | What is wrong | Concrete symptom / test | Fix |
|---:|---|---|---|---|
| 1 | `compute_late_minutes` | It floors elapsed seconds to whole minutes before checking the 10-minute grace period. | For a 09:30 shift, 09:40:01 is incorrectly treated as not late; the contract expects 10 late minutes. | Compare elapsed seconds to 600 first, then floor elapsed minutes. |
| 2 | `compute_late_minutes` | It uses the punch-in's calendar date without the employee's attendance-day/overnight-shift rules. | A 05:00 IST punch-in on a 22:00–06:00 shift belongs to the previous shift date. | Calculate the IST attendance date first and build the shift start on that date. |
| 3 | `compute_work_hours` | Built-in `round` uses ties-to-even and binary floats, not decimal half-up rounding. | A duration exactly halfway between hundredths can round the wrong way. | Use `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It always places shift end on the same date and counts any positive overtime. | A 22:00–06:00 shift with a 06:40 punch-out is miscalculated; 5 minutes must not qualify. | Move overnight shift end to the next day and apply the 30-minute threshold. |
| 5 | Mongo client setup / timestamps | PyMongo's default naive datetimes and `datetime.now()` make UTC handling dependent on assumptions; `fromtimestamp()` uses the host's local timezone. | The same epoch input can produce a different stored date on a host configured outside UTC. | Use aware UTC datetimes at the API boundary and explicit IST conversion for calendar rules. |
| 6 | `health` | It returns success without checking MongoDB. | Stop MongoDB and `/health` still reports `ok`. | Run a MongoDB `ping` and return 503 on failure. |
| 7 | `create_employee` | Duplicate detection is a check-then-insert race and there is no unique index. | Parallel requests with the same `emp_code` can both pass the check. | Enforce uniqueness with a database unique index and translate duplicate-key errors to 409. |
| 8 | `EmployeeIn` | It does not enforce the contract's code/email/date/time formats, field lengths, or distinct shift times. | Invalid employee codes, malformed dates, or identical shift start/end can be stored. | Add Pydantic validation matching the OpenAPI contract. |
| 9 | `create_employee` | It sets `created_at` with a naive local datetime. | Returned creation time is ambiguous and not consistently epoch milliseconds. | Store a truncated UTC instant and serialize it as epoch milliseconds. |
| 10 | `list_employees` | The skip calculation uses `page * page_size`, so page 1 skips the first page. | With page size 20, page 1 starts at item 21. | Use `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` counts the entire collection rather than the filtered query. | Filtering to one department still returns the unfiltered total. | Count with the same query used for the page. |
| 12 | `list_employees` | It does not sort by `emp_code`. | Results can arrive in unstable order across calls. | Sort by `emp_code` ascending before pagination. |
| 13 | `punch_in` | It dereferences the employee without checking whether one exists. | Punching in an unknown code can cause a 500 instead of 404. | Return 404 before accessing shift fields. |
| 14 | `PunchInIn` / `punch_in` | Timestamp input is not validated as an integer epoch-millisecond value; it is interpreted as local-time seconds. | A valid millisecond timestamp becomes an absurd date, while seconds/floats are not rejected correctly. | Validate strict integer milliseconds and convert to UTC after truncating to whole seconds. |
| 15 | `punch_in` | It derives the date from the host-local timestamp and ignores overnight shifts. | Punch-ins near midnight can be written under the wrong attendance date. | Derive the date in IST using the employee's shift. |
| 16 | `punch_in` | Duplicate detection is another check-then-insert race. | Two concurrent punch-ins can both pass `find_one`; a duplicate can be inserted or a request can fail unexpectedly. | Add a unique `(emp_code, date)` index and handle `DuplicateKeyError` as 409. |
| 17 | `punch_in` | It does not validate the supplied status against the three allowed punch-in statuses. | A request can create a record with `ABSENT`, `LEAVE`, or an arbitrary status. | Restrict the request model to `PRESENT`, `WFH`, and `ON_DUTY`. |
| 18 | `punch_in` / `list_attendance` | The response adds an `id` derived from Mongo `_id`, although the contract says attendance is addressed by `(emp_code, date)` and `_id` is internal. | Responses contain an undocumented `id` field. | Return the contract fields only and omit Mongo `_id`. |
| 19 | `list_attendance` | It loads every matching record into Python, sorts and paginates in memory. | Performance and memory usage degrade on the grader's ~100,000 records. | Apply indexed filters, sort, skip, and limit in MongoDB. |
| 20 | `list_attendance` | Sorting only uses descending date; it omits the required ascending `emp_code` tie-breaker. | Multiple employees on the same date can appear in unstable order. | Sort by `date` descending and `emp_code` ascending. |
| 21 | `list_attendance` | It does not validate date syntax, date range order, status, page minimum, or page-size maximum. | Invalid filters or page values can return inconsistent results instead of 422. | Validate filters and constrain pagination to page >= 1 and page_size 1–100. |
| 22 | Whole starter / TODO section | Required punch-out, regularization, analytics, and explain endpoints are absent. | Calls to the documented routes return 404. | Implement the missing routes, their aggregation pipelines, and their concurrency safeguards. |
| 23 | Whole starter | It creates no indexes at startup. | Large queries can scan the full collections, and uniqueness is not guaranteed. | Create idempotent unique and query-supporting indexes at startup. |

## Things I checked and did not treat as defects

- Using `emp_code` as the employee identity is correct; MongoDB `_id` is internal.
- Storing `date` and `joined_on` as `YYYY-MM-DD` strings is intentional in the supplied data model.
- Missing legacy `history`, `half_day`, `late_minutes`, and `overtime_minutes` values should be treated as empty/false/zero, not as corrupt records.
- The `load_dotenv()` approach is compatible with environment variables taking precedence because `python-dotenv` does not override existing environment values by default.
