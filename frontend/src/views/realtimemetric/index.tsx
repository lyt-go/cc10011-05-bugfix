import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/realtimemetric'
const columns = [
  "指标编号",
  "指标名称",
  "计算窗口秒",
  "聚合函数",
  "数据源流",
  "维度字段组",
  "延迟容忍毫秒",
  "精度位数"
]
const statuses = [
  "草稿",
  "定义评审中",
  "已上线",
  "已下线"
]
// 状态机：每个状态只允许推进到下一段，生命周期不能跳段或倒回
const STATUS_ACTIONS: Record<string, string[]> = {
  "草稿": ["提交定义评审"],
  "定义评审中": ["上线指标"],
  "已上线": ["下线指标"],
  "已下线": []
}

type Row = Record<string, any>

const emptyForm = (): Record<string, string> => Object.fromEntries(columns.map((c) => [c, '']))
const emptyWindow = () => ({ 窗口开始: '', 窗口结束: '', 聚合值: '', 样本数: '' })

export default function RealtimemetricView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [formVisible, setFormVisible] = useState(false)
  const [form, setForm] = useState<Record<string, string>>(emptyForm())
  const [windowFor, setWindowFor] = useState<Row | null>(null)
  const [windowForm, setWindowForm] = useState<Record<string, string>>(emptyWindow())
  const [windowRecords, setWindowRecords] = useState<Row[]>([])

  const stats = useMemo(() => [
    { label: '指标定义总数', value: total },
    { label: '已上线指标', value: rows.filter((row) => row.status === '已上线').length },
    { label: '评审中指标', value: rows.filter((row) => row.status === '定义评审中').length },
  ], [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('指标定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '实时指标列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const reloadWindows = useCallback(async (row: Row) => {
    try {
      const response = await request(`${ENDPOINT}/${row.id}/windows`)
      if (!response.ok) {
        throw new Error('窗口聚合记录读取失败')
      }
      const payload = await response.json()
      setWindowRecords(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '窗口聚合记录读取失败')
    }
  }, [])

  async function parseResult(response: Response, fallback: string) {
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload || payload.ok === false) {
      throw new Error(payload?.message ?? payload?.detail ?? fallback)
    }
    return payload
  }

  async function submitForm() {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await parseResult(response, '指标定义登记失败')
      setNoticeMessage(payload.message ?? '指标定义已登记')
      setFormVisible(false)
      setForm(emptyForm())
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标定义登记失败')
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
      const payload = await parseResult(response, '实时指标动作未生效，请稍后重试')
      setNoticeMessage(payload.message ?? `指标定义已${action}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '实时指标操作失败')
    }
  }

  async function openWindowPanel(row: Row) {
    setWindowFor(row)
    setWindowForm(emptyWindow())
    setErrorMessage('')
    setNoticeMessage('')
    await reloadWindows(row)
  }

  async function submitWindow() {
    if (!windowFor) {
      return
    }
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${windowFor.id}/windows`, {
        method: 'POST',
        body: JSON.stringify({ values: windowForm }),
      })
      const payload = await parseResult(response, '窗口聚合记录写入失败')
      setNoticeMessage(payload.message ?? '窗口聚合记录已写入')
      setWindowForm(emptyWindow())
      await reloadWindows(windowFor)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '窗口聚合记录写入失败')
    }
  }

  return (
    <section className="page" data-module="realtimemetric">
      <header className="page-head">
        <div>
          <h2>实时指标管理</h2>
          <p className="page-desc">定义实时计算指标，配置计算窗口、聚合逻辑和延迟容忍。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => { setForm(emptyForm()); setFormVisible(true); setErrorMessage(''); setNoticeMessage('') }}>
            登记指标定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出实时指标清单
          </button>
        </div>
      </header>

      {formVisible && (
        <div className="form-panel">
          <h3 className="panel-title">登记指标定义</h3>
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
            <button className="btn ghost" type="button" onClick={() => setFormVisible(false)}>取消</button>
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
            <th>关联大盘</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status ?? '—'}</td>
              <td>{Array.isArray(row.关联大盘) && row.关联大盘.length > 0 ? row.关联大盘.join('、') : '—'}</td>
              <td className="row-actions">
                {(STATUS_ACTIONS[String(row.status)] ?? []).map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                {row.status === '已上线' && (
                  <button className="link" type="button" onClick={() => void openWindowPanel(row)}>
                    上报窗口结果
                  </button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无实时指标数据，可先登记指标定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {windowFor && (
        <div className="panel">
          <h3 className="panel-title">
            指标窗口聚合记录：{windowFor.指标编号}（别的页面实时指标结果同步写入指标窗口聚合记录集）
          </h3>
          <div className="form-grid">
            {(['窗口开始', '窗口结束', '聚合值', '样本数'] as const).map((field) => (
              <label key={field}>
                <span>{field}</span>
                <input
                  value={windowForm[field] ?? ''}
                  placeholder={`请输入${field}`}
                  onChange={(event) => setWindowForm((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="form-actions">
            <button className="btn primary" type="button" onClick={() => void submitWindow()}>写入窗口聚合记录</button>
            <button className="btn ghost" type="button" onClick={() => setWindowFor(null)}>收起</button>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>记录编号</th><th>窗口开始</th><th>窗口结束</th><th>聚合值</th><th>样本数</th><th>来源</th><th>写入时间</th>
              </tr>
            </thead>
            <tbody>
              {windowRecords.map((record) => (
                <tr key={String(record.记录编号)}>
                  <td>{record.记录编号}</td>
                  <td>{record.窗口开始}</td>
                  <td>{record.窗口结束}</td>
                  <td>{record.聚合值}</td>
                  <td>{record.样本数}</td>
                  <td>{record.来源}</td>
                  <td>{record.写入时间}</td>
                </tr>
              ))}
              {windowRecords.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>暂无窗口聚合记录，大盘刷新时会对该指标生成数据缺失异常卡片</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条实时指标记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
