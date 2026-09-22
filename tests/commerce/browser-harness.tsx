import React, {useState} from 'react'
import {createRoot} from 'react-dom/client'
import {CommercePanel} from '@/pages/orders/CommercePanel'
function Harness() {
  const [account, setAccount] = useState('a')
  return <><label>履约账号<select aria-label="履约账号" value={account} onChange={event => setAccount(event.target.value)}><option value="a">账号A</option><option value="b">账号B</option></select></label><CommercePanel account={account} revision={0}/></>
}
createRoot(document.getElementById('root')!).render(<Harness />)
