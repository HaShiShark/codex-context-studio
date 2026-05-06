import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, MouseEvent } from 'react';
import { flushSync } from 'react-dom';

import {
  cancelActiveRequest,
  clearContextWorkbenchHistoryRequest,
  deleteContextWorkbenchMessageRequest,
  fetchContextWorkbenchSettings,
  fetchContextWorkbenchSuggestionsRequest,
  restoreContextRevisionRequest,
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
  ContextRevisionSummary,
  ContextWorkbenchHistoryEntry,
  ContextWorkbenchSuggestionNode,
  ContextWorkbenchSuggestionStats,
  ContextWorkbenchToolCatalogItem,
  MessageRecord,
  PendingContextRestore,
  ReasoningOption,
  ResponseProviderDraft,
  ResponseProviderModel,
  ResponseProviderSettings,
} from '../types';
import { normalizeSupportedLocale, type UiLocale } from '../i18n';
import { copyText, getReasoningLabel, normalizeConversation } from '../utils';
import ChatModelPicker from './ChatModelPicker';
import Dropdown from './Dropdown';
import MarkdownRenderer from './MarkdownRenderer';

type WorkbenchTab = 'suggestions' | 'manual' | 'restore' | 'settings';
const WORKBENCH_RESTORE_ENABLED = false;

interface ManualWorkbenchMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  pending?: boolean;
}

interface ContextWorkbenchProps {
  messages: MessageRecord[];
  messageTokenStats: ContextMessageTokenStat[];
  selectedNodeIndexes: number[];
  criticalNodeIndexes: number[];
  tokenThresholds: ContextTokenThresholds;
  sessionId: string;
  isMainChatBusy: boolean;
  history: ContextWorkbenchHistoryEntry[];
  revisions: ContextRevisionSummary[];
  pendingRestore: PendingContextRestore | null;
  reasoningOptions: ReasoningOption[];
  uiLocale: UiLocale;
  onHistoryChange: (sessionId: string, history: ContextWorkbenchHistoryEntry[]) => void;
  onConversationChange: (
    sessionId: string,
    conversation: MessageRecord[],
    options?: { resetProxyOverride?: boolean },
  ) => void | Promise<void>;
  onRevisionHistoryChange: (sessionId: string, revisions: ContextRevisionSummary[]) => void;
  onPendingRestoreChange: (sessionId: string, pendingRestore: PendingContextRestore | null) => void;
  onEnsureSession: () => Promise<string>;
  onTokenThresholdsChange: (thresholds: ContextTokenThresholds) => void;
  onUiLocaleChange?: (locale: UiLocale) => void;
}

const DEFAULT_WORKBENCH_MODELS = ['gpt-5.4-mini', 'gpt-5.4', 'gpt-5.2'];
const DEFAULT_WORKBENCH_PROVIDER_ID = 'codex-proxy';
const EMPTY_SUGGESTION_STATS: ContextWorkbenchSuggestionStats = {
  total_token_count: 0,
  tool_token_count: 0,
};

const WORKBENCH_TABS: Array<{
  id: WorkbenchTab;
  label: string;
  icon: string;
}> = [
  { id: 'suggestions', label: 'Suggestions', icon: 'ph-lightbulb' },
  { id: 'manual', label: 'Manual', icon: 'ph-hand-pointing' },
  ...(WORKBENCH_RESTORE_ENABLED
    ? [{ id: 'restore' as const, label: 'Restore', icon: 'ph-arrow-counter-clockwise' }]
    : []),
  { id: 'settings', label: 'Settings', icon: 'ph-gear' },
];

const UI_LANGUAGE_OPTIONS: Array<{ value: UiLocale }> = [
  { value: 'en-US' },
  { value: 'zh-CN' },
];

function uiText(locale: UiLocale, english: string, chinese: string) {
  return locale === 'zh-CN' ? chinese : english;
}

function uiLanguageLabel(value: UiLocale, locale: UiLocale) {
  if (value === 'en-US') {
    return uiText(locale, 'English', '英文');
  }
  return uiText(locale, 'Simplified Chinese', '简体中文');
}

function workbenchTabLabel(tab: WorkbenchTab, locale: UiLocale) {
  switch (tab) {
    case 'suggestions':
      return uiText(locale, 'Suggestions', '建议');
    case 'manual':
      return uiText(locale, 'Manual', '手动');
    case 'restore':
      return uiText(locale, 'Restore', '恢复');
    case 'settings':
      return uiText(locale, 'Settings', '设置');
    default:
      return tab;
  }
}

function reasoningDisplayLabel(value: string, fallbackLabel: string, locale: UiLocale) {
  switch (value) {
    case 'default':
      return uiText(locale, 'Auto', '自动');
    case 'none':
      return uiText(locale, 'Off', '关闭');
    case 'minimal':
      return uiText(locale, 'Minimal', '极简');
    case 'low':
      return uiText(locale, 'Low', '低');
    case 'medium':
      return uiText(locale, 'Medium', '中');
    case 'high':
      return uiText(locale, 'High', '高');
    case 'xhigh':
      return uiText(locale, 'Extra High', '超高');
    default:
      return fallbackLabel;
  }
}

function createManualMessage(
  role: ManualWorkbenchMessage['role'],
  content: string,
  options: Partial<ManualWorkbenchMessage> = {},
): ManualWorkbenchMessage {
  return {
    id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    role,
    content,
    pending: false,
    ...options,
  };
}

function buildManualMessagesFromHistory(history: ContextWorkbenchHistoryEntry[]): ManualWorkbenchMessage[] {
  if (!history.length) {
    return [];
  }

  return history.map((entry, index) =>
    createManualMessage(entry.role, entry.content, {
      id: `history-${index}-${entry.role}`,
    }),
  );
}

function getThrownMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function formatNodeReferenceSegments(nodeNumbers: number[]) {
  if (!nodeNumbers.length) {
    return [];
  }

  const segments: string[] = [];
  let rangeStart = nodeNumbers[0];
  let previous = nodeNumbers[0];

  for (let index = 1; index < nodeNumbers.length; index += 1) {
    const current = nodeNumbers[index];
    if (current === previous + 1) {
      previous = current;
      continue;
    }

    segments.push(rangeStart === previous ? `${rangeStart}` : `${rangeStart}-${previous}`);
    rangeStart = current;
    previous = current;
  }

  segments.push(rangeStart === previous ? `${rangeStart}` : `${rangeStart}-${previous}`);
  return segments;
}

function statusLabel(status: ContextWorkbenchToolCatalogItem['status'], locale: UiLocale) {
  return status === 'available'
    ? uiText(locale, 'Available', '可用')
    : uiText(locale, 'Preview', '预览');
}

function toWorkbenchProviderDraft(provider: ResponseProviderSettings): ResponseProviderDraft {
  return {
    id: provider.id,
    name: provider.name,
    provider_type: provider.provider_type,
    enabled: provider.enabled,
    supports_model_fetch: provider.supports_model_fetch,
    supports_responses: provider.supports_responses,
    api_base_url: provider.api_base_url || '',
    api_key_input: '',
    clear_api_key: false,
    default_model: provider.default_model || '',
    models: Array.isArray(provider.models) ? provider.models : [],
    last_sync_at: provider.last_sync_at || '',
    last_sync_error: provider.last_sync_error || '',
  };
}

function inferWorkbenchProviderId(modelId: string, providers: ResponseProviderDraft[]) {
  const cleanedModelId = modelId.trim();
  if (cleanedModelId) {
    const matchedProvider = providers.find((provider) =>
      provider.models.some((model) => (model.id || '').trim() === cleanedModelId),
    );
    if (matchedProvider) {
      return matchedProvider.id;
    }
  }

  return providers.find((provider) => provider.enabled && provider.models.length > 0)?.id || 'openai';
}

function resolveWorkbenchSelection(modelId: string, providerId: string, providers: ResponseProviderDraft[]) {
  const cleanedModelId = modelId.trim();
  const cleanedProviderId = providerId.trim();
  const matchedProvider = providers.find((provider) => provider.id === cleanedProviderId);
  const matchedModel =
    matchedProvider?.models.find((model) => (model.id || '').trim() === cleanedModelId) ||
    providers.find((provider) => provider.models.some((model) => (model.id || '').trim() === cleanedModelId))
      ?.models.find((model) => (model.id || '').trim() === cleanedModelId);

  if (matchedModel) {
    return {
      providerId: matchedProvider?.models.some((model) => (model.id || '').trim() === cleanedModelId)
        ? matchedProvider.id
        : inferWorkbenchProviderId(cleanedModelId, providers),
      modelId: matchedModel.id || matchedModel.label || DEFAULT_WORKBENCH_MODELS[0],
    };
  }

  const fallbackProvider = providers.find((provider) => provider.enabled && provider.models.length > 0);
  return {
    providerId: fallbackProvider?.id || cleanedProviderId || DEFAULT_WORKBENCH_PROVIDER_ID,
    modelId: fallbackProvider?.default_model || fallbackProvider?.models[0]?.id || cleanedModelId || DEFAULT_WORKBENCH_MODELS[0],
  };
}

function workbenchProviderName(provider: ResponseProviderDraft | undefined) {
  if (!provider) {
    return 'No provider selected';
  }
  return provider.name.trim() || provider.id;
}

function formatChangeTypeLabel(changeType: string) {
  switch (changeType) {
    case 'delete':
      return 'Delete';
    case 'replace':
      return 'Replace';
    case 'compress':
      return 'Compress';
    case 'mixed':
      return 'Mixed';
    default:
      return 'Update';
  }
}

function formatRevisionMeta(revision: ContextRevisionSummary) {
  const revisionNumber = revision.revision_number || 0;
  const operationCount = revision.operation_count || 0;
  const nodeCount = revision.node_count || 0;
  const changedSegments = formatNodeReferenceSegments(revision.changed_nodes || []);
  const parts = [
    `Version ${revisionNumber}`,
    `${operationCount} changes`,
    `${nodeCount} nodes`,
  ];

  if (changedSegments.length) {
    parts.push(`Nodes #${changedSegments.join(' / ')}`);
  }

  return parts.join(' / ');
}

function buildRestoreActionLabel(targetRevision: ContextRevisionSummary) {
  const targetRevisionNumber = targetRevision.revision_number || 0;
  return `Switch to version ${targetRevisionNumber}`;
}

function formatTokenCount(value: number) {
  return value.toLocaleString('zh-CN');
}

function parseTokenThresholdDraft(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : fallback;
}

function formatSuggestionRoleLabel(role: ContextWorkbenchSuggestionNode['role'], locale: UiLocale) {
  return role === 'user' ? uiText(locale, 'User', '用户') : uiText(locale, 'Assistant', '助手');
}

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === 'AbortError';
}

function localizeToolCatalogItem(tool: ContextWorkbenchToolCatalogItem, locale: UiLocale) {
  switch (tool.id) {
    case 'preview_context_selection':
      return {
        label: uiText(locale, 'View overview', '查看概览'),
        description: uiText(locale, 'Review selected nodes, or inspect the whole snapshot first.', '查看选中的节点，或者先检查整份快照。'),
      };
    case 'get_context_node_details':
      return {
        label: uiText(locale, 'Expand node details', '展开节点详情'),
        description: uiText(locale, 'Expand nodes into full content and editable items before deciding whether to edit.', '将节点展开为完整内容和可编辑条目，再决定是否编辑。'),
      };
    case 'find_context_items':
      return {
        label: uiText(locale, 'Find items', '查找条目'),
        description: uiText(locale, 'Search provider items by metadata and previews without loading full node content.', '通过元数据和预览搜索条目，不加载完整节点内容。'),
      };
    case 'edit_context_items':
      return {
        label: uiText(locale, 'Edit items', '编辑条目'),
        description: uiText(locale, 'Batch delete, replace, or compress items selected by node, item, or type filters.', '按节点、条目或类型筛选后，批量删除、替换或压缩条目。'),
      };
    case 'delete_context_item':
      return {
        label: uiText(locale, 'Delete one item', '删除单个条目'),
        description: uiText(locale, 'Delete one item inside a node.', '删除某个节点里的一个条目。'),
      };
    case 'replace_context_item':
      return {
        label: uiText(locale, 'Replace one item', '替换单个条目'),
        description: uiText(locale, 'Replace one item inside a node with new content.', '把某个节点里的一个条目替换成新的内容。'),
      };
    case 'compress_context_item':
      return {
        label: uiText(locale, 'Compress one item', '压缩单个条目'),
        description: uiText(locale, 'Compress one item while keeping its item type.', '把某个条目压缩成更短的版本，同时保留原来的条目类型。'),
      };
    case 'compress_context_nodes':
      return {
        label: uiText(locale, 'Compress nodes', '压缩节点'),
        description: uiText(locale, 'Compress one or more nodes into summary nodes in the working snapshot.', '把一个或多个节点压缩成新的摘要节点。'),
      };
    case 'delete_context_nodes':
      return {
        label: uiText(locale, 'Delete nodes', '删除节点'),
        description: uiText(locale, 'Delete one or more nodes from the working snapshot.', '从当前工作快照里删除一个或多个节点。'),
      };
    default:
      return {
        label: tool.label,
        description: tool.description,
      };
  }
}

export default function ContextWorkbench({
  messages,
  messageTokenStats,
  selectedNodeIndexes,
  criticalNodeIndexes,
  tokenThresholds,
  sessionId,
  isMainChatBusy,
  history,
  revisions,
  pendingRestore,
  reasoningOptions,
  uiLocale,
  onHistoryChange,
  onConversationChange,
  onRevisionHistoryChange,
  onPendingRestoreChange,
  onEnsureSession,
  onTokenThresholdsChange,
  onUiLocaleChange,
}: ContextWorkbenchProps) {
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('manual');
  const [manualDraft, setManualDraft] = useState('');
  const [manualReasoning, setManualReasoning] = useState('default');
  const [isManualReasoningOpen, setIsManualReasoningOpen] = useState(false);
  const [manualMessages, setManualMessages] = useState<ManualWorkbenchMessage[]>(
    () => buildManualMessagesFromHistory(history),
  );
  const [isManualSending, setIsManualSending] = useState(false);
  const [isRestoreBusy, setIsRestoreBusy] = useState(false);
  const [restoreError, setRestoreError] = useState('');
  const [manualFeedback, setManualFeedback] = useState('');
  const [manualFeedbackError, setManualFeedbackError] = useState(false);
  const [workbenchModelDraft, setWorkbenchModelDraft] = useState(DEFAULT_WORKBENCH_MODELS[0]);
  const [workbenchProviderDraft, setWorkbenchProviderDraft] = useState(DEFAULT_WORKBENCH_PROVIDER_ID);
  const [uiLocaleDraft, setUiLocaleDraft] = useState<UiLocale>(uiLocale);
  const [tokenWarningThresholdDraft, setTokenWarningThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.warningThreshold),
  );
  const [tokenCriticalThresholdDraft, setTokenCriticalThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.criticalThreshold),
  );
  const [availableProviders, setAvailableProviders] = useState<ResponseProviderDraft[]>([]);
  const [isModelPickerOpen, setIsModelPickerOpen] = useState(false);
  const [toolCatalog, setToolCatalog] = useState<ContextWorkbenchToolCatalogItem[]>([]);
  const [suggestionStats, setSuggestionStats] = useState<ContextWorkbenchSuggestionStats>(EMPTY_SUGGESTION_STATS);
  const [suggestionNodes, setSuggestionNodes] = useState<ContextWorkbenchSuggestionNode[]>([]);
  const [isSuggestionsLoading, setIsSuggestionsLoading] = useState(true);
  const [suggestionsError, setSuggestionsError] = useState('');
  const [isSettingsLoading, setIsSettingsLoading] = useState(true);
  const [isSettingsSaving, setIsSettingsSaving] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState('');
  const [settingsError, setSettingsError] = useState('');
  const manualListRef = useRef<HTMLDivElement>(null);
  const manualTextareaRef = useRef<HTMLTextAreaElement>(null);
  const manualAbortControllerRef = useRef<AbortController | null>(null);
  const manualActiveSessionIdRef = useRef('');
  const manualStopRequestedRef = useRef(false);
  const manualStopRequestRef = useRef<Promise<unknown> | null>(null);

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
    () => availableProviders.find((provider) => provider.id === workbenchProviderDraft),
    [availableProviders, workbenchProviderDraft],
  );
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
        .map((stat): ContextWorkbenchSuggestionNode => ({
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
  const manualHistoryKey = useMemo(() => JSON.stringify(history || []), [history]);
  const isWorkbenchBusy = isManualSending || isRestoreBusy;
  const isManualComposerLocked = isMainChatBusy || isWorkbenchBusy;
  const manualReasoningDisabled = reasoningOptions.length === 0;
  const isRestoreLocked = isMainChatBusy || isRestoreBusy;
  const hasClearableManualHistory = manualMessages.some((message) => !message.pending);
  const currentManualReasoningLabel = reasoningDisplayLabel(
    manualReasoning,
    getReasoningLabel(manualReasoning, reasoningOptions),
    uiLocaleDraft,
  );
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

  void pendingRestore;

  useEffect(() => {
    setUiLocaleDraft(uiLocale);
  }, [uiLocale]);

  useEffect(() => {
    let cancelled = false;

    async function loadWorkbenchSettings() {
      setIsSettingsLoading(true);
      setSettingsError('');

      try {
        const response = await fetchContextWorkbenchSettings();
        if (cancelled) {
          return;
        }

        const nextModel = response.settings.context_workbench_model || DEFAULT_WORKBENCH_MODELS[0];
        const nextProviders = Array.isArray(response.response_providers)
          ? response.response_providers.map(toWorkbenchProviderDraft)
          : [];
        const nextSelection = resolveWorkbenchSelection(
          nextModel,
          response.settings.context_workbench_provider_id || '',
          nextProviders,
        );
        setWorkbenchModelDraft(nextSelection.modelId);
        setWorkbenchProviderDraft(nextSelection.providerId);
        const loadedLocale = response.settings.user_locale
          ? normalizeSupportedLocale(response.settings.user_locale)
          : uiLocale;
        setUiLocaleDraft(loadedLocale);
        onUiLocaleChange?.(loadedLocale);
        const nextThresholds = normalizeContextTokenThresholds({
          warningThreshold: response.settings.context_token_warning_threshold,
          criticalThreshold: response.settings.context_token_critical_threshold,
        });
        setTokenWarningThresholdDraft(String(nextThresholds.warningThreshold));
        setTokenCriticalThresholdDraft(String(nextThresholds.criticalThreshold));
        onTokenThresholdsChange(nextThresholds);
        setAvailableProviders(nextProviders);
        setToolCatalog(response.tool_catalog || []);
      } catch (error) {
        if (cancelled) {
          return;
        }

        setSettingsError(getThrownMessage(error));
        setWorkbenchProviderDraft(DEFAULT_WORKBENCH_PROVIDER_ID);
        setAvailableProviders([]);
      } finally {
        if (!cancelled) {
          setIsSettingsLoading(false);
        }
      }
    }

    void loadWorkbenchSettings();
    return () => {
      cancelled = true;
    };
  }, [onTokenThresholdsChange]);

  useEffect(() => {
    let cancelled = false;

    async function loadSuggestions() {
      if (!sessionId) {
        setSuggestionStats(EMPTY_SUGGESTION_STATS);
        setSuggestionNodes([]);
        setSuggestionsError('');
        setIsSuggestionsLoading(false);
        return;
      }

      setIsSuggestionsLoading(true);
      setSuggestionsError('');

      try {
        const response = await fetchContextWorkbenchSuggestionsRequest({
          session_id: sessionId,
        });

        if (cancelled) {
          return;
        }

        setSuggestionStats(response.stats || EMPTY_SUGGESTION_STATS);
        setSuggestionNodes(response.nodes || []);
      } catch (error) {
        if (cancelled) {
          return;
        }

        setSuggestionStats(EMPTY_SUGGESTION_STATS);
        setSuggestionNodes([]);
        setSuggestionsError(getThrownMessage(error));
      } finally {
        if (!cancelled) {
          setIsSuggestionsLoading(false);
        }
      }
    }

    void loadSuggestions();
    return () => {
      cancelled = true;
    };
  }, [messages, sessionId]);

  useEffect(() => {
    setManualMessages(buildManualMessagesFromHistory(history));
    setIsManualSending(false);
  }, [manualHistoryKey, sessionId]);

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

  function handleManualReasoningSelect(event: MouseEvent<HTMLDivElement>, option: ReasoningOption) {
    event.preventDefault();
    event.stopPropagation();
    flushSync(() => {
      setManualReasoning(option.value);
      setIsManualReasoningOpen(false);
    });
  }

  async function handleSaveWorkbenchSettings() {
    const nextModel = workbenchModelDraft.trim();
    const nextProviderId = workbenchProviderDraft.trim() || inferWorkbenchProviderId(nextModel, availableProviders);
    if (!nextModel || !nextProviderId || isSettingsSaving) {
      return;
    }

    if (tokenThresholdError) {
      setSettingsMessage('');
      setSettingsError(tokenThresholdError);
      return;
    }

    setIsSettingsSaving(true);
    setSettingsMessage('');
    setSettingsError('');

    try {
      const response = await saveContextWorkbenchSettingsRequest({
        context_workbench_model: nextModel,
        context_workbench_provider_id: nextProviderId,
        context_token_warning_threshold: nextTokenThresholds.warningThreshold,
        context_token_critical_threshold: nextTokenThresholds.criticalThreshold,
        user_locale: uiLocaleDraft,
      });
      const savedModel = response.settings.context_workbench_model || nextModel;
      const nextProviders = Array.isArray(response.response_providers)
        ? response.response_providers.map(toWorkbenchProviderDraft)
        : availableProviders;
      const nextSelection = resolveWorkbenchSelection(
        savedModel,
        response.settings.context_workbench_provider_id || nextProviderId,
        nextProviders,
      );
      setWorkbenchModelDraft(nextSelection.modelId);
      setWorkbenchProviderDraft(nextSelection.providerId);
      const savedThresholds = normalizeContextTokenThresholds({
        warningThreshold: response.settings.context_token_warning_threshold,
        criticalThreshold: response.settings.context_token_critical_threshold,
      });
      setTokenWarningThresholdDraft(String(savedThresholds.warningThreshold));
      setTokenCriticalThresholdDraft(String(savedThresholds.criticalThreshold));
      const savedLocale = response.settings.user_locale
        ? normalizeSupportedLocale(response.settings.user_locale)
        : uiLocaleDraft;
      setUiLocaleDraft(savedLocale);
      onUiLocaleChange?.(savedLocale);
      onTokenThresholdsChange(savedThresholds);
      setAvailableProviders(nextProviders);
      setToolCatalog(response.tool_catalog || []);
      setSettingsMessage(uiText(savedLocale, 'Saved. Later context chats will use this model.', '已保存，后面的上下文对话会使用这个模型。'));
    } catch (error) {
      setSettingsError(getThrownMessage(error));
    } finally {
      setIsSettingsSaving(false);
    }
  }

  async function handleSaveUiLocale(nextLocale: UiLocale) {
    if (isSettingsSaving || nextLocale === uiLocaleDraft) {
      return;
    }

    const previousLocale = uiLocaleDraft;
    setUiLocaleDraft(nextLocale);
    onUiLocaleChange?.(nextLocale);
    setIsSettingsSaving(true);
    setSettingsMessage('');
    setSettingsError('');

    try {
      const response = await saveContextWorkbenchSettingsRequest({
        user_locale: nextLocale,
      });
      const savedLocale = response.settings.user_locale
        ? normalizeSupportedLocale(response.settings.user_locale)
        : nextLocale;
      setUiLocaleDraft(savedLocale);
      onUiLocaleChange?.(savedLocale);
      setSettingsMessage(uiText(savedLocale, 'Settings saved.', '设置已保存。'));
    } catch (error) {
      setUiLocaleDraft(previousLocale);
      onUiLocaleChange?.(previousLocale);
      setSettingsError(getThrownMessage(error));
    } finally {
      setIsSettingsSaving(false);
    }
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
    const targetSessionId = manualActiveSessionIdRef.current || sessionId;
    if (targetSessionId) {
      manualStopRequestRef.current = cancelActiveRequest({
        session_id: targetSessionId,
        mode: 'context',
      }).catch(() => undefined);
    }
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
            return;
          }

          if (event.type === 'error') {
            streamError = event.error;
            return;
          }

          streamCompleted = true;
          onHistoryChange(targetSessionId, event.history);
          conversationCommit = Promise.resolve(
            onConversationChange(targetSessionId, normalizeConversation(event.conversation)),
          );
          onRevisionHistoryChange(targetSessionId, event.revisions || []);
          onPendingRestoreChange(targetSessionId, event.pending_restore || null);
          setManualMessages(buildManualMessagesFromHistory(event.history));
        },
        {
          signal: streamController.signal,
        },
      );

      if (streamError) {
        throw new Error(streamError);
      }

      if (!streamCompleted) {
        throw new Error('娴佸紡鍝嶅簲鎰忓涓柇');
      }

      await conversationCommit;
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

  async function handleRestoreRevision(revisionId: string) {
    if (!sessionId || !revisionId || isRestoreLocked) {
      return;
    }

    setIsRestoreBusy(true);
    setRestoreError('');

    try {
      const response = await restoreContextRevisionRequest({
        session_id: sessionId,
        revision_id: revisionId,
      });
      const restoredRevision = (response.revisions || []).find((revision) => revision.id === revisionId);
      onHistoryChange(sessionId, response.history || []);
      await onConversationChange(sessionId, normalizeConversation(response.conversation), {
        resetProxyOverride: restoredRevision?.revision_number === 0,
      });
      onRevisionHistoryChange(sessionId, response.revisions || []);
      onPendingRestoreChange(sessionId, response.pending_restore || null);
      setManualMessages(buildManualMessagesFromHistory(response.history || []));
    } catch (error) {
      setRestoreError(getThrownMessage(error));
    } finally {
      setIsRestoreBusy(false);
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

  async function handleDeleteManualMessage(messageIndex: number) {
    if (!sessionId || isWorkbenchBusy || isMainChatBusy) {
      return;
    }

    const targetMessage = manualMessages[messageIndex];
    if (!targetMessage || targetMessage.pending) {
      return;
    }

    try {
      const response = await deleteContextWorkbenchMessageRequest({
        session_id: sessionId,
        message_index: messageIndex,
      });
      onHistoryChange(sessionId, response.history || []);
      await onConversationChange(sessionId, normalizeConversation(response.conversation));
      onRevisionHistoryChange(sessionId, response.revisions || []);
      onPendingRestoreChange(sessionId, response.pending_restore || null);
      setManualMessages(buildManualMessagesFromHistory(response.history || []));
      setManualFeedback('');
      setManualFeedbackError(false);
    } catch (error) {
      setManualFeedback(getThrownMessage(error));
      setManualFeedbackError(true);
    }
  }

  async function handleClearManualHistory() {
    if (!sessionId || isManualComposerLocked || !hasClearableManualHistory) {
      return;
    }

    try {
      const response = await clearContextWorkbenchHistoryRequest({
        session_id: sessionId,
      });
      onHistoryChange(sessionId, response.history || []);
      await onConversationChange(sessionId, normalizeConversation(response.conversation));
      onRevisionHistoryChange(sessionId, response.revisions || []);
      onPendingRestoreChange(sessionId, response.pending_restore || null);
      setManualMessages(buildManualMessagesFromHistory(response.history || []));
      setManualFeedback('');
      setManualFeedbackError(false);
    } catch (error) {
      setManualFeedback(getThrownMessage(error));
      setManualFeedbackError(true);
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

              {suggestionsError ? <div className="workbench-setting-feedback error">{suggestionsError}</div> : null}

              <div className="suggestion-stack">
                <div className="workbench-setting-card">
                  <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Node Token Details', '节点 Token 明细')}</div>
                  <div className="workbench-setting-desc">{uiText(uiLocaleDraft, 'Only red nodes from the minimap are shown here.', '这里仅显示 minimap 里的红色节点。')}</div>

                  {isSuggestionsLoading && !localSuggestionNodes.length ? (
                    <div className="suggestion-row">
                      <div className="suggestion-row-title">{uiText(uiLocaleDraft, 'Counting tokens...', '正在统计 Token...')}</div>
                      <div className="suggestion-row-body">{uiText(uiLocaleDraft, 'This includes the context the main chat would send to the model.', '这会把主聊天当前真正会发给模型的上下文一起算进去。')}</div>
                    </div>
                  ) : criticalSuggestionNodes.length ? (
                    criticalSuggestionNodes.map((node) => (
                      <div className="suggestion-row" key={node.node_index}>
                        <div className="suggestion-row-copy">
                          <div className="suggestion-row-title">{uiText(uiLocaleDraft, 'Node', '节点')} #{node.node_number}</div>
                          <div className="restore-revision-meta">
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
                  manualMessages.map((entry, messageIndex) => (
                    <div className={`manual-workbench-message ${entry.role}`} key={entry.id}>
                      <div className="manual-workbench-message-shell">
                        <div className="manual-workbench-bubble">
                          {entry.pending && !entry.content.trim() ? (
                            <div className="thinking-inline-line" role="status">
                              <span className="thinking-inline-text">{uiText(uiLocaleDraft, 'Thinking...', '正在思考...')}</span>
                            </div>
                          ) : entry.role === 'assistant' ? (
                            <MarkdownRenderer content={entry.content} />
                          ) : (
                            <div className="manual-workbench-user-text">{entry.content}</div>
                          )}
                        </div>

                        {!entry.pending ? (
                          <div className="manual-workbench-actions">
                            <button type="button" title={uiText(uiLocaleDraft, 'Copy', '复制')} onClick={() => void handleCopyManualMessage(entry.content)}>
                              <i className="ph-light ph-copy" />
                            </button>
                            <button type="button" title={uiText(uiLocaleDraft, 'Delete', '删除')} onClick={() => void handleDeleteManualMessage(messageIndex)}>
                              <i className="ph-light ph-trash" />
                            </button>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="manual-workbench-empty">
                    <div className="manual-workbench-empty-title">{uiText(uiLocaleDraft, 'You can organize the current context directly', '可以直接整理当前上下文')}</div>
                    <div className="manual-workbench-empty-body">
                      {uiText(uiLocaleDraft, 'Ask which parts are too long, or tell the model what should be kept, compressed, replaced, or removed.', '可以询问哪些内容太长，或者直接告诉模型哪些内容应该保留、压缩、替换或删除。')}
                    </div>
                  </div>
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
                    {hasClearableManualHistory ? (
                      <button
                        className="manual-workbench-clear"
                        disabled={isManualSending || isRestoreBusy}
                        title={uiText(uiLocaleDraft, 'Clear context model chat history', '清空上下文模型对话记录')}
                        type="button"
                        onClick={() => void handleClearManualHistory()}
                      >
                        <i className="ph-light ph-broom" />
                      </button>
                    ) : null}
                    <button
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

          <section className="extended-page" data-page="settings">
            <div className="extended-page-scroll">
              <div className="workbench-panel-title">{uiText(uiLocaleDraft, 'Workspace Settings', '工作区设置')}</div>

              <div className="workbench-setting-card">
                <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Language', '语言')}</div>
                <div className="workbench-setting-desc">{uiText(uiLocaleDraft, 'Choose the display language for the app interface.', '选择应用界面的显示语言。')}</div>
                <div className="workbench-language-toggle" role="group" aria-label={uiText(uiLocaleDraft, 'Language', '语言')}>
                  {UI_LANGUAGE_OPTIONS.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      className={`workbench-language-btn${uiLocaleDraft === option.value ? ' is-active' : ''}`}
                      disabled={isSettingsLoading || isSettingsSaving}
                      aria-pressed={uiLocaleDraft === option.value}
                      onClick={() => {
                        void handleSaveUiLocale(option.value);
                      }}
                    >
                      {uiLanguageLabel(option.value, uiLocaleDraft)}
                    </button>
                  ))}
                </div>
              </div>

              <div className="workbench-setting-card">
                <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Manual Page Model', '手动页模型')}</div>
                <div className="workbench-setting-desc">
                  {uiText(uiLocaleDraft, 'The manual page on the right uses this model for context analysis and edits.', '右侧手动页会固定使用这个模型做上下文分析和编辑。')}
                </div>

                <div className="workbench-setting-control-row">
                  <button
                    className="tool-btn-capsule chat-model-picker-trigger workbench-model-picker-trigger"
                    disabled={isSettingsLoading}
                    type="button"
                    onClick={(event) => event.stopPropagation()}
                    onMouseDown={(event) => {
                      if (event.button !== 0) {
                        return;
                      }
                      event.preventDefault();
                      event.stopPropagation();
                      setIsModelPickerOpen(true);
                      setSettingsMessage('');
                      setSettingsError('');
                    }}
                  >
                    <span>{workbenchModelDraft}</span>
                    <i className="ph-light ph-caret-down" />
                  </button>

                  <button
                    className="tool-btn-primary"
                    disabled={isSettingsLoading || isSettingsSaving || !workbenchModelDraft.trim() || !workbenchProviderDraft.trim()}
                    type="button"
                    onClick={() => {
                      void handleSaveWorkbenchSettings();
                    }}
                  >
                    {isSettingsSaving ? uiText(uiLocaleDraft, 'Saving...', '保存中...') : uiText(uiLocaleDraft, 'Save Settings', '保存设置')}
                  </button>
                </div>

                <div className="workbench-setting-provider-hint">
                  {uiText(uiLocaleDraft, 'Current workspace provider:', '当前工作区供应商：')} {workbenchProviderName(selectedWorkbenchProvider)}
                </div>

                {settingsMessage ? <div className="workbench-setting-feedback">{settingsMessage}</div> : null}
                {settingsError ? <div className="workbench-setting-feedback error">{settingsError}</div> : null}
              </div>

              <div className="workbench-setting-card">
                <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Token Color Thresholds', 'Token 颜色阈值')}</div>
                <div className="workbench-setting-desc">{uiText(uiLocaleDraft, 'Set the green, yellow, and red minimap ranges.', '设置 minimap 的绿色、黄色和红色分段。')}</div>

                <div className="workbench-setting-control-row">
                  <label className="workbench-token-threshold-field">
                    <span>{uiText(uiLocaleDraft, 'Yellow threshold', '黄色阈值')}</span>
                    <input
                      className="settings-input settings-input-small"
                      disabled={isSettingsLoading || isSettingsSaving}
                      min={0}
                      step={100}
                      type="number"
                      value={tokenWarningThresholdDraft}
                      onChange={(event) => {
                        setTokenWarningThresholdDraft(event.target.value);
                        setSettingsMessage('');
                        setSettingsError('');
                      }}
                    />
                  </label>

                  <label className="workbench-token-threshold-field">
                    <span>{uiText(uiLocaleDraft, 'Red threshold', '红色阈值')}</span>
                    <input
                      className="settings-input settings-input-small"
                      disabled={isSettingsLoading || isSettingsSaving}
                      min={1}
                      step={100}
                      type="number"
                      value={tokenCriticalThresholdDraft}
                      onChange={(event) => {
                        setTokenCriticalThresholdDraft(event.target.value);
                        setSettingsMessage('');
                        setSettingsError('');
                      }}
                    />
                  </label>

                  <button
                    className="tool-btn-primary"
                    disabled={isSettingsLoading || isSettingsSaving || Boolean(tokenThresholdError)}
                    type="button"
                    onClick={() => {
                      void handleSaveWorkbenchSettings();
                    }}
                  >
                    {isSettingsSaving ? uiText(uiLocaleDraft, 'Saving...', '保存中...') : uiText(uiLocaleDraft, 'Save Thresholds', '保存阈值')}
                  </button>
                </div>

                {tokenThresholdError ? <div className="workbench-setting-feedback error">{tokenThresholdError}</div> : null}
              </div>

              <div className="workbench-setting-card">
                <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Current Tool Capabilities', '当前工具能力')}</div>
                <div className="workbench-setting-desc">
                  {uiText(uiLocaleDraft, 'These tools only affect current context, not the main task. They can inspect nodes and compress, replace, or delete items.', '这些工具只影响当前上下文，不会去跑主任务；可以查看节点，也可以压缩、替换或删除条目。')}
                </div>

                <div className="workbench-tool-grid">
                  {toolCatalog.map((tool) => {
                    const localized = localizeToolCatalogItem(tool, uiLocaleDraft);
                    return (
                      <div className="workbench-tool-card" key={tool.id}>
                        <div className="workbench-tool-card-head">
                          <span className="workbench-tool-card-title">{localized.label}</span>
                          <span className={`workbench-tool-status ${tool.status}`}>{statusLabel(tool.status, uiLocaleDraft)}</span>
                        </div>
                        <div className="workbench-tool-card-desc">{localized.description}</div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <ChatModelPicker
        activeProviderId={workbenchProviderDraft}
        currentModel={workbenchModelDraft}
        open={isModelPickerOpen}
        providers={availableProviders}
        title="Select workspace model"
        description="This selects the context workspace model and provider, separate from the main chat model."
        selectedContextLabel="Current workspace"
        onClose={() => setIsModelPickerOpen(false)}
        onSelectModel={(providerId: string, model: ResponseProviderModel) => {
          setWorkbenchProviderDraft(providerId);
          setWorkbenchModelDraft(model.id || model.label || '');
          setSettingsMessage('');
          setSettingsError('');
          setIsModelPickerOpen(false);
        }}
      />
    </>
  );
}
