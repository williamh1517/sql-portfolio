-- Secondary indexes for public.loans.
--
-- Executed by finance/scripts/load_loans.py *after* the COPY, inside the same
-- transaction: building these once over a full table is much cheaper than
-- maintaining them row by row during the load.
--
-- The primary key (pk_loans) is declared inline in 01_create_loans.sql instead,
-- so a duplicate id aborts the load as it happens.

CREATE INDEX idx_loans_issue_d     ON public.loans (issue_d);
CREATE INDEX idx_loans_grade       ON public.loans (grade);
CREATE INDEX idx_loans_loan_status ON public.loans (loan_status);
CREATE INDEX idx_loans_purpose     ON public.loans (purpose);
CREATE INDEX idx_loans_addr_state  ON public.loans (addr_state);

-- ANALYZE is transaction-safe (VACUUM is not), so the planner has fresh
-- statistics the moment the load commits.
ANALYZE public.loans;
