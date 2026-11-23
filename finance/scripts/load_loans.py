#!/usr/bin/env python3
"""Load the Lending Club accepted-loans export into public.loans.

Reads data/raw/accepted_2007_to_2018Q4.csv.gz in chunks, keeps 22 of its 151
columns, cleans each chunk, and streams it into Postgres with COPY.

The whole load -- DROP/CREATE, COPY, indexes, verification -- runs in one
transaction, so a failure anywhere leaves the previous table untouched and the
process exits non-zero.

Usage:
    python finance/scripts/load_loans.py
    python finance/scripts/load_loans.py --sample 1000
    python finance/scripts/load_loans.py --file path/to/other.csv.gz
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = REPO_ROOT / "finance" / "sql"
CREATE_SQL = SQL_DIR / "01_create_loans.sql"
INDEX_SQL = SQL_DIR / "02_index_loans.sql"
DEFAULT_FILE = REPO_ROOT / "data" / "raw" / "accepted_2007_to_2018Q4.csv.gz"
DEFAULT_CHUNKSIZE = 200_000

# Source columns to read. Everything else in the 151-column file is ignored.
KEEP_COLUMNS = [
    "id",
    "issue_d",
    "loan_amnt",
    "funded_amnt",
    "term",
    "int_rate",
    "installment",
    "grade",
    "sub_grade",
    "emp_length",
    "home_ownership",
    "annual_inc",
    "verification_status",
    "purpose",
    "addr_state",
    "dti",
    "fico_range_low",
    "fico_range_high",
    "revol_util",
    "loan_status",
    "total_pymnt",
    "last_pymnt_d",
]

# Target columns, in the order they are written to COPY.
COPY_COLUMNS = [
    "id",
    "issue_d",
    "loan_amnt",
    "funded_amnt",
    "term_months",
    "int_rate_pct",
    "installment",
    "grade",
    "sub_grade",
    "emp_length_years",
    "home_ownership",
    "annual_inc",
    "verification_status",
    "purpose",
    "addr_state",
    "dti",
    "fico_range_low",
    "fico_range_high",
    "revol_util_pct",
    "loan_status",
    "meets_credit_policy",
    "total_pymnt",
    "last_pymnt_d",
]

COPY_SQL = (
    f"COPY public.loans ({', '.join(COPY_COLUMNS)}) "
    "FROM STDIN WITH (FORMAT CSV, NULL '')"
)

# Explicit map rather than a digit regex: "< 1 year" contains a 1, so
# str.extract(r"(\d+)") would read it as 1 instead of 0.
EMP_LENGTH_MAP = {
    "< 1 year": 0,
    "1 year": 1,
    "2 years": 2,
    "3 years": 3,
    "4 years": 4,
    "5 years": 5,
    "6 years": 6,
    "7 years": 7,
    "8 years": 8,
    "9 years": 9,
    "10+ years": 10,
}

CREDIT_POLICY_PREFIX = "Does not meet the credit policy. Status:"

NULLABLE_COLUMNS = [
    "emp_length_years",
    "annual_inc",
    "dti",
    "revol_util_pct",
    "last_pymnt_d",
]


@dataclass
class LoadStats:
    """Row counts accumulated on the Python side of the load."""

    rows_read: int = 0
    dropped_bad_id: int = 0
    dropped_null_loan_amnt: int = 0
    rows_loaded: int = 0

    @property
    def rows_dropped(self) -> int:
        return self.dropped_bad_id + self.dropped_null_loan_amnt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the Lending Club accepted-loans CSV into public.loans."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_FILE,
        help=f"gzipped CSV to load (default: {DEFAULT_FILE.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--sample",
        type=int,
        metavar="N",
        help="load only the first N rows of the file (for testing). Note the file "
        "is sorted newest-first, so a sample is not representative.",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=DEFAULT_CHUNKSIZE,
        help=f"rows read and COPYed per chunk (default: {DEFAULT_CHUNKSIZE})",
    )
    args = parser.parse_args(argv)
    if args.sample is not None and args.sample < 1:
        parser.error("--sample must be at least 1")
    if args.chunksize < 1:
        parser.error("--chunksize must be at least 1")
    return args


def connect() -> psycopg.Connection:
    """Open a non-autocommit connection using credentials from .env.

    The password is passed as a connection keyword and is never logged, printed,
    or interpolated into a connection string.
    """
    load_dotenv(REPO_ROOT / ".env")
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"missing environment variable(s): {', '.join(missing)} "
            f"(expected in {REPO_ROOT / '.env'})"
        )
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        autocommit=False,
    )


def run_sql_file(cur: psycopg.Cursor, path: Path) -> None:
    """Execute a .sql file as a single multi-statement command."""
    cur.execute(path.read_text())


def iter_chunks(path: Path, sample: int | None, chunksize: int):
    """Yield raw DataFrames of KEEP_COLUMNS from the gzipped CSV."""
    return pd.read_csv(
        path,
        usecols=KEEP_COLUMNS,
        dtype={"id": "string"},
        chunksize=chunksize,
        nrows=sample,
    )


def _month_to_date(values: pd.Series) -> pd.Series:
    """'Dec-2015' -> '2015-12-01'. Raises on anything that is not %b-%Y."""
    return pd.to_datetime(values, format="%b-%Y").dt.strftime("%Y-%m-%d")


def clean_chunk(raw: pd.DataFrame, stats: LoadStats) -> pd.DataFrame:
    """Apply every row-level rule and return a frame shaped like COPY_COLUMNS."""
    stats.rows_read += len(raw)

    # The file ends with summary rows whose id is text like
    # "Total amount funded in policy code 1: ..." and whose measures are blank.
    # The two drop reasons are applied in order so a row is only counted once.
    good_id = raw["id"].str.fullmatch(r"\d+", na=False)
    stats.dropped_bad_id += int((~good_id).sum())
    df = raw[good_id]

    has_amount = df["loan_amnt"].notna()
    stats.dropped_null_loan_amnt += int((~has_amount).sum())
    df = df[has_amount]

    emp_length_years = df["emp_length"].map(EMP_LENGTH_MAP).astype("Int16")
    unmapped = df["emp_length"].notna() & emp_length_years.isna()
    if bool(unmapped.any()):
        unknown = sorted(set(df.loc[unmapped, "emp_length"]))
        raise ValueError(f"unrecognised emp_length value(s): {unknown}")

    status = df["loan_status"]
    clean = pd.DataFrame(
        {
            "id": df["id"].astype("int64"),
            "issue_d": _month_to_date(df["issue_d"]),
            "loan_amnt": df["loan_amnt"],
            "funded_amnt": df["funded_amnt"],
            "term_months": df["term"].str.extract(r"(\d+)", expand=False).astype("Int16"),
            "int_rate_pct": df["int_rate"],
            "installment": df["installment"],
            "grade": df["grade"],
            "sub_grade": df["sub_grade"],
            "emp_length_years": emp_length_years,
            "home_ownership": df["home_ownership"],
            "annual_inc": df["annual_inc"],
            "verification_status": df["verification_status"],
            "purpose": df["purpose"],
            "addr_state": df["addr_state"],
            "dti": df["dti"],
            # Read as float64 by pandas; cast so they serialise as 675, not 675.0.
            "fico_range_low": df["fico_range_low"].astype("Int16"),
            "fico_range_high": df["fico_range_high"].astype("Int16"),
            "revol_util_pct": df["revol_util"],
            "loan_status": status.str.removeprefix(CREDIT_POLICY_PREFIX).str.strip(),
            "meets_credit_policy": ~status.str.startswith(CREDIT_POLICY_PREFIX, na=False),
            "total_pymnt": df["total_pymnt"],
            "last_pymnt_d": _month_to_date(df["last_pymnt_d"]),
        }
    )
    return clean[COPY_COLUMNS]


def chunk_to_bytes(clean: pd.DataFrame) -> bytes:
    """Serialise a cleaned chunk as the CSV payload COPY expects."""
    buf = io.StringIO()
    clean.to_csv(buf, index=False, header=False, na_rep="")
    return buf.getvalue().encode("utf-8")


def copy_chunk(cur: psycopg.Cursor, clean: pd.DataFrame) -> None:
    with cur.copy(COPY_SQL) as copy:
        copy.write(chunk_to_bytes(clean))


def verify(cur: psycopg.Cursor, stats: LoadStats) -> dict:
    """Run the post-load checks. Raises if the row count disagrees with COPY."""
    cur.execute("SELECT COUNT(*) FROM public.loans")
    db_count = cur.fetchone()[0]
    if db_count != stats.rows_loaded:
        raise RuntimeError(
            f"row count mismatch: COPYed {stats.rows_loaded:,} rows but the table "
            f"holds {db_count:,}"
        )

    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE emp_length_years IS NULL),
               COUNT(*) FILTER (WHERE annual_inc       IS NULL),
               COUNT(*) FILTER (WHERE dti              IS NULL),
               COUNT(*) FILTER (WHERE revol_util_pct   IS NULL),
               COUNT(*) FILTER (WHERE last_pymnt_d     IS NULL)
          FROM public.loans
        """
    )
    null_counts = dict(zip(NULLABLE_COLUMNS, cur.fetchone()))

    cur.execute("SELECT MIN(issue_d), MAX(issue_d) FROM public.loans")
    issue_min, issue_max = cur.fetchone()

    cur.execute(
        """
        SELECT loan_status, meets_credit_policy, COUNT(*)
          FROM public.loans
         GROUP BY loan_status, meets_credit_policy
         ORDER BY COUNT(*) DESC
        """
    )
    status_rows = cur.fetchall()

    return {
        "db_count": db_count,
        "null_counts": null_counts,
        "issue_min": issue_min,
        "issue_max": issue_max,
        "status_rows": status_rows,
    }


def print_report(path: Path, stats: LoadStats, checks: dict, elapsed: float) -> None:
    total = checks["db_count"] or 1
    print()
    print("=" * 66)
    print("LOAD REPORT  public.loans")
    print("=" * 66)
    print(f"source file          {path}")
    print()
    print(f"rows read            {stats.rows_read:>12,}")
    print(f"rows dropped         {stats.rows_dropped:>12,}")
    print(f"  id not numeric     {stats.dropped_bad_id:>12,}")
    print(f"  loan_amnt null     {stats.dropped_null_loan_amnt:>12,}")
    print(f"rows loaded          {stats.rows_loaded:>12,}")
    print()
    match = "PASS" if checks["db_count"] == stats.rows_loaded else "FAIL"
    print(f"SELECT COUNT(*)      {checks['db_count']:>12,}   [{match}]")
    print(f"issue_d range        {checks['issue_min']} .. {checks['issue_max']}")
    print()
    print("null counts")
    for column, count in checks["null_counts"].items():
        print(f"  {column:<20} {count:>12,}  ({100 * count / total:.3f}%)")
    print()
    print("loan_status distribution")
    for loan_status, meets_policy, count in checks["status_rows"]:
        flag = "" if meets_policy else "   (does not meet credit policy)"
        print(f"  {loan_status:<20} {count:>12,}  ({100 * count / total:.3f}%){flag}")
    print()
    print(f"elapsed              {elapsed:>12.1f} s")
    print("=" * 66)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path: Path = args.file
    if not path.is_file():
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 1

    stats = LoadStats()
    started = time.perf_counter()

    try:
        conn = connect()
    except Exception as exc:
        print(f"error: could not connect to Postgres: {exc}", file=sys.stderr)
        return 1

    try:
        # psycopg commits when this block exits cleanly and rolls back otherwise;
        # either way the connection is closed.
        with conn, conn.cursor() as cur:
            run_sql_file(cur, CREATE_SQL)
            for chunk_no, raw in enumerate(
                iter_chunks(path, args.sample, args.chunksize), start=1
            ):
                clean = clean_chunk(raw, stats)
                copy_chunk(cur, clean)
                stats.rows_loaded += len(clean)
                print(
                    f"  chunk {chunk_no:>3}  loaded {stats.rows_loaded:>12,} rows"
                    f"  ({time.perf_counter() - started:6.1f}s)",
                    file=sys.stderr,
                    flush=True,
                )
            print("  building indexes ...", file=sys.stderr, flush=True)
            run_sql_file(cur, INDEX_SQL)
            checks = verify(cur, stats)
    except KeyboardInterrupt:
        print("\nerror: interrupted; the load was rolled back", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: load failed and was rolled back: {exc}", file=sys.stderr)
        return 1

    print_report(path, stats, checks, time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
