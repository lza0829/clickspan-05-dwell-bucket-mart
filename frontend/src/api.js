async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    let detail = text
    try {
      const parsed = JSON.parse(text)
      detail = parsed.detail || text
    } catch {
      /* 保留原始文本 */
    }
    const err = new Error(detail || res.statusText)
    err.status = res.status
    throw err
  }
  return res.json()
}

function jsonBody(payload) {
  return { method: 'PUT', body: JSON.stringify(payload) }
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  visitors: () => req('/api/visitors'),
  sessions: (visitorId) =>
    req(visitorId ? `/api/sessions?visitor_id=${encodeURIComponent(visitorId)}` : '/api/sessions'),
  session: (id) => req(`/api/sessions/${encodeURIComponent(id)}`),
  updateSpan: (id, payload) =>
    req(`/api/sessions/${encodeURIComponent(id)}/span`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  closeSession: (id, payload = {}) =>
    req(`/api/sessions/${encodeURIComponent(id)}/close`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  patchSession: (id, payload) =>
    req(`/api/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  dwellFacts: (sessionId) =>
    req(
      sessionId
        ? `/api/dwell/facts?session_id=${encodeURIComponent(sessionId)}`
        : '/api/dwell/facts',
    ),
  dwellBounds: () => req('/api/dwell/bounds'),
  setDwellBounds: (bounds) => req('/api/dwell/bounds', jsonBody({ bounds })),
  quarantine: () => req('/api/quarantine'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (sessionId) =>
    req(sessionId ? `/api/session-proofs?session_id=${encodeURIComponent(sessionId)}` : '/api/session-proofs'),
  settings: () => req('/api/settings'),
}
