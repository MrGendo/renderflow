// 页面 C：执行尝试详情（全字段 + 事件表中的该次尝试执行记录 + 输出引用）
// W5：改用 GET /api/attempts/{id} 直达（后端已补端点），不再遍历任务列表

import { useCallback, useEffect, useState } from 'react'
import { api, ApiRequestError } from './api'
import type { Attempt, TaskEvent } from './types'
import { ErrorBanner, eventDotClass, fmtDuration, fmtTime, NotFound, SkeletonRows, StatusBadge } from './components'

export default function AttemptDetailPage({ attemptId }: { attemptId: string }) {
  const [attempt, setAttempt] = useState<(Attempt & { task_id?: string }) | null>(null)
  const [events, setEvents] = useState<TaskEvent[] | null>(null)
  const [taskId, setTaskId] = useState('')
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const a = await api.getAttempt(attemptId)
      setAttempt(a)
      setTaskId(a.task_id ?? '')
      const evs = a.task_id
        ? await api.getEvents(a.task_id, 1000).catch(() => [] as TaskEvent[])
        : ([] as TaskEvent[])
      setEvents(evs.filter((e) => e.attempt_id === attemptId))
      setError('')
    } catch (e2) {
      if (e2 instanceof ApiRequestError && e2.httpStatus === 404) setNotFound(true)
      else setError(e2 instanceof ApiRequestError ? `${e2.code}: ${e2.message}` : String(e2))
    }
  }, [attemptId])

  useEffect(() => { void load() }, [load])

  if (notFound) return <div className="page"><NotFound text={`执行尝试 ${attemptId} 不存在`} /></div>

  return (
    <div className="page">
      <div className="topbar">
        <a href={taskId ? `#/tasks/${taskId}` : '#/'}>← 返回任务详情</a>
        <h1 className="mono" style={{ fontSize: 15 }}>尝试 {attemptId.slice(0, 12)}…</h1>
      </div>
      {error && <ErrorBanner error={error} onRetry={() => void load()} />}
      {!attempt && !error && <SkeletonRows rows={4} />}
      {attempt && (
        <>
          <div className="panel">
            <h2>尝试信息</h2>
            <dl className="kv">
              <dt>attempt_id</dt><dd className="mono">{attempt.attempt_id}</dd>
              <dt>step_id</dt><dd className="mono">{attempt.step_id}</dd>
              <dt>attempt_no</dt><dd>{attempt.attempt_no}</dd>
              <dt>worker_id</dt><dd className="mono">{attempt.worker_id ?? '—'}</dd>
              <dt>状态</dt><dd><StatusBadge status={attempt.status} /></dd>
              <dt>开始时间</dt><dd>{fmtTime(attempt.started_at)}</dd>
              <dt>结束时间</dt><dd>{fmtTime(attempt.finished_at)}</dd>
              <dt>执行耗时</dt><dd>{fmtDuration(attempt.duration_ms)}</dd>
              <dt>错误码</dt><dd>{attempt.error_code ?? '—'}</dd>
              <dt>错误详情</dt><dd>{attempt.error_message ?? '—'}</dd>
              <dt>日志引用</dt><dd className="mono">{attempt.log_ref ?? '—'}</dd>
              <dt>输出引用</dt><dd className="mono">{attempt.output_ref ?? '—'}</dd>
            </dl>
          </div>
          <div className="panel">
            <h2>执行记录（来自任务事件表，按 attempt 过滤）</h2>
            {events && events.length === 0 && <div className="small muted">该尝试无关联事件（可能为缓存命中提交）</div>}
            <ul className="timeline">
              {(events ?? []).map((e) => (
                <li key={e.event_id}>
                  <i className={`dot ${eventDotClass(e.event_type)}`} />
                  <span className="t">{fmtTime(e.created_at)}</span>
                  <span className="mono" style={{ minWidth: 150 }}>{e.event_type}</span>
                  <span className="small muted">{e.detail ? JSON.stringify(e.detail).slice(0, 100) : ''}</span>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}
    </div>
  )
}
