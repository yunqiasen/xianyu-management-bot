/** Runtime-correctness lint; TypeScript build owns type and name resolution. */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['react-hooks'],
  ignorePatterns: ['dist', 'node_modules'],
  rules: {
    'no-dupe-args': 'error',
    'no-dupe-keys': 'error',
    'no-unreachable': 'error',
    'no-unsafe-finally': 'error',
    'no-async-promise-executor': 'error',
    'no-constant-condition': ['error', { checkLoops: false }],
    'valid-typeof': 'error',
    'constructor-super': 'error',
    'react-hooks/rules-of-hooks': 'error',
  },
}
