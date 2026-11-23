# sql-portfolio

SQL analysis of the Lending Club loan book (2007 – 2018Q4), against Postgres 17 running locally in
Docker. The repo holds the schema, a loader that turns the raw Kaggle export into a clean typed
table, and the analysis queries built on top of it.

## Prerequisites

- Docker (for Postgres 17)
- Python 3.13 with the virtualenv at `.venv` — `pip install -r requirements.txt`

## Setup

**1. Credentials.** Create `.env` in the repo root (it is gitignored — never commit it):

```sh
POSTGRES_USER=your_user
POSTGRES_PASSWORD=your_password
POSTGRES_DB=lendingclub
```

`POSTGRES_HOST` and `POSTGRES_PORT` are optional and default to `localhost:5432`.

**2. Start the database.**

```sh
docker compose up -d
docker compose ps          # wait for "healthy"
```

**3. Get the data.** Download *Lending Club 2007-2020Q3* from Kaggle
(`wordsforthewise/lending-club`) and put the accepted-loans file at:

```
data/raw/accepted_2007_to_2018Q4.csv.gz
```

`data/` is gitignored — the 392 MB archive stays out of the repo.

## Loading

```sh
.venv/bin/python finance/scripts/load_loans.py              # full load, ~25s
.venv/bin/python finance/scripts/load_loans.py --sample 1000  # quick smoke test
.venv/bin/python finance/scripts/load_loans.py --file path/to/other.csv.gz
```

The loader reads the gzipped CSV in 200k-row chunks, keeps 22 of the 151 columns, cleans each
chunk, and streams it into Postgres with `COPY`. The whole run — `DROP`/`CREATE`
(`finance/sql/01_create_loans.sql`), `COPY`, indexes (`finance/sql/02_index_loans.sql`),
verification — happens in **one transaction**, so it is safe to re-run and a failure anywhere
leaves the previous table untouched and exits non-zero.

It finishes with a load report:

```
==================================================================
LOAD REPORT  public.loans
==================================================================
rows read               2,260,701
rows dropped                   33
  id not numeric               33
  loan_amnt null                0
rows loaded             2,260,668

SELECT COUNT(*)         2,260,668   [PASS]
issue_d range        2007-06-01 .. 2018-12-01

null counts
  emp_length_years          146,907  (6.498%)
  annual_inc                      4  (0.000%)
  dti                         1,711  (0.076%)
  revol_util_pct              1,802  (0.080%)
  last_pymnt_d                2,427  (0.107%)

loan_status distribution
  Fully Paid              1,076,751  (47.630%)
  Current                   878,317  (38.852%)
  Charged Off               268,559  (11.880%)
  ...
  Fully Paid                  1,988  (0.088%)   (does not meet credit policy)
  Charged Off                   761  (0.034%)   (does not meet credit policy)

elapsed                      22.1 s
==================================================================
```

The 33 dropped rows are the summary trailer the source file ends with
(`Total amount funded in policy code 1: …`), identified by a non-numeric `id`.

## `public.loans`

2,260,668 rows. Derived columns are named for what they hold, so no query has to re-do the
cleaning.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT` | primary key |
| `issue_d` | `DATE` | `Dec-2015` → `2015-12-01` |
| `loan_amnt`, `funded_amnt`, `installment`, `total_pymnt` | `NUMERIC(12,2)` | |
| `term_months` | `SMALLINT` | from `" 36 months"`; 36 or 60 |
| `int_rate_pct` | `NUMERIC(6,2)` | a **percent**, not a fraction: `13.99` = 13.99% |
| `grade`, `sub_grade` | `VARCHAR` | `A`–`G`, `A1`–`G5` |
| `emp_length_years` | `SMALLINT` | `< 1 year` → 0, `10+ years` → 10, NULL = not reported |
| `home_ownership`, `verification_status`, `purpose`, `addr_state` | `VARCHAR` | as reported |
| `annual_inc` | `NUMERIC(12,2)` | |
| `dti` | `NUMERIC(7,2)` | source sentinels kept: `-1` and `999` occur |
| `fico_range_low`, `fico_range_high` | `SMALLINT` | |
| `revol_util_pct` | `NUMERIC(6,2)` | a percent; can exceed 100 |
| `loan_status` | `VARCHAR(32)` | `"Does not meet the credit policy. Status:"` prefix stripped |
| `meets_credit_policy` | `BOOLEAN` | `false` where that prefix was present (2,749 rows) |
| `last_pymnt_d` | `DATE` | NULL = no payment recorded |

Indexed on `issue_d`, `grade`, `loan_status`, `purpose`, `addr_state`. Run `\d+ public.loans` in
psql for the column comments and check constraints.

## Querying

```sh
docker exec -it lendingclub-pg psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

**Credit performance by grade** (completed loans only):

```sql
SELECT grade,
       COUNT(*)                                   AS loans,
       ROUND(AVG(int_rate_pct), 2)                AS avg_rate_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE loan_status = 'Charged Off')
             / COUNT(*), 2)                       AS charged_off_pct
  FROM public.loans
 WHERE loan_status IN ('Fully Paid', 'Charged Off')
 GROUP BY grade
 ORDER BY grade;
```

```
 grade | loans  | avg_rate_pct | charged_off_pct
-------+--------+--------------+-----------------
 A     | 235188 |         7.11 |            6.04
 B     | 393095 |        10.68 |           13.39
 C     | 382315 |        14.02 |           22.44
 D     | 201644 |        17.71 |           30.38
 E     |  94186 |        21.11 |           38.43
 F     |  32305 |        24.88 |           45.14
 G     |   9326 |        27.54 |           49.67
```

**Origination volume by year:**

```sql
SELECT date_trunc('year', issue_d)::date AS issue_year,
       COUNT(*)                          AS loans,
       SUM(loan_amnt)                    AS originated
  FROM public.loans
 GROUP BY issue_year
 ORDER BY issue_year;
```

## Layout

```
docker-compose.yml           Postgres 17, port 5432, named volume
finance/sql/
  00_smoke_test.sql          connection check
  01_create_loans.sql        public.loans DDL, constraints, column comments
  02_index_loans.sql         secondary indexes, run after COPY
finance/scripts/
  load_loans.py              the loader
data/raw/                    source archives (gitignored)
```
