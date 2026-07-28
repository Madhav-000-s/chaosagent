-- Model sweep (E4) — does capability substitute for structure?
--
-- The question this table answers: if the frontier model still double-charges
-- under `naive`, then model capability is not a substitute for an idempotency
-- key, and reliability is an interface-design problem rather than a
-- model-selection one.
--
-- Read it by column, not by row: the interesting comparison is `naive` across
-- model tiers versus `guarded` across model tiers.
WITH repeated AS (
    SELECT c.run_id
    FROM calls c
    JOIN runs r USING (run_id)
    WHERE r.experiment = ?
      AND c.env_executed
      AND c.tool IN ('charge_payment', 'reserve_stock', 'ship_order', 'refund_payment')
    GROUP BY c.run_id, c.tool
    HAVING COUNT(*) > 1
)
SELECT
    r.model,
    r.config,
    COUNT(DISTINCT r.run_id)                                        AS n,
    AVG(CASE WHEN r.state_correct THEN 1.0 ELSE 0.0 END)            AS recovery_rate,
    AVG(CASE WHEN r.claimed_success AND NOT r.state_correct
             THEN 1.0 ELSE 0.0 END)                                 AS scr,
    CAST(COUNT(DISTINCT rep.run_id) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT r.run_id), 0)                       AS double_exec_rate,
    AVG(r.usd)                                                      AS usd_per_run
FROM runs r
LEFT JOIN repeated rep USING (run_id)
WHERE r.experiment = ?
  AND r.fault_landed
GROUP BY 1, 2
ORDER BY 1, 2;
