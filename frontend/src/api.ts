import i18n from './i18n'

let currentEnvironment: string | null = null

/** The environment selected in the top bar; sent as ``X-Environment`` with every request (T-006). */
export function setCurrentEnvironment(environment: string | null) {
  currentEnvironment = environment
}

/** An API failure; ``code`` is the server's machine-readable error code (e.g. ``approval.pending_exists``). */
export class ApiRequestError extends Error {
  code: string
  status: number

  constructor(message: string, code: string, status: number) {
    super(message)
    this.code = code
    this.status = status
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  tenantId?: string,
  // Polling that must not count as user activity for the session idle timeout (T-004).
  background = false,
): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (tenantId) headers.set('X-Tenant-ID', tenantId)
  if (background) headers.set('X-Background-Request', '1')
  if (currentEnvironment && !headers.has('X-Environment')) headers.set('X-Environment', currentEnvironment)
  if (options.method && !['GET', 'HEAD', 'OPTIONS'].includes(options.method.toUpperCase())) {
    const csrfToken = document.cookie
      .split('; ')
      .find((cookie) => cookie.startsWith('beework_csrf='))
      ?.split('=')
      .slice(1)
      .join('=')
    if (csrfToken) headers.set('X-CSRF-Token', decodeURIComponent(csrfToken))
  }

  const response = await fetch(path, {
    ...options,
    headers,
    credentials: 'same-origin',
  })
  if (response.status === 204) return undefined as T
  const payload = await response.json().catch(() => ({}))
  if (response.status === 401 && !path.startsWith('/api/v1/auth/login')) {
    // The session ended (idle timeout or expiry): let the shell return to the sign-in page.
    window.dispatchEvent(new Event('beework:unauthorized'))
  }
  if (!response.ok) {
    const message = typeof payload.detail === 'string' ? payload.detail : i18n.t('requestFailed')
    throw new ApiRequestError(message, typeof payload.code === 'string' ? payload.code : 'error', response.status)
  }
  return payload as T
}
