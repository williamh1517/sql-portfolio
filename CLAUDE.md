# CLAUDE.md

SQL analysis portfolio over the Lending Club loan book. See README.md for setup and the
`public.loans` column table.

## Stack

Postgres 17 in Docker (`docker-compose.yml`, container `lendingclub-pg`, `localhost:5432`).
Python in `.venv` — pandas, psycopg 3, python-dotenv; pinned in `requirements.txt`. Run Python as
`.venv/bin/python …`.

```sh
docker compose up -d
docker exec -it lendingclub-pg psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

## Layout

- `finance/sql/NN_*.sql` — numbered, run in order. `01_` is DDL, `02_` is indexes.
- `finance/scripts/` — Python loaders and helpers.
- `data/raw/` — source archives. Gitignored; never commit or copy data into the repo.

## Secrets

Credentials live in `.env` (gitignored): `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.
Read them with `python-dotenv` and pass the password as a connection keyword. Never hard-code it,
never print it, never build it into a logged connection string, and never put it in a committed
file. In shell commands prefer `docker exec lendingclub-pg sh -c 'psql -U "$POSTGRES_USER" …'`,
which uses the container's own env and in-container auth rather than passing a password.

## Conventions

- Loaders are **idempotent and single-transaction**: recreate the table, load, index, verify, then
  commit. Any failure rolls back to the previous table and exits non-zero.
- Bulk load with `COPY` (`cursor.copy()` streaming CSV bytes), never `DataFrame.to_sql`.
- Create secondary indexes *after* the load, not before.
- Fail loud on unexpected data. Named `CHECK` constraints and explicit value maps are preferred
  over silently coercing or filling — if a new input file breaks the load, that is the intended
  behaviour. Do not add `fillna`.
- Pin domains with `CHECK` only where the allowed values are certain (`grade`, `sub_grade`,
  `term_months`, FICO, rates). `loan_status`, `purpose`, `home_ownership` and
  `verification_status` are left unconstrained — their value lists vary between exports.
- Name every constraint (`pk_loans`, `chk_loans_grade`, `idx_loans_issue_d`) so failures are
  readable.

## Data notes

- `public.loans` is a 22-column cleaned subset of the 151-column source; the loader drops the 33
  summary trailer rows (non-numeric `id`).
- `int_rate_pct` and `revol_util_pct` are **percents, not fractions** (`13.99` = 13.99%);
  `revol_util_pct` can exceed 100.
- `dti` keeps the source sentinels: `-1` and `999` are present and are not real ratios.
- `meets_credit_policy = false` encodes the `"Does not meet the credit policy. Status:"` prefix
  that was stripped from `loan_status` (2,749 rows).
- `emp_length_years`: `< 1 year` → 0, `10+ years` → 10, NULL = not reported (6.5% of rows). A
  digit regex would misread `"< 1 year"` as 1 — use the explicit map in `load_loans.py`.
- Money columns are `NUMERIC(12,2)`, so `total_pymnt` is rounded from the source's float noise.
- The source file is sorted **newest-first**, so `--sample N` gives the most recent loans only —
  fine for smoke tests, misleading for any statistic, and it never reaches the pre-2012 rows that
  carry the credit-policy prefix.
- Work one sub-step at a time. After the plan, after the sample run, and before any commit or push, stop and wait for my go-ahead.