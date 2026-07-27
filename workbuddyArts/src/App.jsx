import { Routes, Route, Navigate } from 'react-router-dom'
import { useState, createContext, useContext } from 'react'
import Layout from './components/Layout'
import Login from './pages/Login'
import Register from './pages/Register'
import ChatNew from './pages/ChatNew'
import ChatConversation from './pages/ChatConversation'
import Ingest from './pages/Ingest'
import Library from './pages/Library'
import Spaces from './pages/Spaces'
import Settings from './pages/Settings'
import SettingsProviders from './pages/SettingsProviders'

/* Theme context */
export const ThemeContext = createContext({ theme: 'light', setTheme: () => {} })
export const useTheme = () => useContext(ThemeContext)

/* Sidebar collapsed context */
export const SidebarContext = createContext({ collapsed: false, setCollapsed: () => {} })
export const useSidebar = () => useContext(SidebarContext)

export default function App() {
  const [theme, setTheme] = useState('light')
  const [collapsed, setCollapsed] = useState(false)

  return (
    <ThemeContext.Provider value={{ theme, setTheme }}>
      <SidebarContext.Provider value={{ collapsed, setCollapsed }}>
        <div data-theme={theme} style={{ height: '100vh', overflow: 'hidden' }}>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/" element={<Layout />}>
              <Route index element={<Navigate to="/chat/new" replace />} />
              <Route path="chat/new" element={<ChatNew />} />
              <Route path="chat/:conversationId" element={<ChatConversation />} />
              <Route path="ingest" element={<Ingest />} />
              <Route path="library" element={<Library />} />
              <Route path="spaces/:spaceId?" element={<Spaces />} />
              <Route path="settings" element={<Settings />}>
                <Route index element={<Navigate to="/settings/providers" replace />} />
                <Route path="providers" element={<SettingsProviders />} />
              </Route>
            </Route>
          </Routes>
        </div>
      </SidebarContext.Provider>
    </ThemeContext.Provider>
  )
}
