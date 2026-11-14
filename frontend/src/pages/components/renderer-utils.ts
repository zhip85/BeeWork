/** Every value goes through React text nodes, so a payload can never inject markup (NFR-009). Links are limited to http(s). */
export function safeUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null
  } catch {
    return null
  }
}

/** The sample data of a definition, or ``null`` when it is not valid JSON (the editor shows the error instead of a preview). */
export function parseSample(raw: string): unknown | null {
  try {
    return JSON.parse(raw || '{}')
  } catch {
    return null
  }
}
