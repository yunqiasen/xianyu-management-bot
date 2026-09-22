import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
const root = path.resolve(import.meta.dirname, '../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { build } = require('esbuild')
const result = await build({ entryPoints: [path.join(root, 'frontend/src/pages/ai-settings/AISettings.tsx')], write: false, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', external: ['react', 'react-dom'], alias: { '@': path.join(root, 'frontend/src') } })
const Module = require('node:module'); const mod = new Module('ai-settings-fixture')
mod.paths = [path.join(root, 'frontend/node_modules')];mod._compile(result.outputFiles[0].text, 'fixture.cjs')
const React = require('react'); const { renderToStaticMarkup } = require('react-dom/server')
const html = renderToStaticMarkup(React.createElement(mod.exports.default))
assert.ok(html.includes('AI 回复配置'))
assert.ok(html.includes('选择账号'))
const app = readFileSync(path.join(root, 'frontend/src/App.tsx'), 'utf8')
assert.ok(app.includes('path="ai-settings"'))
console.log('AI configuration route and initial render passed')
