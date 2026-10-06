const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
test('managed deployment shows Git workflow, not latest-version claim', () => {
  const elements = Object.fromEntries(['dashboardHotUpdateGroup','dashboardHotUpdateBtn','dashboardHotUpdateMenuBtn']
    .map(key => [key, {classList: {remove() {}, add() {}}}]));
  const context = vm.createContext({document: {getElementById: id => elements[id]}});
  vm.runInContext(extract('refreshHotUpdateButtonState'), context);
  context.refreshHotUpdateButtonState({update_mode:'git', message:'通过兼容合并后发布'});
  assert.match(elements.dashboardHotUpdateBtn.innerHTML, /Git 分支维护/);
  assert.equal(elements.dashboardHotUpdateMenuBtn.disabled, true);
  assert.match(elements.dashboardHotUpdateBtn.title, /兼容合并/);
});
