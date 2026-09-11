export type Failure = { code: string; message: string };
export type QueryResult = {
  columns: { name: string; type?: string; mysql_type?: string }[];
  rows: (string | number | boolean | null)[][];
  row_count: number;
  truncated: boolean;
  truncation_reason: string | null;
  result_bytes: number;
  server_statement_status: string;
};
export type Report = {
  status?: string;
  decision: string;
  execution_status?: string;
  findings?: { rule_id: string; message: string; evidence?: Record<string, unknown> }[];
  plan_summary?: {
    tables?: Record<string, unknown>[];
    operations?: unknown[];
    query_cost?: number;
  } | null;
  result?: QueryResult | null;
  error?: Failure | null;
  duration_ms?: number;
  limitations?: string[];
  checked_at?: string;
  database?: string;
};
export type Artifact = { sql: string; report: Report };
export type RunEvent = {
  seq: number;
  time: string;
  event: string;
  operation?: string;
  status?: string;
  code?: string;
  duration_ms?: number;
};
export type Run = {
  id: string;
  conversation_id: string;
  request_id: string;
  prompt: string;
  mode: 'chat' | 'analyze';
  created_at: string;
  finished_at: string | null;
  status: 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  answer: string | null;
  error: Failure | null;
  queries: Artifact[];
  analyses: Artifact[];
  events: RunEvent[];
  missing_query_reports?: number;
};
export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  active_run_id?: string | null;
  runs?: Run[];
  context_turns?: number;
  context_paused?: boolean;
};
export type AppStatus = {
  database: string | null;
  database_configured: boolean;
  model_configured: boolean;
  database_error: string | null;
  model_error: string | null;
  read_only: boolean;
  active_run_id: string | null;
};
export type Schema = {
  table: string;
  columns: { name: string; type: string; nullable: string }[];
  indexes: { name: string; unique: boolean; column: string; position: number; type: string }[];
  foreign_keys: {
    name: string;
    columns: string[];
    referenced_table: string;
    referenced_columns: string[];
  }[];
};
