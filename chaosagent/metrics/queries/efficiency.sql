-- Efficiency: what each configuration costs to get its reliability.
--
-- A config that halves silent corruption at 4x the token cost is a different
-- product decision from one that does it at 1.1x, so the cost columns belong
-- next to the benefit columns rather than in an appendix.
--
-- call_overhead    — calls actually made / the reference solver's optimal
--                    count. Verification reads and blind retries both land
--                    here, which is exactly the price of the intervention.
-- retry_storm      — calls made after the fault, normalised by optimal length.
-- budget_exhausted — a config that "recovers" by burning ten times the budget
--                    has not recovered; those runs are reported separately
--                    rather than folded into the failure rate.
SELECT
    config,
    COUNT(*)                                                        AS n,
    AVG(CAST(trajectory_len AS DOUBLE) / NULLIF(optimal_len, 0))    AS call_overhead,
    AVG(CASE WHEN fault_position IS NOT NULL
             THEN CAST(trajectory_len - fault_position AS DOUBLE)
                  / NULLIF(optimal_len, 0) END)                     AS retry_storm,
    AVG(tokens_in + tokens_out)                                     AS tokens_per_run,
    AVG(usd)                                                        AS usd_per_run,
    AVG(CAST(llm_calls AS DOUBLE))                                  AS llm_turns,
    AVG(CASE WHEN stop_reason IN ('budget', 'max_calls')
             THEN 1.0 ELSE 0.0 END)                                 AS budget_exhausted_rate,
    AVG(CASE WHEN stop_reason = 'error' THEN 1.0 ELSE 0.0 END)      AS error_rate
FROM runs
WHERE experiment = ?
GROUP BY 1
ORDER BY usd_per_run;
