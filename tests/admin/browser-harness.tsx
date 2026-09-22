import React from 'react'
import { createRoot } from 'react-dom/client'
import { LoginProtection } from '@/pages/admin/LoginProtection'
import { DataManagement } from '@/pages/admin/DataManagement'
import { BackupVerification } from '@/pages/admin/BackupVerification'
import { LogArchive } from '@/pages/admin/LogArchive'
createRoot(document.getElementById('root')!).render(<>
  <LoginProtection />
  <section aria-label="备份验证"><BackupVerification logId={1} /></section>
  <section aria-label="数据管理"><DataManagement /></section>
  <section aria-label="日志归档"><LogArchive /></section>
</>)
