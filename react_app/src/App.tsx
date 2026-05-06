import { startTransition, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, MouseEvent as ReactMouseEvent } from 'react';
import { flushSync } from 'react-dom';

import {
  archiveProjectSessionsRequest,
  cancelActiveRequest,
  createProjectRequest,
  createSessionRequest,
  deleteMessageRequest,
  deleteProjectRequest,
  deleteSessionRequest,
  fetchInit,
  fetchProviderModelCandidatesRequest,
  fetchSettings,
  pinProjectRequest,
  renameProjectRequest,
  saveSettingsRequest,
  sendMessageRequest,
  streamMessageRequest,
  truncateSessionRequest,
} from './api';
import ChatModelPicker from './components/ChatModelPicker';
import ChatView from './components/ChatView';
import ContextMapSidebar from './components/ContextMapSidebar';
import SettingsView from './components/SettingsView';
import Sidebar from './components/Sidebar';
import Toast from './components/Toast';
import {
  ATTACHMENT_READY_TOAST,
  ATTACHMENT_REMOVED_TOAST,
  COLLAPSE_TOAST,
  COPY_TOAST,
  DELETE_CHAT_TOAST,
  EDIT_TOAST,
  EMPTY_SEND_TOAST,
  INITIAL_TOAST_MESSAGE,
  NEW_CHAT_TOAST,
  NEW_PROJECT_TOAST,
  PAPER_INK_WHITE_THEME,
  PERMISSION_OPTIONS,
  REGENERATE_MISSING_TOAST,
  REGENERATE_TOAST,
  SENDING_TOAST,
  SETTINGS_API_KEY_CLEARED_TOAST,
  SETTINGS_HINTS_OFF_TOAST,
  SETTINGS_HINTS_ON_TOAST,
  SETTINGS_SAVED_TOAST,
  THEME_OPTIONS,
  THEME_TOAST,
} from './constants';
import type {
  ComposerAttachment,
  ContextWorkbenchHistoryEntry,
  ContextRevisionSummary,
  PendingContextRestore,
  DropdownId,
  InitPayload,
  MessageBlock,
  MessageRecord,
  OpenAISettings,
  PermissionOption,
  ProjectSummary,
  ReasoningOption,
  ResponseProviderDraft,
  ResponseProviderModel,
  ResponseProviderSettings,
  ProviderType,
  SessionSummary,
  SettingsDraft,
  SidebarMode,
  ToolSetting,
  ViewName,
  ContextMapState,
} from './types';
import {
  copyText,
  deriveSidebarState,
  getConversation,
  getProjectById,
  getReasoningLabel,
  getSessionMeta,
  normalizeConversation,
  normalizeReasoningOptions,
} from './utils';
import { localizeAppDom, localizeUiText, normalizeSupportedLocale } from './i18n';

type AppearanceMode = 'light' | 'dark' | 'system';
type ResolvedAppearanceMode = 'light' | 'dark';

interface PersistedUiPreferences {
  theme_color: string;
  theme_mode: AppearanceMode;
  background_color: string;
  ui_font: string;
  code_font: string;
  ui_font_size: number;
  code_font_size: number;
  appearance_contrast: number;
  service_hints_enabled: boolean;
}

interface RgbColor {
  r: number;
  g: number;
  b: number;
}

function defaultResponseProviderSettings(): ResponseProviderSettings[] {
  return [
    {
      id: 'openai',
      name: 'OpenAI',
      provider_type: 'responses',
      enabled: true,
      supports_model_fetch: true,
      supports_responses: true,
      api_base_url: 'https://api.openai.com/v1',
      default_model: 'gpt-5.4-mini',
      has_api_key: false,
      api_key_preview: '',
      models: [],
      last_sync_at: '',
      last_sync_error: '',
    },
    {
      id: 'anthropic',
      name: 'Claude',
      provider_type: 'claude',
      enabled: true,
      supports_model_fetch: true,
      supports_responses: false,
      api_base_url: 'https://api.anthropic.com/v1',
      default_model: 'claude-sonnet-4-5',
      has_api_key: false,
      api_key_preview: '',
      models: [],
      last_sync_at: '',
      last_sync_error: '',
    },
    {
      id: 'gemini',
      name: 'Gemini',
      provider_type: 'gemini',
      enabled: true,
      supports_model_fetch: true,
      supports_responses: false,
      api_base_url: 'https://generativelanguage.googleapis.com/v1beta',
      default_model: 'gemini-2.5-pro',
      has_api_key: false,
      api_key_preview: '',
      models: [],
      last_sync_at: '',
      last_sync_error: '',
    },
  ];
}

function providerTypeFromUnknown(rawType: unknown, providerId = ''): ProviderType {
  if (
    rawType === 'chat_completion' ||
    rawType === 'responses' ||
    rawType === 'gemini' ||
    rawType === 'claude'
  ) {
    return rawType;
  }
  if (providerId === 'gemini') {
    return 'gemini';
  }
  if (providerId === 'anthropic' || providerId === 'claude') {
    return 'claude';
  }
  return 'responses';
}

function createProviderDraft(
  providerType: ProviderType,
  existingProviders: ResponseProviderDraft[],
  providerName?: string,
): ResponseProviderDraft {
  const count = existingProviders.filter((provider) => provider.provider_type === providerType).length + 1;
  const id = `custom-${providerType}-${Date.now().toString(36)}`;
  const defaults: Record<ProviderType, { name: string; apiBaseUrl: string; defaultModel: string }> = {
    responses: {
      name: `Responses ${count}`,
      apiBaseUrl: 'https://api.openai.com/v1',
      defaultModel: 'gpt-5.4-mini',
    },
    chat_completion: {
      name: `Chat Completion ${count}`,
      apiBaseUrl: 'https://api.example.com/v1',
      defaultModel: 'gpt-4.1-mini',
    },
    gemini: {
      name: `Gemini ${count}`,
      apiBaseUrl: 'https://generativelanguage.googleapis.com/v1beta',
      defaultModel: 'gemini-2.5-pro',
    },
    claude: {
      name: `Claude ${count}`,
      apiBaseUrl: 'https://api.anthropic.com/v1',
      defaultModel: 'claude-sonnet-4-5',
    },
  };
  const spec = defaults[providerType];
  const resolvedName = providerName?.trim() || spec.name;

  return {
    id,
    name: resolvedName,
    provider_type: providerType,
    enabled: true,
    supports_model_fetch: true,
    supports_responses: providerType === 'responses',
    api_base_url: spec.apiBaseUrl,
    api_key_input: '',
    clear_api_key: false,
    default_model: spec.defaultModel,
    models: [],
    last_sync_at: '',
    last_sync_error: '',
  };
}

function normalizeResponseProviderSettings(rawProviders: unknown): ResponseProviderSettings[] {
  const defaults = defaultResponseProviderSettings();
  const rawProviderList = Array.isArray(rawProviders) ? rawProviders : [];
  const normalizedDefaultIds = new Set(defaults.map((provider) => provider.id));
  const legacyDefaultIds = new Set(['openrouter', 'newapi', 'siliconflow', 'lmstudio']);

  const normalizedDefaults = defaults.map((defaultProvider) => {
    const matchedProvider = rawProviderList.find((item) => {
      if (!item || typeof item !== 'object') {
        return false;
      }
      return (item as { id?: unknown }).id === defaultProvider.id;
    }) as Partial<ResponseProviderSettings> | undefined;

    return {
      ...defaultProvider,
      ...(matchedProvider || {}),
      id: matchedProvider?.id || defaultProvider.id,
      name: typeof matchedProvider?.name === 'string' && matchedProvider.name ? matchedProvider.name : defaultProvider.name,
      provider_type: providerTypeFromUnknown(matchedProvider?.provider_type, defaultProvider.id),
      enabled: matchedProvider?.enabled ?? defaultProvider.enabled,
      supports_model_fetch: matchedProvider?.supports_model_fetch ?? true,
      supports_responses: providerTypeFromUnknown(matchedProvider?.provider_type, defaultProvider.id) === 'responses',
      api_base_url: typeof matchedProvider?.api_base_url === 'string'
        ? matchedProvider.api_base_url
        : defaultProvider.api_base_url,
      default_model: typeof matchedProvider?.default_model === 'string'
        ? matchedProvider.default_model
        : defaultProvider.default_model,
      has_api_key: Boolean(matchedProvider?.has_api_key),
      api_key_preview: typeof matchedProvider?.api_key_preview === 'string' ? matchedProvider.api_key_preview : '',
      api_key: typeof matchedProvider?.api_key === 'string' ? matchedProvider.api_key : '',
      models: Array.isArray(matchedProvider?.models) ? matchedProvider.models : defaultProvider.models,
      last_sync_at: typeof matchedProvider?.last_sync_at === 'string' ? matchedProvider.last_sync_at : '',
      last_sync_error: typeof matchedProvider?.last_sync_error === 'string' ? matchedProvider.last_sync_error : '',
    };
  });

  const customProviders = rawProviderList.flatMap((item): ResponseProviderSettings[] => {
    if (!item || typeof item !== 'object') {
      return [];
    }
    const raw = item as Partial<ResponseProviderSettings>;
    const id = typeof raw.id === 'string' ? raw.id.trim() : '';
    if (!id || normalizedDefaultIds.has(id) || legacyDefaultIds.has(id)) {
      return [];
    }
    const providerType = providerTypeFromUnknown(raw.provider_type, id);
    return [
      {
        id,
        name: typeof raw.name === 'string' && raw.name.trim() ? raw.name.trim() : id,
        provider_type: providerType,
        enabled: raw.enabled ?? true,
        supports_model_fetch: raw.supports_model_fetch ?? true,
        supports_responses: providerType === 'responses',
        api_base_url: typeof raw.api_base_url === 'string' ? raw.api_base_url : '',
        default_model: typeof raw.default_model === 'string' ? raw.default_model : '',
        has_api_key: Boolean(raw.has_api_key),
        api_key_preview: typeof raw.api_key_preview === 'string' ? raw.api_key_preview : '',
        api_key: typeof raw.api_key === 'string' ? raw.api_key : '',
        models: Array.isArray(raw.models) ? raw.models : [],
        last_sync_at: typeof raw.last_sync_at === 'string' ? raw.last_sync_at : '',
        last_sync_error: typeof raw.last_sync_error === 'string' ? raw.last_sync_error : '',
      },
    ];
  });

  return [...normalizedDefaults, ...customProviders];
}

function optionalNumberString(value: number | null | undefined, fallback: string) {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback;
}

function parseOptionalNumber(value: string, enabled: boolean) {
  if (!enabled) {
    return null;
  }

  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeOptionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function normalizeBoundedNumber(value: unknown, min: number, max: number, fallback: number) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return fallback;
  }

  return Math.min(max, Math.max(min, value));
}

function normalizeAppearanceMode(value: unknown): AppearanceMode {
  return value === 'light' || value === 'dark' || value === 'system' ? value : DEFAULT_SETTINGS.theme_mode;
}

function isHexColorValue(value: unknown): value is string {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value);
}

function normalizeThemeColorSetting(value: unknown, fallback: string) {
  if (value === PAPER_INK_WHITE_THEME || isHexColorValue(value)) {
    return value;
  }

  return fallback;
}

function normalizeHexColorSetting(value: unknown, fallback: string) {
  return isHexColorValue(value) ? value : fallback;
}

function normalizeReasoningEffortSetting(value: unknown) {
  return typeof value === 'string' && ['default', 'none', 'low', 'medium', 'high'].includes(value)
    ? value
    : DEFAULT_SETTINGS.default_reasoning_effort;
}

function resolveReasoningEffort(value: string, options: ReasoningOption[]) {
  return options.some((option) => option.value === value)
    ? value
    : options.find((option) => option.value === 'default')?.value || options[0]?.value || DEFAULT_SETTINGS.default_reasoning_effort;
}

function normalizeSettingsPayload(rawSettings: Partial<OpenAISettings> | undefined): OpenAISettings {
  const nextSettings = rawSettings || {};

  return {
    default_model: typeof nextSettings.default_model === 'string' && nextSettings.default_model
      ? nextSettings.default_model
      : DEFAULT_SETTINGS.default_model,
    default_reasoning_effort: normalizeReasoningEffortSetting(nextSettings.default_reasoning_effort),
    context_workbench_model: typeof nextSettings.context_workbench_model === 'string' && nextSettings.context_workbench_model
      ? nextSettings.context_workbench_model
      : DEFAULT_SETTINGS.context_workbench_model,
    context_workbench_provider_id:
      typeof nextSettings.context_workbench_provider_id === 'string' && nextSettings.context_workbench_provider_id
        ? nextSettings.context_workbench_provider_id
        : (typeof nextSettings.active_provider_id === 'string' && nextSettings.active_provider_id
            ? nextSettings.active_provider_id
            : DEFAULT_SETTINGS.context_workbench_provider_id),
    context_token_warning_threshold: typeof nextSettings.context_token_warning_threshold === 'number'
      ? nextSettings.context_token_warning_threshold
      : DEFAULT_SETTINGS.context_token_warning_threshold,
    context_token_critical_threshold: typeof nextSettings.context_token_critical_threshold === 'number'
      ? nextSettings.context_token_critical_threshold
      : DEFAULT_SETTINGS.context_token_critical_threshold,
    openai_base_url: typeof nextSettings.openai_base_url === 'string' ? nextSettings.openai_base_url : '',
    max_tool_rounds: typeof nextSettings.max_tool_rounds === 'number'
      ? nextSettings.max_tool_rounds
      : DEFAULT_SETTINGS.max_tool_rounds,
    assistant_name: typeof nextSettings.assistant_name === 'string' ? nextSettings.assistant_name : DEFAULT_SETTINGS.assistant_name,
    assistant_greeting: typeof nextSettings.assistant_greeting === 'string' ? nextSettings.assistant_greeting : DEFAULT_SETTINGS.assistant_greeting,
    assistant_prompt: typeof nextSettings.assistant_prompt === 'string' ? nextSettings.assistant_prompt : DEFAULT_SETTINGS.assistant_prompt,
    temperature: normalizeOptionalNumber(nextSettings.temperature),
    top_p: normalizeOptionalNumber(nextSettings.top_p),
    context_message_limit: normalizeOptionalNumber(nextSettings.context_message_limit),
    streaming: typeof nextSettings.streaming === 'boolean' ? nextSettings.streaming : DEFAULT_SETTINGS.streaming,
    user_name: typeof nextSettings.user_name === 'string' ? nextSettings.user_name : DEFAULT_SETTINGS.user_name,
    user_locale: normalizeSupportedLocale(nextSettings.user_locale),
    user_timezone: typeof nextSettings.user_timezone === 'string' ? nextSettings.user_timezone : DEFAULT_SETTINGS.user_timezone,
    user_profile: typeof nextSettings.user_profile === 'string' ? nextSettings.user_profile : DEFAULT_SETTINGS.user_profile,
    theme_color: normalizeThemeColorSetting(nextSettings.theme_color, DEFAULT_SETTINGS.theme_color),
    theme_mode: normalizeAppearanceMode(nextSettings.theme_mode),
    background_color: normalizeHexColorSetting(nextSettings.background_color, DEFAULT_SETTINGS.background_color),
    ui_font: typeof nextSettings.ui_font === 'string' && nextSettings.ui_font ? nextSettings.ui_font : DEFAULT_SETTINGS.ui_font,
    code_font: typeof nextSettings.code_font === 'string' && nextSettings.code_font ? nextSettings.code_font : DEFAULT_SETTINGS.code_font,
    ui_font_size: normalizeBoundedNumber(nextSettings.ui_font_size, 12, 22, DEFAULT_SETTINGS.ui_font_size),
    code_font_size: normalizeBoundedNumber(nextSettings.code_font_size, 11, 20, DEFAULT_SETTINGS.code_font_size),
    appearance_contrast: normalizeBoundedNumber(
      nextSettings.appearance_contrast,
      30,
      80,
      DEFAULT_SETTINGS.appearance_contrast,
    ),
    service_hints_enabled: typeof nextSettings.service_hints_enabled === 'boolean'
      ? nextSettings.service_hints_enabled
      : DEFAULT_SETTINGS.service_hints_enabled,
    has_api_key: Boolean(nextSettings.has_api_key),
    api_key_preview: typeof nextSettings.api_key_preview === 'string' ? nextSettings.api_key_preview : '',
    openai_api_key: typeof nextSettings.openai_api_key === 'string' ? nextSettings.openai_api_key : '',
    project_root: typeof nextSettings.project_root === 'string' ? nextSettings.project_root : '',
    active_provider_id: typeof nextSettings.active_provider_id === 'string' && nextSettings.active_provider_id
      ? nextSettings.active_provider_id
      : DEFAULT_SETTINGS.active_provider_id,
    response_providers: normalizeResponseProviderSettings(nextSettings.response_providers),
    tool_settings: normalizeToolSettings(nextSettings.tool_settings),
  };
}

const DEFAULT_TOOL_SETTINGS: ToolSetting[] = [
  { name: 'parallel_tools', label: 'parallel_tools', description: 'Run multiple enabled tools concurrently and return their results together.', enabled: true },
  { name: 'shell_command', label: 'Shell 命令', description: '执行一次性 PowerShell 命令，适合检查环境、运行测试和调试。', enabled: true },
  { name: 'exec_command', label: 'Exec 命令', description: '启动命令并返回输出；长时间运行的进程会给出 process_id。', enabled: true },
  { name: 'write_stdin', label: '写入 stdin', description: '向 exec_command 启动的仍在运行的进程写入输入。', enabled: true },
  { name: 'apply_patch', label: 'Apply Patch', description: '使用 Codex 风格 patch 修改、创建、删除或移动工作区文件。', enabled: true },
  { name: 'list_dir', label: '列出目录', description: '按 Codex list_dir 风格列出目录内容，支持分页和递归深度。', enabled: true },
  { name: 'read_file', label: '读取文件', description: '读取工作区中的文本文件。', enabled: true },
  { name: 'view_image', label: '查看图片', description: '读取工作区图片并以 data URL 形式返回给模型。', enabled: true },
  { name: 'js_repl', label: 'JS REPL', description: '在本地 Node.js kernel 中运行 JavaScript 片段。', enabled: true },
  { name: 'js_repl_reset', label: '重置 JS REPL', description: '重置本地 JavaScript kernel。', enabled: true },
  { name: 'get_current_time', label: '当前时间', description: '获取指定时区的当前时间。', enabled: true },
];

function normalizeToolSettings(rawTools: unknown): ToolSetting[] {
  const rawByName = new Map<string, Partial<ToolSetting>>();
  if (Array.isArray(rawTools)) {
    rawTools.forEach((item) => {
      if (item && typeof item === 'object' && typeof (item as { name?: unknown }).name === 'string') {
        rawByName.set((item as { name: string }).name, item as Partial<ToolSetting>);
      }
    });
  }

  return DEFAULT_TOOL_SETTINGS.map((tool) => {
    const raw = rawByName.get(tool.name);
    return {
      name: tool.name,
      label: typeof raw?.label === 'string' && raw.label ? raw.label : tool.label,
      description: typeof raw?.description === 'string' && raw.description ? raw.description : tool.description,
      enabled: typeof raw?.enabled === 'boolean' ? raw.enabled : tool.enabled,
    };
  });
}

const DEFAULT_SETTINGS: OpenAISettings = {
  default_model: 'gpt-5.4-mini',
  default_reasoning_effort: 'default',
  context_workbench_model: 'gpt-5.4-mini',
  context_workbench_provider_id: 'openai',
  context_token_warning_threshold: 5000,
  context_token_critical_threshold: 10000,
  openai_base_url: '',
  max_tool_rounds: 999999,
  assistant_name: 'Hanako',
  assistant_greeting: '对话开始时先接住情绪，再推进任务，不要一上来就像客服一样念模板。',
  assistant_prompt: '你是一个温柔、可靠、说人话的助手。先理解我的真实意图，再给出清晰直接的建议；少一些官话，多一些陪我一起把事情做完的感觉。',
  temperature: null,
  top_p: null,
  context_message_limit: null,
  streaming: true,
  user_name: '小宝',
  user_locale: 'en-US',
  user_timezone: 'Asia/Shanghai',
  user_profile: '希望它更像一个陪我做事的搭档，不要太客服腔。帮我收住情绪，也帮我推进执行。',
  theme_color: THEME_OPTIONS[0].value,
  theme_mode: 'dark',
  background_color: '#111111',
  ui_font: 'Noto Serif SC',
  code_font: 'JetBrains Mono',
  ui_font_size: 16,
  code_font_size: 14,
  appearance_contrast: 45,
  service_hints_enabled: true,
    has_api_key: false,
    api_key_preview: '',
    openai_api_key: '',
    project_root: '',
  active_provider_id: 'openai',
  response_providers: defaultResponseProviderSettings(),
  tool_settings: DEFAULT_TOOL_SETTINGS,
};

function buildConversationMap(rawConversations: InitPayload['conversations']) {
  const nextConversations: Record<string, MessageRecord[]> = {};

  Object.entries(rawConversations || {}).forEach(([sessionId, records]) => {
    nextConversations[sessionId] = normalizeConversation(records);
  });

  return nextConversations;
}

function buildContextWorkbenchHistoryMap(rawHistories: InitPayload['context_workbench_histories']) {
  const nextHistories: Record<string, ContextWorkbenchHistoryEntry[]> = {};

  Object.entries(rawHistories || {}).forEach(([sessionId, entries]) => {
    if (!Array.isArray(entries)) {
      return;
    }

    const safeEntries = entries
      .filter((entry): entry is ContextWorkbenchHistoryEntry => {
        if (!entry || typeof entry !== 'object') {
          return false;
        }

        return (
          (entry.role === 'user' || entry.role === 'assistant') &&
          typeof entry.content === 'string' &&
          entry.content.trim().length > 0
        );
      })
      .map((entry) => ({
        role: entry.role,
        content: entry.content,
      }));

    if (safeEntries.length) {
      nextHistories[sessionId] = safeEntries;
    }
  });

  return nextHistories;
}

function buildContextRevisionHistoryMap(rawHistories: InitPayload['context_revision_histories']) {
  const nextHistories: Record<string, ContextRevisionSummary[]> = {};

  Object.entries(rawHistories || {}).forEach(([sessionId, entries]) => {
    if (!Array.isArray(entries)) {
      return;
    }

    const safeEntries = entries
      .filter((entry): entry is ContextRevisionSummary => {
        if (!entry || typeof entry !== 'object') {
          return false;
        }

        return (
          typeof entry.id === 'string' &&
          typeof entry.label === 'string' &&
          typeof entry.created_at === 'string' &&
          typeof entry.operation_count === 'number' &&
          typeof entry.node_count === 'number'
        );
      })
      .map((entry) => ({
        id: entry.id,
        label: entry.label,
        summary: typeof entry.summary === 'string' ? entry.summary : entry.label,
        created_at: entry.created_at,
        revision_number: typeof entry.revision_number === 'number' ? entry.revision_number : 0,
        change_type: typeof entry.change_type === 'string' ? entry.change_type : 'update',
        change_types: Array.isArray(entry.change_types)
          ? entry.change_types.filter((item): item is string => typeof item === 'string')
          : [],
        changed_nodes: Array.isArray(entry.changed_nodes)
          ? entry.changed_nodes
              .map((item) => Number(item))
              .filter((item) => Number.isFinite(item) && item > 0)
          : [],
        is_active: Boolean(entry.is_active),
        operation_count: entry.operation_count,
        node_count: entry.node_count,
      }));

    if (safeEntries.length) {
      nextHistories[sessionId] = safeEntries;
    }
  });

  return nextHistories;
}

function buildPendingContextRestoreMap(rawRestores: InitPayload['pending_context_restores']) {
  const nextRestores: Record<string, PendingContextRestore> = {};

  Object.entries(rawRestores || {}).forEach(([sessionId, entry]) => {
    if (!entry || typeof entry !== 'object') {
      return;
    }

    if (
      typeof entry.target_revision_id !== 'string' ||
      typeof entry.target_label !== 'string' ||
      typeof entry.created_at !== 'string' ||
      typeof entry.can_undo !== 'boolean'
    ) {
      return;
    }

    nextRestores[sessionId] = {
      target_revision_id: entry.target_revision_id,
      target_label: entry.target_label,
      created_at: entry.created_at,
      undo_active_revision_id:
        typeof entry.undo_active_revision_id === 'string' ? entry.undo_active_revision_id : undefined,
      can_undo: entry.can_undo,
    };
  });

  return nextRestores;
}

function toSettingsDraft(settings: OpenAISettings): SettingsDraft {
  const normalizedSettings = normalizeSettingsPayload(settings);
  const responseProviders = normalizedSettings.response_providers;

  return {
    openai_api_key: normalizedSettings.openai_api_key || '',
    openai_base_url: normalizedSettings.openai_base_url || '',
    default_model: normalizedSettings.default_model || 'gpt-5.4-mini',
    max_tool_rounds: normalizedSettings.max_tool_rounds || 6,
    assistant_name: normalizedSettings.assistant_name,
    assistant_greeting: normalizedSettings.assistant_greeting,
    assistant_prompt: normalizedSettings.assistant_prompt,
    temperature: optionalNumberString(normalizedSettings.temperature, '0.7'),
    temperature_enabled: normalizedSettings.temperature !== null,
    top_p: optionalNumberString(normalizedSettings.top_p, '1'),
    top_p_enabled: normalizedSettings.top_p !== null,
    context_message_limit: optionalNumberString(normalizedSettings.context_message_limit, '40'),
    context_message_limit_enabled: normalizedSettings.context_message_limit !== null,
    streaming: normalizedSettings.streaming,
    user_name: normalizedSettings.user_name,
    user_locale: normalizedSettings.user_locale,
    user_timezone: normalizedSettings.user_timezone,
    user_profile: normalizedSettings.user_profile,
    active_provider_id: normalizedSettings.active_provider_id || 'openai',
    tool_settings: normalizeToolSettings(normalizedSettings.tool_settings),
    response_providers: responseProviders.map((provider): ResponseProviderDraft => ({
      id: provider.id,
      name: provider.name,
      provider_type: provider.provider_type,
      enabled: provider.enabled,
      supports_model_fetch: provider.supports_model_fetch,
      supports_responses: provider.supports_responses,
      api_base_url: provider.api_base_url || '',
      api_key_input: provider.api_key || '',
      clear_api_key: false,
      default_model: provider.default_model || '',
      models: Array.isArray(provider.models) ? provider.models : [],
      last_sync_at: provider.last_sync_at || '',
      last_sync_error: provider.last_sync_error || '',
    })),
  };
}

function buildSettingsSavePayload(
  draft: SettingsDraft,
  options: {
    activeProviderId?: string;
    clearProviderApiKeyId?: string;
    defaultReasoningEffort?: string;
    uiPreferences?: PersistedUiPreferences;
  } = {},
) {
  const effectiveActiveProviderId = options.activeProviderId || draft.active_provider_id;
  const effectiveActiveProvider = draft.response_providers.find((provider) => provider.id === effectiveActiveProviderId);

  return {
    default_model: effectiveActiveProvider?.default_model || draft.default_model,
    ...(options.defaultReasoningEffort !== undefined
      ? { default_reasoning_effort: options.defaultReasoningEffort }
      : {}),
    openai_base_url: (effectiveActiveProvider?.api_base_url || draft.openai_base_url).trim(),
    max_tool_rounds: draft.max_tool_rounds,
    assistant_name: draft.assistant_name,
    assistant_greeting: draft.assistant_greeting,
    assistant_prompt: draft.assistant_prompt,
    temperature: parseOptionalNumber(draft.temperature, draft.temperature_enabled),
    top_p: parseOptionalNumber(draft.top_p, draft.top_p_enabled),
    context_message_limit: parseOptionalNumber(draft.context_message_limit, draft.context_message_limit_enabled),
    streaming: draft.streaming,
    user_name: draft.user_name,
    user_locale: normalizeSupportedLocale(draft.user_locale),
    user_timezone: draft.user_timezone,
    user_profile: draft.user_profile,
    tool_settings: normalizeToolSettings(draft.tool_settings),
    active_provider_id: effectiveActiveProviderId,
    response_providers: draft.response_providers.map((provider) => {
      const payload: {
        id: string;
        name: string;
        provider_type: ProviderType;
        enabled: boolean;
        supports_model_fetch: boolean;
        supports_responses: boolean;
        api_base_url: string;
        default_model: string;
        models: ResponseProviderDraft['models'];
        last_sync_at: string;
        last_sync_error: string;
        api_key?: string;
        clear_api_key?: boolean;
      } = {
        id: provider.id,
        name: provider.name,
        provider_type: provider.provider_type,
        enabled: provider.enabled,
        supports_model_fetch: provider.supports_model_fetch,
        supports_responses: provider.supports_responses,
        api_base_url: provider.api_base_url.trim(),
        default_model: provider.default_model,
        models: provider.models,
        last_sync_at: provider.last_sync_at,
        last_sync_error: provider.last_sync_error,
      };

      const typedApiKey = provider.api_key_input.trim();
      if (typedApiKey) {
        payload.api_key = typedApiKey;
      }
      if (provider.clear_api_key || options.clearProviderApiKeyId === provider.id) {
        payload.clear_api_key = true;
      }
      return payload;
    }),
    ...((effectiveActiveProvider?.api_key_input || draft.openai_api_key).trim()
      ? { openai_api_key: (effectiveActiveProvider?.api_key_input || draft.openai_api_key).trim() }
      : {}),
    ...(options.uiPreferences || {}),
  };
}

function uiPreferencesFromSettings(settings: OpenAISettings): PersistedUiPreferences {
  return {
    theme_color: settings.theme_color,
    theme_mode: settings.theme_mode,
    background_color: settings.background_color,
    ui_font: settings.ui_font,
    code_font: settings.code_font,
    ui_font_size: settings.ui_font_size,
    code_font_size: settings.code_font_size,
    appearance_contrast: settings.appearance_contrast,
    service_hints_enabled: settings.service_hints_enabled,
  };
}

function settingsPayloadSnapshot(payload: unknown) {
  return JSON.stringify(payload);
}

function removeProviderFromDraft(draft: SettingsDraft, providerId: string): SettingsDraft {
  const nextProviders = draft.response_providers.filter((provider) => provider.id !== providerId);
  const nextActiveProvider = nextProviders.find((provider) => provider.id === draft.active_provider_id) || nextProviders[0];

  return {
    ...draft,
    active_provider_id: nextActiveProvider?.id || '',
    response_providers: nextProviders,
    default_model: nextActiveProvider?.default_model || draft.default_model,
    openai_base_url: nextActiveProvider?.api_base_url || draft.openai_base_url,
    openai_api_key: nextActiveProvider?.id === draft.active_provider_id ? draft.openai_api_key : '',
  };
}

function applyProviderModelSelection(draft: SettingsDraft, providerId: string, modelId: string): SettingsDraft {
  const nextProviders = draft.response_providers.map((provider) =>
    provider.id === providerId
      ? {
          ...provider,
          default_model: modelId,
        }
      : provider,
  );
  const nextActiveProvider = nextProviders.find((provider) => provider.id === providerId) || nextProviders[0];

  return {
    ...draft,
    active_provider_id: nextActiveProvider?.id || draft.active_provider_id,
    response_providers: nextProviders,
    default_model: modelId,
    openai_base_url: nextActiveProvider?.api_base_url || draft.openai_base_url,
    openai_api_key: nextActiveProvider?.id === draft.active_provider_id ? draft.openai_api_key : '',
  };
}

function toMessageAttachments(attachments: ComposerAttachment[]) {
  return attachments.map(({ data_url, ...attachment }) => ({
    ...attachment,
    url: data_url,
  }));
}

function createAttachmentId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createTextBlock(text: string): MessageBlock {
  return {
    kind: 'text',
    text,
  };
}

function createReasoningBlock(text = '', status: 'streaming' | 'completed' = 'streaming'): MessageBlock {
  return {
    kind: 'reasoning',
    text,
    status,
  };
}

function createThinkingBlock(): MessageBlock {
  return {
    kind: 'thinking',
  };
}

function startThinkingBlock(blocks: MessageBlock[]): MessageBlock[] {
  if (blocks.some((block) => block.kind === 'thinking')) {
    return blocks;
  }

  return [...blocks, createThinkingBlock()];
}

function removeThinkingBlocks(blocks: MessageBlock[]): MessageBlock[] {
  return blocks.filter((block) => block.kind !== 'thinking');
}

function appendDeltaToBlocks(blocks: MessageBlock[], delta: string): MessageBlock[] {
  if (!delta) {
    return blocks;
  }

  const nextBlocks = removeThinkingBlocks(blocks);
  const lastBlock = nextBlocks[nextBlocks.length - 1];

  if (lastBlock?.kind === 'text') {
    nextBlocks[nextBlocks.length - 1] = {
      kind: 'text',
      text: `${lastBlock.text}${delta}`,
    };
    return nextBlocks;
  }

  nextBlocks.push(createTextBlock(delta));
  return nextBlocks;
}

function findStreamingReasoningBlockIndex(blocks: MessageBlock[]): number {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.kind === 'reasoning' && block.status === 'streaming') {
      return index;
    }
  }

  return -1;
}

function startReasoningBlock(blocks: MessageBlock[]): MessageBlock[] {
  const activeReasoningIndex = findStreamingReasoningBlockIndex(blocks);
  if (activeReasoningIndex >= 0) {
    return blocks;
  }

  const activeThinkingIndex = blocks.findIndex((block) => block.kind === 'thinking');
  if (activeThinkingIndex >= 0) {
    return blocks.map((block, index) => (
      index === activeThinkingIndex ? createReasoningBlock() : block
    ));
  }

  return [...removeThinkingBlocks(blocks), createReasoningBlock()];
}

function appendReasoningDeltaToBlocks(blocks: MessageBlock[], delta: string): MessageBlock[] {
  if (!delta) {
    return blocks;
  }

  const nextBlocks = startReasoningBlock(blocks);
  const activeReasoningIndex = findStreamingReasoningBlockIndex(nextBlocks);

  if (activeReasoningIndex < 0) {
    return nextBlocks;
  }

  const block = nextBlocks[activeReasoningIndex];
  if (block.kind !== 'reasoning') {
    return nextBlocks;
  }

  return nextBlocks.map((item, index) => (
    index === activeReasoningIndex
      ? {
          ...block,
          text: `${block.text}${delta}`,
          status: 'streaming',
        }
      : item
  ));
}

function completeReasoningBlocks(blocks: MessageBlock[]): MessageBlock[] {
  return removeThinkingBlocks(blocks)
    .map((block) => (
      block.kind === 'reasoning' && block.status === 'streaming'
        ? {
            ...block,
            status: 'completed',
          }
        : block
    ))
    .filter((block) => !(block.kind === 'reasoning' && !block.text.trim() && block.status !== 'streaming'));
}

function appendToolToBlocks(blocks: MessageBlock[], toolEvent: MessageRecord['toolEvents'][number]): MessageBlock[] {
  return [
    ...completeReasoningBlocks(blocks),
    {
      kind: 'tool',
      tool_event: toolEvent,
    },
  ];
}

function getTextFromBlocks(blocks: MessageBlock[]): string {
  return blocks
    .filter((block): block is Extract<MessageBlock, { kind: 'text' }> => block.kind === 'text')
    .map((block) => block.text)
    .join('');
}

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === 'AbortError';
}

function getThrownMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

const AUTO_SCROLL_BOTTOM_THRESHOLD_PX = 96;

function isNearMessageListBottom(messageList: HTMLDivElement) {
  return messageList.scrollHeight - messageList.scrollTop - messageList.clientHeight <= AUTO_SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollMessageListToBottom(messageList: HTMLDivElement) {
  messageList.scrollTop = messageList.scrollHeight;
}

function resolveThemeAccent(themeColor: string) {
  if (themeColor === PAPER_INK_WHITE_THEME) {
    return '#ffffff';
  }

  return themeColor;
}

function parseHexColor(value: string): RgbColor | null {
  const match = /^#?([0-9a-f]{6})$/i.exec(value.trim());

  if (!match) {
    return null;
  }

  const hex = match[1];

  return {
    r: Number.parseInt(hex.slice(0, 2), 16),
    g: Number.parseInt(hex.slice(2, 4), 16),
    b: Number.parseInt(hex.slice(4, 6), 16),
  };
}

function normalizeHexColor(value: string, fallback: string) {
  const parsed = parseHexColor(value);

  if (!parsed) {
    return fallback;
  }

  return rgbToHex(parsed);
}

function rgbToHex(color: RgbColor) {
  const channelToHex = (channel: number) => Math.round(Math.min(255, Math.max(0, channel)))
    .toString(16)
    .padStart(2, '0');

  return `#${channelToHex(color.r)}${channelToHex(color.g)}${channelToHex(color.b)}`;
}

function rgbToCss(color: RgbColor) {
  return `${Math.round(color.r)}, ${Math.round(color.g)}, ${Math.round(color.b)}`;
}

function mixColors(from: RgbColor, to: RgbColor, amount: number): RgbColor {
  const ratio = Math.min(1, Math.max(0, amount));

  return {
    r: from.r + (to.r - from.r) * ratio,
    g: from.g + (to.g - from.g) * ratio,
    b: from.b + (to.b - from.b) * ratio,
  };
}

function getRelativeLuminance(color: RgbColor) {
  const toLinear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };

  return 0.2126 * toLinear(color.r) + 0.7152 * toLinear(color.g) + 0.0722 * toLinear(color.b);
}

function resolveThemeAccentRgb(themeColor: string) {
  const accent = resolveThemeAccent(themeColor);
  const parsedAccent = parseHexColor(accent);

  return parsedAccent ? rgbToCss(parsedAccent) : '203, 166, 247';
}

const DEFAULT_DARK_BACKGROUND_COLOR = '#111111';
const DEFAULT_LIGHT_BACKGROUND_COLOR = '#f3f1ea';
const DEFAULT_BACKGROUND_COLOR = DEFAULT_DARK_BACKGROUND_COLOR;
const DEFAULT_UI_FONT = 'Noto Serif SC';
const DEFAULT_CODE_FONT = 'JetBrains Mono';
const DEFAULT_UI_FONT_SIZE = 16;
const DEFAULT_CODE_FONT_SIZE = 14;
const DEFAULT_APPEARANCE_CONTRAST = 45;

const DEFAULT_DARK_BACKGROUNDS = new Set(['#000000', '#080808', '#111111', '#161616']);
const DEFAULT_LIGHT_BACKGROUNDS = new Set(['#ffffff', '#f7f5ee', '#f3f1ea', '#f1f0ea', '#ebe9e1']);

function getSystemAppearanceMode(): ResolvedAppearanceMode {
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches) {
    return 'light';
  }

  return 'dark';
}

function shouldSwapThemeDefaultBackground(currentBackground: string, nextMode: ResolvedAppearanceMode) {
  const normalizedBackground = normalizeHexColor(currentBackground, DEFAULT_BACKGROUND_COLOR);

  if (nextMode === 'light') {
    return DEFAULT_DARK_BACKGROUNDS.has(normalizedBackground);
  }

  return DEFAULT_LIGHT_BACKGROUNDS.has(normalizedBackground);
}

function deriveAppearanceTokens(backgroundColor: string, resolvedMode: ResolvedAppearanceMode) {
  const normalizedBackground = normalizeHexColor(backgroundColor, DEFAULT_BACKGROUND_COLOR);
  const background = parseHexColor(normalizedBackground) || parseHexColor(DEFAULT_BACKGROUND_COLOR)!;
  const white = { r: 255, g: 255, b: 255 };
  const black = { r: 0, g: 0, b: 0 };
  const luminance = getRelativeLuminance(background);
  const surfaceIsLight = luminance >= 0.52;
  const textBase = surfaceIsLight ? { r: 24, g: 24, b: 24 } : { r: 248, g: 248, b: 248 };
  const mutedBase = mixColors(textBase, background, surfaceIsLight ? 0.42 : 0.34);
  const faintBase = mixColors(textBase, background, surfaceIsLight ? 0.62 : 0.55);
  const panel = surfaceIsLight
    ? mixColors(background, white, resolvedMode === 'light' ? 0.5 : 0.28)
    : mixColors(background, white, 0.06);
  const panelStrong = surfaceIsLight ? mixColors(background, white, 0.72) : mixColors(background, white, 0.095);
  const sidebar = surfaceIsLight
    ? mixColors(background, black, resolvedMode === 'light' ? 0.065 : 0.12)
    : mixColors(background, black, 0.18);
  const input = surfaceIsLight ? mixColors(background, white, 0.72) : mixColors(background, white, 0.08);
  const inputHover = surfaceIsLight ? mixColors(background, white, 0.86) : mixColors(background, white, 0.13);
  const capsule = surfaceIsLight ? mixColors(background, black, 0.08) : mixColors(background, white, 0.16);
  const borderBase = surfaceIsLight ? mixColors(background, black, 0.16) : mixColors(background, white, 0.12);
  const borderSubtle = surfaceIsLight ? mixColors(background, black, 0.1) : mixColors(background, white, 0.075);
  const textCss = rgbToCss(textBase);
  const shadowAlpha = surfaceIsLight ? '0.16' : '0.34';

  return {
    '--app-bg-color': normalizedBackground,
    '--bg-main': normalizedBackground,
    '--bg-sidebar': rgbToHex(sidebar),
    '--bg-input': rgbToHex(input),
    '--bg-input-hover': rgbToHex(inputHover),
    '--bg-capsule': rgbToHex(capsule),
    '--surface-bg': rgbToHex(panel),
    '--surface-bg-strong': rgbToHex(panelStrong),
    '--surface-bg-elevated': rgbToHex(panelStrong),
    '--text-primary': rgbToHex(textBase),
    '--text-secondary': rgbToHex(mutedBase),
    '--text-muted': rgbToHex(faintBase),
    '--text-primary-rgb': textCss,
    '--text-secondary-rgb': rgbToCss(mutedBase),
    '--border-color': rgbToHex(borderBase),
    '--border-subtle': rgbToHex(borderSubtle),
    '--item-hover': `rgba(${textCss}, ${surfaceIsLight ? 0.07 : 0.08})`,
    '--item-active': `rgba(${textCss}, ${surfaceIsLight ? 0.11 : 0.12})`,
    '--scrollbar-thumb': `rgba(${textCss}, ${surfaceIsLight ? 0.16 : 0.18})`,
    '--scrollbar-thumb-hover': `rgba(${textCss}, ${surfaceIsLight ? 0.24 : 0.28})`,
    '--shadow-color': `rgba(0, 0, 0, ${shadowAlpha})`,
    '--theme-surface-is-light': surfaceIsLight ? '1' : '0',
  };
}

const UI_FONT_CANDIDATES = [
  'Noto Serif SC',
  'Noto Sans SC',
  'Microsoft YaHei',
  'Microsoft JhengHei',
  'SimSun',
  'SimHei',
  'KaiTi',
  'FangSong',
  'Source Han Sans SC',
  'Source Han Serif SC',
  'PingFang SC',
  'Heiti SC',
  'Songti SC',
  'Inter',
  'Arial',
  'Segoe UI',
];

const CODE_FONT_CANDIDATES = [
  'JetBrains Mono',
  'Fira Code',
  'Cascadia Code',
  'Consolas',
  'Source Code Pro',
  'SFMono-Regular',
  'Menlo',
  'Monaco',
  'Courier New',
];

function quoteFontName(fontName: string) {
  if (/^(serif|sans-serif|monospace|system-ui|inherit)$/i.test(fontName)) {
    return fontName;
  }

  return `"${fontName.replace(/"/g, '\\"')}"`;
}

function buildUiFontStack(fontName: string) {
  return `${quoteFontName(fontName)}, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`;
}

function buildCodeFontStack(fontName: string) {
  return `${quoteFontName(fontName)}, ui-monospace, SFMono-Regular, Consolas, monospace`;
}

function detectInstalledFonts(candidates: string[]) {
  if (typeof document === 'undefined') {
    return candidates;
  }

  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d');

  if (!context) {
    return candidates;
  }

  const sample = 'mmmmmmmmmmlli中文字体测试0123456789';
  const bases = ['serif', 'sans-serif', 'monospace'];
  const baseWidths = new Map<string, number>();

  bases.forEach((base) => {
    context.font = `72px ${base}`;
    baseWidths.set(base, context.measureText(sample).width);
  });

  return candidates.filter((fontName) =>
    bases.some((base) => {
      context.font = `72px ${quoteFontName(fontName)}, ${base}`;
      return Math.abs(context.measureText(sample).width - (baseWidths.get(base) || 0)) > 0.1;
    }),
  );
}

function mergeFontOptions(defaultFont: string, detectedFonts: string[], fallbackFonts: string[]) {
  const fonts = detectedFonts.length ? detectedFonts : fallbackFonts;
  return Array.from(new Set([defaultFont, ...fonts]));
}

async function fileToComposerAttachment(file: File): Promise<ComposerAttachment> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`读取附件失败：${file.name}`));
    reader.onload = () => resolve(String(reader.result || ''));
    reader.readAsDataURL(file);
  });

  return {
    id: createAttachmentId(),
    name: file.name,
    mime_type: file.type || 'application/octet-stream',
    kind: file.type.startsWith('image/') ? 'image' : 'file',
    size_bytes: file.size,
    data_url: dataUrl,
    url: dataUrl,
  };
}

export default function App() {
  const [view, setView] = useState<ViewName>('chat');
  const [sidebarMode, setSidebarMode] = useState<SidebarMode>('projects');
  const [projectName, setProjectName] = useState('hashcode');
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [chatSessions, setChatSessions] = useState<SessionSummary[]>([]);
  const [projectExpansions, setProjectExpansions] = useState<Record<string, boolean>>({});
  const [currentProjectId, setCurrentProjectId] = useState('');
  const [currentProjectSessionId, setCurrentProjectSessionId] = useState('');
  const [currentChatSessionId, setCurrentChatSessionId] = useState('');
  const [currentSessionId, setCurrentSessionId] = useState('');
  const [conversations, setConversations] = useState<Record<string, MessageRecord[]>>({});
  const [contextWorkbenchHistories, setContextWorkbenchHistories] = useState<
    Record<string, ContextWorkbenchHistoryEntry[]>
  >({});
  const [contextRevisionHistories, setContextRevisionHistories] = useState<Record<string, ContextRevisionSummary[]>>({});
  const [pendingContextRestores, setPendingContextRestores] = useState<Record<string, PendingContextRestore>>({});
  const [models, setModels] = useState<string[]>(['gpt-5.4-mini']);
  const [currentModel, setCurrentModel] = useState('gpt-5.4-mini');
  const [reasoningOptions, setReasoningOptions] = useState<ReasoningOption[]>(normalizeReasoningOptions(undefined));
  const [currentReasoning, setCurrentReasoning] = useState('default');
  const [composerValue, setComposerValue] = useState('');
  const [composerAttachments, setComposerAttachments] = useState<ComposerAttachment[]>([]);
  const [runningSessionIds, setRunningSessionIds] = useState<Record<string, boolean>>({});
  const [isModelPickerOpen, setIsModelPickerOpen] = useState(false);
  const [openDropdown, setOpenDropdown] = useState<DropdownId>(null);
  const [currentPermission, setCurrentPermission] = useState<PermissionOption>(PERMISSION_OPTIONS[1]);
  const [serviceHintsEnabled, setServiceHintsEnabled] = useState(true);
  const [themeColor, setThemeColor] = useState(THEME_OPTIONS[0].value);
  const [themeMode, setThemeMode] = useState<AppearanceMode>('dark');
  const [systemAppearanceMode, setSystemAppearanceMode] = useState<ResolvedAppearanceMode>(getSystemAppearanceMode);
  const [backgroundColor, setBackgroundColor] = useState(DEFAULT_BACKGROUND_COLOR);
  const [uiFont, setUiFont] = useState(DEFAULT_UI_FONT);
  const [codeFont, setCodeFont] = useState(DEFAULT_CODE_FONT);
  const [uiFontSize, setUiFontSize] = useState(DEFAULT_UI_FONT_SIZE);
  const [codeFontSize, setCodeFontSize] = useState(DEFAULT_CODE_FONT_SIZE);
  const [appearanceContrast, setAppearanceContrast] = useState(DEFAULT_APPEARANCE_CONTRAST);
  const [availableUiFonts, setAvailableUiFonts] = useState(UI_FONT_CANDIDATES);
  const [availableCodeFonts, setAvailableCodeFonts] = useState(CODE_FONT_CANDIDATES);
  const [userSidebarCollapsed, setUserSidebarCollapsed] = useState(false);
  const [contextMap, setContextMap] = useState<ContextMapState>({
    stage: 0,
    mode: 'condensed',
    width: 320,
  });
  const [isResizingLeft, setIsResizingLeft] = useState(false);
  const [isResizingRight, setIsResizingRight] = useState(false);
  const [openAISettings, setOpenAISettings] = useState<OpenAISettings>(DEFAULT_SETTINGS);
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft>(toSettingsDraft(DEFAULT_SETTINGS));
  const [isSavingSettings, setIsSavingSettings] = useState(false);
  const [savingProviderId, setSavingProviderId] = useState('');
  const [fetchingProviderId, setFetchingProviderId] = useState('');

  const isSidebarCollapsed = userSidebarCollapsed || contextMap.stage === 2;
  const resolvedThemeMode = themeMode === 'system' ? systemAppearanceMode : themeMode;
  const uiLocale = normalizeSupportedLocale(settingsDraft.user_locale);

  const [toastState, setToastState] = useState({
    message: INITIAL_TOAST_MESSAGE,
    visible: false,
    seq: 0,
  });

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const toastTimerRef = useRef<number | null>(null);
  const settingsDraftRef = useRef<SettingsDraft>(settingsDraft);
  const settingsInitializedRef = useRef(false);
  const settingsAutosaveTimerRef = useRef<number | null>(null);
  const settingsAutosaveSnapshotRef = useRef('');
  const currentModelRef = useRef(currentModel);
  const modelSelectionRequestIdRef = useRef(0);
  const currentReasoningRef = useRef(currentReasoning);
  const reasoningSaveRequestIdRef = useRef(0);
  const currentSessionIdRef = useRef(currentSessionId);
  const currentProjectIdRef = useRef(currentProjectId);
  const streamAbortControllersRef = useRef<Record<string, AbortController>>({});
  const stopRequestedSessionIdsRef = useRef<Set<string>>(new Set());
  const stopRequestPromisesRef = useRef<Record<string, Promise<unknown>>>({});
  const shouldStickToMessageBottomRef = useRef(true);
  const previousConversationLengthRef = useRef(0);
  const previousAutoScrollSessionIdRef = useRef('');

  const currentProject = getProjectById(projects, currentProjectId);
  const currentSessionMeta = getSessionMeta(projects, chatSessions, currentSessionId);
  const currentConversation = getConversation(conversations, currentSessionId);
  const currentReasoningLabel = getReasoningLabel(currentReasoning, reasoningOptions);
  const hasMessages = currentConversation.length > 0;
  const isSending = Boolean(currentSessionId && runningSessionIds[currentSessionId]);

  const headerTitle = sidebarMode === 'projects'
    ? currentSessionMeta?.session?.title
      ? `${currentProject?.title || '我的项目'} · ${currentSessionMeta.session.title}`
      : currentProject?.title || '我的项目'
    : currentSessionMeta?.session?.title || '我的对话';

  const welcomeText = sidebarMode === 'projects'
    ? `What Should We Work In ${projectName || 'hashcode'}?`
    : 'Hello！HaShiShark';

  const welcomeAnimationKey = [
    view,
    sidebarMode,
    currentProjectId || 'no-project',
    currentSessionId || 'no-session',
    hasMessages ? 'messages' : 'welcome',
    welcomeText,
  ].join(':');

  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  useEffect(() => {
    currentModelRef.current = currentModel;
  }, [currentModel]);

  useEffect(() => {
    currentReasoningRef.current = currentReasoning;
  }, [currentReasoning]);

  useEffect(() => localizeAppDom(document.body, uiLocale), [uiLocale]);

  useEffect(() => {
    currentProjectIdRef.current = currentProjectId;
  }, [currentProjectId]);

  const startResizingLeft = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsResizingLeft(true);
  }, []);

  const startResizingRight = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsResizingRight(true);
  }, []);

  const stopResizing = useCallback(() => {
    setIsResizingLeft(false);
    setIsResizingRight(false);
  }, []);

  const resize = useCallback((event: MouseEvent) => {
    if (isResizingLeft) {
      const nextWidth = Math.max(200, Math.min(600, event.clientX));
      document.documentElement.style.setProperty('--sidebar-width', `${nextWidth}px`);
    } else if (isResizingRight) {
      const nextWidth = Math.max(240, Math.min(600, window.innerWidth - event.clientX));
      setContextMap((previous) => ({ ...previous, width: nextWidth }));
      document.documentElement.style.setProperty('--context-map-width', `${nextWidth}px`);
    }
  }, [isResizingLeft, isResizingRight]);

  useEffect(() => {
    if (isResizingLeft || isResizingRight) {
      window.addEventListener('mousemove', resize);
      window.addEventListener('mouseup', stopResizing);
    } else {
      window.removeEventListener('mousemove', resize);
      window.removeEventListener('mouseup', stopResizing);
    }

    return () => {
      window.removeEventListener('mousemove', resize);
      window.removeEventListener('mouseup', stopResizing);
    };
  }, [isResizingLeft, isResizingRight, resize, stopResizing]);

  function showToast(message: string) {
    setToastState({
      message,
      visible: true,
      seq: Date.now(),
    });
  }

  function focusComposer() {
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  }

  function applySidebarPayload(
    payload: {
      projects?: ProjectSummary[];
      chat_sessions?: SessionSummary[];
    },
    preferredCurrentProjectId = currentProjectId,
  ) {
    const next = deriveSidebarState(
      payload,
      projects,
      chatSessions,
      projectExpansions,
      preferredCurrentProjectId,
    );

    startTransition(() => {
      setProjects(next.projects);
      setChatSessions(next.chatSessions);
      setProjectExpansions(next.projectExpansions);
      setCurrentProjectId(next.currentProjectId);
    });

    return next;
  }

  function firstProjectSessionId(projectId: string, projectList: ProjectSummary[]) {
    return projectList.find((project) => project.id === projectId)?.sessions[0]?.id || '';
  }

  function firstChatSessionId(sessionList: SessionSummary[]) {
    return sessionList[0]?.id || '';
  }

  function updatePendingAssistantMessage(
    sessionId: string,
    updater: (message: MessageRecord) => MessageRecord,
  ) {
    setConversations((previous) => {
      const history = [...(previous[sessionId] || [])];
      if (!history.length) {
        return previous;
      }

      const lastMessage = history[history.length - 1];
      history[history.length - 1] = updater(lastMessage);

      return {
        ...previous,
        [sessionId]: history,
      };
    });
  }

  function finalizePendingAssistantMessage(
    sessionId: string,
    sourceText: string,
    fallbackText = '已停止本次回复。',
  ) {
    updatePendingAssistantMessage(sessionId, (lastMessage) => {
      const settledBlocks = completeReasoningBlocks(lastMessage.blocks);
      const nextBlocks = settledBlocks.length
        ? settledBlocks
        : (lastMessage.text.trim() ? [createTextBlock(lastMessage.text)] : [createTextBlock(fallbackText)]);
      const nextText = getTextFromBlocks(nextBlocks) || lastMessage.text.trim() || fallbackText;

      return {
        ...lastMessage,
        role: 'an',
        text: nextText,
        blocks: nextBlocks,
        pending: false,
        sourceText,
      };
    });
  }

  function markSessionRunning(sessionId: string, controller: AbortController) {
    streamAbortControllersRef.current[sessionId] = controller;
    setRunningSessionIds((previous) => ({
      ...previous,
      [sessionId]: true,
    }));
  }

  function clearSessionRunning(sessionId: string, controller?: AbortController) {
    if (!sessionId) {
      return;
    }

    if (!controller || streamAbortControllersRef.current[sessionId] === controller) {
      delete streamAbortControllersRef.current[sessionId];
    }
    stopRequestedSessionIdsRef.current.delete(sessionId);
    delete stopRequestPromisesRef.current[sessionId];
    setRunningSessionIds((previous) => {
      if (!previous[sessionId]) {
        return previous;
      }

      const next = { ...previous };
      delete next[sessionId];
      return next;
    });
  }

  function abortSessionRequest(sessionId: string) {
    const controller = streamAbortControllersRef.current[sessionId];
    if (!controller) {
      return false;
    }

    stopRequestedSessionIdsRef.current.add(sessionId);
    stopRequestPromisesRef.current[sessionId] = cancelActiveRequest({
      session_id: sessionId,
      mode: 'main',
    }).catch(() => undefined);
    controller.abort();
    return true;
  }

  function abortSessionRequests(sessionIds: Iterable<string>) {
    let aborted = false;
    Array.from(sessionIds).forEach((sessionId) => {
      aborted = abortSessionRequest(sessionId) || aborted;
    });
    return aborted;
  }

  function isViewingSession(sessionId: string) {
    return currentSessionIdRef.current === sessionId;
  }

  function currentUiPreferences(): PersistedUiPreferences {
    return {
      theme_color: normalizeThemeColorSetting(themeColor, DEFAULT_SETTINGS.theme_color),
      theme_mode: themeMode,
      background_color: normalizeHexColor(backgroundColor, DEFAULT_BACKGROUND_COLOR),
      ui_font: uiFont || DEFAULT_UI_FONT,
      code_font: codeFont || DEFAULT_CODE_FONT,
      ui_font_size: normalizeBoundedNumber(uiFontSize, 12, 22, DEFAULT_UI_FONT_SIZE),
      code_font_size: normalizeBoundedNumber(codeFontSize, 11, 20, DEFAULT_CODE_FONT_SIZE),
      appearance_contrast: normalizeBoundedNumber(
        appearanceContrast,
        30,
        80,
        DEFAULT_APPEARANCE_CONTRAST,
      ),
      service_hints_enabled: serviceHintsEnabled,
    };
  }

  function syncSettingsState(
    nextSettings: OpenAISettings,
    nextModels?: string[],
    options: {
      preserveCurrentModel?: boolean;
      preserveCurrentReasoning?: boolean;
    } = {},
  ) {
    const preserveCurrentModel = options.preserveCurrentModel ?? false;
    const preserveCurrentReasoning = options.preserveCurrentReasoning ?? false;
    const normalizedSettings = normalizeSettingsPayload(nextSettings);
    const availableModels = Array.isArray(nextModels) && nextModels.length
      ? nextModels
      : model_options_fallback(normalizedSettings.default_model);
    let nextDraft = toSettingsDraft(normalizedSettings);
    if (preserveCurrentModel && currentModelRef.current.trim()) {
      const currentModelId = currentModelRef.current.trim();
      const previousActiveProviderId = settingsDraftRef.current.active_provider_id;
      const providerForCurrentModel =
        nextDraft.response_providers.find(
          (provider) =>
            provider.id === previousActiveProviderId &&
            provider.models.some((model) => (model.id || model.label || '').trim() === currentModelId),
        ) ||
        nextDraft.response_providers.find((provider) =>
          provider.models.some((model) => (model.id || model.label || '').trim() === currentModelId),
        ) ||
        nextDraft.response_providers.find((provider) => provider.id === previousActiveProviderId);

      nextDraft = providerForCurrentModel
        ? applyProviderModelSelection(nextDraft, providerForCurrentModel.id, currentModelId)
        : {
            ...nextDraft,
            default_model: currentModelId,
          };
    }
    const nextPreferences = uiPreferencesFromSettings(normalizedSettings);

    setOpenAISettings(normalizedSettings);
    settingsDraftRef.current = nextDraft;
    settingsAutosaveSnapshotRef.current = settingsPayloadSnapshot(
      buildSettingsSavePayload(nextDraft, {
        defaultReasoningEffort: normalizedSettings.default_reasoning_effort,
        uiPreferences: nextPreferences,
      }),
    );
    settingsInitializedRef.current = true;
    setSettingsDraft(nextDraft);
    setServiceHintsEnabled(nextPreferences.service_hints_enabled);
    setThemeColor(nextPreferences.theme_color);
    setThemeMode(nextPreferences.theme_mode);
    setBackgroundColor(nextPreferences.background_color);
    setUiFont(nextPreferences.ui_font);
    setCodeFont(nextPreferences.code_font);
    setUiFontSize(nextPreferences.ui_font_size);
    setCodeFontSize(nextPreferences.code_font_size);
    setAppearanceContrast(nextPreferences.appearance_contrast);
    setModels((previous) => {
      const merged = [currentModelRef.current, ...availableModels, ...previous].filter(Boolean);
      return Array.from(new Set(merged));
    });
    const nextCurrentModel = preserveCurrentModel && currentModelRef.current
      ? currentModelRef.current
      : normalizedSettings.default_model || availableModels[0] || 'gpt-5.4-mini';
    currentModelRef.current = nextCurrentModel;
    setCurrentModel(nextCurrentModel);
    setCurrentReasoning((previous) => {
      const nextReasoning = preserveCurrentReasoning
        ? currentReasoningRef.current || previous
        : resolveReasoningEffort(normalizedSettings.default_reasoning_effort || previous, reasoningOptions);
      currentReasoningRef.current = nextReasoning;
      return nextReasoning;
    });
  }

  function updateSettingsDraftState(updater: (previous: SettingsDraft) => SettingsDraft) {
    const next = updater(settingsDraftRef.current);
    settingsDraftRef.current = next;
    setSettingsDraft(next);
  }

  function updateProviderDraftState(providerId: string, patch: Partial<ResponseProviderDraft>) {
    updateSettingsDraftState((previous) => {
      const nextProviders = previous.response_providers.map((provider) => {
        if (provider.id !== providerId) {
          return provider;
        }

        return {
          ...provider,
          ...patch,
          clear_api_key: patch.clear_api_key ?? (patch.api_key_input !== undefined ? false : provider.clear_api_key),
        };
      });

      const activeProvider = nextProviders.find((provider) => provider.id === previous.active_provider_id);
      return {
        ...previous,
        response_providers: nextProviders,
        default_model: activeProvider?.default_model || previous.default_model,
        openai_base_url: activeProvider?.api_base_url || previous.openai_base_url,
        openai_api_key:
          providerId === previous.active_provider_id && patch.api_key_input !== undefined
            ? patch.api_key_input
            : previous.openai_api_key,
      };
    });
  }

  function addProviderDraftState(providerType: ProviderType, providerName?: string) {
    const provider = createProviderDraft(providerType, settingsDraftRef.current.response_providers, providerName);
    updateSettingsDraftState((previous) => ({
      ...previous,
      response_providers: [...previous.response_providers, provider],
    }));
    return provider.id;
  }

  async function deleteProviderDraftState(providerId: string): Promise<string> {
    const previousDraft = settingsDraftRef.current;
    const nextDraft = removeProviderFromDraft(previousDraft, providerId);
    const nextSelectedProviderId = nextDraft.active_provider_id || nextDraft.response_providers[0]?.id || 'openai';

    settingsDraftRef.current = nextDraft;
    setSettingsDraft(nextDraft);
    setSavingProviderId(providerId);
    try {
      const response = await saveSettingsRequest({
        ...buildSettingsSavePayload(nextDraft, {
          defaultReasoningEffort: currentReasoningRef.current,
          uiPreferences: currentUiPreferences(),
        }),
        deleted_provider_ids: [providerId],
      });
      syncSettingsState(response.settings, response.models);
      showToast(SETTINGS_SAVED_TOAST);
      return nextSelectedProviderId;
    } catch (error) {
      settingsDraftRef.current = previousDraft;
      setSettingsDraft(previousDraft);
      showToast(getThrownMessage(error));
      return previousDraft.active_provider_id || previousDraft.response_providers[0]?.id || 'openai';
    } finally {
      setSavingProviderId('');
    }
  }

  function applySettingsDraftPatch(patch: Partial<SettingsDraft>) {
    updateSettingsDraftState((previous) => {
      const next = {
        ...previous,
        ...patch,
        user_locale: patch.user_locale !== undefined
          ? normalizeSupportedLocale(patch.user_locale)
          : previous.user_locale,
      };

      const activeProviderIndex = next.response_providers.findIndex((provider) => provider.id === next.active_provider_id);
      if (activeProviderIndex === -1) {
        return next;
      }

      const activeProvider = next.response_providers[activeProviderIndex];
      const nextActiveProvider: ResponseProviderDraft = {
        ...activeProvider,
        default_model: patch.default_model !== undefined ? patch.default_model : activeProvider.default_model,
        api_base_url: patch.openai_base_url !== undefined ? patch.openai_base_url : activeProvider.api_base_url,
        api_key_input: patch.openai_api_key !== undefined ? patch.openai_api_key : activeProvider.api_key_input,
        clear_api_key: patch.openai_api_key !== undefined ? false : activeProvider.clear_api_key,
      };

      const nextProviders = [...next.response_providers];
      nextProviders[activeProviderIndex] = nextActiveProvider;

      return {
        ...next,
        response_providers: nextProviders,
      };
    });
  }

  async function persistProviderSettings(
    providerId: string,
    options: {
      activate?: boolean;
      clearApiKey?: boolean;
      silent?: boolean;
    } = {},
  ) {
    setSavingProviderId(providerId);
    try {
      const response = await saveSettingsRequest(
        buildSettingsSavePayload(settingsDraftRef.current, {
          activeProviderId: options.activate ? providerId : undefined,
          clearProviderApiKeyId: options.clearApiKey ? providerId : undefined,
          defaultReasoningEffort: currentReasoningRef.current,
          uiPreferences: currentUiPreferences(),
        }),
      );
      syncSettingsState(response.settings, response.models);
      if (!options.silent) {
        showToast(SETTINGS_SAVED_TOAST);
      }
    } catch (error) {
      showToast(getThrownMessage(error));
    } finally {
      setSavingProviderId('');
    }
  }

  async function loadProviderModelCandidates(providerId: string): Promise<ResponseProviderModel[]> {
    const provider = settingsDraftRef.current.response_providers.find((item) => item.id === providerId);
    if (!provider) {
      return [];
    }

    setFetchingProviderId(providerId);
    try {
      const response = await fetchProviderModelCandidatesRequest({
        provider_id: providerId,
        api_base_url: provider.api_base_url.trim(),
        provider_type: provider.provider_type,
        ...(provider.api_key_input.trim()
          ? { api_key: provider.api_key_input.trim() }
          : {}),
      });
      return response.models;
    } catch (error) {
      throw error;
    } finally {
      setFetchingProviderId('');
    }
  }

  useEffect(() => {
    async function initializeApp() {
      try {
        const [initData, settingsData] = await Promise.all([fetchInit(), fetchSettings()]);
        const nextReasoningOptions = normalizeReasoningOptions(initData.reasoning_options);
        const nextModels = Array.from(
          new Set(
            [
              ...(Array.isArray(settingsData.models) ? settingsData.models : []),
              ...(Array.isArray(initData.models) ? initData.models : []),
            ].filter(Boolean),
          ),
        );
        const nextModel = initData.default_model || settingsData.settings.default_model || nextModels[0] || 'gpt-5.4-mini';
        const preferredReasoning = nextReasoningOptions.find((option) => option.value === 'default')
          ? 'default'
          : nextReasoningOptions[0]?.value || 'default';
        const nextConversations = buildConversationMap(initData.conversations);
        const nextWorkbenchHistories = buildContextWorkbenchHistoryMap(initData.context_workbench_histories);
        const nextRevisionHistories = buildContextRevisionHistoryMap(initData.context_revision_histories);
        const nextPendingRestores = buildPendingContextRestoreMap(initData.pending_context_restores);
        const nextSidebarState = deriveSidebarState(initData, initData.projects || [], initData.chat_sessions || [], {}, '');
        const nextSettings = normalizeSettingsPayload(
          (settingsData.settings || initData.settings || DEFAULT_SETTINGS) as Partial<OpenAISettings>,
        );
        const nextSettingsDraft = toSettingsDraft(nextSettings);
        const nextPreferences = uiPreferencesFromSettings(nextSettings);
        const nextCurrentReasoning = resolveReasoningEffort(
          nextSettings.default_reasoning_effort || preferredReasoning,
          nextReasoningOptions,
        );
        settingsAutosaveSnapshotRef.current = settingsPayloadSnapshot(
          buildSettingsSavePayload(nextSettingsDraft, {
            defaultReasoningEffort: nextSettings.default_reasoning_effort,
            uiPreferences: nextPreferences,
          }),
        );
        currentModelRef.current = nextModel;
        currentReasoningRef.current = nextCurrentReasoning;

        startTransition(() => {
          setProjectName(initData.project_name || 'hashcode');
          setModels(nextModels.length ? nextModels : [nextModel]);
          setCurrentModel(nextModel);
          setReasoningOptions(nextReasoningOptions);
          setCurrentReasoning(nextCurrentReasoning);
          setConversations(nextConversations);
          setContextWorkbenchHistories(nextWorkbenchHistories);
          setContextRevisionHistories(nextRevisionHistories);
          setPendingContextRestores(nextPendingRestores);
          setProjects(nextSidebarState.projects);
          setChatSessions(nextSidebarState.chatSessions);
          setProjectExpansions(nextSidebarState.projectExpansions);
          setCurrentProjectId(nextSidebarState.currentProjectId);
          setCurrentSessionId('');
          setCurrentProjectSessionId('');
          setCurrentChatSessionId('');
          setOpenAISettings(nextSettings);
          settingsDraftRef.current = nextSettingsDraft;
          setSettingsDraft(nextSettingsDraft);
          setServiceHintsEnabled(nextPreferences.service_hints_enabled);
          setThemeColor(nextPreferences.theme_color);
          setThemeMode(nextPreferences.theme_mode);
          setBackgroundColor(nextPreferences.background_color);
          setUiFont(nextPreferences.ui_font);
          setCodeFont(nextPreferences.code_font);
          setUiFontSize(nextPreferences.ui_font_size);
          setCodeFontSize(nextPreferences.code_font_size);
          setAppearanceContrast(nextPreferences.appearance_contrast);
          settingsInitializedRef.current = true;
        });
      } catch (error) {
        showToast(getThrownMessage(error));
      }
    }

    void initializeApp();
  }, []);

  useEffect(() => {
    settingsDraftRef.current = settingsDraft;
  }, [settingsDraft]);

  useEffect(() => {
    if (!settingsInitializedRef.current) {
      return;
    }

    const payload = buildSettingsSavePayload(settingsDraft, {
      defaultReasoningEffort: currentReasoningRef.current,
      uiPreferences: currentUiPreferences(),
    });
    const snapshot = settingsPayloadSnapshot(payload);
    if (snapshot === settingsAutosaveSnapshotRef.current) {
      return;
    }

    if (settingsAutosaveTimerRef.current) {
      window.clearTimeout(settingsAutosaveTimerRef.current);
    }

    settingsAutosaveTimerRef.current = window.setTimeout(async () => {
      try {
        const response = await saveSettingsRequest(payload);
        settingsAutosaveSnapshotRef.current = snapshot;
        syncSettingsState(response.settings, response.models);
      } catch (error) {
        showToast(getThrownMessage(error));
      }
    }, 800);

    return () => {
      if (settingsAutosaveTimerRef.current) {
        window.clearTimeout(settingsAutosaveTimerRef.current);
      }
    };
  }, [
    appearanceContrast,
    backgroundColor,
    codeFont,
    codeFontSize,
    serviceHintsEnabled,
    settingsDraft,
    themeColor,
    themeMode,
    uiFont,
    uiFontSize,
  ]);

  useEffect(() => {
    if (toastTimerRef.current) {
      window.clearTimeout(toastTimerRef.current);
    }

    if (!toastState.visible) {
      return;
    }

    toastTimerRef.current = window.setTimeout(() => {
      setToastState((previous) => ({ ...previous, visible: false }));
    }, 3000);

    return () => {
      if (toastTimerRef.current) {
        window.clearTimeout(toastTimerRef.current);
      }
    };
  }, [toastState.seq, toastState.visible]);

  useLayoutEffect(() => {
    document.documentElement.style.setProperty('--an-theme', resolveThemeAccent(themeColor));
    document.documentElement.style.setProperty('--an-theme-rgb', resolveThemeAccentRgb(themeColor));
  }, [themeColor]);

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) {
      return;
    }

    const media = window.matchMedia('(prefers-color-scheme: light)');
    const syncSystemAppearanceMode = () => setSystemAppearanceMode(media.matches ? 'light' : 'dark');

    syncSystemAppearanceMode();
    media.addEventListener?.('change', syncSystemAppearanceMode);

    return () => {
      media.removeEventListener?.('change', syncSystemAppearanceMode);
    };
  }, []);

  useLayoutEffect(() => {
    const root = document.documentElement;
    const appearanceTokens = deriveAppearanceTokens(backgroundColor, resolvedThemeMode);

    root.dataset.themeMode = resolvedThemeMode;
    root.dataset.themePreference = themeMode;
    Object.entries(appearanceTokens).forEach(([property, value]) => {
      root.style.setProperty(property, value);
    });
    root.style.setProperty('--ui-font-size', `${uiFontSize}px`);
    root.style.setProperty('--code-font-size', `${codeFontSize}px`);
    root.style.setProperty('--app-contrast-filter', `contrast(${Math.max(80, Math.min(130, appearanceContrast + 55))}%)`);
  }, [appearanceContrast, backgroundColor, codeFontSize, resolvedThemeMode, themeMode, uiFontSize]);

  useLayoutEffect(() => {
    const root = document.documentElement;

    root.style.setProperty('--ui-font-family', buildUiFontStack(uiFont));
    root.style.setProperty('--code-font-family', buildCodeFontStack(codeFont));
  }, [uiFont, codeFont]);

  useEffect(() => {
    setAvailableUiFonts(mergeFontOptions(DEFAULT_UI_FONT, detectInstalledFonts(UI_FONT_CANDIDATES), UI_FONT_CANDIDATES));
    setAvailableCodeFonts(
      mergeFontOptions(DEFAULT_CODE_FONT, detectInstalledFonts(CODE_FONT_CANDIDATES), CODE_FONT_CANDIDATES),
    );
  }, []);

  useEffect(() => {
    document.body.classList.toggle('sidebar-collapsed', isSidebarCollapsed);

    return () => {
      document.body.classList.remove('sidebar-collapsed');
    };
  }, [isSidebarCollapsed]);

  useEffect(() => {
    function handleDocumentClick() {
      setOpenDropdown(null);
    }

    document.addEventListener('click', handleDocumentClick);
    return () => {
      document.removeEventListener('click', handleDocumentClick);
    };
  }, []);

  useEffect(() => {
    if (view !== 'chat') {
      setIsModelPickerOpen(false);
    }
  }, [view]);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }

    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight}px`;
  }, [composerValue, view]);

  useEffect(() => {
    const messageList = messageListRef.current;
    if (!messageList || view !== 'chat') {
      return;
    }

    const scrollContainer = messageList;

    function syncShouldStickToBottom() {
      shouldStickToMessageBottomRef.current = isNearMessageListBottom(scrollContainer);
    }

    syncShouldStickToBottom();
    scrollContainer.addEventListener('scroll', syncShouldStickToBottom, { passive: true });

    return () => {
      scrollContainer.removeEventListener('scroll', syncShouldStickToBottom);
    };
  }, [currentSessionId, hasMessages, view]);

  useLayoutEffect(() => {
    const messageList = messageListRef.current;
    const sessionChanged = previousAutoScrollSessionIdRef.current !== currentSessionId;
    const previousConversationLength = sessionChanged ? 0 : previousConversationLengthRef.current;
    const messageAdded = currentConversation.length > previousConversationLength;

    previousAutoScrollSessionIdRef.current = currentSessionId;
    previousConversationLengthRef.current = currentConversation.length;

    if (!hasMessages || view !== 'chat' || !messageList) {
      shouldStickToMessageBottomRef.current = true;
      return;
    }

    if (sessionChanged || messageAdded || shouldStickToMessageBottomRef.current) {
      scrollMessageListToBottom(messageList);
      shouldStickToMessageBottomRef.current = true;
    }
  }, [currentConversation, currentSessionId, hasMessages, view]);

  async function createProject(
    silent = false,
    options: {
      title?: string;
      rootPath?: string;
    } = {},
  ) {
    const data = await createProjectRequest({
      title: options.title,
      root_path: options.rootPath,
    });
    applySidebarPayload(data, data.project.id);
    setCurrentProjectId(data.project.id);
    setCurrentSessionId('');
    setCurrentProjectSessionId('');
    setView('chat');
    focusComposer();

    if (!silent) {
      showToast(NEW_PROJECT_TOAST);
    }

    return data.project.id;
  }

  async function handleAddSidebarItem() {
    if (sidebarMode !== 'projects') {
      await createSession(false, { scope: 'chat' });
      return;
    }

    if (!window.electronAPI?.selectFolder) {
      showToast('需要在桌面端选择本地文件夹。');
      return;
    }

    const folder = await window.electronAPI.selectFolder();
    if (!folder || folder.canceled) {
      return;
    }

    if (!folder.path) {
      showToast('没有拿到文件夹路径。');
      return;
    }

    await createProject(false, {
      title: folder.name || folder.path.split(/[\\/]/).filter(Boolean).pop() || '新项目',
      rootPath: folder.path,
    });
  }

  async function createSession(
    silent = false,
    options: {
      scope?: 'chat' | 'project';
      projectId?: string | null;
    } = {},
  ) {
    const scope = options.scope || (sidebarMode === 'projects' ? 'project' : 'chat');
    let projectId = options.projectId || null;

    if (scope === 'project' && !projectId) {
      projectId = currentProjectId || projects[0]?.id || null;
      if (!projectId) {
        projectId = await createProject(true);
      }
    }

    const data = await createSessionRequest({
      scope,
      project_id: projectId,
    });

    applySidebarPayload(data, data.session.project_id || projectId || currentProjectId);
    setConversations((previous) => ({
      ...previous,
      [data.session.id]: previous[data.session.id] || [],
    }));
    setContextWorkbenchHistories((previous) => ({
      ...previous,
      [data.session.id]: previous[data.session.id] || [],
    }));
    setContextRevisionHistories((previous) => ({
      ...previous,
      [data.session.id]: previous[data.session.id] || [],
    }));
    setPendingContextRestores((previous) => {
      const next = { ...previous };
      delete next[data.session.id];
      return next;
    });

    if (data.session.scope === 'project') {
      const nextProjectId = data.session.project_id || projectId || currentProjectId;
      setCurrentProjectId(nextProjectId || '');
      currentProjectIdRef.current = nextProjectId || '';
      setCurrentProjectSessionId(data.session.id);
      if (nextProjectId) {
        setProjectExpansions((previous) => ({
          ...previous,
          [nextProjectId]: true,
        }));
      }
    } else {
      setCurrentChatSessionId(data.session.id);
    }

    setCurrentSessionId(data.session.id);
    currentSessionIdRef.current = data.session.id;
    setView('chat');
    focusComposer();

    if (!silent) {
      showToast(NEW_CHAT_TOAST);
    }

    return data.session.id;
  }

  async function ensureSession() {
    if (currentSessionId) {
      return currentSessionId;
    }

    if (sidebarMode === 'projects') {
      if (!currentProjectId && !projects.length) {
        await createProject(true);
      }

      return createSession(true, {
        scope: 'project',
        projectId: currentProjectId || projects[0]?.id || null,
      });
    }

    return createSession(true, { scope: 'chat' });
  }

  async function handleDeleteSession(sessionId: string) {
    const meta = getSessionMeta(projects, chatSessions, sessionId);
    if (!meta) {
      showToast('没找到这条对话。');
      return;
    }

    try {
      const data = await deleteSessionRequest(sessionId);
      const nextSidebar = applySidebarPayload(data, meta.projectId || currentProjectId);
      setConversations((previous) => {
        const next = { ...previous };
        delete next[sessionId];
        return next;
      });
      setContextWorkbenchHistories((previous) => {
        const next = { ...previous };
        delete next[sessionId];
        return next;
      });
      setContextRevisionHistories((previous) => {
        const next = { ...previous };
        delete next[sessionId];
        return next;
      });
      setPendingContextRestores((previous) => {
        const next = { ...previous };
        delete next[sessionId];
        return next;
      });

      if (meta.scope === 'project') {
        if (currentProjectSessionId === sessionId) {
          const fallbackSessionId = firstProjectSessionId(meta.projectId || currentProjectId, nextSidebar.projects);
          setCurrentProjectSessionId(fallbackSessionId);
          if (currentSessionId === sessionId) {
            setCurrentSessionId(fallbackSessionId);
          }
        }
      } else if (currentChatSessionId === sessionId) {
        const fallbackSessionId = firstChatSessionId(nextSidebar.chatSessions);
        setCurrentChatSessionId(fallbackSessionId);
        if (currentSessionId === sessionId) {
          setCurrentSessionId(fallbackSessionId);
        }
      }

      if (currentSessionId === sessionId) {
        abortSessionRequest(sessionId);
        focusComposer();
      }

      showToast(DELETE_CHAT_TOAST);
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  async function handleDeleteProject(project: ProjectSummary) {
    const confirmed = window.confirm(
      localizeUiText(`移除项目“${project.title}”会删除它下面的项目对话，确定继续吗？`, uiLocale),
    );
    if (!confirmed) {
      return;
    }

    const projectSessionIds = new Set((project.sessions || []).map((session) => session.id));

    setView('chat');
    window.focus();
    focusComposer();

    abortSessionRequests(projectSessionIds);

    try {
      const data = await deleteProjectRequest(project.id);
      const deletedSessionIds = new Set(data.deleted_session_ids);
      const next = applySidebarPayload(data, data.projects?.[0]?.id || '');
      const deletedCurrentSession = deletedSessionIds.has(currentSessionId);
      const deletedProjectSession = deletedSessionIds.has(currentProjectSessionId);

      setProjectExpansions((previous) => {
        const updated = { ...previous };
        delete updated[project.id];
        return updated;
      });
      setConversations((previous) => {
        const updated = { ...previous };
        deletedSessionIds.forEach((sessionId) => {
          delete updated[sessionId];
        });
        return updated;
      });
      setContextWorkbenchHistories((previous) => {
        const updated = { ...previous };
        deletedSessionIds.forEach((sessionId) => {
          delete updated[sessionId];
        });
        return updated;
      });
      setContextRevisionHistories((previous) => {
        const updated = { ...previous };
        deletedSessionIds.forEach((sessionId) => {
          delete updated[sessionId];
        });
        return updated;
      });
      setPendingContextRestores((previous) => {
        const updated = { ...previous };
        deletedSessionIds.forEach((sessionId) => {
          delete updated[sessionId];
        });
        return updated;
      });

      if (currentProjectId === project.id) {
        setCurrentProjectId(next.currentProjectId);
      }
      if (deletedProjectSession || currentProjectId === project.id) {
        const fallbackSessionId = firstProjectSessionId(next.currentProjectId, next.projects);
        setCurrentProjectSessionId(fallbackSessionId);
        if (deletedCurrentSession || currentProjectId === project.id) {
          setCurrentSessionId(fallbackSessionId);
        }
      }
      abortSessionRequests(deletedSessionIds);

      setView('chat');
      window.focus();
      focusComposer();
      showToast('项目已移除。');
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  async function handlePinProject(project: ProjectSummary) {
    try {
      const data = await pinProjectRequest(project.id);
      applySidebarPayload(data, project.id);
      setCurrentProjectId(project.id);
      showToast('项目已置顶。');
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  async function handleRenameProject(project: ProjectSummary) {
    const nextTitle = window.prompt(localizeUiText('重命名项目', uiLocale), project.title)?.trim();
    if (!nextTitle || nextTitle === project.title) {
      return;
    }

    try {
      const data = await renameProjectRequest(project.id, nextTitle);
      applySidebarPayload(data, project.id);
      setCurrentProjectId(project.id);
      showToast('项目已重命名。');
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  async function handleOpenProjectParent(project: ProjectSummary) {
    const rootPath = project.root_path || '';
    if (!rootPath) {
      showToast('这个项目没有绑定本地文件夹。');
      return;
    }

    if (!window.electronAPI?.openProjectParentFolder) {
      showToast('需要在桌面端打开资源管理器。');
      return;
    }

    const result = await window.electronAPI.openProjectParentFolder(rootPath);
    if (result.ok) {
      showToast('已打开项目所在父文件夹。');
      return;
    }
    showToast(String(result.error));
  }

  async function handleArchiveProjectSessions(project: ProjectSummary) {
    const sessionCount = project.sessions.length;
    if (!sessionCount) {
      showToast('这个项目还没有可归档的对话。');
      return;
    }

    const confirmed = window.confirm(localizeUiText(`归档项目“${project.title}”下的 ${sessionCount} 条对话？`, uiLocale));
    if (!confirmed) {
      return;
    }

    try {
      const data = await archiveProjectSessionsRequest(project.id);
      const archivedSessionIds = new Set(data.archived_session_ids);
      const next = applySidebarPayload(data, project.id);
      const archivedCurrentSession = archivedSessionIds.has(currentSessionId);
      const archivedProjectSession = archivedSessionIds.has(currentProjectSessionId);

      if (archivedProjectSession || currentProjectId === project.id) {
        const fallbackSessionId = firstProjectSessionId(project.id, next.projects);
        setCurrentProjectSessionId(fallbackSessionId);
        if (archivedCurrentSession || currentProjectId === project.id) {
          setCurrentSessionId(fallbackSessionId);
        }
      }
      abortSessionRequests(archivedSessionIds);

      showToast('项目对话已归档。');
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  function handleSwitchSession(sessionId: string) {
    const meta = getSessionMeta(projects, chatSessions, sessionId);
    if (!meta) {
      showToast('没找到这条对话。');
      return;
    }

    setCurrentSessionId(sessionId);
    currentSessionIdRef.current = sessionId;
    setView('chat');

    if (meta.scope === 'project') {
      const projectId = meta.projectId || '';
      setCurrentProjectId(projectId);
      currentProjectIdRef.current = projectId;
      setCurrentProjectSessionId(sessionId);
      if (projectId) {
        setProjectExpansions((previous) => ({
          ...previous,
          [projectId]: true,
        }));
      }
    } else {
      setCurrentChatSessionId(sessionId);
    }

    focusComposer();
  }

  function handleProjectSelect(projectId: string) {
    setCurrentProjectId(projectId);
    setCurrentSessionId('');
    currentProjectIdRef.current = projectId;
    currentSessionIdRef.current = '';
    setProjectExpansions((previous) => ({
      ...previous,
      [projectId]: !previous[projectId],
    }));
  }

  async function handleComposerAttachmentChange(event: ChangeEvent<HTMLInputElement>) {
    const fileList = Array.from(event.target.files || []);
    event.target.value = '';

    if (!fileList.length) {
      return;
    }

    try {
      const nextAttachments = await Promise.all(fileList.map(fileToComposerAttachment));
      setComposerAttachments((previous) => [...previous, ...nextAttachments]);
      showToast(ATTACHMENT_READY_TOAST);
      focusComposer();
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  function handleRemoveAttachment(attachmentId: string) {
    setComposerAttachments((previous) => previous.filter((item) => item.id !== attachmentId));
    showToast(ATTACHMENT_REMOVED_TOAST);
  }

  async function handleSend() {
    const message = composerValue.trim();
    const attachments = composerAttachments;

    if (!message && !attachments.length) {
      showToast(EMPTY_SEND_TOAST);
      return;
    }

    if (currentSessionId && runningSessionIds[currentSessionId]) {
      showToast(SENDING_TOAST);
      return;
    }

    let sessionId = currentSessionId;
    let streamController: AbortController | null = null;
    try {
      sessionId = await ensureSession();
      if (streamAbortControllersRef.current[sessionId]) {
        showToast(SENDING_TOAST);
        return;
      }

      streamController = new AbortController();
      markSessionRunning(sessionId, streamController);
      const userMessage: MessageRecord = {
        role: 'user',
        text: message,
        attachments: toMessageAttachments(attachments),
        toolEvents: [],
        blocks: message ? [createTextBlock(message)] : [],
        pending: false,
        sourceText: '',
      };
      const pendingMessage: MessageRecord = {
        role: 'an',
        text: '',
        attachments: [],
        toolEvents: [],
        blocks: [],
        pending: true,
        sourceText: message,
      };

      setConversations((previous) => {
        const history = previous[sessionId] || [];
        return {
          ...previous,
          [sessionId]: [...history, userMessage, pendingMessage],
        };
      });

      setComposerValue('');
      setComposerAttachments([]);
      setView('chat');
      setCurrentSessionId(sessionId);
      currentSessionIdRef.current = sessionId;

      let streamError = '';
      let streamCompleted = false;

      const sendPayload = {
        session_id: sessionId,
        message,
        model: currentModel,
        reasoning_effort: currentReasoning,
        attachments,
      };

      if (!settingsDraftRef.current.streaming) {
        const response = await sendMessageRequest(sendPayload);
        streamCompleted = true;
        updatePendingAssistantMessage(sessionId, (lastMessage) => {
          const nextBlocks = response.blocks || lastMessage.blocks;
          const nextText = getTextFromBlocks(nextBlocks) || response.answer || lastMessage.text;

          return {
            role: 'an',
            text: nextText,
            attachments: [],
            toolEvents: response.tool_events || lastMessage.toolEvents,
            blocks: nextBlocks,
            pending: false,
            sourceText: message,
          };
        });
        applySidebarPayload(
          response,
          isViewingSession(sessionId)
            ? response.session.project_id || currentProjectIdRef.current
            : currentProjectIdRef.current,
        );
        if (isViewingSession(sessionId) && response.session.scope === 'project') {
          const nextProjectId = response.session.project_id || currentProjectIdRef.current;
          setCurrentProjectId(nextProjectId || '');
          setCurrentProjectSessionId(response.session.id);
          if (nextProjectId) {
            setProjectExpansions((previous) => ({
              ...previous,
              [nextProjectId]: true,
            }));
          }
        } else if (isViewingSession(sessionId)) {
          setCurrentChatSessionId(response.session.id);
        }
        setPendingContextRestores((previous) => {
          const next = { ...previous };
          delete next[sessionId];
          return next;
        });
        if (Array.isArray(response.tool_events) && response.tool_events.length) {
          showToast(`本轮调用了 ${response.tool_events.length} 个工具。`);
        }
        return;
      }

      await streamMessageRequest(
        sendPayload,
        (event) => {
          if (event.type === 'delta') {
            const isReasoningDelta = event.kind === 'reasoning';
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              text: isReasoningDelta ? lastMessage.text : `${lastMessage.text}${event.delta}`,
              blocks: isReasoningDelta
                ? appendReasoningDeltaToBlocks(lastMessage.blocks, event.delta)
                : appendDeltaToBlocks(lastMessage.blocks, event.delta),
              pending: true,
            }));
            return;
          }

          if (event.type === 'model_start') {
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              blocks: startThinkingBlock(lastMessage.blocks),
              pending: true,
            }));
            return;
          }

          if (event.type === 'model_done') {
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              blocks: completeReasoningBlocks(lastMessage.blocks),
              pending: true,
            }));
            return;
          }

          if (event.type === 'reasoning_start') {
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              blocks: startReasoningBlock(lastMessage.blocks),
              pending: true,
            }));
            return;
          }

          if (event.type === 'reasoning_done') {
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              blocks: completeReasoningBlocks(lastMessage.blocks),
              pending: true,
            }));
            return;
          }

          if (event.type === 'reset') {
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              pending: true,
            }));
            return;
          }

          if (event.type === 'tool_event') {
            updatePendingAssistantMessage(sessionId, (lastMessage) => ({
              ...lastMessage,
              role: 'an',
              toolEvents: [...lastMessage.toolEvents, event.tool_event],
              blocks: appendToolToBlocks(lastMessage.blocks, event.tool_event),
              pending: true,
            }));
            return;
          }

          if (event.type === 'error') {
            streamError = event.error;
            return;
          }

          streamCompleted = true;
          updatePendingAssistantMessage(sessionId, (lastMessage) => {
            const nextBlocks = event.blocks || lastMessage.blocks;
            const nextText = getTextFromBlocks(nextBlocks) || event.answer || lastMessage.text;

            return {
              role: 'an',
              text: nextText,
              attachments: [],
              toolEvents: event.tool_events || lastMessage.toolEvents,
              blocks: nextBlocks,
              pending: false,
              sourceText: message,
            };
          });

          applySidebarPayload(
            event,
            isViewingSession(sessionId)
              ? event.session.project_id || currentProjectIdRef.current
              : currentProjectIdRef.current,
          );

          if (isViewingSession(sessionId) && event.session.scope === 'project') {
            const nextProjectId = event.session.project_id || currentProjectIdRef.current;
            setCurrentProjectId(nextProjectId || '');
            setCurrentProjectSessionId(event.session.id);
            if (nextProjectId) {
              setProjectExpansions((previous) => ({
                ...previous,
                [nextProjectId]: true,
              }));
            }
          } else if (isViewingSession(sessionId)) {
            setCurrentChatSessionId(event.session.id);
          }

          setPendingContextRestores((previous) => {
            const next = { ...previous };
            delete next[sessionId];
            return next;
          });

          if (Array.isArray(event.tool_events) && event.tool_events.length) {
            showToast(`本轮调用了 ${event.tool_events.length} 个工具。`);
          }
        },
        {
          signal: streamController.signal,
        },
      );

      if (streamError) {
        throw new Error(streamError);
      }

      if (!streamCompleted) {
        throw new Error('流式响应意外中断');
      }
    } catch (error) {
      if (sessionId && (stopRequestedSessionIdsRef.current.has(sessionId) || isAbortError(error))) {
        await stopRequestPromisesRef.current[sessionId];
        finalizePendingAssistantMessage(sessionId, message);
        showToast('已停止本次回复。');
        return;
      }

      if (sessionId) {
        setConversations((previous) => {
          const history = [...(previous[sessionId] || [])];
          if (history.length) {
            history[history.length - 1] = {
              role: 'an',
              text: getThrownMessage(error),
              attachments: [],
              toolEvents: [],
              blocks: [],
              pending: false,
              sourceText: message,
            };
          }

          return {
            ...previous,
            [sessionId]: history,
          };
        });
      }

      showToast(getThrownMessage(error));
    } finally {
      if (sessionId) {
        clearSessionRunning(sessionId, streamController || undefined);
      }
    }
  }

  function handleStopSending() {
    if (!currentSessionId || !streamAbortControllersRef.current[currentSessionId]) {
      return;
    }

    abortSessionRequest(currentSessionId);
  }

  async function handleSaveOpenAISettings() {
    setIsSavingSettings(true);
    try {
      const response = await saveSettingsRequest(
        buildSettingsSavePayload(settingsDraftRef.current, {
          defaultReasoningEffort: currentReasoningRef.current,
          uiPreferences: currentUiPreferences(),
        }),
      );

      syncSettingsState(response.settings, response.models);
      showToast(SETTINGS_SAVED_TOAST);
    } catch (error) {
      showToast(getThrownMessage(error));
    } finally {
      setIsSavingSettings(false);
    }
  }

  async function handleClearApiKey() {
    setIsSavingSettings(true);
    try {
      const response = await saveSettingsRequest({
        ...buildSettingsSavePayload(settingsDraftRef.current, {
          clearProviderApiKeyId: settingsDraftRef.current.active_provider_id,
          defaultReasoningEffort: currentReasoningRef.current,
          uiPreferences: currentUiPreferences(),
        }),
        clear_api_key: true,
      });

      syncSettingsState(response.settings, response.models);
      showToast(SETTINGS_API_KEY_CLEARED_TOAST);
    } catch (error) {
      showToast(getThrownMessage(error));
    } finally {
      setIsSavingSettings(false);
    }
  }

  async function handleCopyMessage(text: string) {
    try {
      await copyText(text);
      showToast(COPY_TOAST);
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  async function handleEditMessage(messageIndex: number) {
    const history = currentSessionId ? conversations[currentSessionId] || [] : [];
    const targetMessage = history[messageIndex];
    if (!targetMessage || targetMessage.role !== 'user') {
      return;
    }

    const nextHistory = history.slice(0, messageIndex);

    setComposerValue(targetMessage.text);
    setConversations((previous) => ({
      ...previous,
      [currentSessionId]: nextHistory,
    }));
    setView('chat');
    focusComposer();

    if (!currentSessionId) {
      showToast(EDIT_TOAST);
      return;
    }

    try {
      const response = await truncateSessionRequest(currentSessionId, messageIndex);
      applySidebarPayload(response);
      setConversations((previous) => ({
        ...previous,
        [currentSessionId]: normalizeConversation(response.conversation),
      }));
      showToast(EDIT_TOAST);
    } catch (error) {
      setConversations((previous) => ({
        ...previous,
        [currentSessionId]: nextHistory,
      }));
      showToast(getThrownMessage(error));
    }
  }

  function handleRegenerateMessage(sourceText: string) {
    if (!sourceText) {
      showToast(REGENERATE_MISSING_TOAST);
      return;
    }

    setComposerValue(sourceText);
    setView('chat');
    focusComposer();
    showToast(REGENERATE_TOAST);
  }

  async function handleDeleteMessage(messageIndex: number) {
    if (!currentSessionId) {
      showToast('没找到当前会话。');
      return;
    }

    const sessionId = currentSessionId;
    const history = conversations[sessionId] || [];
    const targetMessage = history[messageIndex];
    if (!targetMessage) {
      showToast('没找到这条消息。');
      return;
    }

    abortSessionRequest(sessionId);

    try {
      const response = await deleteMessageRequest(sessionId, messageIndex);
      applySidebarPayload(response);
      setConversations((previous) => ({
        ...previous,
        [sessionId]: normalizeConversation(response.conversation),
      }));
      showToast('这条消息已经删掉了。');
    } catch (error) {
      showToast(getThrownMessage(error));
    }
  }

  function handleContextWorkbenchHistoryChange(sessionId: string, history: ContextWorkbenchHistoryEntry[]) {
    setContextWorkbenchHistories((previous) => ({
      ...previous,
      [sessionId]: history,
    }));
  }

  function handleContextWorkbenchConversationChange(sessionId: string, conversation: MessageRecord[]) {
    setConversations((previous) => ({
      ...previous,
      [sessionId]: conversation,
    }));
  }

  function handleContextRevisionHistoryChange(sessionId: string, revisions: ContextRevisionSummary[]) {
    setContextRevisionHistories((previous) => ({
      ...previous,
      [sessionId]: revisions,
    }));
  }

  function handlePendingContextRestoreChange(sessionId: string, pendingRestore: PendingContextRestore | null) {
    setPendingContextRestores((previous) => {
      const next = { ...previous };
      if (pendingRestore) {
        next[sessionId] = pendingRestore;
      } else {
        delete next[sessionId];
      }
      return next;
    });
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (view === 'chat') {
        void handleSend();
      }
    }
  }

  function handleToggleDropdown(
    dropdownId: Exclude<DropdownId, null>,
    event: ReactMouseEvent<HTMLButtonElement>,
  ) {
    event.stopPropagation();
    setOpenDropdown((previous) => (previous === dropdownId ? null : dropdownId));
  }

  function handlePermissionSelect(option: PermissionOption) {
    setCurrentPermission(option);
    setOpenDropdown(null);
    showToast(option.toastMessage || `已切换到 ${option.label}。`);
  }

  function handleOpenModelPicker() {
    setOpenDropdown(null);
    setIsModelPickerOpen(true);
  }

  function handleModelSelect(providerId: string, model: ResponseProviderModel) {
    const modelId = (model.id || model.label || '').trim();
    if (!modelId) {
      return;
    }

    const previousDraft = settingsDraftRef.current;
    const targetProvider = previousDraft.response_providers.find((provider) => provider.id === providerId);
    if (!targetProvider) {
      return;
    }

    if (previousDraft.active_provider_id === providerId && previousDraft.default_model === modelId && currentModel === modelId) {
      setIsModelPickerOpen(false);
      return;
    }

    const requestId = modelSelectionRequestIdRef.current + 1;
    modelSelectionRequestIdRef.current = requestId;
    const nextDraft = applyProviderModelSelection(previousDraft, providerId, modelId);
    const savePayload = buildSettingsSavePayload(nextDraft, {
      activeProviderId: providerId,
      defaultReasoningEffort: currentReasoningRef.current,
      uiPreferences: currentUiPreferences(),
    });

    if (settingsAutosaveTimerRef.current) {
      window.clearTimeout(settingsAutosaveTimerRef.current);
      settingsAutosaveTimerRef.current = null;
    }

    flushSync(() => {
      currentModelRef.current = modelId;
      settingsDraftRef.current = nextDraft;
      settingsAutosaveSnapshotRef.current = settingsPayloadSnapshot(savePayload);
      setCurrentModel(modelId);
      setSettingsDraft(nextDraft);
      setIsModelPickerOpen(false);
      setModels((previous) =>
        Array.from(new Set([modelId, ...targetProvider.models.map((item) => item.id), ...previous].filter(Boolean))),
      );
    });
    showToast(`模型已切换到 ${modelId}。`);
    void saveSettingsRequest(savePayload)
      .then((response) => {
        if (modelSelectionRequestIdRef.current !== requestId) {
          return;
        }
        syncSettingsState(response.settings, response.models, {
          preserveCurrentModel: true,
          preserveCurrentReasoning: true,
        });
      })
      .catch((error) => {
        if (modelSelectionRequestIdRef.current !== requestId) {
          return;
        }

        const previousActiveProvider = previousDraft.response_providers.find((provider) => provider.id === previousDraft.active_provider_id);
        const previousModel = previousActiveProvider?.default_model || previousDraft.default_model || currentModelRef.current;
        const previousPayload = buildSettingsSavePayload(previousDraft, {
          defaultReasoningEffort: currentReasoningRef.current,
          uiPreferences: currentUiPreferences(),
        });
        settingsDraftRef.current = previousDraft;
        settingsAutosaveSnapshotRef.current = settingsPayloadSnapshot(previousPayload);
        currentModelRef.current = previousModel;
        setSettingsDraft(previousDraft);
        setCurrentModel(previousModel);
        showToast(getThrownMessage(error));
      });
  }

  function handleReasoningSelect(option: ReasoningOption) {
    const requestId = reasoningSaveRequestIdRef.current + 1;
    reasoningSaveRequestIdRef.current = requestId;
    const previousReasoning = currentReasoningRef.current;
    flushSync(() => {
      currentReasoningRef.current = option.value;
      setCurrentReasoning(option.value);
      setOpenDropdown(null);
    });
    showToast(`推理强度已切换到 ${option.label}。`);
    void saveSettingsRequest(
      buildSettingsSavePayload(settingsDraftRef.current, {
        defaultReasoningEffort: option.value,
        uiPreferences: currentUiPreferences(),
      }),
    )
      .then((response) => {
        if (reasoningSaveRequestIdRef.current !== requestId) {
          return;
        }
        syncSettingsState(response.settings, response.models, {
          preserveCurrentModel: true,
          preserveCurrentReasoning: true,
        });
      })
      .catch((error) => {
        if (reasoningSaveRequestIdRef.current !== requestId) {
          return;
        }
        currentReasoningRef.current = previousReasoning;
        setCurrentReasoning(previousReasoning);
        showToast(getThrownMessage(error));
      });
  }

  function handleToggleSidebar() {
    setUserSidebarCollapsed((previous) => {
      const next = !previous;
      if (next) {
        showToast(COLLAPSE_TOAST);
      }
      return next;
    });
  }

  function handleToggleSidebarMode() {
    const nextMode = sidebarMode === 'projects' ? 'chats' : 'projects';
    setSidebarMode(nextMode);
    setCurrentSessionId('');
  }

  function handleThemeChange(value: string) {
    setThemeColor(value);
    showToast(THEME_TOAST);
  }

  function handleThemeModeChange(nextMode: AppearanceMode) {
    const nextResolvedMode = nextMode === 'system' ? getSystemAppearanceMode() : nextMode;

    setThemeMode(nextMode);
    setBackgroundColor((previousBackground) => {
      if (!shouldSwapThemeDefaultBackground(previousBackground, nextResolvedMode)) {
        return previousBackground;
      }

      return nextResolvedMode === 'light' ? DEFAULT_LIGHT_BACKGROUND_COLOR : DEFAULT_DARK_BACKGROUND_COLOR;
    });
  }

  function handleBackgroundColorChange(value: string) {
    setBackgroundColor((previousBackground) => normalizeHexColor(value, previousBackground));
  }

  function handleToggleServiceHints(enabled: boolean) {
    setServiceHintsEnabled(enabled);
    showToast(enabled ? SETTINGS_HINTS_ON_TOAST : SETTINGS_HINTS_OFF_TOAST);
  }

  function handleToggleContextMap() {
    setContextMap((previous) => ({
      ...previous,
      stage: ((previous.stage + 1) % 3) as 0 | 1 | 2,
    }));
  }

  const handleJumpToMainChatMessage = useCallback((messageIndex: number) => {
    const messageList = messageListRef.current;
    if (!messageList) {
      return;
    }

    const targetNode = messageList.querySelector<HTMLElement>(`[data-message-index="${messageIndex}"]`);
    if (!targetNode) {
      return;
    }

    messageList.scrollTo({
      top: Math.max(targetNode.offsetTop - 12, 0),
      behavior: 'smooth',
    });
  }, []);

  return (
    <div className="app-container">
      <Toast message={toastState.message} visible={toastState.visible} />

      <div className={`title-bar ${contextMap.stage === 2 ? 'main-blurred' : ''}`}>
        <div className="title-bar-left">
          <button className="sidebar-toggle" onClick={handleToggleSidebar} title="切换侧栏">
            <i className="ph-light ph-sidebar-simple" />
          </button>
          <div className="menu-item">文件</div>
          <div className="menu-item">编辑</div>
          <div className="menu-item">查看</div>
          <div className="menu-item">窗口</div>
          <div className="menu-item">帮助</div>
        </div>
        <div className="title-bar-right">
          <button className="window-btn" title="最小化" onClick={() => window.electronAPI?.minimize?.()}>
            <i className="ph-light ph-minus" />
          </button>
          <button className="window-btn" title="最大化" onClick={() => window.electronAPI?.maximize?.()}>
            <i className="ph-light ph-square" />
          </button>
          <button className="window-btn close" title="关闭" onClick={() => window.electronAPI?.close?.()}>
            <i className="ph-light ph-x" />
          </button>
        </div>
      </div>

      <div className="main-layout">
        {view === 'settings' ? (
          <SettingsView
            availableModels={models}
            fetchingProviderId={fetchingProviderId}
            isSaving={isSavingSettings}
            openAISettings={openAISettings}
            savingProviderId={savingProviderId}
            settingsDraft={settingsDraft}
            serviceHintsEnabled={serviceHintsEnabled}
            appearanceContrast={appearanceContrast}
            backgroundColor={backgroundColor}
            codeFontSize={codeFontSize}
            codeFont={codeFont}
            resolvedThemeMode={resolvedThemeMode}
            themeColor={themeColor}
            themeMode={themeMode}
            themeOptions={THEME_OPTIONS}
            uiFontSize={uiFontSize}
            uiFont={uiFont}
            availableCodeFonts={availableCodeFonts}
            availableUiFonts={availableUiFonts}
            view={view}
            onClearApiKey={() => {
              void handleClearApiKey();
            }}
            onDraftChange={(patch) => {
              applySettingsDraftPatch(patch);
            }}
            onProviderDraftChange={(providerId, patch) => {
              updateProviderDraftState(providerId, patch);
            }}
            onProviderAdd={(providerType, providerName) => addProviderDraftState(providerType, providerName)}
            onProviderDelete={(providerId) => deleteProviderDraftState(providerId)}
            onProviderLoadModels={(providerId) => loadProviderModelCandidates(providerId)}
            onProviderPersist={(providerId, options) => {
              void persistProviderSettings(providerId, options);
            }}
            onSaveOpenAISettings={() => {
              void handleSaveOpenAISettings();
            }}
            onAppearanceContrastChange={setAppearanceContrast}
            onBackgroundColorChange={handleBackgroundColorChange}
            onCodeFontSizeChange={setCodeFontSize}
            onCodeFontChange={setCodeFont}
            onSwitchView={setView}
            onThemeChange={handleThemeChange}
            onThemeModeChange={handleThemeModeChange}
            onToggleServiceHints={handleToggleServiceHints}
            onUiFontSizeChange={setUiFontSize}
            onUiFontChange={setUiFont}
          />
        ) : (
          <>
            <Sidebar
              chatSessions={chatSessions}
              currentProjectId={currentProjectId}
              currentSessionId={currentSessionId}
              isSidebarResizing={isResizingLeft}
              runningSessionIds={runningSessionIds}
              projectExpansions={projectExpansions}
              projects={projects}
              sidebarMode={sidebarMode}
              view={view}
              onAddItem={() => {
                void handleAddSidebarItem();
              }}
              onAutomation={() => {}}
              onPlugins={() => {}}
              onProjectArchive={(project) => {
                void handleArchiveProjectSessions(project);
              }}
              onProjectDelete={(project) => {
                void handleDeleteProject(project);
              }}
              onProjectOpenParent={(project) => {
                void handleOpenProjectParent(project);
              }}
              onProjectPin={(project) => {
                void handlePinProject(project);
              }}
              onProjectRename={(project) => {
                void handleRenameProject(project);
              }}
              onProjectSelect={handleProjectSelect}
              onSearch={() => {}}
              onSessionCreate={(projectId) => {
                setCurrentProjectId(projectId);
                setProjectExpansions((previous) => ({
                  ...previous,
                  [projectId]: true,
                }));
                void createSession(false, { scope: 'project', projectId });
              }}
              onSessionDelete={(sessionId) => {
                void handleDeleteSession(sessionId);
              }}
              onSessionSelect={handleSwitchSession}
              onSwitchView={setView}
              onToggleMode={handleToggleSidebarMode}
            />

            <div className={`resizer resizer-left ${isResizingLeft ? 'dragging' : ''}`} onMouseDown={startResizingLeft} />

            <main className={`view-container ${contextMap.stage === 2 ? 'main-blurred' : ''}`}>
              <ChatView
                attachments={composerAttachments}
                composerValue={composerValue}
                currentModel={currentModel}
                currentPermission={currentPermission}
                currentReasoningLabel={currentReasoningLabel}
                currentReasoningValue={currentReasoning}
                dropdownId={openDropdown}
                fileInputRef={fileInputRef}
                hasMessages={hasMessages}
                headerTitle={headerTitle}
                isSending={isSending}
                messageListRef={messageListRef}
                messages={currentConversation}
                contextMap={contextMap}
                onAttachmentInputChange={handleComposerAttachmentChange}
                onToggleContextMap={handleToggleContextMap}
                onComposerChange={(event: ChangeEvent<HTMLTextAreaElement>) => setComposerValue(event.target.value)}
                onComposerKeyDown={handleComposerKeyDown}
                onComposerPlus={() => {
                  fileInputRef.current?.click();
                }}
                onCopyMessage={(text) => {
                  void handleCopyMessage(text);
                }}
                onDeleteMessage={handleDeleteMessage}
                onEditMessage={handleEditMessage}
                onRegenerateMessage={handleRegenerateMessage}
                onRemoveAttachment={handleRemoveAttachment}
                onOpenModelPicker={handleOpenModelPicker}
                onSelectPermission={handlePermissionSelect}
                onSelectReasoning={handleReasoningSelect}
                onSend={() => {
                  void handleSend();
                }}
                onStop={handleStopSending}
                onToggleDropdown={handleToggleDropdown}
                permissionOptions={PERMISSION_OPTIONS}
                reasoningOptions={reasoningOptions}
                textareaRef={textareaRef}
                view={view}
                welcomeAnimationKey={welcomeAnimationKey}
                welcomeText={welcomeText}
              />
            </main>

            <ChatModelPicker
              activeProviderId={settingsDraft.active_provider_id}
              currentModel={currentModel}
              open={isModelPickerOpen}
              providers={settingsDraft.response_providers}
              onClose={() => setIsModelPickerOpen(false)}
              onSelectModel={handleModelSelect}
            />

            <div
              className={`resizer resizer-right ${isResizingRight ? 'dragging' : ''} ${contextMap.stage === 0 ? 'hidden' : ''}`}
              onMouseDown={startResizingRight}
            />

            <ContextMapSidebar
              stage={contextMap.stage}
              messages={currentConversation}
              onToggle={handleToggleContextMap}
              onJumpToMessage={handleJumpToMainChatMessage}
              sessionId={currentSessionId}
              isMainChatBusy={isSending}
              contextWorkbenchHistory={contextWorkbenchHistories[currentSessionId] || []}
              contextRevisionHistory={contextRevisionHistories[currentSessionId] || []}
              pendingContextRestore={pendingContextRestores[currentSessionId] || null}
              reasoningOptions={reasoningOptions}
              uiLocale={uiLocale}
              onContextWorkbenchHistoryChange={handleContextWorkbenchHistoryChange}
              onContextWorkbenchConversationChange={handleContextWorkbenchConversationChange}
              onContextRevisionHistoryChange={handleContextRevisionHistoryChange}
              onPendingContextRestoreChange={handlePendingContextRestoreChange}
              onEnsureSession={ensureSession}
              onUiLocaleChange={(locale) => applySettingsDraftPatch({ user_locale: locale })}
            />
          </>
        )}
      </div>
    </div>
  );
}

function model_options_fallback(defaultModel: string) {
  const ordered = [defaultModel, 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.2'];
  return Array.from(new Set(ordered.filter(Boolean)));
}
