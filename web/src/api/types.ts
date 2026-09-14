export type AttemptRow = {
  number: number
  kind: string
  prompt_id: string
  session_id: string
  error?: string | null
  ended_at?: string | null
}

export type PromptRow = {
  id: string
  text: string
  posted_at: string
}

export type JobItem = {
  job_id: string
  jira_id: string
  status: string
  live: boolean
  agent_mode?: string
  model?: string
  session_id?: string
  repo_url?: string
  source_branch?: string
  clone_path?: string
  serve_pid?: number | null
  serve_port?: number | null
  timeout_in_seconds?: number
  retry_count?: number
  attempt?: number
  started_at?: string | null
  completed_at?: string | null
  accepted_at?: string | null
  error_message?: string | null
  callback_status_code?: number | null
  text?: string
  original_posted?: boolean
  attempts?: AttemptRow[]
  job_kind?: string
  source?: string
  provider?: string
  web_url?: string
  mr_title?: string
  trigger?: string
  mr_key?: string
  target_branch?: string
  comment_text?: string
  parent_comment_text?: string
  error_class?: string
  diagnostics?: Record<string, unknown>
}

export type JobsPayload = {
  jobs: JobItem[]
  total: number
  page: number
  page_size: number
  filter?: string
  server_time: string
}

export type ChatPart = {
  id?: string
  type: string
  text?: string
  tool?: string
  status?: string
  output?: string
  input?: Record<string, unknown>
}

export type ChatMessage = {
  id: string
  session_id: string
  role: string
  finish?: string | null
  created_at?: unknown
  parts: ChatPart[]
}

export type JobChatPayload = {
  job_id: string
  session_ids: string[]
  messages: ChatMessage[]
}

export type LogLine = {
  timestamp: string
  message: string
  job_id?: string
  jira_id?: string
}

export type ReportLogBlob = {
  text: string
  missing: boolean
  truncated?: boolean
  path?: string
  name?: string
}

export type ReviewSettings = {
  review_model: string
  review_timeout_seconds: number
  review_agent: string
  env_model: string
  env_timeout: number
  env_agent: string
  models: string[]
  agents: string[]
  webhook_gitlab_url?: string
  webhook_azure_url?: string
}

export type ReportJobSummary = {
  total?: number
  by_status?: Record<string, number>
  by_kind?: Record<string, number>
  live?: Array<Record<string, unknown>>
  recent?: Array<Record<string, unknown>>
}

export type ReportContext = {
  meta?: { app_name?: string; version?: string; server_time?: string }
  runtime?: Record<string, unknown>
  settings?: Record<string, unknown>
  queue?: { items: JobItem[]; queued_count: number }
  review_queue?: { items: Array<Record<string, unknown>>; queued_count: number }
  live?: {
    running: number
    queued: number
    n8n_running?: number
    n8n_queued?: number
    review_running?: number
    review_queued?: number
  }
  manager?: Record<string, unknown>
  layout?: Record<string, unknown>
  jobs_summary?: ReportJobSummary
  app_log?: ReportLogBlob
  crash_log?: ReportLogBlob
  wrapper_exit_log?: ReportLogBlob
  opencode_logs?: ReportLogBlob[]
  service_logs?: ReportLogBlob[]
  log_files_present?: string[]
  serve_logs_present?: string[]
  serve_logs?: Array<{ name: string; bytes?: number; mtime?: number }>
  server_time?: string
}
