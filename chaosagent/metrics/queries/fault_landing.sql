-- Fault landing rate — how often an injection found an eligible call.
--
-- This must be reported, not assumed. A single-fault schedule stays armed until
-- an eligible call appears, and for some classes that call may never arrive:
-- `stale` and `silent_empty` apply only to reads, and an agent that front-loads
-- its reads and then writes to the end of the trajectory offers no read *after*
-- a mutation for a stale view to be built from.
--
-- Runs where nothing landed are excluded from every faulted-arm rate, so
-- without this column a low landing rate would silently show up as a small `n`
-- with no explanation. It is a property of the agent's read/write interleaving
-- as much as of the injector, and it is why the tool-targeted arm exists.
SELECT
    fault_class,
    COUNT(*)                                                        AS attempted,
    SUM(CASE WHEN fault_landed THEN 1 ELSE 0 END)                   AS landed,
    AVG(CASE WHEN fault_landed THEN 1.0 ELSE 0.0 END)               AS landing_rate,
    AVG(CASE WHEN fault_landed
             THEN CAST(fault_position AS DOUBLE) / NULLIF(optimal_len, 0) END)
                                                                    AS mean_position
FROM runs
WHERE experiment = ?
  AND fault_class IS NOT NULL
GROUP BY 1
ORDER BY landing_rate;
