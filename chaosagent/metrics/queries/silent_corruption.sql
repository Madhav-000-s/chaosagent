-- Silent corruption rate — the headline number.
--
-- The agent reported success and the world is wrong. Both halves are recorded
-- facts: `claimed_success` is parsed from the agent's own RESULT line, and
-- `state_correct` is a state assertion against the task's expected terminal
-- state. Neither is a judgement call.
--
-- Restricted to runs where a fault actually landed. A single-fault run whose
-- fault never found an eligible call is not a faulted run, and pooling it here
-- would dilute every rate in the table.
SELECT
    config,
    COALESCE(fault_class, 'none')                                   AS fault_class,
    COUNT(*)                                                        AS n,
    AVG(CASE WHEN claimed_success AND NOT state_correct
             THEN 1.0 ELSE 0.0 END)                                 AS scr
FROM runs
WHERE experiment = ?
  AND fault_class IS NOT NULL
  AND fault_landed
GROUP BY 1, 2
ORDER BY 1, 2;
