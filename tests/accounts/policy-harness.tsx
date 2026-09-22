import React from 'react'
import { createRoot } from 'react-dom/client'
import { AccountRequestPolicy } from '@/pages/accounts/AccountRequestPolicy'
import { AccountTypedSettings } from '@/pages/accounts/AccountTypedSettings'
createRoot(document.getElementById('root')!).render(<><AccountRequestPolicy accountId="fixture" /><AccountTypedSettings accountId="fixture" /></>)
