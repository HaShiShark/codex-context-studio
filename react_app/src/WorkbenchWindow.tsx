import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';

import {
  createSessionRequest,
  fetchInit,
  fetchProxySessionsRequest,
  resetProxyOverrideRequest,
  saveProxyOverrideRequest,
  syncProxySessionRequest,
} from './api';
import ContextMapSidebar from './components/ContextMapSidebar';
import { normalizeSupportedLocale, type UiLocale } from './i18n';
import type {
  ContextRevisionSummary,
  ContextWorkbenchHistoryEntry,
  InitPayload,
  MessageRecord,
  PendingContextRestore,
  ReasoningOption,
  SessionSummary,
  TranscriptRecord,
} from './types';
import { normalizeConversation, normalizeReasoningOptions } from './utils';

const DEMO_SESSION_ID = 'hash-context-preview';
const LIVE_REFRESH_IDLE_MS = 1600;
const LIVE_REFRESH_RUNNING_MS = 800;
const LOCAL_EDIT_GRACE_MS = 1500;
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

type LoadInitOptions = {
  silent?: boolean;
  targetSessionId?: string;
  refreshSettings?: boolean;
};

type ConversationCommitOptions = {
  resetProxyOverride?: boolean;
};

function repeatForWeight(seed: string, count: number) {
  return Array.from({ length: count }, () => seed).join('\n');
}

const DEMO_TRANSCRIPT: TranscriptRecord[] = [
  {
    role: 'user',
    text: '介绍一下这个项目',
  },
  {
    role: 'assistant',
    text: `我先按我看到的代码和 README，给你说个“人话版”：这个项目是一个本地 AI 工作台，重点不是普通聊天，而是把上下文拆成可观察、可编辑、可回滚的节点。\n\n${repeatForWeight('README、服务端、前端、上下文地图、工具调用、provider adapter、历史版本、恢复点、手动编辑。', 480)}`,
  },
  {
    role: 'user',
    text: '你为什么要给我一般写进简历的，你怎么知道我要干什么的',
  },
  {
    role: 'assistant',
    text:
      '你说得对，这里是我多猜了一步。我不知道你要拿它干什么，刚刚提简历是不严谨的外推。\n\n' +
      '更准确地说，我应该只围绕这个项目本身：\n\n' +
      '- **上下文整理**：把长对话拆成节点。\n' +
      '- **压缩**：把冗余搜索结果和运行日志变短。\n' +
      '- **删除**：移除干扰当前目标的片段。\n' +
      '- **替换和恢复**：改错之后还能回到旧版本。\n\n' +
      `${repeatForWeight('这部分可以被压缩成一个更短的项目说明，不应该擅自假设使用场景。', 20)}`,
  },
  {
    role: 'user',
    text: '客观评估一下这个产品，有没有可能火',
  },
  {
    role: 'assistant',
    text: `客观说：有机会火，但更像“在开发者/AI 重度用户圈子里小范围火”，不是大众产品一上来就爆。核心价值是它把模型上下文这个黑盒变成可视、可控、可修剪的工作区。\n\n${repeatForWeight('市场判断、目标用户、上下文爆炸、工具调用日志、代码搜索结果、代理循环、记忆污染、可解释编辑、版本恢复。', 400)}`,
  },
  {
    role: 'user',
    text: '我发到github会不会被大产品吸收是什么意思',
  },
  {
    role: 'assistant',
    text: `我说的“被大产品吸收”，不是说别人把你整个项目偷偷拿走了。更现实的意思是：如果这个交互证明有价值，IDE、CLI Agent、代码助手这些产品可能会自己做类似功能。\n\n${repeatForWeight('这类功能的壁垒在体验、协议适配、稳定性和开发者生态，不只在单个界面。', 55)}`,
  },
  {
    role: 'user',
    text: '我能收获一些star吗，不需要多久',
  },
  {
    role: 'assistant',
    text: `能，拿到一些 star 是有现实可能的。如果你的目标不是大火，而是让早期开发者看懂价值，最关键的是做一个可复现的 demo：打开 Codex 或 Claude Code，输入一条命令，就能看到上下文地图并修改。\n\n${repeatForWeight('发布策略、README 动图、最小插件、安装命令、使用场景、限制说明、路线图。', 60)}`,
  },
];

const DEMO_WORKBENCH_HISTORY: ContextWorkbenchHistoryEntry[] = [
  {
    role: 'user',
    content: '你能为我做什么呢',
  },
  {
    role: 'assistant',
    content:
      '在这个窗口里，我是你的**上下文管理助手**。\n\n' +
      '简单来说，我可以帮你“修剪”和“整理”咱们刚才聊天的记忆。如果你觉得对话太长了、模型开始变笨、或者有些中间过程（比如那些长长的搜索结果和代码运行日志）太占空间，你可以直接指挥我来处理：\n\n' +
      '1. **清理冗余**：比如第 2 步和第 6 步里那些巨大的工具搜索结果，我可以把它们删掉或压缩，只保留结论，这样能省下不少空间。\n' +
      '2. **精简回顾**：我可以把之前好几轮的讨论合并成一个简短的总结。\n' +
      '3. **精准删除**：如果你觉得某段对话干扰了现在的思路，我可以帮你把它从记忆里彻底抹去。\n' +
      '4. **修改错误**：如果你觉得我之前某句话说得不对，或者逻辑有问题，我可以帮你重写那段记录。\n\n' +
      '举个例子：我看 Node #2 和 Node #6 都有两万多 tokens，里面塞满了搜索结果。如果你想让接下来的对话更清爽，我可以帮你把它们压缩一下。\n\n' +
      '你想先试试清理哪一部分吗？',
  },
];

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
  const [revisions, setRevisions] = useState<Record<string, ContextRevisionSummary[]>>({});
  const [pendingRestores, setPendingRestores] = useState<Record<string, PendingContextRestore | null>>({});
  const [reasoningOptions, setReasoningOptions] = useState<ReasoningOption[]>(normalizeReasoningOptions());
  const [proxySessionId, setProxySessionId] = useState('');
  const [isProxyRunning, setIsProxyRunning] = useState(false);
  const [proxySaveError, setProxySaveError] = useState('');
  const refreshInFlightRef = useRef(false);
  const lastLocalEditAtRef = useRef(0);
  const messagesSignatureRef = useRef('');

  const sessionId = session?.id || '';

  const setMessagesIfChanged = useCallback((nextMessages: MessageRecord[]) => {
    const nextSignature = conversationSignature(nextMessages);
    if (nextSignature === messagesSignatureRef.current) {
      return;
    }

    messagesSignatureRef.current = nextSignature;
    setMessages(nextMessages);
  }, []);

  const loadInit = useCallback(async (options: LoadInitOptions = {}) => {
    if (refreshInFlightRef.current) {
      return;
    }

    refreshInFlightRef.current = true;
    if (!options.silent) {
      setLoading(true);
      setError('');
    }

    try {
      const payload = await fetchInit();
      if (!options.silent || options.refreshSettings) {
        setUiLocale(normalizeSupportedLocale(payload.settings?.user_locale));
      }
      const proxyPayload = await fetchProxySessionsRequest().catch(() => null);
      const targetSessionId = options.targetSessionId?.trim() || currentUrlSessionId();
      const targetProxySession = targetSessionId
        ? proxyPayload?.sessions.find((item) => item.id === targetSessionId) || null
        : null;
      const activeProxySession = targetProxySession
        || (!targetSessionId
          ? proxyPayload?.sessions.find((item) => item.id === proxyPayload.active_session_id)
            || proxyPayload?.sessions[0]
            || null
          : null);

      if (activeProxySession && activeProxySession.transcript?.length) {
        const synced = await syncProxySessionRequest({
          session_id: activeProxySession.id,
          title: activeProxySession.title || 'Codex Context',
          transcript: activeProxySession.transcript,
          is_running: isProxyBusy(activeProxySession.status, activeProxySession.is_running),
        });
        const syncedSession = synced.session;
        const syncedMessages = normalizeConversation(activeProxySession.transcript || synced.conversation);
        const nextHistories = {
          ...(payload.context_workbench_histories || {}),
          [syncedSession.id]: synced.context_workbench_history || [],
        };
        const nextRevisions = {
          ...(payload.context_revision_histories || {}),
          [syncedSession.id]: synced.context_revision_history || [],
        };
        const nextPendingRestores: Record<string, PendingContextRestore | null> = {
          ...(payload.pending_context_restores || {}),
        };
        if (synced.pending_context_restore) {
          nextPendingRestores[syncedSession.id] = synced.pending_context_restore;
        } else {
          delete nextPendingRestores[syncedSession.id];
        }

        setSession(syncedSession);
        setMessagesIfChanged(syncedMessages);
        setHistories(nextHistories);
        setRevisions(nextRevisions);
        setPendingRestores(nextPendingRestores);
        setReasoningOptions(normalizeReasoningOptions(payload.reasoning_options));
        setProxySessionId(activeProxySession.id);
        setIsProxyRunning(isProxyBusy(activeProxySession.status, activeProxySession.is_running));
        return;
      }

      if (targetSessionId) {
        const targetSession = payload.chat_sessions?.find((item) => item.id === targetSessionId) || {
          id: targetSessionId,
          title: `Codex ${targetSessionId.slice(0, 8)}`,
          scope: 'chat' as const,
          project_id: null,
        };
        setSession(targetSession);
        setMessagesIfChanged(normalizeConversation(transcriptForSession(payload, targetSessionId)));
        setHistories(payload.context_workbench_histories || {});
        setRevisions(payload.context_revision_histories || {});
        setPendingRestores(payload.pending_context_restores || {});
        setReasoningOptions(normalizeReasoningOptions(payload.reasoning_options));
        setProxySessionId(targetSessionId);
        setIsProxyRunning(false);
        return;
      }

      let nextSession = firstSession(payload);

      if (!nextSession) {
        const created = await createSessionRequest({ scope: 'chat' });
        nextSession = created.session;
      }

      const normalizedMessages = nextSession ? normalizeConversation(transcriptForSession(payload, nextSession.id)) : [];
      const nextMessages = normalizedMessages.length ? normalizedMessages : normalizeConversation(DEMO_TRANSCRIPT);
      const nextHistories = payload.context_workbench_histories || {};
      const effectiveSessionId = nextSession?.id || DEMO_SESSION_ID;

      setSession(nextSession);
      setMessagesIfChanged(nextMessages);
      setHistories(
        nextHistories[effectiveSessionId]
          ? nextHistories
          : { ...nextHistories, [effectiveSessionId]: DEMO_WORKBENCH_HISTORY },
      );
      setRevisions(payload.context_revision_histories || {});
      setPendingRestores(payload.pending_context_restores || {});
      setReasoningOptions(normalizeReasoningOptions(payload.reasoning_options));
      setProxySessionId('');
      setIsProxyRunning(false);
    } catch (caught) {
      if (!options.silent) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      refreshInFlightRef.current = false;
      if (!options.silent) {
        setLoading(false);
      }
    }
  }, [setMessagesIfChanged]);

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
      void loadInit({ silent: true, targetSessionId, refreshSettings: true });
    };

    window.addEventListener('hash-context-window-show', handleShow);
    return () => window.removeEventListener('hash-context-window-show', handleShow);
  }, [loadInit]);

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

  const currentRevisions = useMemo(
    () => (sessionId ? revisions[sessionId] || [] : []),
    [revisions, sessionId],
  );

  const currentPendingRestore = sessionId ? pendingRestores[sessionId] || null : null;

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

      if (!proxySessionId || isProxyRunning) {
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
      <main className="workbench-window-state">
        <WorkbenchWindowResizeHandles />
        <WorkbenchWindowControls uiLocale={uiLocale} />
        <div className="workbench-window-title">{windowText(uiLocale, 'Context Workbench', '上下文工作台')}</div>
        <div className="workbench-window-muted">
          {windowText(uiLocale, 'Connecting to the local debug backend...', '正在连接本地调试后端...')}
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="workbench-window-state">
        <WorkbenchWindowResizeHandles />
        <WorkbenchWindowControls uiLocale={uiLocale} />
        <div className="workbench-window-title">{windowText(uiLocale, 'Context Workbench', '上下文工作台')}</div>
        <div className="workbench-window-error">{error}</div>
        <button className="workbench-window-button" type="button" onClick={() => void loadInit()}>
          {windowText(uiLocale, 'Retry', '重试')}
        </button>
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
        contextRevisionHistory={currentRevisions}
        pendingContextRestore={currentPendingRestore}
        reasoningOptions={reasoningOptions}
        uiLocale={uiLocale}
        onContextWorkbenchHistoryChange={(changedSessionId, history) => {
          setHistories((current) => ({ ...current, [changedSessionId]: history }));
        }}
        onContextWorkbenchConversationChange={commitContextConversation}
        onContextRevisionHistoryChange={(changedSessionId, revisionHistory) => {
          setRevisions((current) => ({ ...current, [changedSessionId]: revisionHistory }));
        }}
        onPendingContextRestoreChange={(changedSessionId, pendingRestore) => {
          setPendingRestores((current) => ({ ...current, [changedSessionId]: pendingRestore }));
        }}
        onEnsureSession={ensureSession}
        onUiLocaleChange={(locale) => setUiLocale(normalizeSupportedLocale(locale))}
      />
    </main>
  );
}
