-- Position sweep (E2) — does *where* the fault lands matter?
--
-- Position is normalised by the task's optimal trajectory length so "early"
-- means the same thing on a 4-call task and a 7-call one, then bucketed.
-- Buckets match `chaosagent.faults.schedule.bucket_for`.
SELECT
    config,
    CASE
        WHEN CAST(fault_position AS DOUBLE) / NULLIF(optimal_len, 0) < 0.34 THEN 'early'
        WHEN CAST(fault_position AS DOUBLE) / NULLIF(optimal_len, 0) < 0.67 THEN 'mid'
        ELSE 'late'
    END                                                             AS position_bucket,
    fault_class,
    COUNT(*)                                                        AS n,
    AVG(CASE WHEN state_correct THEN 1.0 ELSE 0.0 END)              AS recovery_rate,
    AVG(CASE WHEN claimed_success AND NOT state_correct
             THEN 1.0 ELSE 0.0 END)                                 AS scr
FROM runs
WHERE experiment = ?
  AND fault_landed
  AND fault_position IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
