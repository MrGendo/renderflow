// 统一 fetch 封装：处理 {ok,data|error} 包装、超时、错误分类

import type { ApiError } from './types'

export class ApiRequestError extends Error {
  code: string
  httpStatus: number
  constructor(err: ApiError, httpStatus: number) {
    super(err.message)
    this.code = err.code
    this.httpStatus = httpStatus
  }
}

async function request<T>(path: string, init?: RequestInit, timeoutMs = 8000): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let resp: Response
  try {
    resp = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch (e) {
    throw new ApiRequestError(
      { code: 'NETWORK_ERROR', message: e instanceof Error ? e.message : String(e) },
      0,
    )
  } finally {
    clearTimeout(timer)
  }
  let body: { ok: boolean; data?: T; error?: ApiError }
  try {
    body = await resp.json()
  } catch {
    throw new ApiRequestError({ code: 'BAD_RESPONSE', message: `HTTP ${resp.status} 非 JSON 响应` }, resp.status)
  }
  if (!resp.ok || body.ok === false) {
    throw new ApiRequestError(
      body.error ?? { code: 'UNKNOWN', message: `HTTP ${resp.status}` },
      resp.status,
    )
  }
  return body.data as T
}

export const api = {
  listTasks: (params: { page?: number; pageSize?: number; status?: string } = {}) => {
    const q = new URLSearchParams()
    if (params.page) q.set('page', String(params.page))
    if (params.pageSize) q.set('pageSize', String(params.pageSize))
    if (params.status && params.status !== 'ALL') q.set('status', params.status)
    return request<import('./types').TaskListResponse>(`/api/tasks?${q}`)
  },
  getTask: (id: string) => request<import('./types').TaskDetail>(`/api/tasks/${id}`),
  getSteps: (taskId: string) => request<import('./types').StepFull[]>(`/api/tasks/${taskId}/steps`),
  getAttempts: (stepId: string) => request<import('./types').Attempt[]>(`/api/steps/${stepId}/attempts`),
  getAttempt: (attemptId: string) =>
    request<import('./types').Attempt & { task_id: string }>(`/api/attempts/${attemptId}`),
  getEvents: (taskId: string, limit = 100) =>
    request<import('./types').TaskEvent[]>(`/api/tasks/${taskId}/events?limit=${limit}`),
  createTask: (body: unknown) =>
    request<{ task_id: string; steps: import('./types').StepOverview[] }>('/api/tasks', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  retryStep: (stepId: string) =>
    request<Record<string, unknown>>(`/api/steps/${stepId}/retry`, { method: 'POST' }),
  cancelTask: (taskId: string) =>
    request<Record<string, unknown>>(`/api/tasks/${taskId}/cancel`, { method: 'POST' }),
  health: () => request<{ status: string }>('/api/health'),
}
