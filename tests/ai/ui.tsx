import React from 'react'
import { createRoot } from 'react-dom/client'
import AISettingsPanel from '../../frontend/src/pages/accounts/AISettingsPanel'
createRoot(document.getElementById('root')!).render(<AISettingsPanel accountId="account-1" accounts={[{ id: 'account-1' }, { id: 'account-2' }, { id: 'missing' }]} />)
