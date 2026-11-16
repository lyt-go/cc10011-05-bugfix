import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dashboard'
const columns = [
  "大盘编号",
  "大盘名称",
  "包含指标组",
  "布局模板",
  "刷新频率秒",
  "对比周期",
  "分享范围",
  "负责人"
]
const statuses = [
  "草稿",
  "大盘评审中",
  "已发布",
  "已下线"
]
// 状态机：每个状态只允许推进到下一段，生命周期不能跳段或倒回
const STATUS_ACTIONS: Record<string, string[]> = {
  "草稿": ["提交大盘评审"],
  "大盘评审中": ["发布大盘"],
  "已发布": ["下线大盘"],
  "已下线": []
}

type Row = Record<string, any>
type Snapshot = Record<string, any>

const emptyForm = (): Record<string, string> => Object.fromEntries(columns.map((c) => [c, '']))

export default function DashboardView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [formVisible, setFormVisible] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState<Record<string, string>>(emptyForm())
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [snapshotFor, setSnapshotFor] = useState<number | null>(null)

  const stats = useMemo(() => [
    { label: '大盘配置总数', value: total },
    { label: '已发布大盘', value: rows.filter((row) => row.status === '已发布').length },
    { label: '评审中大盘', value: rows.filter((row) => row.status === '大盘评审中').length },
  ], [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('大盘配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '监控大盘列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function parseResult(response: Response, fallback: string) {
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload || payload.ok === false) {
      throw new Error(payload?.message ?? payload?.detail ?? fallback)
    }
    return payload
  }

  function openCreate() {
    setEditingId(null)
    setForm(emptyForm())
    setFormVisible(true)
    setErrorMessage('')
    setNoticeMessage('')
  }

  function openEdit(row: Row) {
    setEditingId(Number(row.id))
    setForm(Object.fromEntries(columns.map((c) => [c, row[c] == null ? '' : String(row[c])])))
    setFormVisible(true)
    setErrorMessage('')
    setNoticeMessage('')
  }

  async function submitForm() {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const isEdit = editingId !== null
      const response = await request(isEdit ? `${ENDPOINT}/${editingId}` : ENDPOINT, {
        method: isEdit ? 'PUT' : 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await parseResult(response, '大盘配置保存失败')
      setNoticeMessage(payload.message ?? '大盘配置已保存')
      setFormVisible(false)
      setEditingId(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '大盘配置保存失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await parseResult(response, '监控大盘动作未生效，请稍后重试')
      setNoticeMessage(payload.message ?? `大盘配置已${action}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '监控大盘操作失败')
    }
  }

  async function refreshSnapshot(row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshot`, { method: 'POST' })
      const payload = await parseResult(response, '监控大盘指标快照生成失败')
      setSnapshot(payload.entry ?? null)
      setSnapshotFor(Number(row.id))
      setNoticeMessage(payload.message ?? '监控大盘指标快照已生成')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '监控大盘指标快照生成失败')
    }
  }

  async function viewSnapshot(row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshot`)
      if (!response.ok) {
        throw new Error('该大盘还没有生成过指标快照')
      }
      setSnapshot(await response.json())
      setSnapshotFor(Number(row.id))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标快照读取失败')
    }
  }

  return (
    <section className="page" data-module="dashboard">
      <header className="page-head">
        <div>
          <h2>监控大盘管理</h2>
          <p className="page-desc">配置实时监控大盘，组合指标卡片和图表形成业务全景视图。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={openCreate}>
            登记大盘配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出监控大盘清单
          </button>
        </div>
      </header>

      {formVisible && (
        <div className="form-panel">
          <h3 className="panel-title">{editingId !== null ? `保存大盘配置 #${editingId}` : '登记大盘配置'}</h3>
          <div className="form-grid">
            {columns.map((field) => (
              <label key={field}>
                <span>{field}</span>
                <input
                  value={form[field] ?? ''}
                  placeholder={`请输入${field}`}
                  onChange={(event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="form-actions">
            <button className="btn primary" type="button" onClick={() => void submitForm()}>保存</button>
            <button className="btn ghost" type="button" onClick={() => { setFormVisible(false); setEditingId(null) }}>取消</button>
          </div>
        </div>
      )}

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>当前快照编号</th>
            <th>配置冲突</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status ?? '—'}</td>
              <td>{row.当前快照编号 ?? '—'}</td>
              <td>{row.配置冲突 ? <span className="tag-warn">是</span> : '否'}</td>
              <td className="row-actions">
                {(STATUS_ACTIONS[String(row.status)] ?? []).map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                {row.status === '已发布' && (
                  <button className="link" type="button" onClick={() => void refreshSnapshot(row)}>
                    刷新快照
                  </button>
                )}
                {row.当前快照编号 && (
                  <button className="link" type="button" onClick={() => void viewSnapshot(row)}>
                    查看快照
                  </button>
                )}
                {row.status !== '已下线' && (
                  <button className="link" type="button" onClick={() => openEdit(row)}>
                    编辑
                  </button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无监控大盘数据，可先登记大盘配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {snapshot && snapshotFor !== null && (
        <div className="panel">
          <h3 className="panel-title">
            监控大盘指标快照：{snapshot.快照编号}（刷新批次 {snapshot.刷新批次}，状态 {snapshot.状态}）
          </h3>
          <p className="hint-text">
            大盘 #{snapshotFor} 列表与详情引用同一快照；配置版本 {snapshot.配置版本}；生成时间 {snapshot.创建时间}
            {snapshot.完成时间 ? `；完成时间 ${snapshot.完成时间}` : ''}
          </p>
          <p className="hint-text">
            已完成阶段：{(snapshot.已完成阶段 ?? []).map((stage: string) => <span key={stage} className="chip">{stage}</span>)}
          </p>
          <h4 className="panel-title">指标结果</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>指标编号</th><th>指标名称</th><th>聚合函数</th><th>窗口记录数</th>
                <th>聚合值</th><th>最新值</th><th>最大值</th><th>最小值</th>
              </tr>
            </thead>
            <tbody>
              {(snapshot.指标结果 ?? []).map((item: Row) => (
                <tr key={String(item.指标编号)}>
                  <td>{item.指标编号}</td>
                  <td>{item.指标名称 ?? '—'}</td>
                  <td>{item.聚合函数 ?? '—'}</td>
                  <td>{item.窗口记录数 ?? 0}</td>
                  <td>{item.聚合值 ?? '—'}</td>
                  <td>{item.最新值 ?? '—'}</td>
                  <td>{item.最大值 ?? '—'}</td>
                  <td>{item.最小值 ?? '—'}</td>
                </tr>
              ))}
              {(snapshot.指标结果 ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={8}>本批次没有可聚合的指标结果</td></tr>
              )}
            </tbody>
          </table>
          <h4 className="panel-title">异常卡片</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>卡片编号</th><th>指标编号</th><th>异常类型</th><th>说明</th><th>刷新批次</th><th>生成时间</th>
              </tr>
            </thead>
            <tbody>
              {(snapshot.异常卡片 ?? []).map((card: Row) => (
                <tr key={String(card.卡片编号)}>
                  <td>{card.卡片编号}</td>
                  <td>{card.指标编号}</td>
                  <td><span className="tag-warn">{card.异常类型}</span></td>
                  <td>{card.说明}</td>
                  <td>{card.刷新批次}</td>
                  <td>{card.生成时间}</td>
                </tr>
              ))}
              {(snapshot.异常卡片 ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={6}>本批次没有异常卡片</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条监控大盘记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
