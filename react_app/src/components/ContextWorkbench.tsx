import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, MouseEvent, ReactNode } from 'react';
import { flushSync } from 'react-dom';

import {
  cancelActiveRequest,
  clearContextWorkbenchHistoryRequest,
  deleteContextWorkbenchMessageRequest,
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
  ContextWorkbenchHistoryEntry,
  MessageRecord,
  ProxyUsageSummary,
  ReasoningOption,
  ResponseProviderDraft,
  ResponseProviderModel,
} from '../types';
import { normalizeSupportedLocale, type UiLocale } from '../i18n';
import { copyText, getReasoningLabel, normalizeConversation } from '../utils';
import {
  buildManualMessagesFromHistory,
  buildWorkbenchModelOptions,
  createManualMessage,
  DEFAULT_WORKBENCH_MODELS,
  DEFAULT_WORKBENCH_PROVIDER_ID,
  formatNodeReferenceSegments,
  formatSuggestionRoleLabel,
  formatTokenCount,
  getThrownMessage,
  inferWorkbenchProviderId,
  isAbortError,
  parseTokenThresholdDraft,
  reasoningDisplayLabel,
  resolveWorkbenchSelection,
  toWorkbenchProviderDraft,
  UI_LANGUAGE_OPTIONS,
  uiLanguageLabel,
  uiText,
  workbenchProviderName,
  WORKBENCH_TABS,
  workbenchTabLabel,
  type ManualWorkbenchMessage,
  type WorkbenchTab,
} from './ContextWorkbench.helpers';
import Dropdown from './Dropdown';
import MarkdownRenderer from './MarkdownRenderer';
import UsageSummaryCard from './UsageSummaryCard';

interface ContextWorkbenchProps {
  messageTokenStats: ContextMessageTokenStat[];
  selectedNodeIndexes: number[];
  criticalNodeIndexes: number[];
  tokenThresholds: ContextTokenThresholds;
  sessionId: string;
  isMainChatBusy: boolean;
  history: ContextWorkbenchHistoryEntry[];
  reasoningOptions: ReasoningOption[];
  proxyUsageSummary: ProxyUsageSummary | null;
  uiLocale: UiLocale;
  themeMode: 'light' | 'dark';
  onHistoryChange: (sessionId: string, history: ContextWorkbenchHistoryEntry[]) => void;
  onConversationChange: (
    sessionId: string,
    conversation: MessageRecord[],
    options?: { resetProxyOverride?: boolean; skipProxyOverride?: boolean },
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
  messageIndex: number;
  uiLocale: UiLocale;
  onCopy: (content: string) => void;
  onDelete: (messageIndex: number) => void;
}

function ManualMessageItem({
  entry,
  messageIndex,
  uiLocale,
  onCopy,
  onDelete,
}: ManualMessageItemProps) {
  const copyLabel = uiText(uiLocale, 'Copy', '复制');
  const deleteLabel = uiText(uiLocale, 'Delete', '删除');

  return (
    <div className={`manual-workbench-message ${entry.role}`}>
      <div className="manual-workbench-message-shell">
        <div className="manual-workbench-bubble">
          {entry.pending && !entry.content.trim() ? (
            <div className="thinking-inline-line" role="status">
              <span className="thinking-inline-text">{uiText(uiLocale, 'Thinking...', '正在思考...')}</span>
            </div>
          ) : entry.role === 'assistant' ? (
            <>
              <MarkdownRenderer content={entry.content} />
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
            <button
              aria-label={deleteLabel}
              title={deleteLabel}
              type="button"
              onClick={() => onDelete(messageIndex)}
            >
              <i className="ph-light ph-trash" />
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

export default function ContextWorkbench({
  messageTokenStats,
  selectedNodeIndexes,
  criticalNodeIndexes,
  tokenThresholds,
  sessionId,
  isMainChatBusy,
  history,
  reasoningOptions,
  proxyUsageSummary,
  uiLocale,
  themeMode,
  onHistoryChange,
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
    () => buildManualMessagesFromHistory(history),
  );
  const [isManualSending, setIsManualSending] = useState(false);
  const [isUsageClearing, setIsUsageClearing] = useState(false);
  const [usageFeedback, setUsageFeedback] = useState('');
  const [usageFeedbackError, setUsageFeedbackError] = useState(false);
  const [manualFeedback, setManualFeedback] = useState('');
  const [manualFeedbackError, setManualFeedbackError] = useState(false);
  const [workbenchModelDraft, setWorkbenchModelDraft] = useState(DEFAULT_WORKBENCH_MODELS[0]);
  const [workbenchProviderDraft, setWorkbenchProviderDraft] = useState(DEFAULT_WORKBENCH_PROVIDER_ID);
  const [uiLocaleDraft, setUiLocaleDraft] = useState<UiLocale>(uiLocale);
  const [themeModeDraft, setThemeModeDraft] = useState<'light' | 'dark'>(themeMode);
  const [isWorkbenchModelOpen, setIsWorkbenchModelOpen] = useState(false);
  const [tokenWarningThresholdDraft, setTokenWarningThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.warningThreshold),
  );
  const [tokenCriticalThresholdDraft, setTokenCriticalThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.criticalThreshold),
  );
  const [availableProviders, setAvailableProviders] = useState<ResponseProviderDraft[]>([]);
  const [isSettingsLoading, setIsSettingsLoading] = useState(true);
  const [settingsError, setSettingsError] = useState('');
  const [uiFontDraft, setUiFontDraft] = useState('Noto Serif SC');
  const [uiFontSizeDraft, setUiFontSizeDraft] = useState('15');
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
  const workbenchModelOptions = useMemo<ResponseProviderModel[]>(
    () => buildWorkbenchModelOptions(selectedWorkbenchProvider, workbenchModelDraft),
    [selectedWorkbenchProvider, workbenchModelDraft],
  );
  const currentWorkbenchModelLabel =
    workbenchModelOptions.find((model) => (model.id || model.label || '').trim() === workbenchModelDraft)?.label
    || workbenchModelDraft
    || DEFAULT_WORKBENCH_MODELS[0];
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
  const manualHistoryKey = useMemo(() => JSON.stringify(history || []), [history]);
  const isWorkbenchBusy = isManualSending;
  const isManualComposerLocked = isMainChatBusy || isWorkbenchBusy;
  const manualReasoningDisabled = reasoningOptions.length === 0;
  const hasClearableManualHistory = manualMessages.some((message) => !message.pending);
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

  useEffect(() => {
    setUiLocaleDraft(uiLocale);
  }, [uiLocale]);

  useEffect(() => {
    setThemeModeDraft(themeMode);
  }, [themeMode]);

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
        const loadedThemeMode = response.settings.theme_mode === 'dark' ? 'dark' : 'light';
        setThemeModeDraft(loadedThemeMode);
        onThemeModeChange?.(loadedThemeMode);
        const nextThresholds = normalizeContextTokenThresholds({
          warningThreshold: response.settings.context_token_warning_threshold,
          criticalThreshold: response.settings.context_token_critical_threshold,
        });
        setTokenWarningThresholdDraft(String(nextThresholds.warningThreshold));
        setTokenCriticalThresholdDraft(String(nextThresholds.criticalThreshold));
        onTokenThresholdsChange(nextThresholds);
        setAvailableProviders(nextProviders);
        const loadedFont = response.settings.ui_font || 'Noto Serif SC';
        const loadedFontSize = response.settings.ui_font_size || 15;
        setUiFontDraft(loadedFont);
        setUiFontSizeDraft(String(loadedFontSize));
        onUiFontChange?.(loadedFont, loadedFontSize);
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
    if (!isWorkbenchModelOpen) return;
    function handleOutside(event: globalThis.MouseEvent) {
      const target = event.target as Element | null;
      if (!target?.closest('.workbench-model-picker-trigger')?.parentElement?.contains(target)) {
        setIsWorkbenchModelOpen(false);
      }
    }
    document.addEventListener('mousedown', handleOutside);
    return () => document.removeEventListener('mousedown', handleOutside);
  }, [isWorkbenchModelOpen]);


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

  function handleWorkbenchModelSelect(event: MouseEvent<HTMLDivElement>, model: ResponseProviderModel) {
    event.preventDefault();
    event.stopPropagation();
    const nextModel = (model.id || model.label || '').trim();
    if (!nextModel) return;
    const nextProviderId = selectedWorkbenchProvider?.id || DEFAULT_WORKBENCH_PROVIDER_ID;
    flushSync(() => {
      setWorkbenchProviderDraft(nextProviderId);
      setWorkbenchModelDraft(nextModel);
      setIsWorkbenchModelOpen(false);
      setSettingsError('');
    });
    void handleSaveWorkbenchSettings({ model: nextModel, providerId: nextProviderId });
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

  async function handleSaveWorkbenchSettings(overrides?: {
    model?: string;
    providerId?: string;
    thresholds?: { warningThreshold: number; criticalThreshold: number };
    locale?: UiLocale;
  }) {
    const nextModel = (overrides?.model ?? workbenchModelDraft).trim();
    const nextProviderId = (overrides?.providerId ?? workbenchProviderDraft).trim() || inferWorkbenchProviderId(nextModel, availableProviders);
    if (!nextModel || !nextProviderId) {
      return;
    }

    const thresholds = overrides?.thresholds ?? nextTokenThresholds;
    if (!overrides?.thresholds && tokenThresholdError) {
      setSettingsError(tokenThresholdError);
      return;
    }

    setSettingsError('');
    onTokenThresholdsChange(thresholds);

    try {
      const response = await saveContextWorkbenchSettingsRequest({
        context_workbench_model: nextModel,
        context_workbench_provider_id: nextProviderId,
        context_token_warning_threshold: thresholds.warningThreshold,
        context_token_critical_threshold: thresholds.criticalThreshold,
        user_locale: overrides?.locale ?? uiLocaleDraft,
      });
      const nextProviders = Array.isArray(response.response_providers)
        ? response.response_providers.map(toWorkbenchProviderDraft)
        : availableProviders;
      setAvailableProviders(nextProviders);
    } catch (error) {
      setSettingsError(getThrownMessage(error));
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
          onHistoryChange(targetSessionId, event.history);
          conversationCommit = Promise.resolve(
          onConversationChange(targetSessionId, normalizeConversation(event.conversation), {
            skipProxyOverride: true,
          }),
        );
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
      await onConversationChange(sessionId, normalizeConversation(response.conversation), {
        skipProxyOverride: true,
      });
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
      await onConversationChange(sessionId, normalizeConversation(response.conversation), {
        skipProxyOverride: true,
      });
      setManualMessages(buildManualMessagesFromHistory(response.history || []));
      setManualFeedback('');
      setManualFeedbackError(false);
    } catch (error) {
      setManualFeedback(getThrownMessage(error));
      setManualFeedbackError(true);
    }
  }

  async function handleClearUsageSummary() {
    if (!sessionId || isUsageClearing) {
      return;
    }

    setIsUsageClearing(true);
    setUsageFeedback('');
    setUsageFeedbackError(false);
    try {
      const response = await resetProxyUsageRequest(sessionId);
      onProxyUsageSummaryChange(response.summary || null);
      setUsageFeedback(
        response.cleared_count > 0
          ? uiText(uiLocaleDraft, 'Usage count reset for this session.', '已清空这个会话的用量计数。')
          : uiText(uiLocaleDraft, 'This session has no recorded usage yet.', '这个会话还没有记录到用量。'),
      );
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
                  manualMessages.map((entry, messageIndex) => (
                    <ManualMessageItem
                      entry={entry}
                      key={entry.id}
                      messageIndex={messageIndex}
                      uiLocale={uiLocaleDraft}
                      onCopy={(content) => void handleCopyManualMessage(content)}
                      onDelete={(index) => void handleDeleteManualMessage(index)}
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
                        aria-label={uiText(uiLocaleDraft, 'Clear context model chat history', '清空上下文模型对话记录')}
                        className="manual-workbench-clear"
                        disabled={isManualSending}
                        title={uiText(uiLocaleDraft, 'Clear context model chat history', '清空上下文模型对话记录')}
                        type="button"
                        onClick={() => void handleClearManualHistory()}
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
            <div className="extended-page-scroll">
              <div className="workbench-panel-title">{uiText(uiLocaleDraft, 'Workspace Settings', '工作区设置')}</div>

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

                <SettingsRow
                  title={uiText(uiLocaleDraft, 'Manual Page Model', '手动页模型')}
                >
                  <div className="workbench-setting-control-row">
                    <Dropdown
                      align="right"
                      buttonClassName="tool-btn-capsule manual-workbench-reasoning workbench-model-picker-trigger"
                      buttonChildren={(
                        <>
                          <i className="ph-light ph-cpu" />
                          <span>{currentWorkbenchModelLabel}</span>
                          <i className="ph-light ph-caret-down" />
                        </>
                      )}
                      disabled={isSettingsLoading}
                      isOpen={isWorkbenchModelOpen}
                      onToggle={() => {
                        setIsWorkbenchModelOpen((previous) => !previous);
                        setSettingsError('');
                      }}
                    >
                      {workbenchModelOptions.map((model) => {
                        const modelId = (model.id || model.label || '').trim();
                        return (
                          <div
                            className={`dropdown-item ${modelId === workbenchModelDraft ? 'selected' : ''}`}
                            key={modelId}
                            onMouseDown={(event) => handleWorkbenchModelSelect(event, model)}
                          >
                            <div className="dropdown-item-left">
                              <span>{model.label || modelId}</span>
                              {model.group ? <small>{model.group}</small> : null}
                            </div>
                            {modelId === workbenchModelDraft ? <i className="ph-bold ph-check" /> : null}
                          </div>
                        );
                      })}
                    </Dropdown>

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
            </div>
          </section>
        </div>
      </div>

    </>
  );
}
