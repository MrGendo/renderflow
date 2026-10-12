// 入口：hash 路由（#/、#/tasks/:id、#/attempts/:id）+ 亮暗主题切换

import { useEffect, useState } from 'react'
import TaskListPage from './TaskListPage'
import TaskDetailPage from './TaskDetailPage'
import AttemptDetailPage from './AttemptDetailPage'

function useHashRoute(): [string, string[]] {
  const [hash, setHash] = useState(window.location.hash || '#/')
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || '#/')
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  return [parts[0] ?? '', parts]
}

function ThemeToggle() {
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem('rf-theme') as 'light' | 'dark') || 'light',
  )
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('rf-theme', theme)
  }, [theme])
  return (
    <button className="btn small" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>
      {theme === 'light' ? '🌙 暗色' : '☀️ 亮色'}
    </button>
  )
}

export default function App() {
  const [route, parts] = useHashRoute()

  let page: React.ReactNode
  if (route === 'tasks' && parts[1]) page = <TaskDetailPage taskId={parts[1]} />
  else if (route === 'attempts' && parts[1]) page = <AttemptDetailPage attemptId={parts[1]} />
  else page = <TaskListPage />

  return (
    <>
      {page}
      <div style={{ position: 'fixed', right: 16, bottom: 14 }}>
        <ThemeToggle />
      </div>
    </>
  )
}
