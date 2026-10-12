// 后端真实 API 契约对应的类型（字段名与 backend/renderflow/api.py 序列化一字不差）

export interface Ts { iso: string; epoch: number }

export interface StepOverview {
  step_id: string
  step_type: string
  status: StepStatus
  attempt_count: number
  max_attempts: number
  error_code: string | null
}

export type TaskStatus =
  | 'PENDING' | 'RUNNING' | 'RETRY_WAIT' | 'FAILED' | 'SUCCEEDED' | 'CANCELLED'
export type StepStatus =
  | 'PENDING' | 'READY' | 'RUNNING' | 'RETRY_WAIT' | 'FAILED' | 'SUCCEEDED' | 'SKIPPED'

export interface TaskListItem {
  task_id: string
  status: TaskStatus
  derived_status: string
  reason_code: string
  progress_percent: number
  succeeded_steps: number
  total_steps: number
  error_summary: string | null
  created_at: Ts
  started_at: Ts | null
  finished_at: Ts | null
  updated_at?: Ts
  duration_ms?: number | null
  steps?: StepOverview[]
}

export interface TaskDetail extends TaskListItem {
  input: unknown
  step_durations: Record<string, number> | null
  fault_injection: Record<string, Record<string, unknown>> | null
  workflow_version: string
  cancel_requested: boolean
  steps: StepOverview[]
}

export interface StepFull {
  step_id: string
  task_id: string
  step_type: string
  status: StepStatus
  dependencies: string[]
  input_hash: string
  step_version: string
  config_hash: string
  output_ref: string | null
  output_checksum: string | null
  attempt_count: number
  max_attempts: number
  next_retry_at: Ts | null
  worker_id: string | null
  heartbeat_at: Ts | null
  lease_until: Ts | null
  lease_version: number
  created_at: Ts | null
  started_at: Ts | null
  finished_at: Ts | null
  error_code: string | null
  error_message: string | null
}

export interface Attempt {
  attempt_id: string
  step_id: string
  attempt_no: number
  worker_id: string | null
  status: string
  started_at: Ts | null
  finished_at: Ts | null
  duration_ms: number | null
  error_code: string | null
  error_message: string | null
  log_ref: string | null
  output_ref: string | null
}

export interface TaskEvent {
  event_id: number
  task_id: string
  step_id: string | null
  attempt_id: string | null
  worker_id: string | null
  event_type: string
  status: string | null
  duration_ms: number | null
  error_code: string | null
  detail: Record<string, unknown> | null
  created_at: Ts
}

export interface ApiError {
  code: string
  message: string
}

export interface TaskListResponse {
  items: TaskListItem[]
  total: number
  page: number
  page_size: number
}
