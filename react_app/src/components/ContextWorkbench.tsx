import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, MouseEvent, ReactNode } from 'react';
import { flushSync } from 'react-dom';

import {
  clearContextWorkbenchChatRequest,
  fetchContextWorkbenchSettings,
  fetchProxySessionUsageRequest,
  resetProxyUsageRequest,
  saveContextWorkbenchSettingsRequest,
  streamContextChatRequest,
} from '../api';
import {
  DEFAULT_CONTEXT_TOKEN_THRESHOLDS,
  normalizeContextTokenThresholds,
  type ContextMessageTokenStat,
  type ContextTokenThresholds,
} from '../contextTokenWeight';
import type {
  ContextWorkbenchProvider,
  ContextWorkbenchChatMessage,
  ContextWorkbenchSettingsResponse,
  MessageBlock,
  MessageRecord,
  ProxyUsageSummary,
  ResponseProviderModel,
  ResponseProviderType,
  ReasoningOption,
  ToolEvent,
  TranscriptEntry,
} from '../types';
import { normalizeSupportedLocale, type UiLocale } from '../i18n';
import { copyText, countTokens, getReasoningLabel, normalizeConversation } from '../utils';
import {
  buildManualMessagesFromChat,
  buildWorkbenchModelOptions,
  createManualMessage,
  DEFAULT_WORKBENCH_MODELS,
  formatNodeReferenceSegments,
  formatSuggestionRoleLabel,
  formatTokenCount,
  getThrownMessage,
  isAbortError,
  parseTokenThresholdDraft,
  reasoningDisplayLabel,
  UI_LANGUAGE_OPTIONS,
  uiLanguageLabel,
  uiText,
  WORKBENCH_TABS,
  workbenchTabLabel,
  type ManualWorkbenchMessage,
  type WorkbenchTab,
} from './ContextWorkbench.helpers';
import Dropdown from './Dropdown';
import MessageContent from './MessageContent';
import UsageSummaryCard from './UsageSummaryCard';

interface ContextWorkbenchProps {
  messageTokenStats: ContextMessageTokenStat[];
  selectedNodeIndexes: number[];
  criticalNodeIndexes: number[];
  tokenThresholds: ContextTokenThresholds;
  sessionId: string;
  isMainChatBusy: boolean;
  contextWorkbenchChat: ContextWorkbenchChatMessage[];
  reasoningOptions: ReasoningOption[];
  proxyUsageSummary: ProxyUsageSummary | null;
  uiLocale: UiLocale;
  themeMode: 'light' | 'dark';
  onContextWorkbenchChatChange: (sessionId: string, chat: ContextWorkbenchChatMessage[]) => void;
  onConversationChange: (
    sessionId: string,
    conversation: MessageRecord[],
    rawTranscript?: TranscriptEntry[],
  ) => void | Promise<void>;
  onProxyUsageSummaryChange: (summary: ProxyUsageSummary | null) => void;
  onEnsureSession: () => Promise<string>;
  onTokenThresholdsChange: (thresholds: ContextTokenThresholds) => void;
  onUiLocaleChange?: (locale: UiLocale) => void;
  onUiFontChange?: (font: string, fontSize: number) => void;
  onThemeModeChange?: (themeMode: 'light' | 'dark') => void;
}

interface ManualMessageItemProps {
  entry: ManualWorkbenchMessage;
  uiLocale: UiLocale;
  onCopy: (content: string) => void;
}

function manualEntryBlocks(entry: ManualWorkbenchMessage): MessageBlock[] {
  if (entry.blocks?.length) {
    return entry.blocks;
  }

  const blocks: MessageBlock[] = [];
  if (entry.content.trim()) {
    blocks.push({ kind: 'text', text: entry.content });
  }
  entry.toolEvents?.forEach((toolEvent) => {
    blocks.push({ kind: 'tool', tool_event: toolEvent });
  });
  return blocks;
}

function manualEntryRecord(entry: ManualWorkbenchMessage): MessageRecord {
  return {
    nodeId: entry.id,
    role: entry.role === 'assistant' ? 'an' : 'user',
    text: entry.content,
    attachments: [],
    toolEvents: entry.toolEvents || [],
    blocks: manualEntryBlocks(entry),
    providerItems: [],
    pending: Boolean(entry.pending),
    sourceText: '',
  };
}

function ManualMessageItem({
  entry,
  uiLocale,
  onCopy,
}: ManualMessageItemProps) {
  const copyLabel = uiText(uiLocale, 'Copy', '复制');

  return (
    <div className={`manual-workbench-message ${entry.role}`}>
      <div className="manual-workbench-message-shell">
        <div className="manual-workbench-bubble">
          {entry.role === 'assistant' ? (
            <>
              <MessageContent record={manualEntryRecord(entry)} uiLocale={uiLocale} variant="context-map" />
              {entry.pending && entry.statusText ? (
                <div className="thinking-inline-line" role="status">
                  <span className="thinking-inline-text">{entry.statusText}</span>
                </div>
              ) : null}
            </>
          ) : (
            <div className="manual-workbench-user-text">{entry.content}</div>
          )}
        </div>

        {!entry.pending ? (
          <div className="manual-workbench-actions">
            <button
              aria-label={copyLabel}
              title={copyLabel}
              type="button"
              onClick={() => onCopy(entry.content)}
            >
              <i className="ph-light ph-copy" />
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ManualEmptyState({ uiLocale }: { uiLocale: UiLocale }) {
  return (
    <div className="manual-workbench-empty">
      <div className="manual-workbench-empty-title">{uiText(uiLocale, 'You can organize the current context directly', '可以直接整理当前上下文')}</div>
      <div className="manual-workbench-empty-body">
        {uiText(uiLocale, 'Ask which parts are too long, or tell the model what should be kept, compressed, replaced, or removed.', '可以询问哪些内容太长，或者直接告诉模型哪些内容应该保留、压缩、替换或删除。')}
      </div>
    </div>
  );
}

function SettingsRow({
  title,
  meta,
  children,
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="workbench-settings-row">
      <div className="workbench-settings-row-label">
        <div className="workbench-setting-title">{title}</div>
        {meta ? <div className="workbench-settings-row-meta">{meta}</div> : null}
      </div>
      <div className="workbench-settings-row-control">
        {children}
      </div>
    </div>
  );
}

type PromptSettingKey =
  | 'codex_system_prompt'
  | 'manual_local_compact_prompt'
  | 'auto_local_compact_prompt';

type PromptDrafts = Record<PromptSettingKey, string>;

type PromptSettingItem = {
  key: PromptSettingKey;
  title: string;
  placeholder: string;
};

const DEFAULT_CONTEXT_PROVIDER_ID = 'codex-proxy';

const PROVIDER_TYPE_OPTIONS: Array<{ value: ResponseProviderType; label: string; zhLabel: string }> = [
  { value: 'responses', label: 'Responses', zhLabel: 'Responses' },
  { value: 'chat_completion', label: 'Chat Completions', zhLabel: 'Chat Completions' },
  { value: 'claude', label: 'Anthropic Messages', zhLabel: 'Anthropic Messages' },
  { value: 'gemini', label: 'Gemini', zhLabel: 'Gemini' },
];

const EMPTY_PROMPT_DRAFTS: PromptDrafts = {
  codex_system_prompt: '',
  manual_local_compact_prompt: '',
  auto_local_compact_prompt: '',
};

function promptDraftsFromSettings(settings: ContextWorkbenchSettingsResponse['settings']): PromptDrafts {
  return {
    codex_system_prompt: settings.codex_system_prompt || '',
    manual_local_compact_prompt: settings.manual_local_compact_prompt || '',
    auto_local_compact_prompt: settings.auto_local_compact_prompt || '',
  };
}

function promptDefaultsFromSettings(settings: ContextWorkbenchSettingsResponse['settings']): PromptDrafts {
  return {
    codex_system_prompt: settings.codex_system_prompt_default || '',
    manual_local_compact_prompt: settings.manual_local_compact_prompt_default || '',
    auto_local_compact_prompt: settings.auto_local_compact_prompt_default || '',
  };
}

function providerTypeLabel(providerType: ResponseProviderType, uiLocale: UiLocale) {
  const item = PROVIDER_TYPE_OPTIONS.find((option) => option.value === providerType);
  if (!item) return providerType;
  return uiText(uiLocale, item.label, item.zhLabel);
}

function providerGroupLabel(provider: ContextWorkbenchProvider | null, uiLocale: UiLocale) {
  if (!provider) {
    return '';
  }
  if (provider?.id === DEFAULT_CONTEXT_PROVIDER_ID) {
    return uiText(uiLocale, 'Codex built-in', 'Codex 自带');
  }
  return uiText(uiLocale, 'Other provider', '其他服务商');
}

function providerDisplayName(provider: ContextWorkbenchProvider | null, uiLocale: UiLocale) {
  if (!provider) return 'Codex';
  if (provider.id === DEFAULT_CONTEXT_PROVIDER_ID) {
    return 'Codex';
  }
  const providerName = (provider.name || provider.id || '').trim();
  if (provider.provider_type === 'responses') {
    return /responses/i.test(providerName) ? providerName : `${providerName || 'OpenAI'} Responses`;
  }
  if (provider.provider_type === 'chat_completion') {
    return /chat completions?/i.test(providerName)
      ? providerName
      : `${providerName || 'OpenAI'} Chat Completions`;
  }
  if (provider.provider_type === 'claude') {
    return uiText(uiLocale, 'Anthropic Messages', 'Anthropic Messages');
  }
  if (provider.provider_type === 'gemini') {
    return 'Gemini';
  }
  return providerName || providerTypeLabel(provider.provider_type, uiLocale);
}

function readableProviderError(errorText: string) {
  const cleaned = errorText.trim();
  if (!cleaned.startsWith('{')) {
    return cleaned;
  }
  try {
    const parsed = JSON.parse(cleaned) as { message?: unknown; error?: unknown };
    if (typeof parsed.message === 'string' && parsed.message.trim()) {
      return parsed.message.trim();
    }
    if (typeof parsed.error === 'string' && parsed.error.trim()) {
      return parsed.error.trim();
    }
  } catch {
  }
  return cleaned;
}

function providerErrorPlacement(errorText: string): 'base_url' | 'api_key' | 'model' {
  const normalized = errorText.trim().toLowerCase();
  if (!normalized) return 'model';
  if (normalized.includes('base url')) return 'base_url';
  if (normalized.includes('api key') || normalized.includes('key')) return 'api_key';
  return 'model';
}

function providerMapFromList(providers: ContextWorkbenchProvider[]) {
  return providers.reduce<Record<string, ContextWorkbenchProvider>>((result, provider) => {
    if (provider.id) {
      result[provider.id] = provider;
    }
    return result;
  }, {});
}

function firstProviderModel(provider: ContextWorkbenchProvider | null) {
  return provider?.models?.find((model) => model.id)?.id || provider?.default_model || '';
}

function defaultBaseUrlForProviderType(providerType: ResponseProviderType) {
  switch (providerType) {
    case 'gemini':
      return 'https://generativelanguage.googleapis.com/v1beta';
    case 'claude':
      return 'https://api.anthropic.com/v1';
    case 'chat_completion':
    case 'responses':
    default:
      return 'https://api.openai.com/v1';
  }
}

export default function ContextWorkbench({
  messageTokenStats,
  selectedNodeIndexes,
  criticalNodeIndexes,
  tokenThresholds,
  sessionId,
  isMainChatBusy,
  contextWorkbenchChat,
  reasoningOptions,
  proxyUsageSummary,
  uiLocale,
  themeMode,
  onContextWorkbenchChatChange,
  onConversationChange,
  onProxyUsageSummaryChange,
  onEnsureSession,
  onTokenThresholdsChange,
  onUiLocaleChange,
  onUiFontChange,
  onThemeModeChange,
}: ContextWorkbenchProps) {
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('manual');
  const [manualDraft, setManualDraft] = useState('');
  const [manualReasoning, setManualReasoning] = useState('default');
  const [isManualReasoningOpen, setIsManualReasoningOpen] = useState(false);
  const [manualMessages, setManualMessages] = useState<ManualWorkbenchMessage[]>(
    () => buildManualMessagesFromChat(contextWorkbenchChat),
  );
  const [isManualSending, setIsManualSending] = useState(false);
  const [isUsageClearing, setIsUsageClearing] = useState(false);
  const [usageFeedback, setUsageFeedback] = useState('');
  const [usageFeedbackError, setUsageFeedbackError] = useState(false);
  const [manualFeedback, setManualFeedback] = useState('');
  const [manualFeedbackError, setManualFeedbackError] = useState(false);
  const [workbenchProviderIdDraft, setWorkbenchProviderIdDraft] = useState(DEFAULT_CONTEXT_PROVIDER_ID);
  const [workbenchProviders, setWorkbenchProviders] = useState<ContextWorkbenchProvider[]>([]);
  const [workbenchProviderDrafts, setWorkbenchProviderDrafts] = useState<Record<string, ContextWorkbenchProvider>>({});
  const [workbenchApiKeyDraft, setWorkbenchApiKeyDraft] = useState('');
  const [workbenchSavedApiKeyDraft, setWorkbenchSavedApiKeyDraft] = useState('');
  const [isWorkbenchApiKeyDirty, setIsWorkbenchApiKeyDirty] = useState(false);
  const [isWorkbenchApiKeySaving, setIsWorkbenchApiKeySaving] = useState(false);
  const [workbenchModelDraft, setWorkbenchModelDraft] = useState(DEFAULT_WORKBENCH_MODELS[0]);
  const [uiLocaleDraft, setUiLocaleDraft] = useState<UiLocale>(uiLocale);
  const [themeModeDraft, setThemeModeDraft] = useState<'light' | 'dark'>(themeMode);
  const [isWorkbenchProviderOpen, setIsWorkbenchProviderOpen] = useState(false);
  const [isWorkbenchModelOpen, setIsWorkbenchModelOpen] = useState(false);
  const [hasFetchedWorkbenchModels, setHasFetchedWorkbenchModels] = useState(false);
  const [isWorkbenchModelFilterActive, setIsWorkbenchModelFilterActive] = useState(false);
  const [isWorkbenchModelsRefreshing, setIsWorkbenchModelsRefreshing] = useState(false);
  const [tokenWarningThresholdDraft, setTokenWarningThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.warningThreshold),
  );
  const [tokenCriticalThresholdDraft, setTokenCriticalThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.criticalThreshold),
  );
  const [availableWorkbenchModels, setAvailableWorkbenchModels] = useState<ResponseProviderModel[]>([]);
  const [isSettingsLoading, setIsSettingsLoading] = useState(true);
  const [settingsError, setSettingsError] = useState('');
  const [uiFontDraft, setUiFontDraft] = useState('Noto Serif SC');
  const [uiFontSizeDraft, setUiFontSizeDraft] = useState('15');
  const [promptDrafts, setPromptDrafts] = useState<PromptDrafts>(EMPTY_PROMPT_DRAFTS);
  const [promptSavedDrafts, setPromptSavedDrafts] = useState<PromptDrafts>(EMPTY_PROMPT_DRAFTS);
  const [promptDefaults, setPromptDefaults] = useState<PromptDrafts>(EMPTY_PROMPT_DRAFTS);
  const [expandedPromptKey, setExpandedPromptKey] = useState<PromptSettingKey | null>(null);
  const manualListRef = useRef<HTMLDivElement>(null);
  const manualTextareaRef = useRef<HTMLTextAreaElement>(null);
  const manualAbortControllerRef = useRef<AbortController | null>(null);
  const manualActiveSessionIdRef = useRef('');
  const manualStopRequestedRef = useRef(false);
  const manualStopRequestRef = useRef<Promise<unknown> | null>(null);
  const workbenchModelComboboxRef = useRef<HTMLDivElement>(null);

  const selectedNodeNumbers = useMemo(
    () => [...selectedNodeIndexes]
      .sort((left, right) => left - right)
      .map((index) => messageTokenStats.find((stat) => stat.nodeIndex === index)?.nodeNumber || 0)
      .filter((nodeNumber) => nodeNumber > 0),
    [messageTokenStats, selectedNodeIndexes],
  );
  const selectedNodeReferenceSegments = useMemo(
    () => formatNodeReferenceSegments(selectedNodeNumbers),
    [selectedNodeNumbers],
  );
  const selectedWorkbenchProvider = useMemo(
    () =>
      workbenchProviderDrafts[workbenchProviderIdDraft]
      || workbenchProviders.find((provider) => provider.id === workbenchProviderIdDraft)
      || null,
    [workbenchProviderDrafts, workbenchProviderIdDraft, workbenchProviders],
  );
  const orderedWorkbenchProviders = useMemo(
    () => [
      ...workbenchProviders.filter((provider) => provider.id === DEFAULT_CONTEXT_PROVIDER_ID),
      ...workbenchProviders.filter((provider) => provider.id !== DEFAULT_CONTEXT_PROVIDER_ID),
    ],
    [workbenchProviders],
  );
  const currentWorkbenchProviderLabel = providerDisplayName(selectedWorkbenchProvider, uiLocaleDraft);
  const currentWorkbenchProviderGroup = providerGroupLabel(selectedWorkbenchProvider, uiLocaleDraft);
  const currentWorkbenchProviderType = selectedWorkbenchProvider?.provider_type || 'responses';
  const showExternalProviderFields = Boolean(
    selectedWorkbenchProvider && selectedWorkbenchProvider.id !== DEFAULT_CONTEXT_PROVIDER_ID,
  );
  const selectedProviderModels = selectedWorkbenchProvider?.models || [];
  const providerSyncErrorText = readableProviderError(selectedWorkbenchProvider?.last_sync_error || '');
  const providerSyncErrorPlacement = providerErrorPlacement(providerSyncErrorText);
  const workbenchModelOptions = useMemo(
    () => buildWorkbenchModelOptions(
      workbenchModelDraft,
      availableWorkbenchModels.length ? availableWorkbenchModels : selectedProviderModels,
      currentWorkbenchProviderLabel,
    ),
    [availableWorkbenchModels, currentWorkbenchProviderLabel, selectedProviderModels, workbenchModelDraft],
  );
  const filteredWorkbenchModelOptions = useMemo(() => {
    if (!hasFetchedWorkbenchModels) {
      return [];
    }
    const prefix = isWorkbenchModelFilterActive ? workbenchModelDraft.trim().toLowerCase() : '';
    if (!prefix) {
      return workbenchModelOptions;
    }
    return workbenchModelOptions.filter((model) => {
      const modelId = (model.id || '').trim().toLowerCase();
      const modelLabel = (model.label || '').trim().toLowerCase();
      return modelId.startsWith(prefix) || modelLabel.startsWith(prefix);
    });
  }, [hasFetchedWorkbenchModels, isWorkbenchModelFilterActive, workbenchModelDraft, workbenchModelOptions]);
  const criticalNodeIndexSet = useMemo(
    () => new Set(criticalNodeIndexes),
    [criticalNodeIndexes],
  );
  const localSuggestionStats = useMemo(
    () => ({
      total_token_count: messageTokenStats.reduce((total, stat) => total + stat.tokens, 0),
      tool_token_count: messageTokenStats.reduce((total, stat) => total + stat.toolTokens, 0),
    }),
    [messageTokenStats],
  );
  const localSuggestionNodes = useMemo(
    () =>
      messageTokenStats
        .filter((stat) => stat.editable)
        .map((stat): { node_index: number; node_number: number; role: string; token_count: number; tool_token_count: number; preview: string } => ({
          node_index: stat.nodeIndex,
          node_number: stat.nodeNumber,
          role: stat.role,
          token_count: stat.tokens,
          tool_token_count: stat.toolTokens,
          preview: '',
        }))
        .sort((left, right) => right.token_count - left.token_count || left.node_number - right.node_number),
    [messageTokenStats],
  );
  const criticalSuggestionNodes = useMemo(
    () => localSuggestionNodes.filter((node) => criticalNodeIndexSet.has(node.node_index)),
    [localSuggestionNodes, criticalNodeIndexSet],
  );
  const manualChatKey = useMemo(() => JSON.stringify(contextWorkbenchChat || []), [contextWorkbenchChat]);
  const isWorkbenchBusy = isManualSending;
  const isManualComposerLocked = isMainChatBusy || isWorkbenchBusy;
  const manualReasoningDisabled = reasoningOptions.length === 0;
  const hasClearableManualChat = manualMessages.some((message) => !message.pending);
  const currentManualReasoningLabel = reasoningDisplayLabel(
    manualReasoning,
    getReasoningLabel(manualReasoning, reasoningOptions),
    uiLocaleDraft,
  );
  const mainUsageSummary = proxyUsageSummary?.by_kind?.main || null;
  const contextWorkbenchUsageSummary = proxyUsageSummary?.by_kind?.context_workbench || null;
  const nextTokenThresholds = useMemo(() => {
    const warningThreshold = parseTokenThresholdDraft(
      tokenWarningThresholdDraft,
      tokenThresholds.warningThreshold,
    );
    const criticalThreshold = parseTokenThresholdDraft(
      tokenCriticalThresholdDraft,
      tokenThresholds.criticalThreshold,
    );

    return {
      warningThreshold,
      criticalThreshold,
    };
  }, [tokenCriticalThresholdDraft, tokenThresholds, tokenWarningThresholdDraft]);
  const tokenThresholdError =
    nextTokenThresholds.warningThreshold >= nextTokenThresholds.criticalThreshold
      ? uiText(uiLocaleDraft, 'The red threshold must be greater than the yellow threshold.', '红色阈值必须大于黄色阈值。')
      : '';
  const promptSettingItems = useMemo<PromptSettingItem[]>(
    () => [
      {
        key: 'codex_system_prompt',
        title: uiText(uiLocaleDraft, 'Codex System Prompt', 'Codex 系统提示词'),
        placeholder: uiText(uiLocaleDraft, 'Waiting for the first Codex request instructions...', '等待读取第一条 Codex 请求的 instructions...'),
      },
      {
        key: 'manual_local_compact_prompt',
        title: uiText(uiLocaleDraft, 'Manual Compact Prompt', '手动压缩词'),
        placeholder: uiText(uiLocaleDraft, 'Manual compact prompt', '手动压缩词'),
      },
      {
        key: 'auto_local_compact_prompt',
        title: uiText(uiLocaleDraft, 'Auto Compact Prompt', '自动压缩词'),
        placeholder: uiText(uiLocaleDraft, 'Auto compact prompt', '自动压缩词'),
      },
    ],
    [uiLocaleDraft],
  );
  const expandedPromptItem = expandedPromptKey
    ? promptSettingItems.find((item) => item.key === expandedPromptKey) || null
    : null;
  const promptTokenLabels = useMemo<PromptDrafts>(
    () => ({
      codex_system_prompt: `${(countTokens(promptDrafts.codex_system_prompt) / 1000).toFixed(1)}k`,
      manual_local_compact_prompt: `${(countTokens(promptDrafts.manual_local_compact_prompt) / 1000).toFixed(1)}k`,
      auto_local_compact_prompt: `${(countTokens(promptDrafts.auto_local_compact_prompt) / 1000).toFixed(1)}k`,
    }),
    [promptDrafts],
  );

  function applyPromptSettings(settings: ContextWorkbenchSettingsResponse['settings']) {
    const nextPromptDrafts = promptDraftsFromSettings(settings);
    setPromptDrafts(nextPromptDrafts);
    setPromptSavedDrafts(nextPromptDrafts);
    setPromptDefaults(promptDefaultsFromSettings(settings));
  }

  function applyWorkbenchProviderSettings(
    response: ContextWorkbenchSettingsResponse,
    options: { preserveAvailableModels?: boolean } = {},
  ) {
    const nextProviders = response.providers || [];
    const nextProviderMap = providerMapFromList(nextProviders);
    const nextProviderId =
      response.settings.context_workbench_provider_id
      || nextProviders[0]?.id
      || DEFAULT_CONTEXT_PROVIDER_ID;
    const nextProvider = nextProviderMap[nextProviderId] || nextProviders[0] || null;
    const nextModel =
      response.settings.context_workbench_model
      || firstProviderModel(nextProvider)
      || DEFAULT_WORKBENCH_MODELS[0];

    setWorkbenchProviders(nextProviders);
    setWorkbenchProviderDrafts(nextProviderMap);
    setWorkbenchProviderIdDraft(nextProviderId);
    setWorkbenchModelDraft(nextModel);
    setAvailableWorkbenchModels((previous) => (
      options.preserveAvailableModels ? previous : response.models || []
    ));
  }

  useEffect(() => {
    setUiLocaleDraft(uiLocale);
  }, [uiLocale]);

  useEffect(() => {
    setThemeModeDraft(themeMode);
  }, [themeMode]);

  useEffect(() => {
    if (!isWorkbenchModelOpen) {
      return;
    }

    function handleDocumentMouseDown(event: globalThis.MouseEvent) {
      const target = event.target;
      if (target instanceof Node && workbenchModelComboboxRef.current?.contains(target)) {
        return;
      }
      setIsWorkbenchModelOpen(false);
      setIsWorkbenchModelFilterActive(false);
    }

    document.addEventListener('mousedown', handleDocumentMouseDown);
    return () => document.removeEventListener('mousedown', handleDocumentMouseDown);
  }, [isWorkbenchModelOpen]);

  useEffect(() => {
    let cancelled = false;

    async function loadWorkbenchSettings() {
      setIsSettingsLoading(true);
      setSettingsError('');
      try {
        const response = await fetchContextWorkbenchSettings();
        if (cancelled) return;
        const settings = response.settings;
        applyWorkbenchProviderSettings(response);
        const loadedThresholds = normalizeContextTokenThresholds({
          warningThreshold: settings.context_token_warning_threshold,
          criticalThreshold: settings.context_token_critical_threshold,
        });
        setTokenWarningThresholdDraft(String(loadedThresholds.warningThreshold));
        setTokenCriticalThresholdDraft(String(loadedThresholds.criticalThreshold));
        onTokenThresholdsChange(loadedThresholds);
        const loadedLocale = settings.user_locale ? normalizeSupportedLocale(settings.user_locale) : uiLocale;
        setUiLocaleDraft(loadedLocale);
        onUiLocaleChange?.(loadedLocale);
        const loadedThemeMode = settings.theme_mode === 'dark' ? 'dark' : 'light';
        setThemeModeDraft(loadedThemeMode);
        onThemeModeChange?.(loadedThemeMode);
        const loadedFont = settings.ui_font || 'Noto Serif SC';
        const loadedFontSize = settings.ui_font_size || 15;
        setUiFontDraft(loadedFont);
        setUiFontSizeDraft(String(loadedFontSize));
        onUiFontChange?.(loadedFont, loadedFontSize);
        applyPromptSettings(settings);
      } catch (error) {
        if (cancelled) return;
        setSettingsError(getThrownMessage(error));
      } finally {
        if (!cancelled) setIsSettingsLoading(false);
      }
    }

    void loadWorkbenchSettings();
    return () => { cancelled = true; };
  }, [onTokenThresholdsChange]);

  useEffect(() => {
    setManualMessages(buildManualMessagesFromChat(contextWorkbenchChat));
    setIsManualSending(false);
  }, [manualChatKey, sessionId]);

  useEffect(() => {
    setManualDraft('');
    setManualFeedback('');
    setManualFeedbackError(false);
  }, [sessionId]);

  useEffect(() => {
    if (!reasoningOptions.some((option) => option.value === manualReasoning)) {
      setManualReasoning(reasoningOptions.find((option) => option.value === 'default')?.value || reasoningOptions[0]?.value || 'default');
    }
  }, [manualReasoning, reasoningOptions]);

  useEffect(() => {
    if (activeTab !== 'manual') {
      return;
    }

    if (manualListRef.current) {
      manualListRef.current.scrollTop = manualListRef.current.scrollHeight;
    }
  }, [activeTab, manualMessages]);

  useLayoutEffect(() => {
    const textarea = manualTextareaRef.current;
    if (!textarea) {
      return;
    }

    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
    textarea.style.overflowY = textarea.scrollHeight > 160 ? 'auto' : 'hidden';
  }, [manualDraft, activeTab]);

  function updatePendingManualMessage(
    messageId: string,
    updater: (message: ManualWorkbenchMessage) => ManualWorkbenchMessage,
  ) {
    setManualMessages((previous) =>
      previous.map((item) => (item.id === messageId ? updater(item) : item)),
    );
  }

  function appendManualTextBlock(blocks: MessageBlock[] | undefined, delta: string): MessageBlock[] {
    const nextBlocks = [...(blocks || [])];
    const lastBlock = nextBlocks[nextBlocks.length - 1];
    if (lastBlock?.kind === 'text') {
      nextBlocks[nextBlocks.length - 1] = {
        ...lastBlock,
        text: `${lastBlock.text}${delta}`,
      };
    } else {
      nextBlocks.push({ kind: 'text', text: delta });
    }
    return nextBlocks;
  }

  function appendManualToolBlock(blocks: MessageBlock[] | undefined, toolEvent: ToolEvent): MessageBlock[] {
    return [
      ...(blocks || []),
      {
        kind: 'tool',
        tool_event: toolEvent,
      },
    ];
  }

  function handleManualReasoningSelect(event: MouseEvent<HTMLDivElement>, option: ReasoningOption) {
    event.preventDefault();
    event.stopPropagation();
    flushSync(() => {
      setManualReasoning(option.value);
      setIsManualReasoningOpen(false);
    });
  }

  function saveWorkbenchModelDraft(nextModel: string) {
    if (!nextModel) return;
    flushSync(() => {
      setWorkbenchModelDraft(nextModel);
      setIsWorkbenchModelOpen(false);
      setIsWorkbenchModelFilterActive(false);
      setSettingsError('');
    });
    void handleSaveWorkbenchSettings({ model: nextModel });
  }

  function handleWorkbenchModelSelect(event: MouseEvent<HTMLButtonElement>, model: { id?: string; label?: string; group?: string }) {
    event.preventDefault();
    event.stopPropagation();
    const nextModel = (model.id || model.label || '').trim();
    saveWorkbenchModelDraft(nextModel);
  }

  function handleWorkbenchModelKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setIsWorkbenchModelOpen(true);
      return;
    }

    if (event.key === 'Escape') {
      event.preventDefault();
      setIsWorkbenchModelOpen(false);
      setIsWorkbenchModelFilterActive(false);
      return;
    }

    if (event.key === 'Enter') {
      event.preventDefault();
      const firstModel = filteredWorkbenchModelOptions[0];
      if (isWorkbenchModelOpen && firstModel) {
        saveWorkbenchModelDraft((firstModel.id || firstModel.label || '').trim());
        return;
      }
      setIsWorkbenchModelOpen(false);
      setIsWorkbenchModelFilterActive(false);
      (event.target as HTMLInputElement).blur();
    }
  }

  function updateWorkbenchProviderDraft(providerId: string, patch: Partial<ContextWorkbenchProvider>) {
    setWorkbenchProviderDrafts((previous) => {
      const current =
        previous[providerId]
        || workbenchProviders.find((provider) => provider.id === providerId);
      if (!current) return previous;
      return {
        ...previous,
        [providerId]: {
          ...current,
          ...patch,
        },
      };
    });
    setSettingsError('');
  }

  function setWorkbenchProviderSyncError(providerId: string, errorText: string) {
    setWorkbenchProviderDrafts((previous) => {
      const current =
        previous[providerId]
        || workbenchProviders.find((provider) => provider.id === providerId);
      if (!current) return previous;
      return {
        ...previous,
        [providerId]: {
          ...current,
          last_sync_error: readableProviderError(errorText),
        },
      };
    });
  }

  function handleWorkbenchProviderSelect(event: MouseEvent<HTMLDivElement>, provider: ContextWorkbenchProvider) {
    event.preventDefault();
    event.stopPropagation();
    const nextProviderId = provider.id.trim();
    if (!nextProviderId) return;
    const nextModel = firstProviderModel(provider) || workbenchModelDraft;
    flushSync(() => {
      setWorkbenchProviderIdDraft(nextProviderId);
      setWorkbenchModelDraft(nextModel);
      setAvailableWorkbenchModels(provider.models || []);
      setWorkbenchApiKeyDraft('');
      setWorkbenchSavedApiKeyDraft('');
      setIsWorkbenchApiKeyDirty(false);
      setIsWorkbenchProviderOpen(false);
      setIsWorkbenchModelOpen(false);
      setHasFetchedWorkbenchModels(false);
      setIsWorkbenchModelFilterActive(false);
      setSettingsError('');
    });
    void handleSaveWorkbenchSettings({ providerId: nextProviderId, model: nextModel });
  }

  async function refreshProxyUsageSummary(targetSessionId = sessionId) {
    if (!targetSessionId) {
      onProxyUsageSummaryChange(null);
      return;
    }

    try {
      const response = await fetchProxySessionUsageRequest(targetSessionId);
      onProxyUsageSummaryChange(response.summary || null);
    } catch {
    }
  }

  async function handleSaveWorkbenchSettings(updates?: {
    model?: string;
    providerId?: string;
    provider?: Partial<ContextWorkbenchProvider> & {
      api_key?: string;
      clear_api_key?: boolean;
    };
    refreshModels?: boolean;
    thresholds?: { warningThreshold: number; criticalThreshold: number };
    locale?: UiLocale;
  }): Promise<boolean> {
    const nextModel = (updates?.model ?? workbenchModelDraft).trim();
    if (!nextModel) return false;
    const nextProviderId = (updates?.providerId ?? workbenchProviderIdDraft).trim() || DEFAULT_CONTEXT_PROVIDER_ID;
    const baseProvider =
      workbenchProviderDrafts[nextProviderId]
      || workbenchProviders.find((provider) => provider.id === nextProviderId)
      || selectedWorkbenchProvider;
    const providerPatch = updates?.provider || {};
    const nextProvider = baseProvider
      ? {
          ...baseProvider,
          ...providerPatch,
        }
      : null;

    const thresholds = updates?.thresholds ?? nextTokenThresholds;
    if (!updates?.thresholds && tokenThresholdError) {
      setSettingsError(tokenThresholdError);
      return false;
    }

    setSettingsError('');
    onTokenThresholdsChange(thresholds);

    try {
      const payload = {
        context_workbench_provider_id: nextProviderId,
        context_workbench_model: nextModel,
        ...(updates?.refreshModels ? { refresh_models: true } : {}),
        user_locale: updates?.locale ?? uiLocaleDraft,
      };
      const providerPayload = nextProvider
        ? {
            id: nextProvider.id,
            name: nextProvider.name,
            provider_type: nextProvider.provider_type,
            enabled: nextProvider.enabled,
            api_base_url: nextProvider.api_base_url,
            default_model: nextModel,
            models: nextProvider.models || [],
            ...(providerPatch.api_key ? { api_key: providerPatch.api_key } : {}),
            ...(providerPatch.clear_api_key ? { clear_api_key: true } : {}),
          }
        : null;
      const shouldSaveThresholds = !updates?.model || Boolean(updates?.thresholds);
      const response = await saveContextWorkbenchSettingsRequest(
        shouldSaveThresholds
          ? {
              ...payload,
              ...(providerPayload ? { response_providers: [providerPayload] } : {}),
              context_token_warning_threshold: thresholds.warningThreshold,
              context_token_critical_threshold: thresholds.criticalThreshold,
            }
          : {
              ...payload,
              ...(providerPayload ? { response_providers: [providerPayload] } : {}),
            },
      );
      applyWorkbenchProviderSettings(response, {
        preserveAvailableModels: !updates?.refreshModels && hasFetchedWorkbenchModels,
      });
      if (updates?.refreshModels) {
        const refreshedProvider = (response.providers || []).find((provider) => provider.id === nextProviderId);
        const syncError = readableProviderError(refreshedProvider?.last_sync_error || '');
        setHasFetchedWorkbenchModels(!syncError);
        setIsWorkbenchModelOpen(!syncError);
        setIsWorkbenchModelFilterActive(false);
      }
      return true;
    } catch (error) {
      const errorMessage = readableProviderError(getThrownMessage(error));
      if (updates?.refreshModels) {
        setWorkbenchProviderSyncError(nextProviderId, errorMessage);
      } else {
        setSettingsError(errorMessage);
      }
      return false;
    }
  }

  async function handleSaveWorkbenchProviderUrl() {
    if (!selectedWorkbenchProvider || selectedWorkbenchProvider.id === DEFAULT_CONTEXT_PROVIDER_ID) {
      return;
    }
    await handleSaveWorkbenchSettings({
      provider: {
        api_base_url: selectedWorkbenchProvider.api_base_url,
      },
    });
  }

  async function handleSaveWorkbenchApiKey() {
    if (!selectedWorkbenchProvider || selectedWorkbenchProvider.id === DEFAULT_CONTEXT_PROVIDER_ID) {
      return;
    }
    if (!isWorkbenchApiKeyDirty) {
      return;
    }
    const nextApiKey = workbenchApiKeyDraft.trim();
    const savedApiKey = workbenchSavedApiKeyDraft.trim();
    if (nextApiKey === savedApiKey) {
      setIsWorkbenchApiKeyDirty(false);
      return;
    }
    if (!nextApiKey && !selectedWorkbenchProvider.has_api_key && !savedApiKey) {
      setIsWorkbenchApiKeyDirty(false);
      return;
    }

    setIsWorkbenchApiKeySaving(true);
    try {
      const didSave = await handleSaveWorkbenchSettings({
        provider: nextApiKey
          ? { api_key: nextApiKey }
          : { clear_api_key: true },
      });
      if (didSave) {
        setWorkbenchSavedApiKeyDraft(nextApiKey);
        setIsWorkbenchApiKeyDirty(false);
      }
    } finally {
      setIsWorkbenchApiKeySaving(false);
    }
  }

  async function handleRefreshWorkbenchModels() {
    if (isWorkbenchModelsRefreshing) return;
    if (!selectedWorkbenchProvider) return;
    setSettingsError('');

    const providerId = selectedWorkbenchProvider.id;
    const isCodexProvider = providerId === DEFAULT_CONTEXT_PROVIDER_ID;
    const providerBaseUrl = (selectedWorkbenchProvider.api_base_url || '').trim();
    const pendingApiKey = workbenchApiKeyDraft.trim();
    const hasProviderApiKey = Boolean(pendingApiKey || (!isWorkbenchApiKeyDirty && selectedWorkbenchProvider.has_api_key));

    if (!isCodexProvider && !providerBaseUrl) {
      setWorkbenchProviderSyncError(providerId, uiText(uiLocaleDraft, 'Base URL is required before getting models.', '获取模型前需要填写 Base URL。'));
      return;
    }

    if (!isCodexProvider && !hasProviderApiKey) {
      setWorkbenchProviderSyncError(providerId, uiText(uiLocaleDraft, 'API Key is required before getting models.', '获取模型前需要填写 API Key。'));
      return;
    }

    setIsWorkbenchModelsRefreshing(true);
    setSettingsError('');
    try {
      const didRefresh = await handleSaveWorkbenchSettings({
        provider: pendingApiKey ? { api_key: pendingApiKey } : undefined,
        refreshModels: true,
      });
      if (didRefresh && pendingApiKey) {
        setWorkbenchSavedApiKeyDraft(pendingApiKey);
        setIsWorkbenchApiKeyDirty(false);
      }
    } finally {
      setIsWorkbenchModelsRefreshing(false);
    }
  }

  async function handleSaveUiLocale(nextLocale: UiLocale) {
    if (nextLocale === uiLocaleDraft) return;
    const previousLocale = uiLocaleDraft;
    setUiLocaleDraft(nextLocale);
    onUiLocaleChange?.(nextLocale);
    setSettingsError('');
    try {
      await saveContextWorkbenchSettingsRequest({ user_locale: nextLocale });
    } catch (error) {
      setUiLocaleDraft(previousLocale);
      onUiLocaleChange?.(previousLocale);
      setSettingsError(getThrownMessage(error));
    }
  }

  async function handleSaveThemeMode(nextThemeMode: 'light' | 'dark') {
    if (nextThemeMode === themeModeDraft) return;
    const previousThemeMode = themeModeDraft;
    setThemeModeDraft(nextThemeMode);
    onThemeModeChange?.(nextThemeMode);
    setSettingsError('');
    try {
      await saveContextWorkbenchSettingsRequest({ theme_mode: nextThemeMode });
    } catch (error) {
      setThemeModeDraft(previousThemeMode);
      onThemeModeChange?.(previousThemeMode);
      setSettingsError(getThrownMessage(error));
    }
  }

  function applyUiFontDraft(font: string, fontSizeValue = uiFontSizeDraft) {
    const size = Math.max(10, Math.min(32, Number.parseInt(fontSizeValue, 10) || 15));
    onUiFontChange?.(font.trim(), size);
  }

  async function handleSaveUiFont() {
    const font = uiFontDraft.trim();
    const size = Math.max(10, Math.min(32, Number.parseInt(uiFontSizeDraft, 10) || 15));
    setUiFontSizeDraft(String(size));
    onUiFontChange?.(font, size);
    setSettingsError('');
    try {
      await saveContextWorkbenchSettingsRequest({ ui_font: font, ui_font_size: size });
    } catch (error) {
      setSettingsError(getThrownMessage(error));
    }
  }

  async function handleSavePromptSetting(key: PromptSettingKey) {
    const nextValue = promptDrafts[key];
    if (nextValue === promptSavedDrafts[key]) return;
    setSettingsError('');
    try {
      const response = await saveContextWorkbenchSettingsRequest({ [key]: nextValue });
      const savedValue = response.settings[key] || '';
      setPromptSavedDrafts((previous) => ({
        ...previous,
        [key]: savedValue,
      }));
      setPromptDrafts((previous) => (previous[key] === nextValue ? { ...previous, [key]: savedValue } : previous));
      setPromptDefaults(promptDefaultsFromSettings(response.settings));
      applyWorkbenchProviderSettings(response, {
        preserveAvailableModels: hasFetchedWorkbenchModels,
      });
    } catch (error) {
      setSettingsError(getThrownMessage(error));
    }
  }

  async function handleResetPromptSetting(key: PromptSettingKey) {
    const nextValue = promptDefaults[key] || '';
    if (nextValue === promptDrafts[key] && nextValue === promptSavedDrafts[key]) return;
    setPromptDrafts((previous) => ({ ...previous, [key]: nextValue }));
    setSettingsError('');
    try {
      const response = await saveContextWorkbenchSettingsRequest({ [key]: nextValue });
      const savedValue = response.settings[key] || '';
      setPromptSavedDrafts((previous) => ({
        ...previous,
        [key]: savedValue,
      }));
      setPromptDrafts((previous) => (previous[key] === nextValue ? { ...previous, [key]: savedValue } : previous));
      setPromptDefaults(promptDefaultsFromSettings(response.settings));
      applyWorkbenchProviderSettings(response, {
        preserveAvailableModels: hasFetchedWorkbenchModels,
      });
    } catch (error) {
      setSettingsError(getThrownMessage(error));
    }
  }

  function renderPromptEditor(item: PromptSettingItem, expanded = false) {
    const expandLabel = expanded
      ? uiText(uiLocaleDraft, 'Close editor', '关闭输入框')
      : uiText(uiLocaleDraft, 'Expand editor', '展开输入框');
    const resetLabel = uiText(uiLocaleDraft, 'Reset', '重置');
    const closeButton = (
      <button
        aria-label={expandLabel}
        className="workbench-prompt-icon-btn"
        disabled={isSettingsLoading}
        title={expandLabel}
        type="button"
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          if (expanded) {
            setExpandedPromptKey(null);
            void handleSavePromptSetting(item.key);
            return;
          }
          setExpandedPromptKey(item.key);
          setSettingsError('');
        }}
      >
        <i className={`ph-light ${expanded ? 'ph-arrows-in-simple' : 'ph-arrows-out-simple'}`} />
      </button>
    );
    const resetButton = (
      <button
        className="workbench-prompt-reset-btn"
        disabled={isSettingsLoading || promptDrafts[item.key] === promptDefaults[item.key]}
        type="button"
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => void handleResetPromptSetting(item.key)}
      >
        {resetLabel}
      </button>
    );

    return (
      <div
        className={`workbench-prompt-editor${expanded ? ' is-expanded' : ''}`}
        key={item.key}
      >
        <div className="workbench-prompt-editor-header">
          <div className="workbench-prompt-editor-title">
            <span>{item.title}</span>
            <span className="workbench-prompt-token-count">
              {uiLocaleDraft.startsWith('zh') ? '：' : ': '}
              {promptTokenLabels[item.key]}
            </span>
          </div>
          <div className="workbench-prompt-editor-actions">
            {expanded ? resetButton : closeButton}
            {expanded ? closeButton : resetButton}
          </div>
        </div>
        <textarea
          className="settings-input workbench-prompt-textarea"
          disabled={isSettingsLoading}
          placeholder={item.placeholder}
          spellCheck={false}
          value={promptDrafts[item.key]}
          onBlur={() => void handleSavePromptSetting(item.key)}
          onChange={(event) => {
            setPromptDrafts((previous) => ({ ...previous, [item.key]: event.target.value }));
            setSettingsError('');
          }}
        />
        {expanded && settingsError ? (
          <div className="workbench-setting-feedback error">{settingsError}</div>
        ) : null}
      </div>
    );
  }

  function finalizeStoppedManualMessage(messageId: string) {
    updatePendingManualMessage(messageId, (lastMessage) => ({
      ...lastMessage,
      content: lastMessage.content.trim() ? lastMessage.content : 'Stopped this context model chat.',
      pending: false,
    }));
  }

  function handleStopManualMessage() {
    const controller = manualAbortControllerRef.current;
    if (!controller) {
      return;
    }

    manualStopRequestedRef.current = true;
    controller.abort();
  }

  async function handleSendManualMessage() {
    const nextMessage = manualDraft.trim();
    if (!nextMessage || isManualComposerLocked) {
      return;
    }

    const userMessage = createManualMessage('user', nextMessage);
    const pendingMessage = createManualMessage('assistant', '', { pending: true });

    setManualMessages((previous) => [...previous, userMessage, pendingMessage]);
    setManualDraft('');
    setIsManualSending(true);
    setIsManualReasoningOpen(false);
    manualStopRequestedRef.current = false;
    manualStopRequestRef.current = null;
    const streamController = new AbortController();
    manualAbortControllerRef.current = streamController;
    manualActiveSessionIdRef.current = '';

    try {
      const targetSessionId = sessionId || await onEnsureSession();
      if (!targetSessionId) {
        throw new Error('No available session');
      }

      manualActiveSessionIdRef.current = targetSessionId;
      if (streamController.signal.aborted) {
        throw new DOMException('Aborted', 'AbortError');
      }

      let streamError = '';
      let streamCompleted = false;
      let conversationCommit: Promise<void> = Promise.resolve();

      await streamContextChatRequest(
        {
          session_id: targetSessionId,
          message: nextMessage,
          selected_node_indexes: selectedNodeIndexes,
          reasoning_effort: manualReasoning,
        },
        (event) => {
          if (event.type === 'delta') {
            if (event.kind === 'reasoning') {
              return;
            }
            updatePendingManualMessage(pendingMessage.id, (lastMessage) => ({
              ...lastMessage,
              content: `${lastMessage.content}${event.delta}`,
              blocks: appendManualTextBlock(lastMessage.blocks, event.delta),
              pending: true,
            }));
            return;
          }

          if (event.type === 'reset') {
            updatePendingManualMessage(pendingMessage.id, (lastMessage) => ({
              ...lastMessage,
              pending: true,
            }));
            return;
          }

          if (event.type === 'reasoning_start' || event.type === 'reasoning_done') {
            return;
          }

          if (event.type === 'tool_event') {
            updatePendingManualMessage(pendingMessage.id, (lastMessage) => ({
              ...lastMessage,
              toolEvents: [...(lastMessage.toolEvents || []), event.tool_event],
              blocks: appendManualToolBlock(lastMessage.blocks, event.tool_event),
              pending: true,
              statusText: '',
            }));
            return;
          }

          if (event.type === 'finalizing') {
            updatePendingManualMessage(pendingMessage.id, (lastMessage) => ({
              ...lastMessage,
              pending: true,
              statusText: uiText(uiLocaleDraft, 'Updating context map...', '正在更新上下文地图...'),
            }));
            return;
          }

          if (event.type === 'error') {
            streamError = event.error;
            return;
          }

          streamCompleted = true;
          onContextWorkbenchChatChange(targetSessionId, event.history);
          conversationCommit = Promise.resolve(
            onConversationChange(targetSessionId, normalizeConversation(event.conversation), event.conversation),
          );
          setManualMessages(buildManualMessagesFromChat(event.history));
        },
        {
          signal: streamController.signal,
        },
      );

      if (streamError) {
        throw new Error(streamError);
      }

      if (!streamCompleted) {
        throw new Error(uiText(uiLocaleDraft, 'The streaming response ended unexpectedly.', '流式响应意外中断'));
      }

      await conversationCommit;
      await refreshProxyUsageSummary(targetSessionId);
    } catch (error) {
      if (manualStopRequestedRef.current || isAbortError(error)) {
        await manualStopRequestRef.current;
        finalizeStoppedManualMessage(pendingMessage.id);
        setManualFeedback('Stopped this context model chat.');
        setManualFeedbackError(false);
        return;
      }

      setManualMessages((previous) =>
        previous.map((item) =>
          item.id === pendingMessage.id
            ? {
                ...item,
                content: getThrownMessage(error),
                pending: false,
              }
            : item,
        ),
      );
    } finally {
      if (manualAbortControllerRef.current === streamController) {
        manualAbortControllerRef.current = null;
      }
      manualActiveSessionIdRef.current = '';
      manualStopRequestedRef.current = false;
      manualStopRequestRef.current = null;
      setIsManualSending(false);
    }
  }

  function handleManualDraftChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setManualDraft(event.target.value);
  }

  function handleManualDraftKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void handleSendManualMessage();
    }
  }

  async function handleCopyManualMessage(content: string) {
    try {
      await copyText(content);
      setManualFeedback('');
      setManualFeedbackError(false);
    } catch (error) {
      setManualFeedback(getThrownMessage(error));
      setManualFeedbackError(true);
    }
  }

  async function handleClearManualChat() {
    if (!sessionId || isManualComposerLocked || !hasClearableManualChat) return;
    try {
      const response = await clearContextWorkbenchChatRequest(sessionId);
      onContextWorkbenchChatChange(sessionId, response.history);
      await onConversationChange(sessionId, normalizeConversation(response.conversation), response.conversation);
      setManualMessages(buildManualMessagesFromChat(response.history));
      setManualFeedback('');
      setManualFeedbackError(false);
    } catch (error) {
      setManualFeedback(getThrownMessage(error));
      setManualFeedbackError(true);
    }
  }

  async function handleClearUsageSummary() {
    if (!sessionId || isUsageClearing) return;
    setIsUsageClearing(true);
    setUsageFeedback('');
    setUsageFeedbackError(false);
    try {
      await resetProxyUsageRequest(sessionId);
      const refreshed = await fetchProxySessionUsageRequest(sessionId);
      onProxyUsageSummaryChange(refreshed.summary || null);
      setUsageFeedback(uiText(uiLocaleDraft, 'Usage count reset for this session.', '已清空这个会话的用量计数。'));
    } catch (error) {
      setUsageFeedback(getThrownMessage(error));
      setUsageFeedbackError(true);
    } finally {
      setIsUsageClearing(false);
    }
  }

  return (
    <>
      <div className="extended-header">
        {WORKBENCH_TABS.map((tab) => (
          <button
            aria-pressed={activeTab === tab.id}
            className={`extended-tab ${activeTab === tab.id ? 'active' : ''}`}
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
          >
            <i className={`ph-light ${tab.icon}`} />
            <span>{workbenchTabLabel(tab.id, uiLocaleDraft)}</span>
          </button>
        ))}
      </div>

      <div className="extended-content">
        <div
          className="extended-track"
          style={{
            transform: `translateX(-${WORKBENCH_TABS.findIndex((tab) => tab.id === activeTab) * 100}%)`,
          }}
        >
          <section className="extended-page" data-page="suggestions">
            <div className="extended-page-scroll">
              <div className="workbench-panel-title">{uiText(uiLocaleDraft, 'Token Overview', 'Token 概览')}</div>
              <div className="workbench-panel-desc">
                {uiText(uiLocaleDraft, 'Review the current context token usage before deciding whether to edit it manually.', '查看当前上下文的 token 使用情况，再决定是否手动处理。')}
              </div>

              <div className="suggestion-card-grid">
                <div className="suggestion-card">
                  <div className="suggestion-card-label">{uiText(uiLocaleDraft, 'Total Tokens', '总 Token 数')}</div>
                  <div className="suggestion-card-value">{formatTokenCount(localSuggestionStats.total_token_count)}</div>
                  <div className="suggestion-card-note">{uiText(uiLocaleDraft, 'Counts the content currently shown in the context map.', '统计当前上下文地图中的节点内容。')}</div>
                </div>
                <div className="suggestion-card">
                  <div className="suggestion-card-label">{uiText(uiLocaleDraft, 'Tool Call Tokens', '工具调用 Token')}</div>
                  <div className="suggestion-card-value">{formatTokenCount(localSuggestionStats.tool_token_count)}</div>
                  <div className="suggestion-card-note">{uiText(uiLocaleDraft, 'Counts tool display content and tool outputs.', '统计工具展示内容和工具输出。')}</div>
                </div>
                <div className="suggestion-card">
                  <div className="suggestion-card-label">{uiText(uiLocaleDraft, 'Current Focus', '当前聚焦')}</div>
                  <div className="suggestion-card-value">{selectedNodeNumbers.length || uiText(uiLocaleDraft, 'All', '全部')}</div>
                  <div className="suggestion-card-note">
                    {selectedNodeReferenceSegments.length
                      ? uiText(uiLocaleDraft, `The manual page will prioritize nodes #${selectedNodeReferenceSegments.join(' / ')}.`, `手动页会优先围绕节点 #${selectedNodeReferenceSegments.join(' / ')}。`)
                      : uiText(uiLocaleDraft, 'No nodes are selected, so the manual page will use the full context.', '当前没有单独选中节点，所以手动页会基于完整上下文处理。')}
                  </div>
                </div>
              </div>

              <div className="suggestion-stack">
                <div className="workbench-setting-card">
                  <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Node Token Details', '节点 Token 明细')}</div>
                  <div className="workbench-setting-desc">{uiText(uiLocaleDraft, 'Only red nodes from the minimap are shown here.', '这里仅显示 minimap 里的红色节点。')}</div>

                  {criticalSuggestionNodes.length ? (
                    criticalSuggestionNodes.map((node) => (
                      <div className="suggestion-row" key={node.node_index}>
                        <div className="suggestion-row-copy">
                          <div className="suggestion-row-title">{uiText(uiLocaleDraft, 'Node', '节点')} #{node.node_number}</div>
                          <div className="suggestion-row-meta">
                            {formatSuggestionRoleLabel(node.role, uiLocaleDraft)} - {formatTokenCount(node.token_count)} Token
                            {node.tool_token_count > 0
                              ? ` - ${uiText(uiLocaleDraft, 'Tool call', '工具调用')} ${formatTokenCount(node.tool_token_count)} Token`
                              : ''}
                          </div>
                        </div>
                      </div>
                    ))
                  ) : localSuggestionNodes.length ? (
                    <div className="suggestion-row">
                      <div className="suggestion-row-title">{uiText(uiLocaleDraft, 'No red nodes right now', '当前没有红色节点')}</div>
                    </div>
                  ) : (
                    <div className="suggestion-row">
                      <div className="suggestion-row-title">{uiText(uiLocaleDraft, 'No nodes to count yet', '当前还没有可统计的节点')}</div>
                      <div className="suggestion-row-body">{uiText(uiLocaleDraft, 'Nodes will appear here once the main chat has real context.', '等主聊天里有实际上下文之后，这里会列出每个节点的 Token 数。')}</div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </section>

          <section className="extended-page" data-page="manual">
            <div className="manual-workbench">
              <div className="manual-workbench-list" ref={manualListRef}>
                {manualMessages.length ? (
                  manualMessages.map((entry) => (
                    <ManualMessageItem
                      entry={entry}
                      key={entry.id}
                      uiLocale={uiLocaleDraft}
                      onCopy={(content) => void handleCopyManualMessage(content)}
                    />
                  ))
                ) : (
                  <ManualEmptyState uiLocale={uiLocaleDraft} />
                )}
              </div>

              <div className="manual-workbench-composer">
                <div className="manual-workbench-composer-shell">
                  {manualFeedback ? (
                    <div className={`workbench-setting-feedback${manualFeedbackError ? ' error' : ''}`}>
                      {manualFeedback}
                    </div>
                  ) : null}

                  {selectedNodeReferenceSegments.length ? (
                    <div className="manual-workbench-reference-strip">
                      {selectedNodeReferenceSegments.map((segment) => (
                        <span className="manual-workbench-reference-chip" key={segment}>
                          {uiText(uiLocaleDraft, 'Node', '节点')} #{segment}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  <div className="manual-workbench-toolbar">
                    <Dropdown
                      buttonClassName="tool-btn-capsule manual-workbench-reasoning"
                      buttonChildren={(
                        <>
                          <i className="ph-light ph-brain" />
                          <span>{uiText(uiLocaleDraft, 'Reasoning', '思考')}: {currentManualReasoningLabel}</span>
                        </>
                      )}
                      disabled={manualReasoningDisabled}
                      isOpen={isManualReasoningOpen}
                      onClose={() => setIsManualReasoningOpen(false)}
                      onToggle={() => setIsManualReasoningOpen((previous) => !previous)}
                    >
                      {reasoningOptions.map((option) => (
                        <div
                          className={`dropdown-item ${option.value === manualReasoning ? 'selected' : ''}`}
                          key={option.value}
                          onMouseDown={(event) => handleManualReasoningSelect(event, option)}
                        >
                          <span>{reasoningDisplayLabel(option.value, option.label, uiLocaleDraft)}</span>
                          {option.value === manualReasoning ? <i className="ph-bold ph-check" /> : null}
                        </div>
                      ))}
                    </Dropdown>
                  </div>

                  <div className="manual-workbench-input-row">
                    <textarea
                      className="manual-workbench-input"
                      disabled={isManualComposerLocked}
                      onChange={handleManualDraftChange}
                      onKeyDown={handleManualDraftKeyDown}
                      placeholder={isMainChatBusy
                        ? uiText(uiLocaleDraft, 'The main chat is still running...', '主聊天还在运行...')
                        : uiText(uiLocaleDraft, 'Ask what is too long, or what should be kept...', '询问哪里太长，或者哪些内容应该保留...')}
                      ref={manualTextareaRef}
                      rows={1}
                      value={manualDraft}
                    />
                    {hasClearableManualChat ? (
                      <button
                        aria-label={uiText(uiLocaleDraft, 'Clear context model chat', '清空上下文模型对话')}
                        className="manual-workbench-clear"
                        disabled={isManualSending}
                        title={uiText(uiLocaleDraft, 'Clear context model chat', '清空上下文模型对话')}
                        type="button"
                        onClick={() => void handleClearManualChat()}
                      >
                        <i className="ph-light ph-broom" />
                      </button>
                    ) : null}
                    <button
                      aria-label={isManualSending
                        ? uiText(uiLocaleDraft, 'Stop context model chat', '停止上下文模型对话')
                        : uiText(uiLocaleDraft, 'Send context model message', '发送上下文模型消息')}
                      className={`send-btn manual-workbench-send ${isManualSending ? 'is-stop-action' : 'is-send-action'}`}
                      disabled={isManualSending ? false : (!manualDraft.trim() || isManualComposerLocked)}
                      type="button"
                      onClick={() => {
                        if (isManualSending) {
                          handleStopManualMessage();
                        } else {
                          void handleSendManualMessage();
                        }
                      }}
                    >
                      <i className={`ph-light ${isManualSending ? 'ph-stop' : 'ph-paper-plane-tilt'}`} />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section className="extended-page" data-page="usage">
            <div className="extended-page-scroll">
              <div className="workbench-panel-title">{uiText(uiLocaleDraft, 'Session Usage', '会话用量')}</div>
              <div className="workbench-panel-desc">
                {uiText(
                  uiLocaleDraft,
                  'Historical usage recorded from real Responses API usage returned through the Codex proxy.',
                  '从 Codex 代理收到的真实 Responses API usage 中累计记录的历史用量。',
                )}
              </div>
              <div className="workbench-setting-control-row usage-actions-row">
                <button
                  className="tool-btn-capsule"
                  disabled={isUsageClearing || !sessionId}
                  type="button"
                  onClick={() => void handleClearUsageSummary()}
                >
                  <i className={`ph-light ${isUsageClearing ? 'ph-circle-notch' : 'ph-broom'}`} />
                  <span>
                    {isUsageClearing
                      ? uiText(uiLocaleDraft, 'Resetting...', '正在清空...')
                      : uiText(uiLocaleDraft, 'Reset Count', '重新计数')}
                  </span>
                </button>
              </div>
              {usageFeedback ? (
                <div className={`workbench-setting-feedback${usageFeedbackError ? ' error' : ''}`}>
                  {usageFeedback}
                </div>
              ) : null}

              <div className="usage-page-stack">
                <UsageSummaryCard
                  description={uiText(uiLocaleDraft, 'All model calls recorded for this session.', '这个会话中已经发生过的所有模型调用。')}
                  summary={proxyUsageSummary}
                  title={uiText(uiLocaleDraft, 'Total Session Usage', '会话历史总消耗')}
                  uiLocale={uiLocaleDraft}
                />

                <UsageSummaryCard
                  compact
                  description={uiText(uiLocaleDraft, 'Token usage from the model that inspects, compresses, replaces, or deletes context.', '用于检查、压缩、替换或删除上下文的模型消耗。')}
                  summary={contextWorkbenchUsageSummary}
                  title={uiText(uiLocaleDraft, 'Context Model Usage', '上下文模型消耗')}
                  uiLocale={uiLocaleDraft}
                />

                {mainUsageSummary ? (
                  <UsageSummaryCard
                    compact
                    description={uiText(uiLocaleDraft, 'Token usage from normal Codex task requests.', '正常 Codex 任务请求产生的模型消耗。')}
                    summary={mainUsageSummary}
                    title={uiText(uiLocaleDraft, 'Main Codex Usage', '主 Codex 消耗')}
                    uiLocale={uiLocaleDraft}
                  />
                ) : null}
              </div>
            </div>
          </section>

          <section className="extended-page" data-page="settings">
            <div className={`extended-page-scroll${expandedPromptItem ? ' has-expanded-prompt' : ''}`}>
              {expandedPromptItem ? (
                renderPromptEditor(expandedPromptItem, true)
              ) : (
                <>
                  <div className="workbench-panel-title">{uiText(uiLocaleDraft, 'Prompts', '提示词')}</div>

                  <div className="workbench-prompts-panel">
                    <div className="workbench-prompt-list">
                      {promptSettingItems.map((item) => renderPromptEditor(item))}
                    </div>
                  </div>

                  <div className="workbench-panel-title workbench-settings-title">
                    {uiText(uiLocaleDraft, 'Context Model', '上下文模型')}
                  </div>

                  <div className="workbench-settings-panel workbench-context-model-panel">
                    <SettingsRow
                      meta={currentWorkbenchProviderGroup}
                      title={uiText(uiLocaleDraft, 'Provider Type', '服务商类型')}
                    >
                      <div className="workbench-setting-control-row">
                        <Dropdown
                          align="right"
                          buttonClassName="tool-btn-capsule manual-workbench-reasoning workbench-provider-picker-trigger"
                          buttonChildren={(
                            <>
                              <i className="ph-light ph-cpu" />
                              <span>{currentWorkbenchProviderLabel}</span>
                              <i className="ph-light ph-caret-down" />
                            </>
                          )}
                          disabled={isSettingsLoading || orderedWorkbenchProviders.length === 0}
                          isOpen={isWorkbenchProviderOpen}
                          onClose={() => setIsWorkbenchProviderOpen(false)}
                          onToggle={() => {
                            setIsWorkbenchProviderOpen((previous) => !previous);
                            setSettingsError('');
                          }}
                        >
                          {orderedWorkbenchProviders.map((provider) => (
                            <div
                              className={`dropdown-item ${provider.id === workbenchProviderIdDraft ? 'selected' : ''}`}
                              key={provider.id}
                              onMouseDown={(event) => handleWorkbenchProviderSelect(event, provider)}
                            >
                              <div className="dropdown-item-left">
                                <span>{providerDisplayName(provider, uiLocaleDraft)}</span>
                                <small>{providerGroupLabel(provider, uiLocaleDraft)}</small>
                              </div>
                              {provider.id === workbenchProviderIdDraft ? <i className="ph-bold ph-check" /> : null}
                            </div>
                          ))}
                        </Dropdown>
                      </div>
                    </SettingsRow>

                        {showExternalProviderFields ? (
                      <>
                        <SettingsRow title={uiText(uiLocaleDraft, 'Base URL', 'Base URL')}>
                          <div className="workbench-field-stack">
                            <div className="workbench-setting-control-row">
                              <input
                                className="settings-input settings-input-url"
                                disabled={isSettingsLoading || !selectedWorkbenchProvider}
                                placeholder={defaultBaseUrlForProviderType(currentWorkbenchProviderType)}
                                type="text"
                                value={selectedWorkbenchProvider?.api_base_url || ''}
                                onBlur={() => void handleSaveWorkbenchProviderUrl()}
                                onChange={(event) => {
                                  if (!selectedWorkbenchProvider) return;
                                  updateWorkbenchProviderDraft(selectedWorkbenchProvider.id, {
                                    api_base_url: event.target.value,
                                    last_sync_error: '',
                                  });
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
                                }}
                              />
                            </div>
                            {providerSyncErrorText && providerSyncErrorPlacement === 'base_url' ? (
                              <div className="workbench-field-feedback error" role="alert">
                                {providerSyncErrorText}
                              </div>
                            ) : null}
                          </div>
                        </SettingsRow>

                        <SettingsRow title={uiText(uiLocaleDraft, 'API Key', 'API Key')}>
                          <div className="workbench-field-stack">
                            <div className="workbench-api-key-row">
                              <input
                                className="settings-input settings-input-key"
                                disabled={isSettingsLoading || isWorkbenchApiKeySaving || !selectedWorkbenchProvider}
                                placeholder="sk-..."
                                type="password"
                                value={workbenchApiKeyDraft}
                                onBlur={() => void handleSaveWorkbenchApiKey()}
                                onChange={(event) => {
                                  setWorkbenchApiKeyDraft(event.target.value);
                                  setIsWorkbenchApiKeyDirty(true);
                                  if (selectedWorkbenchProvider) {
                                    setWorkbenchProviderSyncError(selectedWorkbenchProvider.id, '');
                                  }
                                  setSettingsError('');
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
                                }}
                              />
                            </div>
                            {providerSyncErrorText && providerSyncErrorPlacement === 'api_key' ? (
                              <div className="workbench-field-feedback error" role="alert">
                                {providerSyncErrorText}
                              </div>
                            ) : null}
                          </div>
                        </SettingsRow>
                      </>
                    ) : null}

                    <SettingsRow title={uiText(uiLocaleDraft, 'Model', '模型')}>
                      <div className="workbench-field-stack">
                        <div className="workbench-model-config-row">
                          <div className={`workbench-model-combobox${isWorkbenchModelOpen ? ' is-open' : ''}`} ref={workbenchModelComboboxRef}>
                            <input
                              aria-autocomplete="list"
                              aria-controls="context-workbench-model-options"
                              aria-expanded={isWorkbenchModelOpen}
                              className="settings-input settings-input-model"
                              disabled={isSettingsLoading}
                              placeholder={firstProviderModel(selectedWorkbenchProvider) || DEFAULT_WORKBENCH_MODELS[0]}
                              role="combobox"
                              type="text"
                              value={workbenchModelDraft}
                              onBlur={() => void handleSaveWorkbenchSettings()}
                              onChange={(event) => {
                                setWorkbenchModelDraft(event.target.value);
                                setIsWorkbenchModelOpen(true);
                                setIsWorkbenchModelFilterActive(true);
                                setSettingsError('');
                              }}
                              onFocus={() => {
                                setIsWorkbenchModelOpen(true);
                                setIsWorkbenchModelFilterActive(false);
                                setSettingsError('');
                              }}
                              onKeyDown={handleWorkbenchModelKeyDown}
                            />
                            <i className="ph-light ph-caret-down workbench-model-combobox-icon" />
                            <div
                              className={`dropdown-menu workbench-model-suggestion-menu ${isWorkbenchModelOpen ? 'show' : ''}`}
                              id="context-workbench-model-options"
                              role="listbox"
                              onMouseDown={(event) => event.stopPropagation()}
                            >
                              {!hasFetchedWorkbenchModels ? (
                                <div className="workbench-model-suggestion-empty">
                                  {uiText(uiLocaleDraft, 'Click Get all models first', '先点击获取所有模型')}
                                </div>
                              ) : filteredWorkbenchModelOptions.length ? (
                                filteredWorkbenchModelOptions.map((model) => {
                                  const modelId = (model.id || model.label || '').trim();
                                  return (
                                    <button
                                      aria-selected={modelId === workbenchModelDraft}
                                      className={`dropdown-item workbench-model-suggestion-option ${modelId === workbenchModelDraft ? 'selected' : ''}`}
                                      key={modelId}
                                      role="option"
                                      type="button"
                                      onMouseDown={(event) => handleWorkbenchModelSelect(event, model)}
                                    >
                                      <div className="dropdown-item-left">
                                        <span>{model.label || modelId}</span>
                                      </div>
                                      {modelId === workbenchModelDraft ? <i className="ph-bold ph-check" /> : null}
                                    </button>
                                  );
                                })
                              ) : (
                                <div className="workbench-model-suggestion-empty">
                                  {uiText(uiLocaleDraft, 'No matching models', '没有匹配的模型')}
                                </div>
                              )}
                            </div>
                          </div>

                          <button
                            aria-label={uiText(uiLocaleDraft, 'Get all models', '获取所有模型')}
                            className="workbench-icon-action"
                            disabled={isSettingsLoading || isWorkbenchModelsRefreshing}
                            title={uiText(uiLocaleDraft, 'Get all models', '获取所有模型')}
                            type="button"
                            onClick={() => void handleRefreshWorkbenchModels()}
                          >
                            <i className={`ph-light ph-circle-notch${isWorkbenchModelsRefreshing ? ' is-spinning' : ''}`} />
                          </button>
                        </div>
                        {providerSyncErrorText && providerSyncErrorPlacement === 'model' ? (
                          <div className="workbench-field-feedback error" role="alert">
                            {providerSyncErrorText}
                          </div>
                        ) : null}
                      </div>
                    </SettingsRow>
                  </div>

                  <div className="workbench-panel-title workbench-settings-title">
                    {uiText(uiLocaleDraft, 'Workspace Settings', '工作区设置')}
                  </div>

                  <div className="workbench-settings-panel">
                <SettingsRow title={uiText(uiLocaleDraft, 'Language', '语言')}>
                  <div className="workbench-language-toggle" role="group" aria-label={uiText(uiLocaleDraft, 'Language', '语言')}>
                    {UI_LANGUAGE_OPTIONS.map((option) => (
                      <button
                        key={option.value}
                        type="button"
                        className={`workbench-language-btn${uiLocaleDraft === option.value ? ' is-active' : ''}`}
                        disabled={isSettingsLoading}
                        aria-pressed={uiLocaleDraft === option.value}
                        onClick={() => {
                          void handleSaveUiLocale(option.value);
                        }}
                      >
                        {uiLanguageLabel(option.value, uiLocaleDraft)}
                      </button>
                    ))}
                  </div>
                </SettingsRow>

                <SettingsRow title={uiText(uiLocaleDraft, 'Theme', '主题')}>
                  <div className="workbench-language-toggle" role="group" aria-label={uiText(uiLocaleDraft, 'Theme', '主题')}>
                    {(['light', 'dark'] as const).map((option) => (
                      <button
                        key={option}
                        type="button"
                        className={`workbench-language-btn${themeModeDraft === option ? ' is-active' : ''}`}
                        disabled={isSettingsLoading}
                        aria-pressed={themeModeDraft === option}
                        onClick={() => {
                          void handleSaveThemeMode(option);
                        }}
                      >
                        {option === 'light'
                          ? uiText(uiLocaleDraft, 'Light', '浅色')
                          : uiText(uiLocaleDraft, 'Dark', '深色')}
                      </button>
                    ))}
                  </div>
                </SettingsRow>

                <SettingsRow title={uiText(uiLocaleDraft, 'Token Color Thresholds', 'Token 颜色阈值')}>
                  <div className="workbench-setting-control-row">
                    <label className="workbench-token-threshold-field">
                      <span>{uiText(uiLocaleDraft, '黄', '黄')}</span>
                      <input
                        className="settings-input settings-input-small"
                        disabled={isSettingsLoading}
                        min={0}
                        step={100}
                        type="number"
                        value={tokenWarningThresholdDraft}
                        onChange={(event) => {
                          setTokenWarningThresholdDraft(event.target.value);
                          setSettingsError('');
                        }}
                        onBlur={() => {
                          if (!tokenThresholdError) void handleSaveWorkbenchSettings();
                        }}
                      />
                    </label>

                    <label className="workbench-token-threshold-field">
                      <span>{uiText(uiLocaleDraft, '红', '红')}</span>
                      <input
                        className="settings-input settings-input-small"
                        disabled={isSettingsLoading}
                        min={1}
                        step={100}
                        type="number"
                        value={tokenCriticalThresholdDraft}
                        onChange={(event) => {
                          setTokenCriticalThresholdDraft(event.target.value);
                          setSettingsError('');
                        }}
                        onBlur={() => {
                          if (!tokenThresholdError) void handleSaveWorkbenchSettings();
                        }}
                      />
                    </label>
                  </div>
                </SettingsRow>

                <SettingsRow title={uiText(uiLocaleDraft, 'UI Font', '界面字体')}>
                  <div className="workbench-setting-control-row">
                    <input
                      className="settings-input settings-input-font"
                      disabled={isSettingsLoading}
                      placeholder="Noto Serif SC"
                      type="text"
                      value={uiFontDraft}
                      onChange={(event) => {
                        const nextFont = event.target.value;
                        setUiFontDraft(nextFont);
                        applyUiFontDraft(nextFont);
                      }}
                      onBlur={() => void handleSaveUiFont()}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
                      }}
                    />
                  </div>
                </SettingsRow>

                <SettingsRow title={uiText(uiLocaleDraft, 'Font Size', '字体大小')}>
                  <div className="workbench-setting-control-row">
                    <input
                      className="settings-input settings-input-small"
                      disabled={isSettingsLoading}
                      min={10}
                      max={32}
                      step={1}
                      type="number"
                      value={uiFontSizeDraft}
                      onChange={(event) => {
                        const nextSize = event.target.value;
                        setUiFontSizeDraft(nextSize);
                        applyUiFontDraft(uiFontDraft, nextSize);
                      }}
                      onBlur={() => void handleSaveUiFont()}
                    />
                  </div>
                </SettingsRow>
              </div>

              {settingsError ? <div className="workbench-setting-feedback error">{settingsError}</div> : null}
              {tokenThresholdError ? <div className="workbench-setting-feedback error">{tokenThresholdError}</div> : null}
                </>
              )}
            </div>
          </section>
        </div>
      </div>

    </>
  );
}
