const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/js/app.js'),'utf8');
function extract(name){const start=source.indexOf(`function ${name}(`);assert.ok(start>=0,`${name} missing`);return source.slice(start,source.indexOf('\n}',start)+2)}
test('channel editing keeps other fields and blank templates clear overrides',()=>{
 const context=vm.createContext({document:{getElementById:id=>({value:id.endsWith('chat_template')?'{{message}}':''})}});
 vm.runInContext(extract('collectChannelTemplates'),context);
 const result=context.collectChannelTemplates('edit_',{secret:'kept',custom_key:'retained'});
 assert.equal(result.secret,'kept');assert.equal(result.custom_key,'retained');
 assert.equal(result.chat_template,'{{message}}');assert.equal(result.account_template,'');
});
