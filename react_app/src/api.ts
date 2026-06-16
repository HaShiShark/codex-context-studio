import type {
  ContextChatResponse,
  ContextChatStreamEvent,
  ContextWorkbenchHistoryEntry,
  ContextWorkbenchSettingsResponse,
  CreateSessionResponse,
  InitPayload,
  ProxyUsageSummary,
  ProviderModelCandidatesResponse,
  ProviderModelsResponse,
  ResponseProviderModel,
  SettingsResponse,
  TranscriptRecord,
} from './types';

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);

  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  let data: unknown = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }

  if (!response.ok) {
    throw new Error(extractResponseError(response, data));
  }

  return data as T;
}

function extractResponseError(response: Response, data: unknown): string {
  if (typeof data === 'object' && data !== null && 'error' in data) {
    const error = (data as { error?: unknown }).error;
    if (error !== undefined && error !== null && String(error).trim()) {
      return String(error);
    }
  }

  return response.statusText || `HTTP ${response.status}`;
}

async function extractResponseErrorMessage(response: Response): Promise<string> {
  let message = response.statusText || `HTTP ${response.status}`;

  try {
    const data = await response.json();
    if (typeof data === 'object' && data !== null && 'error' in data) {
      message = extractResponseError(response, data);
    }
  } catch {
  }

  return message;
}

async function readJsonLineStream<T>(
  response: Response,
  onEvent: (event: T) => void,
): Promise<void> {
  if (!response.body) {
    throw new Error('当前环境不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });

    let newlineIndex = buffer.indexOf('\n');
    while (newlineIndex !== -1) {
      const rawLine = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);

      if (rawLine) {
        onEvent(JSON.parse(rawLine) as T);
      }

      newlineIndex = buffer.indexOf('\n');
    }
  }

  const tail = buffer.trim();
  if (tail) {
    onEvent(JSON.parse(tail) as T);
  }
}

function isResponseProviderModel(value: unknown): value is ResponseProviderModel {
  if (typeof value !== 'object' || value === null) {
    return false;
  }

  const record = value as Record<string, unknown>;
  return typeof record.id === 'string';
}

function normalizeProviderModel(value: ResponseProviderModel): ResponseProviderModel | null {
  const modelId = value.id.trim();
  if (!modelId) {
    return null;
  }

  const nextModel: ResponseProviderModel = {
    id: modelId,
    label: value.label.trim() || modelId,
    group: value.group.trim() || 'Other',
  };

  if (typeof value.provider === 'string' && value.provider.trim()) {
    nextModel.provider = value.provider.trim();
  }

  return nextModel;
}

function normalizeProviderModelCandidatesResponse(
  raw: unknown,
  fallbackProviderId: string,
): ProviderModelCandidatesResponse {
  if (typeof raw !== 'object' || raw === null) {
    throw new Error('模型列表返回格式不对，请重试');
  }

  const record = raw as Record<string, unknown>;

  if ('settings' in record && !('provider_id' in record)) {
    throw new Error('本地服务还是旧版本，重启应用后再试');
  }

  if (!Array.isArray(record.models)) {
    throw new Error('模型列表返回格式不对，请重试');
  }

  if (record.models.length && !record.models.some((item) => isResponseProviderModel(item))) {
    throw new Error('本地服务还是旧版本，重启应用后再试');
  }

  const models = record.models
    .filter(isResponseProviderModel)
    .map(normalizeProviderModel)
    .filter((item): item is ResponseProviderModel => Boolean(item));

  return {
    provider_id: typeof record.provider_id === 'string' && record.provider_id.trim()
      ? record.provider_id.trim()
      : fallbackProviderId,
    fetched_count: typeof record.fetched_count === 'number' ? record.fetched_count : models.length,
    models,
  };
}

export function fetchInit(sessionId?: string, options: { includeConversation?: boolean } = {}): Promise<InitPayload> {
  const safeSessionId = sessionId?.trim();
  const params = new URLSearchParams();
  if (safeSessionId) {
    params.set('session_id', safeSessionId);
  }
  if (options.includeConversation === false) {
    params.set('include_conversation', '0');
  }
  const query = params.toString() ? `?${params.toString()}` : '';
  return apiFetch<InitPayload>(`/api/init${query}`);
}

export function fetchSettings(): Promise<SettingsResponse> {
  return apiFetch<SettingsResponse>('/api/settings');
}

export function fetchContextWorkbenchSettings(options: { refreshModels?: boolean } = {}): Promise<ContextWorkbenchSettingsResponse> {
  const params = new URLSearchParams();
  if (options.refreshModels) {
    params.set('refresh_models', '1');
  }
  const query = params.toString() ? `?${params.toString()}` : '';
  return apiFetch<ContextWorkbenchSettingsResponse>(`/api/context-workbench-settings${query}`);
}

export function saveSettingsRequest(payload: {
  default_model: string;
  openai_base_url: string;
  max_tool_rounds: number;
  assistant_name?: string;
  assistant_greeting?: string;
  assistant_prompt?: string;
  temperature?: number | null;
  top_p?: number | null;
  context_message_limit?: number | null;
  streaming?: boolean;
  user_name?: string;
  user_locale?: string;
  user_timezone?: string;
  user_profile?: string;
  theme_color?: string;
  theme_mode?: 'light' | 'dark';
  background_color?: string;
  ui_font?: string;
  code_font?: string;
  ui_font_size?: number;
  code_font_size?: number;
  appearance_contrast?: number;
  service_hints_enabled?: boolean;
  openai_api_key?: string;
  clear_api_key?: boolean;
  active_provider_id?: string;
  deleted_provider_ids?: string[];
  tool_settings?: Array<{
    name: string;
    label?: string;
    description?: string;
    enabled: boolean;
  }>;
  response_providers?: Array<{
    id: string;
    name: string;
    provider_type: 'chat_completion' | 'responses' | 'gemini' | 'claude';
    enabled: boolean;
    supports_model_fetch: boolean;
    supports_responses: boolean;
    api_base_url: string;
    default_model: string;
    models: Array<{
      id: string;
      label: string;
      group: string;
      provider?: string;
    }>;
    last_sync_at?: string;
    last_sync_error?: string;
    api_key?: string;
    clear_api_key?: boolean;
  }>;
}): Promise<SettingsResponse> {
  return apiFetch<SettingsResponse>('/api/settings', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function fetchProviderModelsRequest(payload: {
  provider_id: string;
  api_base_url: string;
  provider_type?: 'chat_completion' | 'responses' | 'gemini' | 'claude';
  api_key?: string;
}): Promise<ProviderModelsResponse> {
  return apiFetch<ProviderModelsResponse>('/api/provider-models', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function fetchProviderModelCandidatesRequest(payload: {
  provider_id: string;
  api_base_url: string;
  provider_type?: 'chat_completion' | 'responses' | 'gemini' | 'claude';
  api_key?: string;
}): Promise<ProviderModelCandidatesResponse> {
  return apiFetch<unknown>('/api/provider-models', {
    method: 'POST',
    body: JSON.stringify({
      ...payload,
      preview_only: true,
    }),
  }).then((response) => normalizeProviderModelCandidatesResponse(response, payload.provider_id));
}

export function saveContextWorkbenchSettingsRequest(payload: {
  context_workbench_model?: string;
  context_workbench_provider_id?: string;
  context_token_warning_threshold?: number;
  context_token_critical_threshold?: number;
  user_locale?: string;
  theme_mode?: 'light' | 'dark';
  ui_font?: string;
  ui_font_size?: number;
}): Promise<ContextWorkbenchSettingsResponse> {
  return apiFetch<ContextWorkbenchSettingsResponse>('/api/context-workbench-settings', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function createSessionRequest(payload: {
  scope: 'chat' | 'project';
  project_id?: string | null;
}): Promise<CreateSessionResponse> {
  return apiFetch<CreateSessionResponse>('/api/sessions', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export interface ProxySessionSummary {
  id: string;
  title: string;
  status: 'mirror' | 'running' | 'compacting' | 'override' | 'error' | string;
  transcript?: TranscriptRecord[];
  active_transcript?: TranscriptRecord[];
  raw_transcript?: TranscriptRecord[];
  edited_transcript?: TranscriptRecord[] | null;
  active_context_source?: 'raw' | 'committed' | 'pending' | string;
  has_override?: boolean;
  is_running?: boolean;
  last_error?: string;
  created_at?: string;
  updated_at?: string;
  usage_summary?: ProxyUsageSummary;
}

export interface ProxySessionsResponse {
  active_session_id: string;
  sessions: ProxySessionSummary[];
}

export function fetchProxySessionsRequest(): Promise<ProxySessionsResponse> {
  return apiFetch<ProxySessionsResponse>('/api/proxy/sessions');
}

export function fetchProxySessionRequest(sessionId: string): Promise<ProxySessionSummary> {
  return apiFetch<ProxySessionSummary>(`/api/proxy/sessions/${encodeURIComponent(sessionId)}`);
}

export function fetchProxySessionUsageRequest(sessionId: string): Promise<{
  summary: ProxyUsageSummary;
}> {
  return apiFetch<{ summary: ProxyUsageSummary }>(`/api/proxy/sessions/${encodeURIComponent(sessionId)}/usage`);
}

export function syncProxySessionRequest(payload: {
  session_id: string;
  title: string;
  transcript: TranscriptRecord[];
  is_running: boolean;
}): Promise<{
  session: {
    id: string;
    title: string;
    scope: 'chat' | 'project';
    project_id: string | null;
  };
  conversation: TranscriptRecord[];
  context_workbench_history: ContextWorkbenchHistoryEntry[];
  context_revision_history: unknown[];
  pending_context_restore: unknown | null;
}> {
  return apiFetch('/api/proxy-sync-session', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function saveProxyOverrideRequest(sessionId: string, transcript: TranscriptRecord[]): Promise<ProxySessionSummary> {
  return apiFetch<ProxySessionSummary>('/api/proxy-session-override', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, transcript }),
  });
}

export function resetProxyOverrideRequest(sessionId: string): Promise<ProxySessionSummary> {
  return apiFetch<ProxySessionSummary>('/api/proxy-session-reset', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export function resetProxyUsageRequest(sessionId: string): Promise<{
  cleared_count: number;
  summary: ProxyUsageSummary;
}> {
  return apiFetch('/api/proxy-session-usage-reset', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export function cancelActiveRequest(payload: {
  session_id: string;
  mode?: 'main' | 'context';
}): Promise<{ cancelled: boolean }> {
  return apiFetch<{ cancelled: boolean }>('/api/cancel-request', {
    method: 'POST',
    body: JSON.stringify({
      session_id: payload.session_id,
      mode: payload.mode || 'main',
    }),
  });
}

export function sendContextChatRequest(payload: {
  session_id: string;
  message: string;
  selected_node_indexes?: number[];
  reasoning_effort?: string;
}): Promise<ContextChatResponse> {
  return apiFetch<ContextChatResponse>('/api/context-chat', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function streamContextChatRequest(
  payload: {
    session_id: string;
    message: string;
    selected_node_indexes?: number[];
    reasoning_effort?: string;
  },
  onEvent: (event: ContextChatStreamEvent) => void,
  options: {
    signal?: AbortSignal;
  } = {},
): Promise<void> {
  const response = await fetch('/api/context-chat-stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    signal: options.signal,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(await extractResponseErrorMessage(response));
  }

  await readJsonLineStream<ContextChatStreamEvent>(response, onEvent);
}

export function deleteContextWorkbenchMessageRequest(payload: {
  session_id: string;
  message_index: number;
}): Promise<{
  conversation: TranscriptRecord[];
  history: ContextWorkbenchHistoryEntry[];
  revisions: unknown[];
  pending_restore: unknown | null;
}> {
  return apiFetch<{
    conversation: TranscriptRecord[];
    history: ContextWorkbenchHistoryEntry[];
    revisions: unknown[];
    pending_restore: unknown | null;
  }>('/api/context-workbench-history-message-delete', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function clearContextWorkbenchHistoryRequest(payload: {
  session_id: string;
}): Promise<{
  conversation: TranscriptRecord[];
  history: ContextWorkbenchHistoryEntry[];
  revisions: unknown[];
  pending_restore: unknown | null;
}> {
  return apiFetch<{
    conversation: TranscriptRecord[];
    history: ContextWorkbenchHistoryEntry[];
    revisions: unknown[];
    pending_restore: unknown | null;
  }>('/api/context-workbench-history-clear', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
