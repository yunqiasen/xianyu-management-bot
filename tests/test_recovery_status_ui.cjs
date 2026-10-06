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
const context = vm.createContext({});
for (const name of ['getAboutRuntimeOverview', 'getAboutStatusText']) vm.runInContext(extract(name), context);
test('cooldown has priority over connecting in account diagnosis', () => {
  const result = context.getAboutRuntimeOverview({running:true, connection_state:'reconnecting',
    token_refresh_status:'password_login_backoff_wait', token_refresh_error_message:'滑块验证失败，剩余123秒'});
  assert.equal(result.title, '恢复冷却中');
  assert.match(result.note, /滑块验证失败/);
  assert.equal(context.getAboutStatusText('token','password_login_backoff_wait'), '恢复冷却中');
});
test('manual verification is shown explicitly, not connection in progress', () => {
  for (const status of ['verification_pending_manual','manual_verification_required']) {
    const result = context.getAboutRuntimeOverview({running:true, connection_state:'connecting', token_refresh_status:status});
    assert.equal(result.title, '等待人工验证');
    assert.equal(context.getAboutStatusText('token',status), '等待人工验证');
  }
});
test('ordinary connection and ready account keep their original status', () => {
  assert.equal(context.getAboutRuntimeOverview({running:true,connection_state:'connecting'}).title, '连接正在恢复');
  assert.equal(context.getAboutRuntimeOverview({running:true,ws_ready:true,session_ready:true,
    has_current_token:true,message_stream_ready:true}).title, '链路稳定可用');
});
