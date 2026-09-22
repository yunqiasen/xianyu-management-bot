import React from 'react'
import { createRoot } from 'react-dom/client'
import { MonitorReliabilityPanel } from '../../frontend/src/pages/product-monitor/MonitorReliabilityPanel'
createRoot(document.getElementById('root')!).render(<React.StrictMode><MonitorReliabilityPanel tasks={[
  {id: 1, keyword: '相机', monitor_type: 'listing', interval_minutes: 5, collect_pages: 2, account_ids: ['a'], is_enabled: true},
]} /></React.StrictMode>)
import '../../frontend/src/styles/globals.css'
import '../../frontend/src/styles/theme.css'
