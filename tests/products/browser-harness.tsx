import React, {useState} from 'react'
import {createRoot} from 'react-dom/client'
import {BatchPublish} from '@/pages/product-publish/BatchPublish'
import {PublishLogs} from '@/pages/product-publish/PublishLogs'
import {ProductFeedbackPanel} from '@/pages/product-feedback/ProductFeedbackPanel'
import {PolishWindow} from '@/pages/items/PolishWindow'
function Harness(){
 const [page,setPage]=useState('batch')
 return <><nav>{['batch','logs','rate','flower','polish'].map(p=><button key={p} onClick={()=>setPage(p)}>{p}</button>)}</nav>
 {page==='batch'&&<BatchPublish/>}{page==='logs'&&<PublishLogs/>}
 {page==='rate'&&<ProductFeedbackPanel kind="rate"/>}{page==='flower'&&<ProductFeedbackPanel kind="red_flower"/>}
 {page==='polish'&&<PolishWindow accountId="a1"/>}</>
}
createRoot(document.getElementById('root')!).render(<Harness/>);
