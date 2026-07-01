export type ContextMapMode = 'all' | 'condensed';

export interface ContextMapState {
  stage: 0 | 1 | 2;
  mode: ContextMapMode;
  width: number;
}

export interface AttachmentRecord {
  id?: string;
  name: string;
  mime_type: string;
  kind: 'image' | 'file';
  size_bytes?: number;
  url?: string;
  relative_path?: string;
}

export interface ComposerAttachment extends AttachmentRecord {
  data_url: string;
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
  role: 'user' | 'an' | 'subagent' | 'system' | 'developer' | 'compaction' | 'context';
  text: string;
  attachments: AttachmentRecord[];
  toolEvents: ToolEvent[];
  blocks: MessageBlock[];
  providerItems?: ProviderItem[];
  pending: boolean;
  sourceText: string;
}

export interface SessionSummary {
  id: string;
  title: string;
  scope: 'chat' | 'project';
  project_id: string | null;
}

export interface ProjectSummary {
  id: string;
  title: string;
  root_path?: string;
  sessions: SessionSummary[];
}

export interface ReasoningOption {
  value: string;
  label: string;
}

export interface OpenAISettings {
  default_model: string;
  default_reasoning_effort: string;
  context_workbench_model: string;
  context_workbench_provider_id: string;
  context_token_warning_threshold: number;
  context_token_critical_threshold: number;
  openai_base_url: string;
  max_tool_rounds: number;
  assistant_name: string;
  assistant_greeting: string;
  assistant_prompt: string;
  temperature: number | null;
  top_p: number | null;
  context_message_limit: number | null;
  streaming: boolean;
  user_name: string;
  user_locale: string;
  user_timezone: string;
  user_profile: string;
  theme_color: string;
  theme_mode: 'light' | 'dark';
  background_color: string;
  ui_font: string;
  code_font: string;
  ui_font_size: number;
  code_font_size: number;
  appearance_contrast: number;
  service_hints_enabled: boolean;
  has_api_key: boolean;
  api_key_preview: string;
  openai_api_key?: string;
  project_root: string;
  active_provider_id: string;
  response_providers: ResponseProviderSettings[];
  tool_settings: ToolSetting[];
}

export interface ResponseProviderModel {
  id: string;
  label: string;
  group: string;
  provider?: string;
}

export type ProviderType = 'chat_completion' | 'responses' | 'gemini' | 'claude';

export interface ResponseProviderSettings {
  id: string;
  name: string;
  provider_type: ProviderType;
  enabled: boolean;
  supports_model_fetch: boolean;
  supports_responses: boolean;
  api_base_url: string;
  default_model: string;
  has_api_key: boolean;
  api_key_preview: string;
  api_key?: string;
  models: ResponseProviderModel[];
  last_sync_at: string;
  last_sync_error: string;
}

export interface ResponseProviderDraft {
  id: string;
  name: string;
  provider_type: ProviderType;
  enabled: boolean;
  supports_model_fetch: boolean;
  supports_responses: boolean;
  api_base_url: string;
  api_key_input: string;
  clear_api_key: boolean;
  default_model: string;
  models: ResponseProviderModel[];
  last_sync_at: string;
  last_sync_error: string;
}

export interface ContextWorkbenchToolCatalogItem {
  id: string;
  label: string;
  description: string;
  status: 'available' | 'preview';
}

export interface ToolSetting {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
}

export interface ContextWorkbenchChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ProxyUsageBucket {
  request_count: number;
  input_tokens: number;
  cached_input_tokens: number;
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

export interface SettingsDraft {
  openai_api_key: string;
  openai_base_url: string;
  default_model: string;
  max_tool_rounds: number;
  assistant_name: string;
  assistant_greeting: string;
  assistant_prompt: string;
  temperature: string;
  temperature_enabled: boolean;
  top_p: string;
  top_p_enabled: boolean;
  context_message_limit: string;
  context_message_limit_enabled: boolean;
  streaming: boolean;
  user_name: string;
  user_locale: string;
  user_timezone: string;
  user_profile: string;
  active_provider_id: string;
  response_providers: ResponseProviderDraft[];
  tool_settings: ToolSetting[];
}

export interface InitPayload {
  settings?: {
    workbench_model?: string;
    theme_mode?: 'light' | 'dark';
    ui_font?: string;
    ui_font_size?: number;
    user_locale?: string;
  };
  active_session_id?: string;
  sessions?: Array<{ id: string; title: string; status: string; is_running?: boolean }>;
  conversations?: Record<string, TranscriptEntry[]>;
  context_workbench_histories?: Record<string, ContextWorkbenchChatMessage[]>;
}

export interface SidebarPayload {
  projects?: ProjectSummary[];
  chat_sessions?: SessionSummary[];
}

export interface SettingsResponse {
  settings: OpenAISettings;
  models: string[];
}

export interface ProviderModelsResponse extends SettingsResponse {
  provider_id: string;
  fetched_count: number;
}

export interface ProviderModelCandidatesResponse {
  provider_id: string;
  fetched_count: number;
  models: ResponseProviderModel[];
}

export interface CreateProjectResponse extends SidebarPayload {
  project: {
    id: string;
    title: string;
    root_path?: string;
  };
}

export interface ProjectActionResponse extends SidebarPayload {
  project: {
    id: string;
    title: string;
    root_path?: string;
  };
}

export interface ArchiveProjectSessionsResponse extends ProjectActionResponse {
  archived_session_ids: string[];
}

export interface CreateSessionResponse extends SidebarPayload {
  session: SessionSummary;
}

export interface DeleteSessionResponse extends SidebarPayload {
  deleted_session_id: string;
  deleted_scope: 'chat' | 'project';
  deleted_project_id: string | null;
}

export interface DeleteProjectResponse extends SidebarPayload {
  deleted_project_id: string;
  deleted_session_ids: string[];
}

export interface ResetSessionResponse extends SidebarPayload {
  session: SessionSummary;
}

export interface TruncateSessionResponse extends SidebarPayload {
  session: SessionSummary;
  conversation: TranscriptEntry[];
}

export interface SendMessageResponse extends SidebarPayload {
  answer: string;
  tool_events: ToolEvent[];
  blocks?: MessageBlock[];
  session: SessionSummary;
}

export interface ContextChatResponse {
  answer: string;
  used_model?: string;
  tool_events?: ToolEvent[];
  history: ContextWorkbenchChatMessage[];
  conversation: TranscriptEntry[];
}

export interface ContextWorkbenchSettingsResponse {
  settings: {
    context_workbench_model: string;
    context_workbench_provider_id: string;
    context_token_warning_threshold: number;
    context_token_critical_threshold: number;
    user_locale?: string;
    theme_mode?: 'light' | 'dark';
    ui_font?: string;
    ui_font_size?: number;
  };
  models: string[];
  response_providers?: ResponseProviderSettings[];
  tool_catalog: ContextWorkbenchToolCatalogItem[];
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

export interface StreamModelStartEvent {
  type: 'model_start';
}

export interface StreamModelDoneEvent {
  type: 'model_done';
}

export interface StreamToolEvent {
  type: 'tool_event';
  tool_event: ToolEvent;
}

export interface ContextChatFinalizingEvent {
  type: 'finalizing';
  stage?: 'commit' | string;
}

export interface StreamDoneEvent extends SidebarPayload {
  type: 'done';
  answer: string;
  tool_events: ToolEvent[];
  blocks?: MessageBlock[];
  session: SessionSummary;
}

export interface StreamErrorEvent {
  type: 'error';
  error: string;
}

export type SendMessageStreamEvent =
  | StreamDeltaEvent
  | StreamResetEvent
  | StreamModelStartEvent
  | StreamModelDoneEvent
  | StreamReasoningStartEvent
  | StreamReasoningDoneEvent
  | StreamToolEvent
  | StreamDoneEvent
  | StreamErrorEvent;

export interface ContextChatStreamDoneEvent {
  type: 'done';
  answer: string;
  used_model?: string;
  tool_events?: ToolEvent[];
  history: ContextWorkbenchChatMessage[];
  conversation: TranscriptEntry[];
}

export type ContextChatStreamEvent =
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

export type PromptBlockKind = 'system' | 'developer' | 'memory' | 'summary';
// Mirrors agent_runtime CanonicalItem.role only. Proxy transcript nodes use
// TranscriptNode.role and MessageRecord.role for lossless multi-role display.
export type TranscriptRole = 'user' | 'assistant';
export type CanonicalItemType = 'message' | 'tool_call' | 'tool_result';
export type CanonicalStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'error'
  | 'skipped'
  | (string & {});

export interface ProviderRaw {
  provider_id?: string;
  model?: string;
  request_id?: string;
  event_type?: string;
  payload?: CanonicalJsonValue;
  notes?: string[];
}

export interface PromptBlock {
  kind: PromptBlockKind;
  text: string;
  editable?: boolean;
  source?: string;
  id?: string;
  metadata?: CanonicalJsonObject;
}

export interface CanonicalItem {
  type: CanonicalItemType;
  role?: TranscriptRole;
  content?: CanonicalJsonValue;
  name?: string;
  call_id?: string;
  arguments?: CanonicalJsonValue;
  output?: CanonicalJsonValue;
  status?: CanonicalStatus;
  provider_raw?: ProviderRaw;
  providerRaw?: ProviderRaw;
  metadata?: CanonicalJsonObject;
}

export interface ToolEventRecord {
  name: string;
  arguments?: CanonicalJsonValue;
  output_preview?: string;
  raw_output?: string;
  display_title?: string;
  display_detail?: string;
  display_result?: string;
  status?: CanonicalStatus;
  call_id?: string;
  error?: string;
  provider_raw?: ProviderRaw;
  providerRaw?: ProviderRaw;
  metadata?: CanonicalJsonObject;
}

export interface AssistantRoundState {
  round_id?: string;
  answer_text?: string;
  canonical_items?: CanonicalItem[];
  tool_events?: ToolEventRecord[];
  provider_raw?: ProviderRaw;
  providerRaw?: ProviderRaw;
  is_final?: boolean;
  error?: string;
  metadata?: CanonicalJsonObject;
}
