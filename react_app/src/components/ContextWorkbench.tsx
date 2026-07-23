import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent, MouseEvent } from 'react';
import { flushSync } from 'react-dom';

import {
  cancelContextChatRequest,
  clearContextWorkbenchChatRequest,
  fetchProxySessionUsageRequest,
  resetProxyUsageRequest,
  streamContextChatRequest,
} from '../api';
import {
  type ContextMessageTokenStat,
  type ContextTokenThresholds,
} from '../contextTokenWeight';
import type {
  ContextReview,
  ContextWorkbenchChatMessage,
  MessageBlock,
  MessageRecord,
  ProxyUsageSummary,
  ReasoningOption,
  ToolEvent,
  TranscriptEntry,
} from '../types';
import type { UiLocale } from '../i18n';
import { copyText, getReasoningLabel, normalizeConversation } from '../utils';
import {
  buildManualMessagesFromChat,
  createManualMessage,
  formatNodeReferenceSegments,
  formatTokenCount,
  getThrownMessage,
  reasoningDisplayLabel,
  uiText,
  WORKBENCH_TABS,
  workbenchTabLabel,
  type ManualWorkbenchMessage,
  type WorkbenchTab,
} from './ContextWorkbench.helpers';
import Dropdown from './Dropdown';
import ContextWorkbenchSettings from './ContextWorkbenchSettings';
import MessageContent from './MessageContent';
import UsageSummaryCard from './UsageSummaryCard';

interface ContextWorkbenchProps {
  messageTokenStats: ContextMessageTokenStat[];
  selectedNodeIndexes: number[];
  tokenThresholds: ContextTokenThresholds;
  sessionId: string;
  isMainChatBusy: boolean;
  isContextPreviewActive: boolean;
  contextWorkbenchChat: ContextWorkbenchChatMessage[];
  reasoningOptions: ReasoningOption[];
  pendingContextReview: ContextReview | null;
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
  onContextReviewGenerate: () => Promise<ContextReview | null>;
  onContextReviewPreview: (review: ContextReview) => Promise<void>;
  onContextReviewPreviewClose: () => void;
  onContextReviewApply: (reviewId: string) => Promise<ContextReview | null>;
  onContextReviewDiscard: (reviewId: string) => Promise<ContextReview | null>;
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

type ContextReviewAction = 'generate' | 'preview' | 'apply' | 'discard' | null;

function formatContextReviewDate(value: string, locale: UiLocale) {
  const createdAt = new Date(value);
  if (Number.isNaN(createdAt.getTime())) {
    return '';
  }
  return createdAt.toLocaleString(locale === 'zh-CN' ? 'zh-CN' : 'en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function contextReviewReductionPercent(review: ContextReview | null) {
  const beforeTokens = Number(review?.before?.token_count || 0);
  const afterTokens = Number(review?.after?.token_count || 0);
  if (beforeTokens <= 0 || afterTokens <= 0 || afterTokens >= beforeTokens) {
    return '';
  }
  return `${Math.round(((beforeTokens - afterTokens) / beforeTokens) * 100)}%`;
}

function contextReviewStatValue(value: number | undefined) {
  return formatTokenCount(Number(value || 0));
}

export default function ContextWorkbench({
  messageTokenStats,
  selectedNodeIndexes,
  tokenThresholds,
  sessionId,
  isMainChatBusy,
  isContextPreviewActive,
  contextWorkbenchChat,
  reasoningOptions,
  pendingContextReview,
  proxyUsageSummary,
  uiLocale,
  themeMode,
  onContextWorkbenchChatChange,
  onConversationChange,
  onProxyUsageSummaryChange,
  onEnsureSession,
  onContextReviewGenerate,
  onContextReviewPreview,
  onContextReviewPreviewClose,
  onContextReviewApply,
  onContextReviewDiscard,
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
  const [isManualStopping, setIsManualStopping] = useState(false);
  const [isUsageClearing, setIsUsageClearing] = useState(false);
  const [usageFeedback, setUsageFeedback] = useState('');
  const [usageFeedbackError, setUsageFeedbackError] = useState(false);
  const [manualFeedback, setManualFeedback] = useState('');
  const [manualFeedbackError, setManualFeedbackError] = useState(false);
  const [contextReviewAction, setContextReviewAction] = useState<ContextReviewAction>(null);
  const [contextReviewFeedback, setContextReviewFeedback] = useState('');
  const [contextReviewFeedbackError, setContextReviewFeedbackError] = useState(false);
  const uiLocaleDraft = uiLocale;
  const manualListRef = useRef<HTMLDivElement>(null);
  const manualTextareaRef = useRef<HTMLTextAreaElement>(null);
  const manualAbortControllerRef = useRef<AbortController | null>(null);
  const manualActiveSessionIdRef = useRef('');
  const manualActiveRequestIdRef = useRef('');
  const manualPendingMessageIdRef = useRef('');
  const manualStopRequestedRef = useRef(false);
  const manualStopRequestRef = useRef<ReturnType<typeof cancelContextChatRequest> | null>(null);

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
  const isContextReviewBusy = contextReviewAction !== null;
  const pendingReviewCreatedAt = formatContextReviewDate(pendingContextReview?.created_at || '', uiLocaleDraft);
  const pendingReviewReduction = contextReviewReductionPercent(pendingContextReview);
  const pendingReviewBeforeNodes = Number(pendingContextReview?.before?.node_count || 0);
  const pendingReviewAfterNodes = Number(pendingContextReview?.after?.node_count || 0);
  const pendingReviewBeforeTokens = contextReviewStatValue(pendingContextReview?.before?.token_count);
  const pendingReviewAfterTokens = contextReviewStatValue(pendingContextReview?.after?.token_count);
  const mainUsageSummary = proxyUsageSummary?.by_kind?.main || null;
  const contextWorkbenchUsageSummary = proxyUsageSummary?.by_kind?.context_workbench || null;

  useEffect(() => {
    setManualMessages(buildManualMessagesFromChat(contextWorkbenchChat));
    setIsManualSending(false);
    setIsManualStopping(false);
  }, [manualChatKey, sessionId]);

  useEffect(() => {
    setManualDraft('');
    setManualFeedback('');
    setManualFeedbackError(false);
  }, [sessionId]);

  useEffect(() => {
    if (!isMainChatBusy) return;
    setContextReviewFeedback('');
    setContextReviewFeedbackError(false);
  }, [isMainChatBusy]);

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

  function finalizeStoppedManualMessage(messageId: string) {
    updatePendingManualMessage(messageId, (lastMessage) => ({
      ...lastMessage,
      content: lastMessage.content.trim()
        ? lastMessage.content
        : uiText(uiLocaleDraft, 'Stopped this context model chat.', '已停止这次上下文模型对话。'),
      pending: false,
      statusText: '',
    }));
  }

  function beginManualStopRequest() {
    if (manualStopRequestRef.current) {
      return manualStopRequestRef.current;
    }

    const targetSessionId = manualActiveSessionIdRef.current;
    const targetRequestId = manualActiveRequestIdRef.current;
    if (!targetSessionId || !targetRequestId) {
      return null;
    }

    const stopRequest = cancelContextChatRequest(targetSessionId, targetRequestId).then((response) => {
      if (!response.cancelled || !response.completed) {
        throw new Error(uiText(
          uiLocaleDraft,
          'The server did not confirm that the context model stopped. You can try stopping it again.',
          '服务端尚未确认上下文模型已经停止，可以再次尝试停止。',
        ));
      }
      return response;
    });
    manualStopRequestRef.current = stopRequest;
    void stopRequest.then(() => {
      manualAbortControllerRef.current?.abort();
    }).catch((error) => {
      if (manualStopRequestRef.current !== stopRequest) {
        return;
      }
      manualStopRequestRef.current = null;
      manualStopRequestedRef.current = false;
      setIsManualStopping(false);
      setManualFeedback(getThrownMessage(error));
      setManualFeedbackError(true);
      const pendingMessageId = manualPendingMessageIdRef.current;
      if (pendingMessageId) {
        updatePendingManualMessage(pendingMessageId, (message) => ({
          ...message,
          statusText: '',
        }));
      }
    });
    return stopRequest;
  }

  function handleStopManualMessage() {
    const controller = manualAbortControllerRef.current;
    if (!controller || isManualStopping) {
      return;
    }

    manualStopRequestedRef.current = true;
    setIsManualStopping(true);
    setManualFeedback('');
    setManualFeedbackError(false);
    const pendingMessageId = manualPendingMessageIdRef.current;
    if (pendingMessageId) {
      updatePendingManualMessage(pendingMessageId, (message) => ({
        ...message,
        statusText: uiText(uiLocaleDraft, 'Stopping...', '正在停止...'),
      }));
    }
    beginManualStopRequest();
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
    manualActiveRequestIdRef.current = '';
    manualPendingMessageIdRef.current = pendingMessage.id;
    setIsManualStopping(false);
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
          if (event.type === 'started') {
            manualActiveRequestIdRef.current = event.request_id;
            if (manualStopRequestedRef.current) {
              beginManualStopRequest();
            }
            return;
          }

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
      if (manualStopRequestedRef.current) {
        const stopRequest = manualStopRequestRef.current;
        if (stopRequest) {
          try {
            await stopRequest;
            finalizeStoppedManualMessage(pendingMessage.id);
            setManualFeedback(uiText(uiLocaleDraft, 'Stopped this context model chat.', '已停止这次上下文模型对话。'));
            setManualFeedbackError(false);
            return;
          } catch (stopError) {
            error = stopError;
          }
        }
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
      manualActiveRequestIdRef.current = '';
      manualPendingMessageIdRef.current = '';
      manualStopRequestedRef.current = false;
      manualStopRequestRef.current = null;
      setIsManualStopping(false);
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

  async function handleGenerateContextReview() {
    if (!sessionId || isContextReviewBusy || isMainChatBusy || pendingContextReview) return;
    setContextReviewAction('generate');
    setContextReviewFeedback('');
    setContextReviewFeedbackError(false);
    try {
      const review = await onContextReviewGenerate();
      if (!review) {
        setContextReviewFeedback(uiText(uiLocaleDraft, 'No compression proposal was needed for the current context.', '当前上下文暂时不需要生成压缩建议。'));
      }
    } catch (error) {
      setContextReviewFeedback(getThrownMessage(error));
      setContextReviewFeedbackError(true);
    } finally {
      setContextReviewAction(null);
    }
  }

  async function handlePreviewContextReview() {
    if (!pendingContextReview || isContextReviewBusy) return;
    setContextReviewAction('preview');
    setContextReviewFeedback('');
    setContextReviewFeedbackError(false);
    try {
      await onContextReviewPreview(pendingContextReview);
    } catch (error) {
      setContextReviewFeedback(getThrownMessage(error));
      setContextReviewFeedbackError(true);
    } finally {
      setContextReviewAction(null);
    }
  }

  async function handleApplyContextReview() {
    if (!pendingContextReview || isContextReviewBusy || isMainChatBusy) return;
    const confirmed = window.confirm(
      uiText(
        uiLocaleDraft,
        'Apply this proposal and replace the live context?',
        '确定应用这个建议并覆盖当前正式上下文吗？',
      ),
    );
    if (!confirmed) return;

    setContextReviewAction('apply');
    setContextReviewFeedback('');
    setContextReviewFeedbackError(false);
    try {
      await onContextReviewApply(pendingContextReview.id);
    } catch (error) {
      setContextReviewFeedback(getThrownMessage(error));
      setContextReviewFeedbackError(true);
    } finally {
      setContextReviewAction(null);
    }
  }

  async function handleDiscardContextReview() {
    if (!pendingContextReview || isContextReviewBusy) return;
    setContextReviewAction('discard');
    setContextReviewFeedback('');
    setContextReviewFeedbackError(false);
    try {
      await onContextReviewDiscard(pendingContextReview.id);
    } catch (error) {
      setContextReviewFeedback(getThrownMessage(error));
      setContextReviewFeedbackError(true);
    } finally {
      setContextReviewAction(null);
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
              <div className="workbench-panel-title">{uiText(uiLocaleDraft, 'Context Suggestions', '上下文建议')}</div>
              <div className="workbench-panel-desc">
                {uiText(uiLocaleDraft, 'Review the compressed transcript proposal before it replaces the live context.', '审核压缩后的 transcript 草稿，再决定是否覆盖当前正式上下文。')}
              </div>

              {contextReviewFeedback ? (
                <div className={`workbench-setting-feedback${contextReviewFeedbackError ? ' error' : ''}`}>
                  {contextReviewFeedback}
                </div>
              ) : null}

              {pendingContextReview ? (
                <div className="context-review-card workbench-setting-card">
                  <div className="context-review-card-header">
                    <div>
                      <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'Pending Compression Review', '待审核压缩建议')}</div>
                      <div className="workbench-setting-desc">
                        {pendingReviewCreatedAt
                          ? uiText(uiLocaleDraft, `Generated ${pendingReviewCreatedAt}`, `生成时间：${pendingReviewCreatedAt}`)
                          : uiText(uiLocaleDraft, 'Generated by the context model.', '由上下文模型生成。')}
                      </div>
                    </div>
                    <span className="context-review-status">
                      {uiText(uiLocaleDraft, pendingContextReview.source === 'auto_idle' ? 'Auto' : 'Manual', pendingContextReview.source === 'auto_idle' ? '自动' : '手动')}
                    </span>
                  </div>

                  <div className="context-review-summary">
                    {pendingContextReview.summary || uiText(uiLocaleDraft, 'The model prepared a compressed transcript proposal.', '模型已准备压缩后的 transcript 草稿。')}
                  </div>

                  <div className="context-review-stats" aria-label={uiText(uiLocaleDraft, 'Review statistics', '审核统计')}>
                    <div className="context-review-stat">
                      <div className="suggestion-card-label">{uiText(uiLocaleDraft, 'Before', '压缩前')}</div>
                      <div className="suggestion-card-value">{pendingReviewBeforeNodes}</div>
                      <div className="suggestion-card-note">{pendingReviewBeforeTokens} Tokens</div>
                    </div>
                    <div className="context-review-stat">
                      <div className="suggestion-card-label">{uiText(uiLocaleDraft, 'After', '压缩后')}</div>
                      <div className="suggestion-card-value">{pendingReviewAfterNodes}</div>
                      <div className="suggestion-card-note">{pendingReviewAfterTokens} Tokens</div>
                    </div>
                    <div className="context-review-stat">
                      <div className="suggestion-card-label">{uiText(uiLocaleDraft, 'Reduced', '减少')}</div>
                      <div className="suggestion-card-value">{pendingReviewReduction || '-'}</div>
                      <div className="suggestion-card-note">{uiText(uiLocaleDraft, 'Estimated token change', '估算 token 变化')}</div>
                    </div>
                  </div>

                  <div className="context-review-actions">
                    <button
                      className="tool-btn-capsule"
                      disabled={isContextReviewBusy}
                      type="button"
                      onClick={() => void handlePreviewContextReview()}
                    >
                      {contextReviewAction === 'preview'
                        ? uiText(uiLocaleDraft, 'Loading preview...', '正在加载预览...')
                        : uiText(uiLocaleDraft, 'Preview', '预览')}
                    </button>
                    {isContextPreviewActive ? (
                      <button
                        className="tool-btn-capsule"
                        disabled={isContextReviewBusy}
                        type="button"
                        onClick={onContextReviewPreviewClose}
                      >
                        {uiText(uiLocaleDraft, 'Close Preview', '关闭预览')}
                      </button>
                    ) : null}
                    <button
                      className="tool-btn-capsule context-review-apply"
                      disabled={isContextReviewBusy || isMainChatBusy}
                      type="button"
                      onClick={() => void handleApplyContextReview()}
                    >
                      {contextReviewAction === 'apply'
                        ? uiText(uiLocaleDraft, 'Applying...', '正在应用...')
                        : uiText(uiLocaleDraft, 'Apply', '应用')}
                    </button>
                    <button
                      className="tool-btn-capsule"
                      disabled={isContextReviewBusy}
                      type="button"
                      onClick={() => void handleDiscardContextReview()}
                    >
                      {contextReviewAction === 'discard'
                        ? uiText(uiLocaleDraft, 'Discarding...', '正在丢弃...')
                        : uiText(uiLocaleDraft, 'Discard', '丢弃')}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="context-review-empty workbench-setting-card">
                  <div className="workbench-setting-title">{uiText(uiLocaleDraft, 'No pending review', '暂无待审核建议')}</div>
                  <div className="workbench-setting-desc">
                    {uiText(
                      uiLocaleDraft,
                      'Automatic analysis runs after the configured idle interval. You can also analyze the current context now.',
                      '达到设置的闲置时间后会自动分析。你也可以现在手动分析当前上下文。',
                    )}
                  </div>
                  <div className="context-review-actions">
                    <button
                      className="tool-btn-capsule context-review-primary"
                      disabled={isContextReviewBusy || isMainChatBusy || !sessionId}
                      type="button"
                      onClick={() => void handleGenerateContextReview()}
                    >
                      {contextReviewAction === 'generate'
                        ? uiText(uiLocaleDraft, 'Analyzing...', '正在分析...')
                        : uiText(uiLocaleDraft, 'Analyze Now', '立即分析')}
                    </button>
                  </div>
                </div>
              )}
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
                        ? (isManualStopping
                          ? uiText(uiLocaleDraft, 'Stopping context model chat', '正在停止上下文模型对话')
                          : uiText(uiLocaleDraft, 'Stop context model chat', '停止上下文模型对话'))
                        : uiText(uiLocaleDraft, 'Send context model message', '发送上下文模型消息')}
                      className={`send-btn manual-workbench-send ${isManualSending ? 'is-stop-action' : 'is-send-action'}`}
                      disabled={isManualSending ? isManualStopping : (!manualDraft.trim() || isManualComposerLocked)}
                      type="button"
                      onClick={() => {
                        if (isManualSending) {
                          handleStopManualMessage();
                        } else {
                          void handleSendManualMessage();
                        }
                      }}
                    >
                      <i className={`ph-light ${isManualStopping ? 'ph-circle-notch is-spinning' : (isManualSending ? 'ph-stop' : 'ph-paper-plane-tilt')}`} />
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
                  'Historical token usage reported by every model provider used in this session. Costs use the GPT-5.6 Sol reference price.',
                  '这个会话中各模型 Provider 返回的真实 Token 用量；费用统一按 GPT-5.6 Sol 参考价估算。',
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

          <ContextWorkbenchSettings
            onThemeModeChange={onThemeModeChange}
            onTokenThresholdsChange={onTokenThresholdsChange}
            onUiFontChange={onUiFontChange}
            onUiLocaleChange={onUiLocaleChange}
            themeMode={themeMode}
            tokenThresholds={tokenThresholds}
            uiLocale={uiLocale}
          />
        </div>
      </div>

    </>
  );
}
