
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import 'reactflow/dist/style.css'


if (window.Telegram?.WebApp) {
  window.Telegram.WebApp.ready()
  window.Telegram.WebApp.expand()
  window.Telegram.WebApp.disableClosingConfirmation?.()
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
