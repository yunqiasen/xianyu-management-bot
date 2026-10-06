const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/js/app.js'),'utf8');
function extract(name){let start=source.indexOf(`function ${name}(`);assert.ok(start>=0,`${name} missing`);if(source.slice(start-6,start)==='async ')start-=6;return source.slice(start,source.indexOf('\n}',start)+2)}
function setup(names,extra={}){const nodes={publishPlatformCategory:{value:'0'},publishPlatformCategoryStatus:{},publishCategoryAttributes:{querySelectorAll:()=>[]}};const ctx=vm.createContext({document:{getElementById:id=>nodes[id]},itemPublishCategoryState:{choice:{channel_cat_id:'20'},candidates:[],properties:[],requestId:0},renderPublishCategory:()=>{},showToast:()=>{},...extra});for(const name of names)vm.runInContext(extract(name),ctx);return {ctx,nodes};}
test('material and JSON publish preserve chosen platform category',()=>{
 const {ctx}=setup(['buildItemPublishJsonPayload','buildItemPublishMaterialPayload'],{parseOptionalPublishNumber:()=>null});
 const values={accountId:'fixture',platformCategory:{channel_cat_id:'20',attributes:[{property_id:'brand',value_id:'b2'}]}};
 assert.equal(ctx.buildItemPublishJsonPayload(values,[]).platform_category.channel_cat_id,'20');
 assert.equal(ctx.buildItemPublishMaterialPayload(values,[]).platform_category.attributes[0].value_id,'b2');
});
test('editing title invalidates selection and ignores in-flight recommendation',async()=>{
 let resolve;const response=new Promise(r=>resolve=r);
 const {ctx}=setup(['loadPublishCategories','resetPublishCategory'],{getItemPublishFormValues:()=>({accountId:'fixture',title:'手机',description:'描述',category:''}),requestItemPublishJson:()=>response});
 const pending=ctx.loadPublishCategories();ctx.resetPublishCategory();
 resolve({success:true,category:{channel_cat_id:'old'},candidates:[],properties:[]});await pending;
 assert.equal(ctx.itemPublishCategoryState.choice,null);
});
test('material pagination requests selected page and page size',async()=>{
 let requested;const {ctx,nodes}=setup(['loadItemPublishMaterials'],{itemPublishMaterialPage:2,itemPublishMaterialPageSize:10,itemPublishMaterialTotal:0,itemPublishMaterialRequestId:0,renderItemPublishMaterials:()=>{},renderItemPublishMaterialPager:()=>{},requestItemPublishJson:async path=>{requested=path;return {list:[],total:25,page:2,page_size:10};}});
 nodes.publishMaterialList={};await ctx.loadItemPublishMaterials();assert.equal(requested,'/product-materials?page=2&page_size=10');
});
