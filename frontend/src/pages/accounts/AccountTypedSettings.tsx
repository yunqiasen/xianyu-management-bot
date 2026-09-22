import { useCallback, useEffect, useRef, useState } from 'react'
import {
  announceConfigurationChange, applyAccountConfiguration, readTypedConfiguration, saveTypedConfiguration,
  type ConfigurationScope, type TypedConfiguration,
} from '@/api/account-runtime'

const sources = { account: '账号显式', user: '用户默认', system: '系统默认', default: '内置默认' }
const types = { integer: '整数', boolean: '开关', string: '文字', object: 'JSON 对象' }
const display = (value: unknown) => value === '' ? '空字符串' : JSON.stringify(value)

export function AccountTypedSettings({ accountId }: { accountId: string }) {
  const [configuration, setConfiguration] = useState<TypedConfiguration>()
  const [scope, setScope] = useState<ConfigurationScope>('account')
  const [key, setKey] = useState('pause_duration')
  const [draft, setDraft] = useState('')
  const [nullValue, setNullValue] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const dirty = useRef(false)
  const requestEpoch = useRef(0)
  const load = useCallback(async () => {
    const epoch = ++requestEpoch.current
    const result = await readTypedConfiguration(accountId)
    if (!result.success || !result.data) throw new Error('配置读取失败')
    if (epoch === requestEpoch.current && !dirty.current) setConfiguration(result.data)
  }, [accountId])
  useEffect(() => {
    const refresh = (event: Event) => {
      if ((event as CustomEvent<string>).detail === accountId) void load().catch(() => setMessage('配置读取失败'))
    }
    void load().catch(() => setMessage('配置读取失败'))
    window.addEventListener('account-configuration-changed', refresh)
    return () => window.removeEventListener('account-configuration-changed', refresh)
  }, [accountId, load])
  const definition = configuration?.schema[key]
  const current = configuration?.values[key]
  useEffect(() => {
    if (!configuration || dirty.current) return
    const value = scope === 'account' ? configuration.values[key]?.value
      : Object.prototype.hasOwnProperty.call(configuration.scope_values[scope], key)
        ? configuration.scope_values[scope][key] : configuration.schema[key]?.default
    setNullValue(value === null)
    setDraft(value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value))
  }, [configuration, key, scope])
  const save = async (action: 'set' | 'inherit') => {
    if (!configuration || !definition) return
    let value: unknown = draft
    if (action === 'set' && nullValue) value = null
    else if (action === 'set' && definition.type !== 'string') {
      try { value = JSON.parse(draft) } catch { setMessage('配置值格式有误'); return }
    }
    requestEpoch.current += 1
    setBusy(true); setMessage('')
    try {
      const result = await saveTypedConfiguration(accountId, { scope, key, action, value,
        expected_version: configuration.versions[scope] })
      if (!result.success || !result.data) throw new Error('配置保存失败')
      dirty.current = false
      setConfiguration(result.data)
      setMessage(`配置已保存，影响 ${result.data.affected_accounts ?? 0} 个账号；待应用服务见下方`)
      announceConfigurationChange(accountId)
    } catch { setMessage('保存失败：检查类型与范围，或刷新后重试') }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!configuration) return
    setBusy(true)
    try {
      const result = await applyAccountConfiguration(accountId, configuration.versions.account)
      if (!result.success || !result.data) throw new Error('应用失败')
      setMessage(result.data.complete ? '三个服务已应用此版本' : `待补齐：${result.data.pending_consumers.join('、')}`)
      announceConfigurationChange(accountId)
    } catch { setMessage('应用失败，请刷新后重试') }
    finally { setBusy(false) }
  }
  const inputClass = 'mt-1 w-full rounded border border-slate-300 px-2 py-1 text-slate-900'
  return <section className="mt-3 space-y-2 border-t border-slate-200 pt-3" aria-label="配置继承">
    <h4 className="font-medium">配置继承</h4>
    <p>旧配置保持账号显式值；选择继承后才采用用户默认 → 系统默认 → 内置默认。</p>
    {configuration && definition && current && <>
      <label className="block">配置作用域<select aria-label="配置作用域" className={inputClass} value={scope} disabled={busy} onChange={e => { dirty.current = false; setScope(e.target.value as ConfigurationScope) }}>
        <option value="account">当前账号</option><option value="user">此用户默认</option>
        {configuration.can_edit_system && <option value="system">系统默认</option>}
      </select></label>
      <label className="block">配置项<select aria-label="配置项" className={inputClass} value={key} disabled={busy} onChange={e => { dirty.current = false; setKey(e.target.value) }}>
        {Object.entries(configuration.schema).map(([name, field]) => <option key={name} value={name}>{field.label}</option>)}
      </select></label>
      <p>类型：{types[definition.type]}{definition.minimum !== null && ` · 最小 ${definition.minimum}`}{definition.maximum !== null && ` · 最大 ${definition.maximum}`} · 作用域 v{configuration.versions[scope]}</p>
      <p>有效值：{display(current.value)} · 来源：{sources[current.source]}</p>
      {current.error && <p role="alert">旧值超出类型或范围，保存前请核对。</p>}
      <label className="block">配置值
        {definition.type === 'boolean' ? <select aria-label="配置值" className={inputClass} value={draft} disabled={busy || nullValue} onChange={e => { dirty.current = true; setDraft(e.target.value) }}>
          <option value="false">关闭</option><option value="true">开启</option>
        </select> : definition.type === 'object' ? <textarea aria-label="配置值" className={inputClass} rows={3} value={draft} disabled={busy || nullValue} onChange={e => { dirty.current = true; setDraft(e.target.value) }} />
          : <input aria-label="配置值" className={inputClass} type={definition.type === 'integer' ? 'number' : 'text'} min={definition.minimum ?? undefined}
            max={definition.maximum ?? undefined} step={1} value={draft} disabled={busy || nullValue} onChange={e => { dirty.current = true; setDraft(e.target.value) }} />}
      </label>
      {definition.nullable && <label className="flex gap-2"><input type="checkbox" checked={nullValue} disabled={busy} onChange={e => { dirty.current = true; setNullValue(e.target.checked) }} />保存空值 null（不是继承）</label>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-50" disabled={busy} onClick={() => void save('set')}>保存显式值</button>
        <button type="button" className="rounded border px-3 py-1.5 disabled:opacity-50" disabled={busy} onClick={() => void save('inherit')}>继承上级</button>
        <button type="button" className="rounded border px-3 py-1.5 disabled:opacity-50" disabled={busy || !configuration.pending_consumers.length} onClick={() => void apply()}>应用此账号配置</button>
        <button type="button" className="text-blue-600" disabled={busy} onClick={() => { dirty.current = false; void load().catch(() => setMessage('配置读取失败')) }}>重新读取</button>
      </div>
      <p>账号 v{configuration.versions.account} · 待应用：{configuration.pending_consumers.join('、') || '无'}</p>
      {!!configuration.unknown_keys.length && <p>旧未知键已保留：{configuration.unknown_keys.join('、')}</p>}
    </>}
    {message && <p role="status">{message}</p>}
  </section>
}
