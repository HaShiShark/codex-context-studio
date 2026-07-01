import type {
  ContextChatStreamEvent,
  ContextWorkbenchChatMessage,
  InitPayload,
  ProxyUsageSummary,
  TranscriptEntry,
} from './types';

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(path, { ...options, headers });
  let data: unknown = {};
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const err = (typeof data === 'object' && data !== null && 'error' in data)
      ? String((data as { error?: unknown }).error || '').trim()
      : '';
    throw new Error(err || response.statusText || `HTTP ${response.status}`);
  }
  return data as T;
}

async function readJsonLineStream<T>(response: Response, onEvent: (event: T) => void): Promise<void> {
  if (!response.body) throw new Error('当前环境不支持流式响应');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx = buffer.indexOf('\n');
    while (idx !== -1) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line) { try { onEvent(JSON.parse(line) as T); } catch { /* skip malformed line */ } }
      idx = buffer.indexOf('\n');
    }
  }
  if (buffer.trim()) { try { onEvent(JSON.parse(buffer.trim()) as T); } catch { /* skip malformed */ } }
}

// ── init / settings ───────────────────────────────────────────────────────────

export function fetchInit(options: { sessionId?: string; includeConversation?: boolean } = {}): Promise<InitPayload> {
  const params = new URLSearchParams();
  if (options.sessionId) {
    params.set('session_id', options.sessionId);
  }
  if (options.includeConversation === false) {
    params.set('include_conversation', '0');
  }
  const query = params.toString();
  return apiFetch<InitPayload>(query ? `/api/init?${query}` : '/api/init');
}

export interface ProxySettings {
  workbench_model: string;
  theme_mode: 'light' | 'dark';
  ui_font: string;
  ui_font_size: number;
  user_locale: string;
}

export function fetchSettings(): Promise<ProxySettings> {
  return apiFetch<ProxySettings>('/api/settings');
}

export function saveSettingsRequest(payload: Partial<ProxySettings>): Promise<ProxySettings> {
  return apiFetch<ProxySettings>('/api/settings', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

// ── proxy sessions ────────────────────────────────────────────────────────────

export interface ProxySessionSummary {
  id: string;
  title: string;
  status: 'mirror' | 'running' | 'compacting' | 'error' | string;
  transcript?: TranscriptEntry[];
  is_running?: boolean;
  last_error?: string;
  created_at?: string;
  updated_at?: string;
  transcript_version?: number;
  usage_summary?: ProxyUsageSummary;
}

export interface ProxySessionsResponse {
  active_session_id: string;
  sessions: ProxySessionSummary[];
}

export type TranscriptPatchOp =
  | {
      op: 'splice_nodes';
      index: number;
      delete_count: number;
      nodes: TranscriptEntry[];
    }
  | {
      op: 'append_node';
      node: TranscriptEntry;
    }
  | {
      op: 'replace_node';
      index: number;
      node: TranscriptEntry;
    }
  | {
      op: 'delete_node';
      index: number;
    };

export type ProxyRealtimeEvent = {
  type: string;
  event_id?: number;
  session_id?: string;
  session?: ProxySessionSummary | null;
  session_list?: ProxySessionsResponse;
  status?: string;
  is_running?: boolean;
  last_error?: string;
  reason?: string;
  phase?: string;
  base_version?: number;
  next_version?: number;
  transcript_version?: number;
  transcript?: TranscriptEntry[];
  ops?: TranscriptPatchOp[];
  usage_summary?: ProxyUsageSummary;
  message?: string;
  code?: string;
};

export function proxyRealtimeUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const hostname = window.location.hostname || 'localhost';
  return `${protocol}://${hostname}:8787/api/proxy/ws`;
}

export function fetchProxySessionsRequest(): Promise<ProxySessionsResponse> {
  return apiFetch<ProxySessionsResponse>('/api/proxy/sessions');
}

export function fetchProxySessionRequest(sessionId: string): Promise<ProxySessionSummary> {
  return apiFetch<ProxySessionSummary>(`/api/proxy/sessions/${encodeURIComponent(sessionId)}`);
}

export function fetchProxySessionUsageRequest(sessionId: string): Promise<{ summary: ProxyUsageSummary }> {
  return apiFetch<{ summary: ProxyUsageSummary }>(`/api/proxy/sessions/${encodeURIComponent(sessionId)}/usage`);
}

export function resetProxyUsageRequest(
  sessionId: string,
): Promise<{ cleared_count: number; summary: ProxyUsageSummary }> {
  return apiFetch('/api/proxy/sessions/' + encodeURIComponent(sessionId) + '/usage/reset', { method: 'POST' });
}

export function clearContextWorkbenchChatRequest(
  sessionId: string,
): Promise<{ conversation: TranscriptEntry[]; history: ContextWorkbenchChatMessage[] }> {
  return apiFetch('/api/context-workbench-history-clear', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export function syncProxySessionRequest(payload: {
  session_id: string;
  title: string;
}): Promise<{ ok: boolean }> {
  return apiFetch('/api/proxy-sync-session', { method: 'POST', body: JSON.stringify(payload) });
}

// ── workbench chat ────────────────────────────────────────────────────────────

export async function streamContextChatRequest(
  payload: { session_id: string; message: string; selected_node_indexes?: number[]; reasoning_effort?: string },
  onEvent: (event: ContextChatStreamEvent) => void,
  options: { signal?: AbortSignal } = {},
): Promise<void> {
  const response = await fetch('/api/context-chat-stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal: options.signal,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    let msg = response.statusText || `HTTP ${response.status}`;
    try {
      const d = await response.json();
      if (typeof d === 'object' && d !== null && 'error' in d) msg = String((d as { error: unknown }).error);
    } catch { /* */ }
    throw new Error(msg);
  }

  await readJsonLineStream<ContextChatStreamEvent>(response, onEvent);
}
