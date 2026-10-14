1. **Indexes.** `employees.emp_code` is unique to make employee creation race-safe. The compound `attendance_logs(emp_code, date)` index both enforces one record per employee/day and supports employee-month queries. Date/employee and status/date indexes support the list filters and stable sorting; employee/punch-in supports the most recent open-record lookup. Department/join-date indexes help the headcount and trend lookups. I would measure before adding more indexes because each one adds write cost and storage.

2. **Punch-in race.** Both requests may pass the employee lookup, but the unique `(emp_code, date)` index permits only one insert. The winner gets 201; MongoDB rejects the other insert and the API translates that duplicate-key error to 409.

3. **Ties.** The leaderboard uses `$rank`, not array position. If rank `limit` is shared by multiple employees, all tied rows are returned, ordered by late minutes and then employee code.

4. **Headcount.** The summary starts from employees and uses a lookup with `preserveNullAndEmptyArrays`, so employees who have joined by month-end count even when they have no attendance logs.

5. **100x data.** I would inspect execution stats and query plans, tune compound indexes to actual filters, and consider pre-aggregated daily summaries if analytics latency became a problem.
