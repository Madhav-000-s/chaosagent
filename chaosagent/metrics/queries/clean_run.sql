-- Clean-run competence — the control arm, and the check that keeps the study honest.
--
-- Any configuration that fixes faults but degrades performance with no faults
-- present has not made the agent better, and this is where that shows up. It is
-- also the task-competence baseline: if `naive` cannot do the task cleanly, no
-- recovery number below it means anything.
SELECT
    config,
    COUNT(*)                                                        AS n,
    AVG(CASE WHEN state_correct THEN 1.0 ELSE 0.0 END)              AS clean_success_rate,
    AVG(CASE WHEN claimed_success AND NOT state_correct
             THEN 1.0 ELSE 0.0 END)                                 AS clean_scr,
    AVG(CAST(trajectory_len AS DOUBLE) / NULLIF(optimal_len, 0))    AS clean_call_overhead,
    AVG(usd)                                                        AS clean_usd_per_run
FROM runs
WHERE experiment = ?
  AND fault_class IS NULL
GROUP BY 1
ORDER BY clean_success_rate DESC;
