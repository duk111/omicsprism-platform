// Auto-generated from FastAPI /openapi.json; run `npm run generate-api-types` to refresh
// Source: FastAPI application export

export interface AgentAdvisoryBlock {
  type?: "advisory";
  category: "general_biology" | "analysis_guidance";
  text: string;
}

export interface AgentErrorBlock {
  type?: "error";
  code: string;
  user_message: string;
  retryable: boolean;
  request_id?: string | null;
}

export interface AgentEvidenceBlock {
  type?: "evidence";
  claims: GroundedClaim[];
}

export type AgentFeedbackCategory = "incorrect_result" | "missing_context" | "bad_plan" | "unsafe_action" | "latency" | "other";

export interface AgentFeedbackCreateRequest {
  rating: AgentFeedbackRating;
  failure_category?: AgentFeedbackCategory | null;
  correction_text?: string | null;
}

export interface AgentFeedbackListResponse {
  feedback: AgentFeedbackResponse[];
  next_cursor?: string | null;
}

export type AgentFeedbackRating = "helpful" | "unhelpful";

export interface AgentFeedbackResponse {
  feedback_id: string;
  message_id: string;
  rating: AgentFeedbackRating;
  failure_category?: AgentFeedbackCategory | null;
  correction_text?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentInputBundleResponse {
  bundle_id: string;
  thread_id: string;
  status: AgentInputBundleStatus;
  expires_at: string;
  created_at: string;
  files?: AgentInputFileResponse[];
}

export type AgentInputBundleStatus = "active" | "consumed" | "expired";

export interface AgentInputFileResponse {
  file_id: string;
  field: string;
  filename: string;
  checksum: string;
  content_type?: string | null;
  size_bytes: number;
  created_at: string;
}

export interface AgentInputSummaryBlock {
  type?: "input_summary";
  bundle_id: string;
  files: AgentInputFileResponse[];
}

export interface AgentJobBlock {
  type?: "job";
  job_id: string;
  status: JobStatus;
  progress: number;
  progress_url: string;
  results_url?: string | null;
}

export interface AgentJobWaitListResponse {
  waits: AgentJobWaitResponse[];
  next_cursor?: string | null;
}

export interface AgentJobWaitResponse {
  wait_id: string;
  thread_id: string;
  turn_id: string;
  run_id: string;
  job_id: string;
  wait_status: "waiting" | "resume_queued" | "completed" | "failed" | "cancelled" | "expired";
  job_status?: JobStatus | null;
  progress?: number | null;
  progress_step?: string | null;
  error?: string | null;
  continuation_turn_id?: string | null;
  created_at: string;
  updated_at: string;
  job_updated_at?: string | null;
}

export interface AgentMessageListResponse {
  messages: AgentMessageResponse[];
  next_cursor?: string | null;
}

export interface AgentMessageResponse {
  message_id: string;
  thread_id: string;
  run_id: string;
  trace_id?: string;
  role: AgentMessageRole;
  blocks: (AgentTextBlock | AgentAdvisoryBlock | AgentInputSummaryBlock | AgentRecommendationBlock | AgentJobBlock | AgentEvidenceBlock | AgentErrorBlock)[];
  created_at: string;
}

export type AgentMessageRole = "user" | "assistant";

export interface AgentRecommendationBlock {
  type?: "recommendation";
  recommendations: AgentRecommendationItem[];
}

export interface AgentRecommendationItem {
  analysis_type: AnalysisType;
  display_label: string;
  reasons?: string[];
}

export interface AgentRunResponse {
  run_id: string;
  thread_id: string;
  focus: RunFocus;
  version: number;
}

export interface AgentStreamEvent {
  event_id: string;
  event_type: "turn.updated" | "message.created" | "job.updated" | "interrupt.updated";
  data: AgentTurnResponse | AgentMessageResponse | AgentJobWaitResponse | GraphPendingInterrupt | null;
}

export interface AgentTextBlock {
  type?: "text";
  text: string;
}

export interface AgentThreadCreateRequest {
  focus_job_ids?: string[];
}

export interface AgentThreadDetailResponse {
  thread: AgentThreadResponse;
  run: AgentRunResponse;
}

export interface AgentThreadListResponse {
  threads: AgentThreadResponse[];
  next_cursor?: string | null;
}

export interface AgentThreadResponse {
  thread_id: string;
  title: string;
  current_run_id: string;
  status: AgentThreadStatus;
  version: number;
  created_at: string;
  updated_at: string;
}

export type AgentThreadStatus = "active" | "archived";

export interface AgentTraceEventListResponse {
  events: AgentTraceEventResponse[];
  next_cursor?: string | null;
}

export interface AgentTraceEventResponse {
  event_id: string;
  trace_id: string;
  thread_id: string;
  turn_id?: string | null;
  run_id?: string | null;
  event_type: "turn.queued" | "turn.started" | "turn.completed" | "turn.failed" | "model.call" | "tool.call" | "job.submitted";
  component: "api" | "runtime" | "graph" | "model" | "tool" | "job";
  name: string;
  schema_version: string;
  graph_version: string;
  prompt_version?: string | null;
  model_provider?: string | null;
  model_name?: string | null;
  tool_name?: string | null;
  job_id?: string | null;
  outcome?: string | null;
  latency_ms?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
  cached_tokens?: number | null;
  usage_status?: "reported" | "unknown" | null;
  retry_count?: number;
  error_code?: string | null;
  created_at: string;
}

export interface AgentTurnCreateRequest {
  message: string;
  input_bundle_id?: string | null;
  focus_job_ids?: string[];
}

export interface AgentTurnListResponse {
  turns: AgentTurnResponse[];
  next_cursor?: string | null;
}

export interface AgentTurnResponse {
  turn_id: string;
  thread_id: string;
  run_id: string;
  trace_id?: string;
  status: AgentTurnStatus;
  attempt: number;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export type AgentTurnStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export type AnalysisType = "deg" | "dem" | "gma";

export interface ApiErrorDetail {
  category: ErrorCategory;
  code: string;
  message: string;
  user_message: string;
  suggestions?: string[];
  technical_detail?: string | null;
  context?: Record<string, unknown>;
}

export interface Body_create_input_bundle_api_agent_threads__thread_id__input_bundles_post {
  files: string[];
  fields: string[];
}

export interface Body_create_job_api_jobs_post {
  analysis_type: string;
  counts?: string | null;
  metadata?: string | null;
  metabs?: string | null;
  transcriptome?: string | null;
  metabolome?: string | null;
  group?: string | null;
  compare_field?: string | null;
  tested_levels?: string | null;
  reference_level?: string | null;
  padj_cutoff?: number | null;
  log2fc_cutoff?: number | null;
  min_total_count?: number | null;
  min_replicates?: number | null;
  same_fields?: string | null;
  normalize?: boolean | null;
  filter_low_expression?: boolean | null;
  method?: string | null;
  fdr_cutoff?: number | null;
  enable_modules?: boolean | null;
  vip_cutoff?: number | null;
  pseudocount?: number | null;
  max_missing_fraction?: number | null;
  impute_method?: string | null;
  log_transform?: boolean | null;
  trans_log2?: boolean | null;
  metab_log2?: boolean | null;
  n_orthogonal_components?: number | null;
}

export interface Body_preflight_job_api_jobs_preflight_post {
  analysis_type: string;
  counts?: string | null;
  metadata?: string | null;
  metabs?: string | null;
  transcriptome?: string | null;
  metabolome?: string | null;
  group?: string | null;
  compare_field?: string | null;
  tested_levels?: string | null;
  reference_level?: string | null;
  padj_cutoff?: number | null;
  log2fc_cutoff?: number | null;
  min_total_count?: number | null;
  min_replicates?: number | null;
  same_fields?: string | null;
  normalize?: boolean | null;
  filter_low_expression?: boolean | null;
  method?: string | null;
  fdr_cutoff?: number | null;
  enable_modules?: boolean | null;
  vip_cutoff?: number | null;
  pseudocount?: number | null;
  max_missing_fraction?: number | null;
  impute_method?: string | null;
  log_transform?: boolean | null;
  trans_log2?: boolean | null;
  metab_log2?: boolean | null;
  n_orthogonal_components?: number | null;
}

export interface Citation {
  artifact: string;
  checksum: string;
  row_ids: number[];
}

export interface ClarificationItem {
  field: string;
  options?: string[];
  reason: string;
}

export interface ClarificationPayload {
  kind?: "clarification";
  missing?: ClarificationItem[];
  question: string;
}

export interface ConfirmationPayload {
  kind?: "confirmation";
  analysis_type: "DEG" | "DEM" | "GMA";
  resolved_params: DEGParams | DEMParams | GMAParams;
  preview?: ContrastPreview | null;
  warnings?: Issue[];
  input_fingerprint: string;
  plan_id: string;
  plan_version: number;
}

export interface ContrastPreview {
  compare_field: string;
  tested_level: string;
  reference_level: string;
  scope?: ScopeSpec;
  same_fields?: string[];
  same_values?: { [key: string]: string };
  tested_count: number;
  reference_count: number;
}

export interface ContrastSpec {
  compare_field: string;
  tested_level: string;
  reference_level: string;
  scope?: ScopeSpec;
}

export interface DEGParams {
  contrast: ContrastSpec;
  min_replicates?: number;
  analysis_type?: "DEG";
  padj_cutoff?: number;
  log2fc_cutoff?: number;
  min_total_count?: number;
  normalize?: boolean;
  filter_low_expression?: boolean;
}

export interface DEMParams {
  contrast: ContrastSpec;
  min_replicates?: number;
  analysis_type?: "DEM";
  padj_cutoff?: number;
  log2fc_cutoff?: number;
  vip_cutoff?: number;
  pseudocount?: number;
  max_missing_fraction?: number;
  impute_method?: string;
  normalize?: boolean;
  log_transform?: boolean;
  n_orthogonal_components?: number;
}

export type ErrorCategory = "input_error" | "permission_error" | "resource_error" | "analysis_failed" | "system_error";

export interface FigureDataResponse {
  figure_id: string;
  title: string;
  chart_type: string;
  interactive_page_id?: string | null;
  static_files?: { [key: string]: string | null };
  plotly_spec?: Record<string, unknown>;
  default_state?: Record<string, unknown>;
  available_states?: Record<string, unknown>;
  style?: Record<string, unknown>;
  tree_data?: Record<string, unknown> | null;
  upset_data?: Record<string, unknown> | null;
  ridge_data?: Record<string, unknown> | null;
  bar_data?: unknown[] | null;
  circos_data?: Record<string, unknown> | null;
}

export type FileArtifactKind = "input" | "output" | "report" | "image" | "log" | "figure" | "temp";

export interface GMAParams {
  analysis_type?: "GMA";
  fdr_cutoff?: number;
  enable_modules?: boolean;
  trans_log2?: boolean;
  metab_log2?: boolean;
  max_missing_fraction?: number;
}

export interface GraphClarificationResumeRequest {
  kind?: "clarification";
  interrupt_id: string;
  answer: string;
}

export interface GraphConfirmationResumeRequest {
  kind?: "confirmation";
  interrupt_id: string;
  plan_id: string;
  plan_version: number;
  message?: string | null;
  approve?: boolean | null;
}

export interface GraphInterrupt {
  interrupt_id: string;
  payload: ClarificationPayload | ConfirmationPayload;
}

export interface GraphPendingInterrupt {
  checkpoint_turn_id: string;
  interrupt: GraphInterrupt;
}

export interface GraphTurnResult {
  checkpoint_turn_id: string;
  turn: AgentTurnResponse;
  message?: AgentMessageResponse | null;
  interrupt?: GraphInterrupt | null;
}

export interface GroundedClaim {
  text: string;
  citation: Citation;
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

export interface ImageInfo {
  kind?: FileArtifactKind;
  field?: string | null;
  filename: string;
  path: string;
  storage_key: string;
  checksum?: string | null;
  content_type?: string | null;
  size_bytes: number;
  created_at: string;
  name: string;
  thumbnail_url: string;
  full_url: string;
  interactive_url?: string | null;
}

export interface Issue {
  code: string;
  message: string;
  field?: string | null;
}

export interface JobFilesResponse {
  job_id: string;
  inputs: UploadedFileInfo[];
  result_files: ResultFileInfo[];
  report_links?: ReportLinks;
}

export interface JobListResponse {
  jobs: JobResponse[];
}

export interface JobLogResponse {
  job_id: string;
  log_name?: string | null;
  content?: string;
}

export type JobOwnerType = "user" | "project";

export interface JobProgressResponse {
  started_at?: string | null;
  completed_at?: string | null;
  estimated_total_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
  estimated_range_min_seconds?: number | null;
  estimated_range_max_seconds?: number | null;
  elapsed_seconds?: number | null;
  estimated_range_label?: string | null;
  job_id: string;
  project_id?: string | null;
  status: JobStatus;
  is_demo?: boolean;
  progress?: number;
  progress_step?: string;
  error?: string | null;
  error_info?: ApiErrorDetail | null;
  recent_log_name?: string | null;
  recent_log_excerpt?: string | null;
}

export interface JobResponse {
  started_at?: string | null;
  completed_at?: string | null;
  estimated_total_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
  estimated_range_min_seconds?: number | null;
  estimated_range_max_seconds?: number | null;
  elapsed_seconds?: number | null;
  estimated_range_label?: string | null;
  id: string;
  project_id?: string | null;
  project_name: string;
  analysis_type: AnalysisType;
  status: JobStatus;
  is_demo?: boolean;
  created_at: string;
  updated_at: string;
  owner_type?: JobOwnerType;
  owner_id?: string;
  owner_label?: string | null;
  progress?: number;
  progress_step?: string;
  error?: string | null;
  result_files?: ResultFileInfo[];
  report_links?: ReportLinks;
  params?: { [key: string]: string | number | boolean | null };
  attempt?: number;
  max_retries?: number;
  error_info?: ApiErrorDetail | null;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface PreflightFileSummary {
  field: string;
  filename: string;
  rows?: number;
  columns?: number;
  sample_names?: string[];
  sample_ids?: string[];
  feature_ids?: string[];
  duplicate_ids?: string[];
  empty_columns?: string[];
  required_columns?: string[];
  non_numeric_cells?: number;
  row_length_issues?: number;
}

export interface PreflightIssue {
  code: PreflightIssueCode;
  field?: string | null;
  severity?: "error" | "warning";
  message: string;
  context?: Record<string, unknown>;
  suggestions?: string[];
}

export type PreflightIssueCode = "invalid_analysis_type" | "missing_required_field" | "missing_required_columns" | "empty_file" | "invalid_csv" | "matrix_schema_invalid" | "group_schema_invalid" | "empty_column" | "duplicate_feature_id" | "duplicate_sample_id" | "sample_mismatch" | "sample_order_mismatch" | "non_numeric_value" | "inconsistent_row_length";

export interface PreflightResponse {
  analysis_type: AnalysisType;
  ok: boolean;
  can_submit: boolean;
  normalized_params?: { [key: string]: string | number | boolean | null };
  files?: PreflightFileSummary[];
  errors?: PreflightIssue[];
  warnings?: PreflightIssue[];
}

export interface ReportLinks {
  summary?: string | null;
  interactive?: string | null;
}

export interface ResultFileInfo {
  kind?: FileArtifactKind;
  field?: string | null;
  filename: string;
  path: string;
  storage_key: string;
  checksum?: string | null;
  content_type?: string | null;
  size_bytes: number;
  created_at: string;
  name: string;
  download_url: string;
}

export interface RunFocus {
  in_scope_job_ids: string[];
  resolved_entities: { [key: string]: string };
  last_citation: Citation | null;
  draft_params?: { [key: string]: string | number | boolean | null };
  preferences?: { [key: string]: string | number | boolean | null };
  draft_analysis_type?: string | null;
  params_source_ref?: string | null;
}

export interface ScopeSpec {
  mode: "fixed" | "stratified" | "all" | "unknown";
  fixed_filters?: { [key: string]: string };
  blocking_fields?: string[];
}

export interface UploadedFileInfo {
  kind?: FileArtifactKind;
  field: string;
  filename: string;
  path: string;
  storage_key: string;
  checksum?: string | null;
  content_type?: string | null;
  size_bytes: number;
  created_at: string;
}

export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}
