# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique (`emp_code`, `date`) guards punch-in and serves the monthly and trend scans; (`date` desc, `emp_code`) gives the list its sort order and the leaderboard its month range; (`status`, `date` desc, `emp_code`) serves the status filter; (`emp_code`, `punch_in` desc) finds the record to punch out. `employees`: unique `emp_code`, (`department`, `emp_code`), `joined_on` and (`department`, `joined_on`) for headcount. Rejected: an index on `late_minutes`; the month range already narrows the scan and every write would pay for it.

2. **Punch-in race.** Both requests build the same record and call insert. The unique (`emp_code`, `date`) index lets one insert succeed (201); the other raises a duplicate-key error, which I turn into 409. There is no check-then-insert.

3. **Ties.** The pipeline ranks with `$rank` (1, 2, 2, 4) and only then keeps `rank <= limit`, so every employee tied at the cutoff is returned, ordered by minutes, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs, so someone with no logs gets an empty array and is still counted.

5. **100x data.** I would keep a pre-aggregated monthly rollup per employee, updated on punch and correction, instead of aggregating raw logs per request.
