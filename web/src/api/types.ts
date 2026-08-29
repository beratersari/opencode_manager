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
