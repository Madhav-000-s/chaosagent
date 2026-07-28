-- Double-execution rate — binary, damning, unarguable.
--
-- Counted from `calls.env_executed`, which is the environment's own record of
-- what ran, NOT from anything the agent observed. That distinction is the whole
-- point: under a partial_write fault the agent's trace shows one *failed* call
-- while the world shows one committed write. If this were computed from the
-- agent's view it would report zero and the study would have nothing to say.
--
-- The second distinction cost a real bug. An idempotency-key replay *succeeds*
-- and returns the original payload while executing nothing, so `result.ok` is
-- true and the world did not move. Deriving `env_executed` from success rather
-- than from the environment's log invented a double execution that never
-- happened -- and did so precisely for the configurations that got idempotency
-- right. `idempotent_replay` is carried explicitly so that can be audited, and
-- the exclusion below is deliberately redundant with `env_executed`.
--
-- `runs_with_key` is reported alongside because it is not only the harness that
-- supplies keys: a sufficiently capable model populates the optional
-- `idempotency_key` argument on its own, and that shows up here rather than in
-- any configuration flag.
WITH repeated AS (
    SELECT c.run_id, c.tool, COUNT(*) AS n
    FROM calls c
    JOIN runs r USING (run_id)
    WHERE r.experiment = ?
      AND c.env_executed
      AND NOT COALESCE(c.idempotent_replay, FALSE)
      AND c.tool IN ('charge_payment', 'reserve_stock', 'ship_order', 'refund_payment')
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
),
keyed AS (
    SELECT DISTINCT c.run_id
    FROM calls c
    JOIN runs r USING (run_id)
    WHERE r.experiment = ?
      AND c.idempotency_key IS NOT NULL
)
SELECT
    r.config,
    COUNT(DISTINCT r.run_id)                                        AS n,
    COUNT(DISTINCT repeated.run_id)                                 AS runs_with_double_exec,
    CAST(COUNT(DISTINCT repeated.run_id) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT r.run_id), 0)                       AS double_exec_rate,
    COUNT(DISTINCT keyed.run_id)                                    AS runs_with_key
FROM runs r
LEFT JOIN repeated USING (run_id)
LEFT JOIN keyed USING (run_id)
WHERE r.experiment = ?
GROUP BY 1
ORDER BY double_exec_rate DESC;
