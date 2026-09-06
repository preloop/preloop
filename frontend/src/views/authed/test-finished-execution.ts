/**
 * One finished execution, shared by the executions list and the execution
 * page tests.
 *
 * Staging showed the same run as "0 tool calls, $0.03" in the table and "16
 * tool calls, $0.08" on its own page, because the table printed the rollup
 * columns the orchestrator writes once while the page showed the aggregation
 * behind /metrics. The server now projects that aggregation onto the list
 * rows too, so both views read one row and have to state one pair of
 * numbers. The fixture is shared so the two tests cannot drift apart.
 */
export const FINISHED_EXECUTION_TOOL_CALLS = 16;
export const FINISHED_EXECUTION_COST = 0.08;

export const FINISHED_EXECUTION = {
  id: 'exec-shared-numbers-1',
  flow_id: 'flow-1',
  flow_name: 'Nightly Sync',
  status: 'SUCCEEDED',
  start_time: '2026-03-09T10:00:00Z',
  end_time: '2026-03-09T10:05:00Z',
  tool_calls_count: FINISHED_EXECUTION_TOOL_CALLS,
  estimated_cost: FINISHED_EXECUTION_COST,
  total_tokens: 1200,
  mcp_usage_logs: [],
  logs: [],
};
