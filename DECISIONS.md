# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index and unique `(attendance_logs.emp_code, date)` index enforce identity and prevent duplicate records. `(date, emp_code)` supports attendance listing and month ranges; `(date, status)` helps status filters; `(emp_code, punch_in)` finds the latest punch-out candidate. Employee indexes on `(department, joined_on)` and `joined_on` support department and headcount queries. I rejected a standalone status index because status has few values and is not selective.

2. **Punch-in race.** Both requests may look up the employee, but neither relies on checking whether a record exists. Both try to insert. The unique `(emp_code, date)` index allows one insert; MongoDB raises a duplicate-key error for the other, which becomes 409.

3. **Ties.** MongoDB `$rank` ranks total late minutes before `rank <= limit` is applied. Everyone tied at the cutoff is returned, and the next rank is skipped. `emp_code` orders tied rows only.

4. **Headcount.** The department summary starts from employees who joined by month-end, then looks up their logs. Employees with no logs still count.

5. **100x scale.** I would benchmark plans and workload, then consider pre-aggregated daily summaries refreshed in the background for expensive analytics.
