import { createContext, useCallback, useContext, useMemo } from 'react'
import type { ReactNode } from 'react'
import { ApiRequestError, apiRequest } from '../api'
import type { PortalAppEntry, PortalConfig, PortalConversation, PortalProject } from './portalTypes'

type PortalState = {
  tenantId: string
  config: PortalConfig
  userName: string
  apps: PortalAppEntry[]
  conversations: PortalConversation[]
  projects: PortalProject[]
  reload: () => void
  /** Request helper for portal endpoints: when the server says the seat is gone, the shell falls back to the no-seat page. */
  request: <T>(path: string, options?: RequestInit) => Promise<T>
  streamChat: (body: Record<string, unknown>, onEvent: (name: string, data: Record<string, unknown>) => void) => Promise<void>
}

const PortalContext = createContext<PortalState | null>(null)

export function PortalProvider({ value, onSeatLost, children }: {
  value: Omit<PortalState, 'request' | 'streamChat'>
  onSeatLost: () => void
  children: ReactNode
}) {
  const { tenantId } = value
  const request = useCallback(async <T,>(path: string, options: RequestInit = {}) => {
    try {
      return await apiRequest<T>(path, options, tenantId)
    } catch (failure) {
      if (failure instanceof ApiRequestError && failure.code === 'portal.seat_required') onSeatLost()
      throw failure
    }
  }, [tenantId, onSeatLost])

  const streamChat = useCallback(async (body: Record<string, unknown>, onEvent: (name: string, data: Record<string, unknown>) => void) => {
    const csrf = document.cookie.split('; ').find((cookie) => cookie.startsWith('beework_csrf='))?.split('=').slice(1).join('=')
    const response = await fetch('/api/v1/portal/me/chat', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': tenantId, ...(csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {}) },
      body: JSON.stringify({ ...body, stream: true }),
    })
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => ({}))
      const code = typeof payload.code === 'string' ? payload.code : 'error'
      if (code === 'portal.seat_required') onSeatLost()
      throw new ApiRequestError(typeof payload.detail === 'string' ? payload.detail : '', code, response.status)
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { value: chunk, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(chunk, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
        const name = /^event: (.+)$/m.exec(frame)?.[1]
        const data = /^data: (.+)$/m.exec(frame)?.[1]
        if (name && data) onEvent(name, JSON.parse(data) as Record<string, unknown>)
      }
    }
  }, [tenantId, onSeatLost])

  const state = useMemo(() => ({ ...value, request, streamChat }), [value, request, streamChat])
  return <PortalContext.Provider value={state}>{children}</PortalContext.Provider>
}

export function usePortal(): PortalState {
  const state = useContext(PortalContext)
  if (!state) throw new Error('usePortal outside PortalProvider')
  return state
}
