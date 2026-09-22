import base from '../../frontend/vite.config'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
export default { ...base, resolve: { ...base.resolve, dedupe: ['react', 'react-dom'] }, server: { ...base.server, fs: { allow: [root] } } }
