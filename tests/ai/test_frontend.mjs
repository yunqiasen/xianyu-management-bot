import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
const root = path.resolve(import.meta.dirname, '../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
assert.ok(existsSync(path.join(root, 'frontend/src/pages/accounts/AISettingsPanel.tsx')), '缺少 AI 配置/预设组件')
const { build } = require('esbuild')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const outfile = path.join(root, 'tests/ai/.frontend-test.cjs')
await build({ entryPoints: [path.join(root, 'frontend/src/pages/accounts/AISettingsPanel.tsx')], outfile, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', external: ['react', 'react-dom'], alias: { '@': path.join(root, 'frontend/src') } })
const Module = require('node:module')
// 测试输出位于 tests，显式复用已有 frontend 的 React，无额外安装。
const mod = new Module(outfile)
mod.paths = [path.join(root, 'frontend/node_modules')]
mod._compile(require('node:fs').readFileSync(outfile, 'utf8'), outfile)
const { AIProtocolFields } = mod.exports
for (const provider of ['openai_compatible', 'responses', 'azure', 'anthropic', 'gemini', 'dashscope_app']) {
  const html = renderToStaticMarkup(React.createElement(AIProtocolFields, { settings: { provider_type: provider, api_key_configured: true }, onChange() {} }))
  assert.ok(html.includes('API 地址'))
  assert.ok(html.includes('type="password"'))
  if (provider === 'azure') {
    assert.ok(html.includes('Azure 部署名'))
    assert.ok(html.includes('API 版本'))
    assert.ok(html.includes('认证方式'))
  }
  if (provider === 'dashscope_app') assert.ok(html.includes('应用标识'))
}
require('node:fs').unlinkSync(outfile)
console.log('6 种协议字段 SSR 渲染通过')
