-- The trap itself, at call granularity.
--
-- One row per call where the world executed and the agent was told it failed.
-- Almost every headline number in the project is a rollup of this condition;
-- keeping it as its own query makes that visible rather than implied.
--
-- `settled` records what the agent did next with the affected entity: whether
-- any later call touched it at all. A trap call with no follow-up is an agent
-- that walked away from a write it never confirmed.
SELECT
    r.config,
    COALESCE(r.fault_class, 'none')                                 AS fault_class,
    c.tool,
    COUNT(*)                                                        AS trap_calls,
    SUM(CASE WHEN c.idempotency_key IS NOT NULL THEN 1 ELSE 0 END)  AS with_idempotency_key,
    SUM(CASE WHEN follow.n > 0 THEN 1 ELSE 0 END)                   AS followed_up
FROM calls c
JOIN runs r USING (run_id)
LEFT JOIN (
    SELECT c2.run_id, c2.call_index AS origin, COUNT(*) AS n
    FROM calls c2
    JOIN calls c3
      ON c3.run_id = c2.run_id
     AND c3.call_index > c2.call_index
    GROUP BY 1, 2
) follow
  ON follow.run_id = c.run_id AND follow.origin = c.call_index
WHERE r.experiment = ?
  AND c.env_executed
  AND c.agent_saw_error
GROUP BY 1, 2, 3
ORDER BY trap_calls DESC;
