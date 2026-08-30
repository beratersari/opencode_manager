export function parseTimeMs(value: unknown): number | null {
  if (value == null || value === '') return null
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 1e12 ? value * 1000 : value
  }
  if (typeof value === 'string') {
    const asNum = Number(value)
    if (Number.isFinite(asNum) && value.trim() !== '') {
      return asNum < 1e12 ? asNum * 1000 : asNum
    }
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  if (typeof value === 'object') {
    const rec = value as { created?: unknown; created_at?: unknown }
    return parseTimeMs(rec.created ?? rec.created_at)
  }
  return null
}

export function formatChatTime(value: unknown): string {
  const ms = parseTimeMs(value)
  if (ms == null) return ''
  try {
    return new Date(ms).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}
