// 公共组件与工具

import { useEffect, useRef, useState } from 'react'
import type { TaskStatus, Ts } from './types'

export function fmtTime(ts: Ts | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts.epoch * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

export const TERMINAL: TaskStatus[] = ['SUCCEEDED', 'FAILED', 'CANCELLED']
export const isTerminal = (s: string) => TERMINAL.includes(s as TaskStatus)

/** 状态色点（时间线用） */
export function eventDotClass(t: string): string {
  if (t.includes('SUCCEED') || t === 'CACHE_HIT' || t === 'TASK_CREATED') return 'ok'
  if (t.includes('FAIL') || t === 'TASK_CANCELLED') return 'err'
  if (t.includes('RETRY') || t.includes('RECOVER') || t.includes('FENC') || t.includes('EXPIR')) return 'warn'
  return ''
}

/** 轮询 hook：终态自动降频，页面不可见暂停 */
export function usePolling(fn: () => void, activeMs: number, idleMs: number, enabled = true) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (!enabled) return
    let timer: number | undefined
    let interval = activeMs
    const tick = () => {
      if (document.visibilityState === 'visible') ref.current()
      // 由 fn 内部通过设置 window.__poll_idle 影响节奏
      interval = (window as unknown as { __poll_idle?: boolean }).__poll_idle ? idleMs : activeMs
      timer = window.setTimeout(tick, interval)
    }
    timer = window.setTimeout(tick, interval)
    return () => window.clearTimeout(timer)
  }, [activeMs, idleMs, enabled])
}

/** 操作反馈 toast */
export function useToast() {
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3200)
    return () => clearTimeout(t)
  }, [toast])
  return {
    toast,
    ok: (text: string) => setToast({ kind: 'ok', text }),
    err: (text: string) => setToast({ kind: 'err', text }),
  }
}

export function Toast({ toast }: { toast: { kind: 'ok' | 'err'; text: string } | null }) {
  if (!toast) return null
  return <div className={`banner ${toast.kind}`} role="status">{toast.kind === 'ok' ? '✓ ' : '✕ '}{toast.text}</div>
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${status}`}>{status}</span>
}

export function Progress({ percent, state }: { percent: number; state: string }) {
  const cls = state === 'SUCCEEDED' ? 'done' : state === 'FAILED' ? 'err' : ''
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div className={`progress ${cls}`} style={{ width: 90 }}>
        <i style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
      <span className="small muted">{percent}%</span>
    </div>
  )
}

export function ErrorBanner({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="banner err">
      <span style={{ flex: 1 }}>接口异常：{error}</span>
      {onRetry && <button className="btn small" onClick={onRetry}>重试</button>}
    </div>
  )
}

export function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>
}

export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div style={{ padding: '8px 0' }}>
      {Array.from({ length: rows }).map((_, i) => <div key={i} className="skeleton" />)}
    </div>
  )
}

export function NotFound({ text, back = true }: { text: string; back?: boolean }) {
  return (
    <div className="empty">
      <div style={{ fontSize: 15, marginBottom: 8 }}>{text}</div>
      {back && <a href="#/">← 返回任务列表</a>}
    </div>
  )
}
