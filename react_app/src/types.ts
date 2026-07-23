export interface AttachmentRecord {
  id?: string;
  name: string;
  mime_type: string;
  kind: 'image' | 'file';
  size_bytes?: number;
  url?: string;
  relative_path?: string;
}

export interface ToolEvent {
  name?: string;
  arguments?: unknown;
  call_id?: string;
  output_preview?: string;
  raw_output?: string;
  display_title?: string;
  display_detail?: string;
  display_result?: string;
  status?: 'completed' | 'error' | string;
  error?: string;
  provider_raw?: ProviderRaw;
  providerRaw?: ProviderRaw;
  metadata?: CanonicalJsonObject;
}

export interface ProviderMessageItem {
  type: 'message';
  role: 'system' | 'developer' | 'user' | 'assistant';
  content: string | Array<Record<string, unknown>>;
}

export interface ProviderFunctionCallItem {
  type: 'function_call';
  call_id: string;
  name: string;
  arguments: string;
}

export interface ProviderFunctionCallOutputItem {
  type: 'function_call_output';
  call_id: string;
  output: unknown;
}

export type ProviderItem =
  | ProviderMessageItem
  | ProviderFunctionCallItem
  | ProviderFunctionCallOutputItem
  | Record<string, unknown>;

export interface TranscriptNodeItem {
  kind: string;
  providerItem: ProviderItem;
  inputIndex?: number;
}

export interface TranscriptNode {
  id: string;
  role: string;
  items: TranscriptNodeItem[];
  source_map: Record<string, string>;
}

export interface TextMessageBlock {
  kind: 'text';
  text: string;
}

export interface ReasoningMessageBlock {
  kind: 'reasoning';
  text: string;
  status?: 'streaming' | 'completed' | string;
}

export interface ThinkingMessageBlock {
  kind: 'thinking';
}

export interface ToolMessageBlock {
  kind: 'tool';
  tool_event: ToolEvent;
}

export type MessageBlock = TextMessageBlock | ReasoningMessageBlock | ThinkingMessageBlock | ToolMessageBlock;

export type TranscriptEntry = TranscriptNode;

export interface MessageRecord {
  nodeId: string;
  role: 'user' | 'an' | 'subagent' | 'system' | 'developer' | 'compaction' | 'context';
  text: string;
  attachments: AttachmentRecord[];
  toolEvents: ToolEvent[];
  blocks: MessageBlock[];
  providerItems?: ProviderItem[];
  pending: boolean;
  sourceText: string;
  tokenEstimate?: number;
  toolTokenEstimate?: number;
}

export interface ReasoningOption {
  value: string;
  label: string;
}

export interface ResponseProviderModel {
  id: string;
  label: string;
  group: string;
  provider?: string;
  source?: 'provider' | 'configured';
}

export type ResponseProviderType = 'responses' | 'chat_completion' | 'claude' | 'gemini';

export interface ContextWorkbenchProvider {
  id: string;
  name: string;
  provider_type: ResponseProviderType;
  enabled: boolean;
  supports_model_fetch: boolean;
  supports_responses: boolean;
  api_base_url: string;
  api_key: string;
  default_model: string;
  models: ResponseProviderModel[];
  last_sync_at?: string;
  last_sync_error?: string;
}

export interface ContextWorkbenchChatMessage {
  role: 'user' | 'assistant';
  content: string;
  toolEvents?: ToolEvent[];
  blocks?: MessageBlock[];
}

export interface ProxyUsageBucket {
  request_count: number;
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_tokens?: number;
  non_cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  known_cost_usd: number;
  unknown_cost_request_count: number;
  cache_hit_rate: number;
  latest_at?: string;
}

export interface ProxyUsageSummary extends ProxyUsageBucket {
  session_id: string;
  by_kind?: Record<string, ProxyUsageBucket>;
  by_model?: Record<string, ProxyUsageBucket>;
}

export interface ContextReviewStats {
  node_count: number;
  token_count: number;
  tool_token_count?: number;
}

export interface ContextReview {
  id: string;
  session_id: string;
  status: 'pending' | 'running' | 'stale' | string;
  source?: string;
  base_transcript_version: number;
  created_at: string;
  summary: string;
  model?: string;
  before?: ContextReviewStats;
  after?: ContextReviewStats;
  proposed_transcript?: TranscriptEntry[];
}

export interface InitPayload {
  settings?: {
    workbench_model?: string;
    theme_mode?: 'light' | 'dark';
    ui_font?: string;
    ui_font_size?: number;
    user_locale?: string;
  };
  runtime?: {
    proxy_port?: number;
    proxy_realtime_path?: string;
  };
  active_session_id?: string;
  sessions?: Array<{ id: string; title: string; status: string; is_running?: boolean }>;
  conversations?: Record<string, TranscriptEntry[]>;
  context_workbench_histories?: Record<string, ContextWorkbenchChatMessage[]>;
}

export interface ContextWorkbenchSettingsResponse {
  scope: 'global';
  settings: {
    context_workbench_model: string;
    context_workbench_provider_id: string;
    context_review_auto_enabled: boolean;
    context_review_interval_minutes: number;
    context_token_warning_threshold: number;
    context_token_critical_threshold: number;
    user_locale?: string;
    theme_mode?: 'light' | 'dark';
    ui_font?: string;
    ui_font_size?: number;
    codex_system_prompt: string;
    codex_system_prompt_default: string;
    manual_local_compact_prompt: string;
    manual_local_compact_prompt_default: string;
    auto_local_compact_prompt: string;
    auto_local_compact_prompt_default: string;
  };
  providers: ContextWorkbenchProvider[];
}

export interface StreamDeltaEvent {
  type: 'delta';
  delta: string;
  kind?: 'text' | 'reasoning';
}

export interface StreamResetEvent {
  type: 'reset';
}

export interface StreamReasoningStartEvent {
  type: 'reasoning_start';
}

export interface StreamReasoningDoneEvent {
  type: 'reasoning_done';
}

export interface StreamToolEvent {
  type: 'tool_event';
  tool_event: ToolEvent;
}

export interface ContextChatFinalizingEvent {
  type: 'finalizing';
  stage?: 'commit' | string;
}

export interface StreamErrorEvent {
  type: 'error';
  error: string;
}

export interface ContextChatStreamDoneEvent {
  type: 'done';
  answer: string;
  used_model?: string;
  tool_events?: ToolEvent[];
  history: ContextWorkbenchChatMessage[];
  conversation: TranscriptEntry[];
}

export interface ContextChatStreamStartedEvent {
  type: 'started';
  request_id: string;
}

export type ContextChatStreamEvent =
  | ContextChatStreamStartedEvent
  | StreamDeltaEvent
  | StreamResetEvent
  | StreamReasoningStartEvent
  | StreamReasoningDoneEvent
  | StreamToolEvent
  | ContextChatFinalizingEvent
  | StreamErrorEvent
  | ContextChatStreamDoneEvent;



export type CanonicalJsonValue =
  | string
  | number
  | boolean
  | null
  | CanonicalJsonValue[]
  | { [key: string]: CanonicalJsonValue };

export type CanonicalJsonObject = { [key: string]: CanonicalJsonValue };

export interface ProviderRaw {
  provider_id?: string;
  model?: string;
  request_id?: string;
  event_type?: string;
  payload?: CanonicalJsonValue;
  notes?: string[];
}
