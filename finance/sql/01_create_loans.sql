-- Lending Club accepted loans, 2007 through 2018Q4.
--
-- Source:  data/raw/accepted_2007_to_2018Q4.csv.gz (151 columns; 22 kept here)
-- Loader:  finance/scripts/load_loans.py  (run that, don't load this by hand)
-- Indexes: finance/sql/02_index_loans.sql (created after COPY, in the same transaction)
--
-- The loader executes this file first, inside the load transaction, so a failed
-- load rolls back to whatever table was here before.

DROP TABLE IF EXISTS public.loans;

CREATE TABLE public.loans (
    id                   BIGINT        NOT NULL,
    issue_d              DATE          NOT NULL,
    loan_amnt            NUMERIC(12,2) NOT NULL,
    funded_amnt          NUMERIC(12,2) NOT NULL,
    term_months          SMALLINT      NOT NULL,
    int_rate_pct         NUMERIC(6,2)  NOT NULL,
    installment          NUMERIC(12,2) NOT NULL,
    grade                VARCHAR(1)    NOT NULL,
    sub_grade            VARCHAR(2)    NOT NULL,
    emp_length_years     SMALLINT,
    home_ownership       VARCHAR(16)   NOT NULL,
    annual_inc           NUMERIC(12,2),
    verification_status  VARCHAR(20)   NOT NULL,
    purpose              VARCHAR(32)   NOT NULL,
    addr_state           VARCHAR(2)    NOT NULL,
    dti                  NUMERIC(7,2),
    fico_range_low       SMALLINT      NOT NULL,
    fico_range_high      SMALLINT      NOT NULL,
    revol_util_pct       NUMERIC(6,2),
    loan_status          VARCHAR(32)   NOT NULL,
    meets_credit_policy  BOOLEAN       NOT NULL,
    total_pymnt          NUMERIC(12,2) NOT NULL,
    last_pymnt_d         DATE,

    CONSTRAINT pk_loans PRIMARY KEY (id),

    -- Domains below are pinned only where the allowed values are certain.
    -- loan_status, purpose, home_ownership and verification_status are left
    -- unconstrained: their value lists vary between Lending Club exports.
    CONSTRAINT chk_loans_grade            CHECK (grade IN ('A','B','C','D','E','F','G')),
    CONSTRAINT chk_loans_sub_grade        CHECK (sub_grade ~ '^[A-G][1-5]$'),
    CONSTRAINT chk_loans_term_months      CHECK (term_months IN (36, 60)),
    CONSTRAINT chk_loans_emp_length_years CHECK (emp_length_years BETWEEN 0 AND 10),
    CONSTRAINT chk_loans_loan_amnt        CHECK (loan_amnt > 0),
    CONSTRAINT chk_loans_int_rate_pct     CHECK (int_rate_pct BETWEEN 0 AND 100),
    CONSTRAINT chk_loans_addr_state       CHECK (char_length(addr_state) = 2),
    CONSTRAINT chk_loans_fico_range       CHECK (fico_range_low  BETWEEN 300 AND 850
                                            AND fico_range_high BETWEEN 300 AND 850
                                            AND fico_range_low <= fico_range_high)
);

COMMENT ON TABLE public.loans IS
    'Lending Club accepted loans 2007-2018Q4: a 22-column cleaned subset of the '
    'Kaggle accepted_2007_to_2018Q4 export. Loaded by finance/scripts/load_loans.py.';

COMMENT ON COLUMN public.loans.id IS
    'Lending Club loan id (source column was text; non-numeric trailer rows are dropped at load).';
COMMENT ON COLUMN public.loans.issue_d IS
    'Month the loan was funded, stored as the first of that month (source: "Dec-2015").';
COMMENT ON COLUMN public.loans.term_months IS
    'Loan term in months, parsed from the source text " 36 months" / " 60 months".';
COMMENT ON COLUMN public.loans.int_rate_pct IS
    'Interest rate as a percent, not a fraction: 13.99 means 13.99%.';
COMMENT ON COLUMN public.loans.emp_length_years IS
    'Employment length in years: "< 1 year" -> 0, "10+ years" -> 10, NULL = not reported.';
COMMENT ON COLUMN public.loans.dti IS
    'Debt-to-income as reported. Source sentinels are preserved: values of -1 and 999 occur.';
COMMENT ON COLUMN public.loans.revol_util_pct IS
    'Revolving line utilisation as a percent, not a fraction. Can exceed 100.';
COMMENT ON COLUMN public.loans.loan_status IS
    'Loan status with the "Does not meet the credit policy. Status:" prefix stripped; '
    'see meets_credit_policy.';
COMMENT ON COLUMN public.loans.meets_credit_policy IS
    'FALSE when the source loan_status carried the "Does not meet the credit policy. Status:" prefix.';
COMMENT ON COLUMN public.loans.last_pymnt_d IS
    'Month of the last payment received, first of month. NULL = no payment recorded.';
