import { createRequire } from 'node:module'
import path from 'node:path'
const root = path.resolve(import.meta.dirname, '../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const react = require('@vitejs/plugin-react')
export default {
  root, plugins: [react()],
  resolve: { alias: { '@': path.join(root, 'frontend/src'), react: path.join(root, 'frontend/node_modules/react'), 'react-dom': path.join(root, 'frontend/node_modules/react-dom') } },
  server: { host: '127.0.0.1', port: Number(process.env.AI_UI_PORT), strictPort: true, proxy: { '/api': `http://127.0.0.1:${process.env.AI_FIXTURE_PORT}` } },
}
