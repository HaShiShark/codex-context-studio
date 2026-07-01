import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  fetchInit,
  fetchProxySessionRequest,
  fetchProxySessionsRequest,
  proxyRealtimeUrl,
  syncProxySessionRequest,
  type ProxyRealtimeEvent,
  type ProxySessionSummary,
  type TranscriptPatchOp,
} from './api';
import ContextMapSidebar from './components/ContextMapSidebar';
import { normalizeSupportedLocale, type UiLocale } from './i18n';
import type {
  ContextWorkbenchChatMessage,
  MessageRecord,
  ProxyUsageSummary,
  ReasoningOption,
  TranscriptEntry,
} from './types';
import { normalizeConversation, normalizeReasoningOptions } from './utils';

const MIN_WORKBENCH_WINDOW_WIDTH = 600;
const MIN_WORKBENCH_WINDOW_HEIGHT = 360;

type WindowResizeEdge =
  | 'top'
  | 'right'
  | 'bottom'
  | 'left'
  | 'top-left'
  | 'top-right'
  | 'bottom-left'
  | 'bottom-right';

function isProxyBusy(status?: string, isRunning?: boolean): boolean {
  return Boolean(isRunning || status === 'running' || status === 'compacting');
}

function currentUrlSessionId(): string {
  return new URLSearchParams(window.location.search).get('session_id')?.trim() || '';
}

function conversationSignature(messages: MessageRecord[]): string {
  return JSON.stringify(messages);
}

function applyTranscriptPatch(current: TranscriptEntry[], ops: TranscriptPatchOp[] = []): TranscriptEntry[] {
  const next = [...current];
  for (const op of ops) {
    if (op.op === 'splice_nodes') {
      next.splice(Math.max(0, op.index), Math.max(0, op.delete_count), ...op.nodes);
      continue;
    }
    if (op.op === 'append_node') {
      next.push(op.node);
      continue;
    }
    if (op.op === 'replace_node') {
      if (op.index >= 0 && op.index < next.length) next[op.index] = op.node;
      continue;
    }
    if (op.op === 'delete_node') {
      if (op.index >= 0 && op.index < next.length) next.splice(op.index, 1);
    }
  }
  return next;
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
  if (edge.includes('right')) width = Math.max(MIN_WORKBENCH_WINDOW_WIDTH, startBounds.width + deltaX);
  if (edge.includes('bottom')) height = Math.max(MIN_WORKBENCH_WINDOW_HEIGHT, startBounds.height + deltaY);
  if (edge.includes('left')) { const nw = startBounds.width - deltaX; width = Math.max(MIN_WORKBENCH_WINDOW_WIDTH, nw); x = startBounds.x + startBounds.width - width; }
  if (edge.includes('top')) { const nh = startBounds.height - deltaY; height = Math.max(MIN_WORKBENCH_WINDOW_HEIGHT, nh); y = startBounds.y + startBounds.height - height; }
  return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) };
}

const WINDOW_RESIZE_EDGE_PX = 8;
const WINDOW_DRAG_SURFACE_SELECTOR =
  '.context-map-header, .extended-header, .workbench-window-state-panel, .workbench-window-title';
const WINDOW_DRAG_BLOCK_SELECTOR =
  'button, a, input, textarea, select, [role="button"], [role="menuitem"], .dropdown-menu, .dropdown-item, .extended-tab, .context-map-toggle';

const WINDOW_RESIZE_CURSORS: Record<WindowResizeEdge, string> = {
  top: 'ns-resize', bottom: 'ns-resize', left: 'ew-resize', right: 'ew-resize',
  'top-left': 'nwse-resize', 'bottom-right': 'nwse-resize',
  'top-right': 'nesw-resize', 'bottom-left': 'nesw-resize',
};

function resizeEdgeAt(x: number, y: number, rect: DOMRect): WindowResizeEdge | null {
  const iv = y >= rect.top - WINDOW_RESIZE_EDGE_PX && y <= rect.bottom + WINDOW_RESIZE_EDGE_PX;
  const ih = x >= rect.left - WINDOW_RESIZE_EDGE_PX && x <= rect.right + WINDOW_RESIZE_EDGE_PX;
  const top = ih && Math.abs(y - rect.top) <= WINDOW_RESIZE_EDGE_PX;
  const bottom = ih && Math.abs(y - rect.bottom) <= WINDOW_RESIZE_EDGE_PX;
  const left = iv && Math.abs(x - rect.left) <= WINDOW_RESIZE_EDGE_PX;
  const right = iv && Math.abs(x - rect.right) <= WINDOW_RESIZE_EDGE_PX;
  if (top && left) return 'top-left';
  if (top && right) return 'top-right';
  if (bottom && left) return 'bottom-left';
  if (bottom && right) return 'bottom-right';
  if (top) return 'top';
  if (bottom) return 'bottom';
  if (left) return 'left';
  if (right) return 'right';
  return null;
}

let windowChromeInstalled = false;

function installWindowChrome() {
  if (windowChromeInstalled || !window.electronAPI?.isElectron || !window.electronAPI.getWindowBounds || !window.electronAPI.setWindowBounds) return;
  windowChromeInstalled = true;
  const root = document.documentElement;
  let active = false;
  let isMaximized = false;
  const frameRect = () => document.querySelector('.workbench-window-frame')?.getBoundingClientRect() || null;
  const updateHoverCursor = (event: PointerEvent) => {
    if (active) return;
    const rect = frameRect();
    const edge = !isMaximized && rect ? resizeEdgeAt(event.clientX, event.clientY, rect) : null;
    if (edge) root.style.cursor = WINDOW_RESIZE_CURSORS[edge];
    else if (root.style.cursor) root.style.cursor = '';
  };
  const onPointerDown = (event: PointerEvent) => {
    if (event.button !== 0 || active) return;
    const rect = frameRect();
    const edge = !isMaximized && rect ? resizeEdgeAt(event.clientX, event.clientY, rect) : null;
    const target = event.target as Element | null;
    const onDragSurface = Boolean(target && target.closest(WINDOW_DRAG_SURFACE_SELECTOR) && !target.closest(WINDOW_DRAG_BLOCK_SELECTOR));
    if (!edge && !onDragSurface) return;
    event.preventDefault();
    event.stopPropagation();
    active = true;
    const startPoint = { x: event.screenX, y: event.screenY };
    void window.electronAPI!.getWindowBounds!().then((startBounds) => {
      if (!startBounds) { active = false; return; }
      root.dataset[edge ? 'workbenchWindowResizing' : 'workbenchWindowMoving'] = edge || 'true';
      root.style.cursor = edge ? WINDOW_RESIZE_CURSORS[edge] : 'grabbing';
      const onMove = (ev: PointerEvent) => {
        ev.preventDefault();
        const point = { x: ev.screenX, y: ev.screenY };
        if (edge) window.electronAPI?.setWindowBounds?.(nextWindowBounds(edge, startBounds, startPoint, point));
        else window.electronAPI?.setWindowBounds?.({ x: Math.round(startBounds.x + (point.x - startPoint.x)), y: Math.round(startBounds.y + (point.y - startPoint.y)), width: startBounds.width, height: startBounds.height });
      };
      const onStop = () => {
        active = false;
        delete root.dataset.workbenchWindowResizing;
        delete root.dataset.workbenchWindowMoving;
        root.style.cursor = '';
        window.removeEventListener('pointermove', onMove, true);
        window.removeEventListener('pointerup', onStop, true);
        window.removeEventListener('pointercancel', onStop, true);
      };
      window.addEventListener('pointermove', onMove, true);
      window.addEventListener('pointerup', onStop, true);
      window.addEventListener('pointercancel', onStop, true);
    });
  };
  window.addEventListener('pointermove', updateHoverCursor, true);
  window.addEventListener('pointerdown', onPointerDown, true);
  void window.electronAPI.isWindowMaximized?.().then((next) => {
    isMaximized = Boolean(next);
    root.dataset.workbenchWindowMaximized = isMaximized ? 'true' : 'false';
  });
  window.electronAPI.onWindowMaximizedChange?.((next) => {
    isMaximized = next;
    root.dataset.workbenchWindowMaximized = isMaximized ? 'true' : 'false';
    if (isMaximized) root.style.cursor = '';
  });
}

function WorkbenchWindowChrome() {
  useEffect(() => { installWindowChrome(); }, []);
  return null;
}

function WorkbenchWindowControls({ uiLocale }: { uiLocale: UiLocale }) {
  return (
    <div className="workbench-window-controls" aria-label={windowText(uiLocale, 'Window controls', '窗口控制')}>
      <button className="workbench-window-control-btn" type="button" title={windowText(uiLocale, 'Minimize', '最小化')} aria-label={windowText(uiLocale, 'Minimize', '最小化')} onMouseDown={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()} onClick={() => window.electronAPI?.minimize?.()}><i className="ph-light ph-minus" /></button>
      <button className="workbench-window-control-btn" type="button" title={windowText(uiLocale, 'Maximize', '最大化')} aria-label={windowText(uiLocale, 'Maximize', '最大化')} onMouseDown={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()} onClick={() => window.electronAPI?.maximize?.()}><i className="ph-light ph-square" /></button>
      <button className="workbench-window-control-btn close" type="button" title={windowText(uiLocale, 'Close', '关闭')} aria-label={windowText(uiLocale, 'Close', '关闭')} onMouseDown={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()} onClick={() => window.electronAPI?.close?.()}><i className="ph-light ph-x" /></button>
    </div>
  );
}

export default function WorkbenchWindow() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [uiLocale, setUiLocale] = useState(normalizeSupportedLocale('en-US'));
  const [themeMode, setThemeMode] = useState<'light' | 'dark'>('light');
  const [uiFont, setUiFont] = useState('Noto Serif SC');
  const [uiFontSize, setUiFontSize] = useState(15);
  const [proxySessionId, setProxySessionId] = useState('');
  const [isProxyRunning, setIsProxyRunning] = useState(false);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [contextWorkbenchChats, setContextWorkbenchChats] = useState<Record<string, ContextWorkbenchChatMessage[]>>({});
  const [reasoningOptions] = useState<ReasoningOption[]>(normalizeReasoningOptions());
  const [proxyUsageSummary, setProxyUsageSummary] = useState<ProxyUsageSummary | null>(null);
  const [realtimeError, setRealtimeError] = useState('');
  const loadRequestIdRef = useRef(0);
  const visibleLoadInFlightRef = useRef(false);
  const messagesSignatureRef = useRef('');
  const proxySessionIdRef = useRef('');
  const transcriptRef = useRef<TranscriptEntry[]>([]);
  const transcriptVersionRef = useRef(0);
  const lastRealtimeEventIdRef = useRef(0);
  const realtimeClientIdRef = useRef(`frontend-window-${Math.random().toString(36).slice(2)}`);

  const setMessagesIfChanged = useCallback((next: MessageRecord[]) => {
    const sig = conversationSignature(next);
    if (sig === messagesSignatureRef.current) return;
    messagesSignatureRef.current = sig;
    setMessages(next);
  }, []);

  const applyProxySession = useCallback((session: ProxySessionSummary | null | undefined) => {
    if (!session?.id) return;
    const sessionChanged = proxySessionIdRef.current !== session.id;
    proxySessionIdRef.current = session.id;
    const version = Number(session.transcript_version || 0);
    transcriptVersionRef.current = sessionChanged ? version : Math.max(transcriptVersionRef.current, version);
    transcriptRef.current = session.transcript || [];
    setProxySessionId(session.id);
    setIsProxyRunning(isProxyBusy(session.status, session.is_running));
    setProxyUsageSummary(session.usage_summary || null);
    setMessagesIfChanged(normalizeConversation(transcriptRef.current));
  }, [setMessagesIfChanged]);

  const loadInit = useCallback(async (opts: { silent?: boolean; targetSessionId?: string } = {}) => {
    const targetSid = opts.targetSessionId?.trim() || currentUrlSessionId();
    if (opts.silent && visibleLoadInFlightRef.current) return;
    const requestId = opts.silent ? loadRequestIdRef.current : loadRequestIdRef.current + 1;
    if (!opts.silent) {
      loadRequestIdRef.current = requestId;
      visibleLoadInFlightRef.current = true;
      setLoading(true);
      setError('');
    }
    const isCurrentLoad = () => loadRequestIdRef.current === requestId;

    try {
      const session = targetSid
        ? await fetchProxySessionRequest(targetSid)
        : await (async () => {
            const proxyPayload = await fetchProxySessionsRequest();
            if (!isCurrentLoad()) return null;
            const activeSummary = proxyPayload.sessions.find((s) => s.id === proxyPayload.active_session_id) || proxyPayload.sessions[0] || null;
            return activeSummary ? fetchProxySessionRequest(activeSummary.id) : null;
          })();
      if (!isCurrentLoad()) return;

      if (!session) {
        proxySessionIdRef.current = '';
        transcriptVersionRef.current = 0;
        transcriptRef.current = [];
        setProxySessionId('');
        setIsProxyRunning(false);
        setProxyUsageSummary(null);
        setMessagesIfChanged([]);
        return;
      }

      if (!opts.silent) {
        const initPayload = await fetchInit({ sessionId: session.id, includeConversation: false });
        if (!isCurrentLoad()) return;
        if (initPayload.settings) {
          setUiLocale(normalizeSupportedLocale(initPayload.settings.user_locale));
          setThemeMode(initPayload.settings.theme_mode === 'dark' ? 'dark' : 'light');
        }
        setContextWorkbenchChats((prev) => ({
          ...prev,
          [session.id]: initPayload.context_workbench_histories?.[session.id] || prev[session.id] || [],
        }));
      }

      applyProxySession(session);
      setRealtimeError('');

      void syncProxySessionRequest({ session_id: session.id, title: session.title || 'Codex Context' }).catch((caught) => {
        setRealtimeError(caught instanceof Error ? caught.message : String(caught));
      });
    } catch (caught) {
      if (!opts.silent) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (!opts.silent) { visibleLoadInFlightRef.current = false; if (isCurrentLoad()) setLoading(false); }
    }
  }, [applyProxySession]);

  useEffect(() => { void loadInit(); }, [loadInit]);

  useEffect(() => { document.documentElement.lang = uiLocale === 'zh-CN' ? 'zh-CN' : 'en'; }, [uiLocale]);

  useEffect(() => {
    if (themeMode === 'dark') { document.documentElement.dataset.themeMode = 'dark'; window.electronAPI?.setWindowThemeMode?.('dark'); return; }
    delete document.documentElement.dataset.themeMode;
    window.electronAPI?.setWindowThemeMode?.('light');
  }, [themeMode]);

  useEffect(() => {
    const font = uiFont.trim() ? `"${uiFont.trim()}", "Noto Serif SC", Georgia, "Times New Roman", serif` : `"Noto Serif SC", Georgia, "Times New Roman", serif`;
    document.documentElement.style.setProperty('--ui-font-family', font);
    document.documentElement.style.setProperty('--code-font-family', font);
    document.documentElement.style.setProperty('--ui-font-size', `${uiFontSize}px`);
    document.documentElement.style.fontSize = `${uiFontSize}px`;
    document.documentElement.style.fontFamily = font;
    document.body.style.fontFamily = font;
    document.getElementById('root')?.style.setProperty('font-family', font);
    document.documentElement.style.zoom = '';
  }, [uiFont, uiFontSize]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ sessionId?: string }>).detail;
      const targetSid = detail?.sessionId?.trim() || '';
      if (targetSid) {
        const u = new URL(window.location.href);
        u.searchParams.set('session_id', targetSid);
        window.history.replaceState(null, '', `${u.pathname}${u.search}${u.hash}`);
      }
      void loadInit({ silent: !targetSid, targetSessionId: targetSid });
    };
    window.addEventListener('hash-context-window-show', handler);
    return () => window.removeEventListener('hash-context-window-show', handler);
  }, [loadInit]);

  const applyRealtimeEvent = useCallback((event: ProxyRealtimeEvent) => {
    if (typeof event.event_id === 'number') {
      lastRealtimeEventIdRef.current = Math.max(lastRealtimeEventIdRef.current, event.event_id);
    }

    if (event.type === 'connection_ack' || event.type === 'pong') {
      setRealtimeError('');
      return;
    }

    if (event.type === 'error') {
      setRealtimeError(event.message || 'Realtime proxy error');
      return;
    }

    const eventSessionId = event.session_id || event.session?.id || '';
    const activeSessionId = proxySessionIdRef.current;
    if (eventSessionId && activeSessionId && eventSessionId !== activeSessionId) return;

    setRealtimeError('');

    if (event.type === 'snapshot') {
      applyProxySession(event.session);
      return;
    }

    if (event.type === 'session_status') {
      setIsProxyRunning(isProxyBusy(event.status || event.session?.status, event.is_running ?? event.session?.is_running));
      if (event.session?.usage_summary) setProxyUsageSummary(event.session.usage_summary);
      return;
    }

    if (event.type === 'transcript_update') {
      const nextVersion = Number(event.transcript_version || event.session?.transcript_version || 0);
      if (nextVersion && nextVersion <= transcriptVersionRef.current) return;
      if (nextVersion) transcriptVersionRef.current = nextVersion;
      transcriptRef.current = event.transcript || event.session?.transcript || [];
      setMessagesIfChanged(normalizeConversation(transcriptRef.current));
      if (event.session) {
        setIsProxyRunning(isProxyBusy(event.session.status, event.session.is_running));
        setProxyUsageSummary(event.session.usage_summary || null);
      }
      return;
    }

    if (event.type === 'transcript_patch') {
      const baseVersion = Number(event.base_version || 0);
      const nextVersion = Number(event.next_version || event.session?.transcript_version || 0);
      if (baseVersion !== transcriptVersionRef.current) {
        setRealtimeError(windowText(uiLocale, 'Transcript version mismatch', '上下文版本不一致'));
        return;
      }
      transcriptRef.current = applyTranscriptPatch(transcriptRef.current, event.ops || []);
      if (nextVersion) transcriptVersionRef.current = nextVersion;
      setMessagesIfChanged(normalizeConversation(transcriptRef.current));
      if (event.session) {
        setIsProxyRunning(isProxyBusy(event.session.status, event.session.is_running));
        setProxyUsageSummary(event.session.usage_summary || null);
      }
      return;
    }

    if (event.type === 'usage_update') {
      setProxyUsageSummary(event.usage_summary || null);
    }
  }, [applyProxySession, setMessagesIfChanged, uiLocale]);

  useEffect(() => {
    if (!proxySessionId) return undefined;

    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let attempt = 0;

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(proxyRealtimeUrl());

      socket.onopen = () => {
        attempt = 0;
        setRealtimeError('');
        socket?.send(JSON.stringify({
          type: 'subscribe',
          client_id: realtimeClientIdRef.current,
          session_id: proxySessionId,
          last_event_id: lastRealtimeEventIdRef.current,
        }));
      };

      socket.onmessage = (message) => {
        try {
          applyRealtimeEvent(JSON.parse(String(message.data)) as ProxyRealtimeEvent);
        } catch (caught) {
          setRealtimeError(caught instanceof Error ? caught.message : String(caught));
        }
      };

      socket.onerror = () => {
        setRealtimeError(windowText(uiLocale, 'Realtime connection failed', '实时连接失败'));
      };

      socket.onclose = () => {
        if (disposed) return;
        setRealtimeError(windowText(uiLocale, 'Realtime connection closed', '实时连接已断开'));
        const delay = Math.min(1000 * (2 ** attempt), 10000);
        attempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [applyRealtimeEvent, proxySessionId, uiLocale]);

  const currentContextWorkbenchChat = useMemo(
    () => (proxySessionId ? contextWorkbenchChats[proxySessionId] || [] : []),
    [contextWorkbenchChats, proxySessionId],
  );

  const handleConversationChange = useCallback(
    (changedSessionId: string, conversation: MessageRecord[]) => {
      if (changedSessionId !== proxySessionId) return;
      setMessagesIfChanged(conversation);
    },
    [proxySessionId, setMessagesIfChanged],
  );

  if (loading) {
    return (
      <main className="workbench-window-shell loading">
        <WorkbenchWindowChrome />
        <div className="workbench-window-frame loading">
          <WorkbenchWindowControls uiLocale={uiLocale} />
          <div className="workbench-window-state-panel">
            <div className="workbench-window-title">{windowText(uiLocale, 'Context Workbench', '上下文工作台')}</div>
            <div className="workbench-window-muted">{windowText(uiLocale, 'Loading current context...', '正在加载当前上下文...')}</div>
          </div>
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="workbench-window-shell loading">
        <WorkbenchWindowChrome />
        <div className="workbench-window-frame loading">
          <WorkbenchWindowControls uiLocale={uiLocale} />
          <div className="workbench-window-state-panel">
            <div className="workbench-window-title">{windowText(uiLocale, 'Context Workbench', '上下文工作台')}</div>
            <div className="workbench-window-error">{error}</div>
            <button className="workbench-window-button" type="button" onClick={() => void loadInit()}>{windowText(uiLocale, 'Retry', '重试')}</button>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="workbench-window-shell">
      <WorkbenchWindowChrome />
      <div className="workbench-window-frame">
        <WorkbenchWindowControls uiLocale={uiLocale} />
        {realtimeError ? <div className="workbench-realtime-error">{realtimeError}</div> : null}
        <ContextMapSidebar
          stage={2}
          messages={messages}
          onToggle={() => undefined}
          onJumpToMessage={() => undefined}
          sessionId={proxySessionId}
          isMainChatBusy={isProxyRunning}
          contextWorkbenchChat={currentContextWorkbenchChat}
          reasoningOptions={reasoningOptions}
          proxyUsageSummary={proxyUsageSummary}
          uiLocale={uiLocale}
          themeMode={themeMode}
          onContextWorkbenchChatChange={(sid, chat) => {
            setContextWorkbenchChats((prev) => ({ ...prev, [sid]: chat }));
          }}
          onContextWorkbenchConversationChange={handleConversationChange}
          onProxyUsageSummaryChange={setProxyUsageSummary}
          onEnsureSession={async () => proxySessionId}
          onUiLocaleChange={(locale) => setUiLocale(normalizeSupportedLocale(locale))}
          onUiFontChange={(font, size) => { setUiFont(font); setUiFontSize(size); }}
          onThemeModeChange={(mode: 'light' | 'dark') => setThemeMode(mode)}
        />
      </div>
    </main>
  );
}
