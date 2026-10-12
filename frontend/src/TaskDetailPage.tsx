// 页面 B：任务详情（DAG / 步骤表 + attempts 展开 / 事件时间线 / 取消与重试）

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiRequestError } from './api'
import type { Attempt, StepFull, TaskDetail, TaskEvent } from './types'
import {
  ErrorBanner, eventDotClass, fmtDuration, fmtTime, isTerminal, NotFound,
  SkeletonRows, StatusBadge, Toast, usePolling, useToast,
} from './components'

/** 6 节点 DAG 布局（列 x, 行 y），节点 150px 宽 */
const DAG_POS: Record<string, [number, number]> = {
  parse_assets: [240, 10],
  analyze_scenes: [240, 75],
  generate_clips: [110, 150],
  generate_audio: [370, 150],
  compose_video: [240, 225],
  export_result: [240, 288],
}
const EDGES: [string, string][] = [
  ['parse_assets', 'analyze_scenes'],
  ['analyze_scenes', 'generate_clips'],
  ['analyze_scenes', 'generate_audio'],
  ['generate_clips', 'compose_video'],
  ['generate_audio', 'compose_video'],
  ['compose_video', 'export_result'],
]

function DagView({ steps }: { steps: StepFull[] }) {
  const byType = useMemo(() => new Map(steps.map((s) => [s.step_type, s])), [steps])
  return (
    <div className="dag-wrap">
      <div className="dag">
        {EDGES.map(([from, to]) => {
          const [x1, y1] = DAG_POS[from]; const [x2, y2] = DAG_POS[to]
          const w = Math.hypot(x2 + 75 - (x1 + 75), y2 - y1)
          const ang = (Math.atan2(y2 - y1, x2 + 75 - (x1 + 75)) * 180) / Math.PI
          return <div key={`${from}->${to}`} className="dag-edge" style={{ left: x1 + 75, top: y1 + 42, width: w, transform: `rotate(${ang}deg)` }} />
        })}
        {Object.entries(DAG_POS).map(([type, [x, y]]) => {
          const s = byType.get(type)
          if (!s) return null
          return (
            <div key={type} className={`dag-node ${s.status}`} style={{ left: x, top: y }}>
              <div className="name">{type}</div>
              <div className="st">
                <StatusBadge status={s.status} />
                {s.attempt_count > 0 && <span className="small muted"> · #{s.attempt_count}/{s.max_attempts}</span>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function AttemptList({ stepId }: { stepId: string }) {
  const [attempts, setAttempts] = useState<Attempt[] | null>(null)
  useEffect(() => {
    let alive = true
    api.getAttempts(stepId).then((d) => { if (alive) setAttempts(d) }).catch(() => { if (alive) setAttempts([]) })
    return () => { alive = false }
  }, [stepId])
  if (attempts === null) return <div className="small muted">加载尝试历史…</div>
  if (attempts.length === 0) return <div className="small muted">尚无执行尝试</div>
  return (
    <table className="tbl">
      <thead>
        <tr><th>#</th><th>attempt_id</th><th>worker</th><th>状态</th><th className="num">耗时</th><th>错误</th><th></th></tr>
      </thead>
      <tbody>
        {attempts.map((a) => (
          <tr key={a.attempt_id}>
            <td className="num">{a.attempt_no}</td>
            <td className="mono" title={a.attempt_id}>{a.attempt_id.slice(0, 8)}…</td>
            <td className="mono" title={a.worker_id ?? ''}>{a.worker_id ? a.worker_id.split(':').slice(0, 2).join(':') : '—'}</td>
            <td><StatusBadge status={a.status} /></td>
            <td className="num">{fmtDuration(a.duration_ms)}</td>
            <td className="small" title={a.error_message ?? ''}>{a.error_code ?? (a.status === 'SUCCEEDED' && (a.duration_ms ?? 1) === 0 ? '缓存命中' : '—')}</td>
            <td><a href={`#/attempts/${a.attempt_id}`}>详情 →</a></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function TaskDetailPage({ taskId }: { taskId: string }) {
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [steps, setSteps] = useState<StepFull[] | null>(null)
  const [events, setEvents] = useState<TaskEvent[] | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const { toast, ok, err } = useToast()

  const load = useCallback(async () => {
    try {
      const [t, s, e] = await Promise.all([
        api.getTask(taskId),
        api.getSteps(taskId),
        api.getEvents(taskId, 100),
      ])
      setTask(t); setSteps(s); setEvents(e); setError(''); setNotFound(false)
    } catch (e2) {
      if (e2 instanceof ApiRequestError && e2.httpStatus === 404) setNotFound(true)
      else setError(e2 instanceof ApiRequestError ? `${e2.code}: ${e2.message}` : String(e2))
    }
  }, [taskId])

  useEffect(() => { void load() }, [load])

  const active = task != null && !isTerminal(task.status)
  ;(window as unknown as { __poll_idle?: boolean }).__poll_idle = !active
  usePolling(() => { void load() }, 2000, 10_000, !notFound)

  const cancelTask = async () => {
    if (!task || busy) return
    if (!window.confirm(`确认取消任务 ${task.task_id.slice(0, 8)}…？运行中的步骤将在检查点停止。`)) return
    setBusy(true)
    try {
      await api.cancelTask(task.task_id)
      ok('取消请求已提交')
      await load()
    } catch (e2) {
      err(e2 instanceof ApiRequestError ? `取消失败 ${e2.code}: ${e2.message}` : String(e2))
    } finally { setBusy(false) }
  }

  const retryStep = async (step: StepFull) => {
    if (busy) return
    setBusy(true)
    try {
      const r = await api.retryStep(step.step_id)
      ok(`重试已受理（attempt 计数已重置${Array.isArray((r as { downstream_reset?: string[] }).downstream_reset) ? '，下游已复位' : ''}）`)
      await load()
    } catch (e2) {
      err(e2 instanceof ApiRequestError ? `重试失败 ${e2.code}: ${e2.message}` : String(e2))
    } finally { setBusy(false) }
  }

  if (notFound) return <div className="page"><NotFound text={`任务 ${taskId} 不存在（可能已被清理或 ID 有误）`} /></div>

  const totalMs = task && task.started_at
    ? Math.round(((task.finished_at?.epoch ?? Date.now() / 1000) - task.started_at.epoch) * 1000)
    : null

  return (
    <div className="page">
      <div className="topbar">
        <a href="#/">← 任务列表</a>
        <h1 className="mono" style={{ fontSize: 16 }}>{taskId}</h1>
        <span className="spacer" />
        {task && !isTerminal(task.status) && (
          <button className="btn danger" disabled={busy} onClick={() => void cancelTask()}>取消任务</button>
        )}
      </div>

      <Toast toast={toast} />
      {error && <ErrorBanner error={error} onRetry={() => void load()} />}
      {!task && !error && !notFound && <SkeletonRows rows={6} />}

      {task && (
        <>
          <div className="panel">
            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'center' }}>
              <StatusBadge status={task.status} />
              <span className="small muted">推导状态: <b>{task.derived_status}</b> ({task.reason_code})</span>
              <span className="small muted">步骤: <b>{task.succeeded_steps}/{task.total_steps}</b> · 进度 <b>{task.progress_percent}%</b>（等权：成功步骤数/总步骤数）</span>
              <span className="small muted">工作流版本: {task.workflow_version}</span>
              {task.cancel_requested && <span className="badge FAILED">已请求取消</span>}
            </div>
            <dl className="kv" style={{ marginTop: 10 }}>
              <dt>创建时间</dt><dd>{fmtTime(task.created_at)}</dd>
              <dt>开始时间</dt><dd>{fmtTime(task.started_at)}</dd>
              <dt>结束时间</dt><dd>{fmtTime(task.finished_at)}</dd>
              <dt>总耗时</dt><dd>{task.started_at ? fmtDuration(totalMs) : '—'}</dd>
              <dt>错误摘要</dt><dd>{task.error_summary ?? '—'}</dd>
              {task.fault_injection && Object.keys(task.fault_injection).length > 0 && (
                <>
                  <dt>故障注入（演示）</dt>
                  <dd className="mono">{JSON.stringify(task.fault_injection)}</dd>
                </>
              )}
            </dl>
          </div>

          {steps && (
            <div className="panel">
              <h2>步骤 DAG（当前实际状态）</h2>
              <DagView steps={steps} />
            </div>
          )}

          {steps && (
            <div className="panel">
              <h2>步骤明细（点击行展开执行尝试）</h2>
              <table className="tbl">
                <thead>
                  <tr>
                    <th>步骤</th><th>状态</th><th>依赖</th><th className="num">尝试</th>
                    <th>下次重试</th><th>最近错误</th><th>输出</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {steps.map((s) => {
                    const canRetry = s.status === 'FAILED'
                    const blocked = canRetry && s.dependencies.length > 0
                    return (
                      <>
                        <tr key={s.step_id} style={{ cursor: 'pointer' }} onClick={() => setExpanded(expanded === s.step_id ? null : s.step_id)}>
                          <td className="mono">{s.step_type}</td>
                          <td><StatusBadge status={s.status} /></td>
                          <td className="small muted">{s.dependencies.join(', ') || '—'}</td>
                          <td className="num">{s.attempt_count}/{s.max_attempts}</td>
                          <td className="small nowrap">{fmtTime(s.next_retry_at)}</td>
                          <td className="small" title={s.error_message ?? ''}>
                            {s.error_code ? `${s.error_code}` : '—'}
                            {s.error_message && <div className="muted">{s.error_message.slice(0, 36)}…</div>}
                          </td>
                          <td className="mono small" title={s.output_ref ?? ''}>{s.output_ref ? s.output_ref.split('/').slice(-3, -1).join('/') : '—'}</td>
                          <td onClick={(e) => e.stopPropagation()}>
                            {canRetry && (
                              <button
                                className="btn small"
                                disabled={busy}
                                title={blocked ? '依赖未全部成功，重试将被后端拒绝（DEPENDENCY_NOT_SATISFIED）' : '重新执行该步骤'}
                                onClick={() => void retryStep(s)}
                              >重试</button>
                            )}
                          </td>
                        </tr>
                        {expanded === s.step_id && (
                          <tr key={`${s.step_id}-att`}>
                            <td colSpan={8} style={{ background: 'var(--bg)' }}>
                              <div className="attempt-box">
                                <div className="small muted" style={{ marginBottom: 6 }}>
                                  执行尝试历史（{s.step_type}）· input_hash <span className="mono">{s.input_hash.slice(0, 12)}…</span> · step_version {s.step_version}
                                </div>
                                <AttemptList stepId={s.step_id} />
                              </div>
                            </td>
                          </tr>
                        )}
                      </>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          {events && (
            <div className="panel">
              <h2>事件时间线（最近 {events.length} 条，倒序）</h2>
              <ul className="timeline">
                {events.slice().reverse().map((e) => (
                  <li key={e.event_id}>
                    <span className="dot {eventDotClass(e.event_type) ? '' : ''}" style={{}} ><i className={`dot ${eventDotClass(e.event_type)}`} /></span>
                    <span className="t">{fmtTime(e.created_at)}</span>
                    <span className="mono" style={{ minWidth: 150 }}>{e.event_type}</span>
                    <span className="small muted" style={{ flex: 1, wordBreak: 'break-all' }}>
                      {e.step_id ? `${e.step_id.slice(0, 8)}… ` : ''}
                      {e.worker_id ? `· ${e.worker_id.split(':')[1] ?? e.worker_id}` : ''}
                      {e.error_code ? ` · ${e.error_code}` : ''}
                      {e.duration_ms != null ? ` · ${e.duration_ms}ms` : ''}
                      {e.detail ? ` · ${JSON.stringify(e.detail).slice(0, 80)}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  )
}
