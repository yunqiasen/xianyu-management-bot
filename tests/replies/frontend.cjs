const test = require('node:test'); const assert = require('node:assert/strict');
const fs = require('node:fs'); const ts = require('../../frontend/node_modules/typescript');
function subject() {
 const path = 'frontend/src/pages/chat-new/replyEventState.ts';
 assert.ok(fs.existsSync(path), '缺少实际消息合并入口');
 const code = ts.transpileModule(fs.readFileSync(path, 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
 const mod = {exports:{}}; new Function('module','exports',code)(mod,mod.exports); return mod.exports;
}
test('消息ID去重，旧版本不覆盖新发送状态', () => {
 const {mergeChatMessage} = subject();
 const original = {messageId:'m', version:2, text:'new'};
 assert.deepEqual(mergeChatMessage([original], {messageId:'m',version:1,text:'old'}), [original]);
 assert.deepEqual(mergeChatMessage([original], {messageId:'m',version:3,text:'confirmed'}), [{messageId:'m',version:3,text:'confirmed'}]);
});
test('相同内容不同事件保留，不按文本吞消息', () => {
 const {mergeChatMessage} = subject();
 const a = {messageId:'a',isSelf:true,text:'你好',time:1}; const b = {...a,messageId:'b',time:2};
 assert.equal(mergeChatMessage([a],b).length,2);
});
test('核实结果保留消息身份并使用服务器版本，待核实不伪装成功', () => {
 const {applyOutboundVerification} = subject();
 const message = {messageId:'out:r', version:2, status:'unknown', text:'hello'};
 assert.equal(typeof applyOutboundVerification, 'function');
 assert.deepEqual(applyOutboundVerification(message, {requestId:'r',version:3,status:'confirmed'}), {...message,version:3,status:'confirmed',failed:false,failReason:undefined});
 assert.equal(applyOutboundVerification(message, {requestId:'r',version:2,status:'unknown',verification:'未找到证据'}).status, 'unknown');
 assert.equal(applyOutboundVerification(message, {requestId:'other',version:5,status:'confirmed'}), message);
});
test('发送API沿用调用方请求身份，断网后仍可按原身份核实', async () => {
 const code = ts.transpileModule(fs.readFileSync('frontend/src/api/chatNew.ts','utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
 const calls=[]; const mod={exports:{}};
 new Function('module','exports','require',code)(mod,mod.exports,()=>({post:async (url,payload)=>{calls.push([url,payload]);throw new Error('lost reply')}}));
 await assert.rejects(()=>mod.exports.sendTextMessage('a','c','buyer','hi','stable-request'));
 assert.equal(calls[0][1].requestId, 'stable-request');
});
test('账号切换时拒绝旧会话响应，头像缓存按账号区分', () => {
 const {chatScopeMatches, chatCacheKey} = subject();
 assert.equal(typeof chatScopeMatches, 'function');
 assert.equal(chatScopeMatches('a','same','b','same'), false);
 assert.equal(chatScopeMatches('a','same','a','same'), true);
 assert.notEqual(chatCacheKey('a','buyer'), chatCacheKey('b','buyer'));
});
