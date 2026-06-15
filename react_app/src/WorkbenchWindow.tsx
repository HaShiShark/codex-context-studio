import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';

import {
  createSessionRequest,
  fetchInit,
  fetchProxySessionRequest,
  fetchProxySessionsRequest,
  fetchProxySessionUsageRequest,
  resetProxyOverrideRequest,
  saveProxyOverrideRequest,
  syncProxySessionRequest,
} from './api';
import ContextMapSidebar from './components/ContextMapSidebar';
import { normalizeSupportedLocale, type UiLocale } from './i18n';
import type {
  ContextWorkbenchHistoryEntry,
  InitPayload,
  MessageRecord,
  ProxyUsageSummary,
  ReasoningOption,
  SessionSummary,
  TranscriptRecord,
} from './types';
import { normalizeConversation, normalizeReasoningOptions } from './utils';

const LIVE_REFRESH_IDLE_MS = 4000;
const LIVE_REFRESH_RUNNING_MS = 1500;
const PENDING_CONTEXT_REFRESH_MS = 400;
const PENDING_CONTEXT_REFRESH_MAX_MS = 8000;
const LOCAL_EDIT_GRACE_MS = 2000;
const MIN_WORKBENCH_WINDOW_WIDTH = 760;
const MIN_WORKBENCH_WINDOW_HEIGHT = 520;

type WindowResizeEdge =
  | 'top'
  | 'right'
  | 'bottom'
  | 'left'
  | 'top-left'
  | 'top-right'
  | 'bottom-left'
  | 'bottom-right';

const WINDOW_RESIZE_EDGES: WindowResizeEdge[] = [
  'top',
  'right',
  'bottom',
  'left',
  'top-left',
  'top-right',
  'bottom-left',
  'bottom-right',
];

function isProxyBusy(status?: string, isRunning?: boolean): boolean {
  return Boolean(isRunning || status === 'running' || status === 'compacting');
}

function hasPendingAssistantMessage(messages: MessageRecord[]): boolean {
  const lastAssistant = [...messages].reverse().find((message) => message.role === 'an');
  if (!lastAssistant) {
    return false;
  }

  return Boolean(
    lastAssistant.pending
    || (!lastAssistant.text.trim() && lastAssistant.blocks.some((block) => block.kind === 'thinking'))
    || lastAssistant.blocks.some((block) => block.kind === 'reasoning' && block.status === 'streaming'),
  );
}

type LoadInitOptions = {
  silent?: boolean;
  targetSessionId?: string;
  refreshSettings?: boolean;
};

type ConversationCommitOptions = {
  resetProxyOverride?: boolean;
  skipProxyOverride?: boolean;
};

function currentUrlSessionId(): string {
  return new URLSearchParams(window.location.search).get('session_id')?.trim() || '';
}

function firstSession(payload: InitPayload): SessionSummary | null {
  const urlSessionId = currentUrlSessionId();
  const sessions = payload.chat_sessions || [];

  if (urlSessionId) {
    return sessions.find((session) => session.id === urlSessionId) || null;
  }

  return sessions[0] || null;
}

function transcriptForSession(payload: InitPayload, sessionId: string): TranscriptRecord[] {
  return payload.conversations?.[sessionId] || [];
}

function transcriptFromMessages(messages: MessageRecord[]): TranscriptRecord[] {
  return messages.map((message) => ({
    role: message.role === 'an' ? 'assistant' : message.role,
    text: message.text || '',
    attachments: message.attachments || [],
    toolEvents: message.toolEvents || [],
    blocks: message.blocks || [],
    providerItems: message.providerItems || [],
  }));
}

function conversationSignature(messages: MessageRecord[]): string {
  return JSON.stringify(messages);
}

function windowText(locale: UiLocale, english: string, chinese: string) {
  return locale === 'zh-CN' ? chinese : english;
}

function nextWindowBounds(
  edge: WindowResizeEdge,
  startBounds: ElectronWindowBounds,
  startPoint: { x: number; y: number },
  currentPoint: { x: number; y: number },
): ElectronWindowBounds {
  const deltaX = currentPoint.x - startPoint.x;
  const deltaY = currentPoint.y - startPoint.y;
  let x = startBounds.x;
  let y = startBounds.y;
  let width = startBounds.width;
  let height = startBounds.height;

  if (edge.includes('right')) {
    width = Math.max(MIN_WORKBENCH_WINDOW_WIDTH, startBounds.width + deltaX);
  }

  if (edge.includes('bottom')) {
    height = Math.max(MIN_WORKBENCH_WINDOW_HEIGHT, startBounds.height + deltaY);
  }

  if (edge.includes('left')) {
    const nextWidth = startBounds.width - deltaX;
    width = Math.max(MIN_WORKBENCH_WINDOW_WIDTH, nextWidth);
    x = startBounds.x + startBounds.width - width;
  }

  if (edge.includes('top')) {
    const nextHeight = startBounds.height - deltaY;
    height = Math.max(MIN_WORKBENCH_WINDOW_HEIGHT, nextHeight);
    y = startBounds.y + startBounds.height - height;
  }

  return {
    x: Math.round(x),
    y: Math.round(y),
    width: Math.round(width),
    height: Math.round(height),
  };
}

function WorkbenchWindowResizeHandles() {
  const startResizing = useCallback(async (event: ReactPointerEvent<HTMLDivElement>, edge: WindowResizeEdge) => {
    if (event.button !== 0 || !window.electronAPI?.getWindowBounds || !window.electronAPI?.setWindowBounds) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    const target = event.currentTarget;
    const pointerId = event.pointerId;
    target.setPointerCapture(pointerId);

    const startBounds = await window.electronAPI.getWindowBounds();
    if (!startBounds) {
      target.releasePointerCapture(pointerId);
      return;
    }

    const startPoint = { x: event.screenX, y: event.screenY };
    document.documentElement.dataset.workbenchWindowResizing = edge;

    const resize = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      window.electronAPI?.setWindowBounds?.(
        nextWindowBounds(edge, startBounds, startPoint, {
          x: moveEvent.screenX,
          y: moveEvent.screenY,
        }),
      );
    };

    const stopResizing = () => {
      delete document.documentElement.dataset.workbenchWindowResizing;
      window.removeEventListener('pointermove', resize, true);
      window.removeEventListener('pointerup', stopResizing, true);
      window.removeEventListener('pointercancel', stopResizing, true);
      if (target.hasPointerCapture(pointerId)) {
        target.releasePointerCapture(pointerId);
      }
    };

    window.addEventListener('pointermove', resize, true);
    window.addEventListener('pointerup', stopResizing, true);
    window.addEventListener('pointercancel', stopResizing, true);
  }, []);

  if (!window.electronAPI?.isElectron) {
    return null;
  }

  return (
    <div className="workbench-window-resize-handles" aria-hidden="true">
      {WINDOW_RESIZE_EDGES.map((edge) => (
        <div
          key={edge}
          className={`workbench-window-resize-handle ${edge}`}
          onPointerDown={(event) => {
            void startResizing(event, edge);
          }}
        />
      ))}
    </div>
  );
}

function WorkbenchWindowControls({ uiLocale }: { uiLocale: UiLocale }) {
  return (
    <div className="workbench-window-controls" aria-label={windowText(uiLocale, 'Window controls', '窗口控制')}>
      <button
        className="workbench-window-control-btn"
        type="button"
        title={windowText(uiLocale, 'Minimize', '最小化')}
        aria-label={windowText(uiLocale, 'Minimize', '最小化')}
        onMouseDown={(event) => event.stopPropagation()}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={() => window.electronAPI?.minimize?.()}
      >
        <i className="ph-light ph-minus" />
      </button>
      <button
        className="workbench-window-control-btn"
        type="button"
        title={windowText(uiLocale, 'Maximize', '最大化')}
        aria-label={windowText(uiLocale, 'Maximize', '最大化')}
        onMouseDown={(event) => event.stopPropagation()}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={() => window.electronAPI?.maximize?.()}
      >
        <i className="ph-light ph-square" />
      </button>
      <button
        className="workbench-window-control-btn close"
        type="button"
        title={windowText(uiLocale, 'Close', '关闭')}
        aria-label={windowText(uiLocale, 'Close', '关闭')}
        onMouseDown={(event) => event.stopPropagation()}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={() => window.electronAPI?.close?.()}
      >
        <i className="ph-light ph-x" />
      </button>
    </div>
  );
}

export default function WorkbenchWindow() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [uiLocale, setUiLocale] = useState(normalizeSupportedLocale('en-US'));
  const [session, setSession] = useState<SessionSummary | null>(null);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [histories, setHistories] = useState<Record<string, ContextWorkbenchHistoryEntry[]>>({});
  const [reasoningOptions, setReasoningOptions] = useState<ReasoningOption[]>(normalizeReasoningOptions());
  const [proxySessionId, setProxySessionId] = useState('');
  const [isProxyRunning, setIsProxyRunning] = useState(false);
  const [proxySaveError, setProxySaveError] = useState('');
  const [proxyUsageSummary, setProxyUsageSummary] = useState<ProxyUsageSummary | null>(null);
  const visibleLoadInFlightRef = useRef(false);
  const loadRequestIdRef = useRef(0);
  const lastLocalEditAtRef = useRef(0);
  const messagesSignatureRef = useRef('');

  const sessionId = session?.id || '';

  const refreshProxyUsage = useCallback(async (nextProxySessionId: string) => {
    if (!nextProxySessionId) {
      setProxyUsageSummary(null);
      return;
    }

    try {
      const response = await fetchProxySessionUsageRequest(nextProxySessionId);
      setProxyUsageSummary(response.summary || null);
    } catch {
    }
  }, []);

  const setMessagesIfChanged = useCallback((nextMessages: MessageRecord[]) => {
    const nextSignature = conversationSignature(nextMessages);
    if (nextSignature === messagesSignatureRef.current) {
      return;
    }

    messagesSignatureRef.current = nextSignature;
    setMessages(nextMessages);
  }, []);

  const loadInit = useCallback(async (options: LoadInitOptions = {}) => {
    const targetSessionId = options.targetSessionId?.trim() || currentUrlSessionId();
    const isVisibleLoad = !options.silent;
    if (visibleLoadInFlightRef.current) {
      return;
    }

    const requestId = isVisibleLoad ? loadRequestIdRef.current + 1 : loadRequestIdRef.current;
    if (isVisibleLoad) {
      loadRequestIdRef.current = requestId;
      visibleLoadInFlightRef.current = true;
      setLoading(true);
      setError('');
      if (targetSessionId && targetSessionId !== sessionId) {
        setSession(null);
        setMessagesIfChanged([]);
        setHistories({});
        setProxySessionId('');
        setIsProxyRunning(false);
        setProxyUsageSummary(null);
      }
    }

    const isCurrentLoad = () => loadRequestIdRef.current === requestId;

    try {
      const proxyPayloadPromise = fetchProxySessionsRequest().catch(() => null);
      const payloadPromise = fetchInit(targetSessionId || undefined, {
        includeConversation: false,
      });
      const proxyPayload = await proxyPayloadPromise;
      if (!isCurrentLoad()) {
        return;
      }
      const targetProxySummary = targetSessionId
        ? proxyPayload?.sessions.find((item) => item.id === targetSessionId) || null
        : null;
      const activeProxySummary = targetProxySummary
        || (!targetSessionId
          ? proxyPayload?.sessions.find((item) => item.id === proxyPayload.active_session_id)
            || proxyPayload?.sessions[0]
            || null
          : null);
      if (activeProxySummary) {
        setIsProxyRunning(isProxyBusy(activeProxySummary.status, activeProxySummary.is_running));
      }

      const activeProxySessionPromise = activeProxySummary
        ? fetchProxySessionRequest(activeProxySummary.id).catch(() => activeProxySummary)
        : Promise.resolve(null);
      const [payload, activeProxySession] = await Promise.all([
        payloadPromise,
        activeProxySessionPromise,
      ]);
      if (!isCurrentLoad()) {
        return;
      }
      if (!options.silent || options.refreshSettings) {
        setUiLocale(normalizeSupportedLocale(payload.settings?.user_locale));
      }

      const activeProxyTranscript = activeProxySession?.active_transcript || activeProxySession?.transcript || [];

      if (activeProxySession && activeProxyTranscript.length) {
        const nextIsProxyRunning = isProxyBusy(activeProxySession.status, activeProxySession.is_running);
        const syncedMessages = normalizeConversation(activeProxyTranscript);
        const shouldSyncProxySession =
          sessionId !== activeProxySession.id
          || conversationSignature(syncedMessages) !== messagesSignatureRef.current;

        const fallbackSession = {
          id: activeProxySession.id,
          title: activeProxySession.title || `Codex ${activeProxySession.id.slice(0, 8)}`,
          scope: 'chat' as const,
          project_id: null,
        };
        const payloadSession = payload.chat_sessions?.find((item) => item.id === activeProxySession.id) || null;
        const syncedSession = payloadSession || fallbackSession;
        const nextHistories = payload.context_workbench_histories || {};

        setSession(syncedSession);
        setMessagesIfChanged(syncedMessages);
        setHistories(nextHistories);
        setReasoningOptions(normalizeReasoningOptions(payload.reasoning_options));
        setProxySessionId(activeProxySession.id);
        setIsProxyRunning(nextIsProxyRunning);
        setProxyUsageSummary(activeProxySession.usage_summary || null);
        void refreshProxyUsage(activeProxySession.id);

        if (shouldSyncProxySession) {
          void syncProxySessionRequest({
              session_id: activeProxySession.id,
              title: activeProxySession.title || 'Codex Context',
              transcript: activeProxyTranscript,
              is_running: nextIsProxyRunning,
            })
            .then((synced) => {
              setSession(synced.session);
              setHistories((current) => ({
                ...current,
                [synced.session.id]: synced.context_workbench_history || [],
              }));
            })
            .catch(() => {});
        }
        return;
      }

      if (targetSessionId) {
        const fallbackPayload = await fetchInit(targetSessionId, { includeConversation: true });
        if (!isCurrentLoad()) {
          return;
        }
        const targetSession = payload.chat_sessions?.find((item) => item.id === targetSessionId) || {
          id: targetSessionId,
          title: `Codex ${targetSessionId.slice(0, 8)}`,
          scope: 'chat' as const,
          project_id: null,
        };
        setSession(targetSession);
        setMessagesIfChanged(normalizeConversation(transcriptForSession(fallbackPayload, targetSessionId)));
        setHistories(fallbackPayload.context_workbench_histories || {});
        setReasoningOptions(normalizeReasoningOptions(fallbackPayload.reasoning_options));
        setProxySessionId(targetSessionId);
        setIsProxyRunning(false);
        setProxyUsageSummary(targetProxySummary?.usage_summary || null);
        void refreshProxyUsage(targetSessionId);
        return;
      }

      const fallbackPayload = await fetchInit(undefined, { includeConversation: true });
      if (!isCurrentLoad()) {
        return;
      }
      let nextSession = firstSession(fallbackPayload);

      if (!nextSession) {
        const created = await createSessionRequest({ scope: 'chat' });
        if (!isCurrentLoad()) {
          return;
        }
        nextSession = created.session;
      }

      const nextMessages = normalizeConversation(transcriptForSession(fallbackPayload, nextSession.id));
      const nextHistories = fallbackPayload.context_workbench_histories || {};

      setSession(nextSession);
      setMessagesIfChanged(nextMessages);
      setHistories(nextHistories);
      setReasoningOptions(normalizeReasoningOptions(fallbackPayload.reasoning_options));
      setProxySessionId('');
      setIsProxyRunning(false);
      setProxyUsageSummary(null);
    } catch (caught) {
      if (!options.silent) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      if (isVisibleLoad) {
        visibleLoadInFlightRef.current = false;
      }
      if (isVisibleLoad && isCurrentLoad()) {
        setLoading(false);
      }
    }
  }, [refreshProxyUsage, sessionId, setMessagesIfChanged]);

  useEffect(() => {
    void loadInit();
  }, [loadInit]);

  useEffect(() => {
    document.documentElement.lang = uiLocale === 'en-US' ? 'en' : 'zh-CN';
  }, [uiLocale]);

  useEffect(() => {
    const handleShow = (event: Event) => {
      const detail = (event as CustomEvent<{ sessionId?: string }>).detail;
      const targetSessionId = detail?.sessionId?.trim() || '';
      if (targetSessionId) {
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set('session_id', targetSessionId);
        window.history.replaceState(null, '', `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
      }
      void loadInit({ silent: !targetSessionId || targetSessionId === sessionId, targetSessionId, refreshSettings: true });
    };

    window.addEventListener('hash-context-window-show', handleShow);
    return () => window.removeEventListener('hash-context-window-show', handleShow);
  }, [loadInit, sessionId]);

  useEffect(() => {
    const refreshMs = isProxyRunning ? LIVE_REFRESH_RUNNING_MS : LIVE_REFRESH_IDLE_MS;
    const intervalId = window.setInterval(() => {
      if (Date.now() - lastLocalEditAtRef.current < LOCAL_EDIT_GRACE_MS) {
        return;
      }

      void loadInit({ silent: true });
    }, refreshMs);

    return () => window.clearInterval(intervalId);
  }, [isProxyRunning, loadInit]);

  const ensureSession = useCallback(async () => {
    if (sessionId) {
      return sessionId;
    }

    const created = await createSessionRequest({ scope: 'chat' });
    setSession(created.session);
    setMessagesIfChanged([]);
    return created.session.id;
  }, [sessionId, setMessagesIfChanged]);

  const currentHistory = useMemo(
    () => (sessionId ? histories[sessionId] || [] : []),
    [histories, sessionId],
  );

  const hasPendingAssistant = useMemo(() => hasPendingAssistantMessage(messages), [messages]);

  useEffect(() => {
    if (!hasPendingAssistant) {
      return undefined;
    }

    const startedAt = Date.now();
    const intervalId = window.setInterval(() => {
      if (Date.now() - startedAt >= PENDING_CONTEXT_REFRESH_MAX_MS) {
        window.clearInterval(intervalId);
        return;
      }
      if (Date.now() - lastLocalEditAtRef.current < LOCAL_EDIT_GRACE_MS) {
        return;
      }

      void loadInit({ silent: true });
    }, PENDING_CONTEXT_REFRESH_MS);

    return () => window.clearInterval(intervalId);
  }, [hasPendingAssistant, loadInit]);

  const commitContextConversation = useCallback(
    async (
      changedSessionId: string,
      conversation: MessageRecord[],
      options: ConversationCommitOptions = {},
    ) => {
      if (changedSessionId !== sessionId) {
        return;
      }

      lastLocalEditAtRef.current = Date.now();
      setMessagesIfChanged(conversation);

      if (options.skipProxyOverride || !proxySessionId || isProxyRunning) {
        return;
      }

      try {
        if (options.resetProxyOverride) {
          await resetProxyOverrideRequest(proxySessionId);
        } else {
          await saveProxyOverrideRequest(proxySessionId, transcriptFromMessages(conversation));
        }
        setProxySaveError('');
      } catch (caught) {
        const message = caught instanceof Error ? caught.message : String(caught);
        setProxySaveError(message);
        throw caught;
      }
    },
    [isProxyRunning, proxySessionId, sessionId, setMessagesIfChanged],
  );

  if (loading) {
    return (
      <main className="workbench-window-shell loading">
        <WorkbenchWindowResizeHandles />
        <WorkbenchWindowControls uiLocale={uiLocale} />
        <div className="workbench-window-state-panel">
          <div className="workbench-window-title">{windowText(uiLocale, 'Context Workbench', '上下文工作台')}</div>
          <div className="workbench-window-muted">
            {windowText(uiLocale, 'Loading current context...', '正在加载当前上下文...')}
          </div>
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="workbench-window-shell loading">
        <WorkbenchWindowResizeHandles />
        <WorkbenchWindowControls uiLocale={uiLocale} />
        <div className="workbench-window-state-panel">
          <div className="workbench-window-title">{windowText(uiLocale, 'Context Workbench', '上下文工作台')}</div>
          <div className="workbench-window-error">{error}</div>
          <button className="workbench-window-button" type="button" onClick={() => void loadInit()}>
            {windowText(uiLocale, 'Retry', '重试')}
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="workbench-window-shell">
      <WorkbenchWindowResizeHandles />
      <WorkbenchWindowControls uiLocale={uiLocale} />
      {proxySaveError ? (
        <div className="workbench-window-error" role="alert">
          {proxySaveError}
        </div>
      ) : null}
      <ContextMapSidebar
        stage={2}
        messages={messages}
        onToggle={() => undefined}
        onJumpToMessage={() => undefined}
        sessionId={sessionId}
        isMainChatBusy={isProxyRunning}
        contextWorkbenchHistory={currentHistory}
        reasoningOptions={reasoningOptions}
        proxyUsageSummary={proxyUsageSummary}
        uiLocale={uiLocale}
        onContextWorkbenchHistoryChange={(changedSessionId, history) => {
          setHistories((current) => ({ ...current, [changedSessionId]: history }));
        }}
        onContextWorkbenchConversationChange={commitContextConversation}
        onProxyUsageSummaryChange={setProxyUsageSummary}
        onEnsureSession={ensureSession}
        onUiLocaleChange={(locale) => setUiLocale(normalizeSupportedLocale(locale))}
      />
    </main>
  );
}
