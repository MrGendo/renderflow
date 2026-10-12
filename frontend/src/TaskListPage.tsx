// 页面 A：任务列表（创建 / 筛选 / 轮询刷新）

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiRequestError } from './api'
import type { TaskListItem } from './types'
import {
  Empty, ErrorBanner, fmtDuration, fmtTime, isTerminal, Progress,
  SkeletonRows, StatusBadge, Toast, usePolling, useToast,
} from './components'

const STATUS_FILTERS = ['ALL', 'PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED', 'CANCELLED'] as const

function taskDurationMs(t: TaskListItem): number | null {
  if (t.duration_ms != null) return t.duration_ms
  const start = t.started_at?.epoch ?? t.created_at.epoch
  const end = t.finished_at?.epoch
  if (!end) return Math.max(0, Math.round((Date.now() / 1000 - start) * 1000))
  return Math.round((end - start) * 1000)
}

export default function TaskListPage() {
  const [items, setItems] = useState<TaskListItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState<string>('ALL')
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [assetsText, setAssetsText] = useState('hero.png\nbg.png\nsfx.mp3')
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [advancedJson, setAdvancedJson] = useState('')
  const { toast, ok, err } = useToast()

  const onPickFiles = (e: React.ChangeEvent<HTMLInputElement>) => {
    const list = Array.from(e.target.files ?? [])
    setUploadFiles(list)
    if (list.length > 0) {
      setAssetsText(list.map((f) => f.name).join('\n'))
    }
  }

  const uploadAll = async (taskId: string) => {
    for (const f of uploadFiles) {
      await fetch(`/api/tasks/${taskId}/assets?name=${encodeURIComponent(f.name)}`, {
        method: 'POST', body: await f.arrayBuffer(),
        headers: { 'Content-Type': 'application/octet-stream' },
      })
    }
  }

  const load = useCallback(async () => {
    try {
      const data = await api.listTasks({ page, pageSize: 20, status })
      setItems(data.items)
      setTotal(data.total)
      setError('')
    } catch (e) {
      setError(e instanceof ApiRequestError ? `${e.code}: ${e.message}` : String(e))
    }
  }, [page, status])

  useEffect(() => { void load() }, [load])

  const hasActive = useMemo(
    () => (items ?? []).some((t) => !isTerminal(t.status)),
    [items],
  )
  // 终态任务降频；全部终态再放宽（10s）；页面不可见暂停
  ;(window as unknown as { __poll_idle?: boolean }).__poll_idle = !hasActive
  usePolling(() => { void load() }, 2000, 10_000)

  const createTask = async () => {
    setCreating(true)
    try {
      let body: Record<string, unknown>
      if (advancedJson.trim()) {
        body = JSON.parse(advancedJson) as Record<string, unknown>
      } else {
        const assets = assetsText.split('\n').map((s) => s.trim()).filter(Boolean)
        if (assets.length === 0 && uploadFiles.length === 0) { err('至少输入一个素材文件名或选择上传文件'); setCreating(false); return }
        body = { input: { assets: assets.length > 0 ? assets : ['placeholder.txt'] } }
      }
      const data = await api.createTask(body)
      if (uploadFiles.length > 0) {
        await uploadAll(data.task_id)
        ok(`任务已创建，已上传 ${uploadFiles.length} 个真实素材文件`)
        setUploadFiles([])
      } else {
        ok(`任务已创建：${data.task_id.slice(0, 8)}…`)
      }
      setShowForm(false)
      setPage(1)
      await load()
      window.location.hash = `#/tasks/${data.task_id}`
    } catch (e) {
      err(e instanceof ApiRequestError ? `创建失败 ${e.code}: ${e.message}` : `创建失败: ${String(e)}`)
    } finally {
      setCreating(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / 20))

  return (
    <div className="page">
      <div className="topbar">
        <h1><span className="logo">RenderFlow</span> · 任务管线控制台</h1>
        <span className="spacer" />
        <span className="small muted">{total} 个任务 · 每 {hasActive ? '2s' : '10s'} 自动刷新</span>
        <button className="btn" onClick={() => void load()}>刷新</button>
        <button className="btn primary" onClick={() => setShowForm((v) => !v)}>{showForm ? '收起' : '＋ 新建任务'}</button>
      </div>

      <Toast toast={toast} />

      {showForm && (
        <div className="panel">
          <h2>新建动画素材处理任务</h2>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div>
              <label className="small muted">素材文件（每行一个）</label>
              <textarea value={assetsText} onChange={(e) => setAssetsText(e.target.value)} style={{ marginTop: 4 }} />
              <div style={{ marginTop: 8 }}>
                <label className="small muted">或上传真实素材文件（PNG 会按字节真实解析）：</label>
                <input type="file" multiple accept=".png,.jpg,.mp3,.wav,.txt,.mp4"
                       onChange={onPickFiles} style={{ marginTop: 4, fontSize: 12 }} />
                {uploadFiles.length > 0 && (
                  <div className="small muted" style={{ marginTop: 4 }}>
                    已选 {uploadFiles.length} 个：{uploadFiles.map((f) => f.name).join(', ')}
                  </div>
                )}
              </div>
            </div>
            <div>
              <label className="small muted">高级：完整请求 JSON（可含 step_durations / fault_injection，会覆盖左侧）</label>
              <textarea
                value={advancedJson}
                onChange={(e) => setAdvancedJson(e.target.value)}
                placeholder={'{\n  "input": {"assets": ["hero.png"]},\n  "fault_injection": {"generate_clips": {"fail_first_n_attempts": 1, "failure_type": "TRANSIENT_ERROR"}}\n}'}
                style={{ marginTop: 4, fontFamily: 'ui-monospace, Menlo, monospace' }}
              />
            </div>
          </div>
          <div style={{ marginTop: 10, display: 'flex', gap: 10 }}>
            <button className="btn primary" disabled={creating} onClick={() => void createTask()}>
              {creating ? '创建中…' : '创建任务'}
            </button>
            <span className="small muted" style={{ alignSelf: 'center' }}>创建后立即返回 task_id，后台异步执行</span>
          </div>
        </div>
      )}

      <div className="panel">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>任务列表</h2>
          <span className="spacer" />
          <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
            {STATUS_FILTERS.map((s) => <option key={s} value={s}>{s === 'ALL' ? '全部状态' : s}</option>)}
          </select>
        </div>

        {error && <ErrorBanner error={error} onRetry={() => void load()} />}

        {items === null && !error && <SkeletonRows />}

        {items !== null && items.length === 0 && <Empty text="暂无任务——点击右上角「新建任务」创建一个" />}

        {items !== null && items.length > 0 && (
          <table className="tbl">
            <thead>
              <tr>
                <th>任务 ID</th>
                <th>创建时间</th>
                <th>状态</th>
                <th>进度</th>
                <th className="num">步骤</th>
                <th className="num">耗时</th>
                <th>失败摘要</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.task_id}>
                  <td className="mono" title={t.task_id}>
                    <a href={`#/tasks/${t.task_id}`}>{t.task_id.slice(0, 8)}…</a>
                  </td>
                  <td className="nowrap">{fmtTime(t.created_at)}</td>
                  <td><StatusBadge status={t.status} />{t.status !== t.derived_status && t.derived_status
                    ? <div className="small muted">推导: {t.derived_status} ({t.reason_code})</div> : null}</td>
                  <td><Progress percent={t.progress_percent} state={t.status} /></td>
                  <td className="num">{t.succeeded_steps}/{t.total_steps}</td>
                  <td className="num nowrap">{isTerminal(t.status) ? fmtDuration(taskDurationMs(t)) : `${fmtDuration(taskDurationMs(t))} ⏱`}</td>
                  <td className="small" title={t.error_summary ?? ''}>
                    {t.error_summary ? t.error_summary.slice(0, 40) : '—'}
                  </td>
                  <td><a href={`#/tasks/${t.task_id}`}>详情 →</a></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div style={{ display: 'flex', gap: 8, marginTop: 12, alignItems: 'center' }}>
          <button className="btn small" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← 上一页</button>
          <span className="small muted">第 {page} / {totalPages} 页</span>
          <button className="btn small" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>下一页 →</button>
        </div>
      </div>
    </div>
  )
}
