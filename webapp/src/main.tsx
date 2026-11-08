import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import DesignPreview from '../design-reference/static-card'
import Launcher from './launcher/Launcher'
import './styles.css'

const design = new URLSearchParams(location.search).get('design') === '1'
const launcher = new URLSearchParams(location.search).get('launcher') === '1'
if (!launcher && !['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) {
  const bridge = document.createElement('script')
  bridge.src = 'https://st.max.ru/js/max-web-app.js'
  bridge.async = true
  document.head.append(bridge)
}
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {launcher ? <Launcher /> : design ? <DesignPreview /> : <App />}
  </React.StrictMode>,
)
