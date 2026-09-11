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
  result_id?: string;
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
export type AnalysisSelection = {
  dimension: number;
  measure: number;
  kind: 'comparison' | 'trend';
};
export type ResultAnalysis = {
  selection: AnalysisSelection;
  dimension_label: string;
  measure_label: string;
  aggregation: 'sum';
  points: {
    dimension: string | number | boolean | null;
    label: string;
    sum: string | null;
    position: number | null;
    row_count: number;
    non_null_count: number;
  }[];
  zero_position: number;
  minimum: string | null;
  maximum: string | null;
  first_to_last_difference: string | null;
  notes: string[];
};
export type ResultSnapshot = {
  version: string;
  conversation_id: string;
  run_id: string;
  result_id: string;
  finished_at: string;
  prompt: string;
  sql: string;
  report: Report & { result: QueryResult };
  notes: string[];
  analysis: ResultAnalysis | null;
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
  mode: 'chat' | 'analyze' | 'query';
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
export type Identity = {
  authorization_version: string;
  username: string;
  display_name: string;
  allowed_tables: string[];
  model_tables: string[];
  model_enabled: boolean;
};
export type AuthSession = {
  access_mode?: 'local' | 'password';
  session_id?: string;
  authenticated: boolean;
  identity?: Identity;
  model_boundary: string;
};
export type AppStatus = {
  changes_enabled?: boolean;
  identity: Identity;
  model_boundary: string;
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

export type KnowledgeDraft = {
  kind: 'metric' | 'relationship' | 'sql_template';
  title: string;
  definition: string;
  source: string;
  source_version: string;
  invalidation_condition: string;
  expires_at: string;
  tables: string[];
  sql: string | null;
  relationship: {
    table: string;
    columns: string[];
    referenced_table: string;
    referenced_columns: string[];
  } | null;
};
export type KnowledgeItem = {
  id: string;
  payload: KnowledgeDraft;
  digest: string;
  state: 'draft' | 'confirmed' | 'revoked';
  created_at: string;
  confirmed_at: string | null;
  revoked_at: string | null;
  reason: string | null;
  schema_hashes: Record<string, string> | null;
};
