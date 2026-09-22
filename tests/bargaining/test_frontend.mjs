import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync, rmSync } from 'node:fs'
import path from 'node:path'
const root = path.resolve(import.meta.dirname, '../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { build } = require('esbuild')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const outfile = '/tmp/xymb-bargaining-frontend.cjs'
await build({ entryPoints: [path.join(root, 'frontend/src/pages/ai-settings/BargainingDiagnostics.tsx')], outfile, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', external: ['react', 'react-dom'], alias: { '@': path.join(root, 'frontend/src') } })
const Module = require('node:module')
const mod = new Module(outfile)
mod.paths = [path.join(root, 'frontend/node_modules')]
mod._compile(readFileSync(outfile, 'utf8'), outfile)
const View = mod.exports.BargainingDiagnosticsView
const render = data => renderToStaticMarkup(React.createElement(View, { data, onRefresh() {} }))
assert.equal(render({ candidate_enabled: false, records: [] }), '')
const html = render({ candidate_enabled: true, records: [{ log_id: 1, chat_id: 'chat-a', stage: 'success', bargaining: { count: 2, is_bargaining: true }, history_window: { selected_messages: 7, trimmed_messages: 14, manual_messages: 3, first_event_id: 'start', last_event_id: 'end' } }] })
for (const text of ['候选诊断', 'chat-a', '已确认议价 2 次', '采用 7 条', '裁剪 14 条', '人工消息 3 条', 'start', 'end']) assert.ok(html.includes(text), text)
assert.ok(!html.includes('已发布'))
rmSync(outfile)
console.log('PASS DEV45 诊断显示及关闭隐藏')
