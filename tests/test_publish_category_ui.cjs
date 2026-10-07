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

test('save or publish waits for the selected category request to finish',()=>{
 const {ctx}=setup(['validateItemPublishValues'],{itemPublishCategoryState:{loading:true}});
 assert.throws(()=>ctx.validateItemPublishValues({accountId:'fixture',title:'test',description:'desc',files:[]}), /类目.*等待|等待.*类目/);
});

test('multi-select property keeps every selected platform value',()=>{
 const state={choice:{},properties:[{property_id:'tags',is_multiple:true,options:[{value_id:'a'},{value_id:'b'}]}]};
 const {ctx,nodes}=setup(['updatePublishCategoryAttributes'],{itemPublishCategoryState:state});
 nodes.publishCategoryAttributes.querySelectorAll=()=>[{value:'0',dataset:{propertyIndex:'0'},selectedOptions:[{value:'0'},{value:'1'}]}];
 ctx.updatePublishCategoryAttributes();
 assert.deepEqual(Array.from(state.choice.attributes[0].values,v=>v.value_id),['a','b']);
});

test('clearing a multi-select sends explicit empty values',()=>{
 const state={choice:{},properties:[{property_id:'tags',is_multiple:true,options:[]}]};
 const {ctx,nodes}=setup(['updatePublishCategoryAttributes'],{itemPublishCategoryState:state});
 nodes.publishCategoryAttributes.querySelectorAll=()=>[{value:'',dataset:{propertyIndex:'0'},selectedOptions:[]}];
 ctx.updatePublishCategoryAttributes();
 assert.equal(state.choice.attributes[0].property_id,'tags');
 assert.equal(state.choice.attributes[0].values.length,0);
});
