export type RunEvent = { seq: number; type: string; data: Record<string, any> } // eslint-disable-line @typescript-eslint/no-explicit-any

/** Follows a run's server-sent events with fetch (EventSource cannot send the tenant header). */
export async function streamRunEvents(
  runId: string,
  tenantId: string,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/v1/runs/${runId}/events`, {
    headers: { 'X-Tenant-ID': tenantId, Accept: 'text/event-stream' },
    credentials: 'same-origin',
    signal,
  })
  if (!response.ok || !response.body) throw new Error(String(response.status))
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) return
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const data = block.split('\n').filter((line) => line.startsWith('data: ')).map((line) => line.slice(6)).join('\n')
      if (data) {
        const event = JSON.parse(data) as RunEvent
        onEvent(event)
        if (['done', 'error', 'cancelled'].includes(event.type)) return
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}
