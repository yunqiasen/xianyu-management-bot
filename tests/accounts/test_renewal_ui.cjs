const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),ts=require('../../frontend/node_modules/typescript');
function subject(){const code=ts.transpileModule(fs.readFileSync('frontend/src/pages/accounts/renewalResult.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;const mod={exports:{}};new Function('module','exports',code)(mod,mod.exports);return mod.exports;}
test('跳过和未知不得显示全部成功',()=>{const {renewalToast}=subject();for(const key of ['failed_count','skipped_count','unknown_count']){const result=renewalToast({success_count:0,[key]:1});assert.equal(result.type,'warning');assert.match(result.message,/成功 0/);}});
test('逐类统计，只有全部验证完成才显示成功',()=>{const {renewalToast}=subject();assert.deepEqual(renewalToast({success_count:2,failed_count:0,skipped_count:0,unknown_count:0}),{type:'success',message:'续期：成功 2，失败 0，跳过 0，待核实 0'});assert.equal(renewalToast({}).type,'warning');});
