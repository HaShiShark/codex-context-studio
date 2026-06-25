import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  fetchInit,
  fetchProxySessionRequest,
  fetchProxySessionsRequest,
  fetchProxySessionUsageRequest,
  syncProxySessionRequest,
} from './api';
import ContextMapSidebar from './components/ContextMapSidebar';
import { normalizeSupportedLocale, type UiLocale } from './i18n';
import type {
  ContextWorkbenchHistoryEntry,
  MessageRecord,
  ProxyUsageSummary,
  ReasoningOption,
  TranscriptRecord,
} from './types';
import { normalizeConversation, normalizeReasoningOptions } from './utils';

const LIVE_REFRESH_IDLE_MS = 4000;
const LIVE_REFRESH_RUNNING_MS = 1500;
const PENDING_CONTEXT_REFRESH_MS = 400;
const PENDING_CONTEXT_REFRESH_MAX_MS = 8000;
const LOCAL_EDIT_GRACE_MS = 2000;
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

function hasPendingAssistantMessage(messages: MessageRecord[]): boolean {
  const lastAssistant = [...messages].reverse().find((message) => message.role === 'an');
  if (!lastAssistant) return false;
  return Boolean(
    lastAssistant.pending
    || (!lastAssistant.text.trim() && lastAssistant.blocks.some((block) => block.kind === 'thinking'))
    || lastAssistant.blocks.some((block) => block.kind === 'reasoning' && block.status === 'streaming'),
  );
}

function currentUrlSessionId(): string {
  return new URLSearchParams(window.location.search).get('session_id')?.trim() || '';
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
  const [histories, setHistories] = useState<Record<string, ContextWorkbenchHistoryEntry[]>>({});
  const [reasoningOptions] = useState<ReasoningOption[]>(normalizeReasoningOptions());
  const [proxyUsageSummary, setProxyUsageSummary] = useState<ProxyUsageSummary | null>(null);
  const loadRequestIdRef = useRef(0);
  const visibleLoadInFlightRef = useRef(false);
  const lastLocalEditAtRef = useRef(0);
  const messagesSignatureRef = useRef('');

  const setMessagesIfChanged = useCallback((next: MessageRecord[]) => {
    const sig = conversationSignature(next);
    if (sig === messagesSignatureRef.current) return;
    messagesSignatureRef.current = sig;
    setMessages(next);
  }, []);

  const refreshProxyUsage = useCallback(async (sid: string) => {
    if (!sid) { setProxyUsageSummary(null); return; }
    try { const r = await fetchProxySessionUsageRequest(sid); setProxyUsageSummary(r.summary || null); } catch { /* */ }
  }, []);

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
      const [proxyPayload] = await Promise.all([fetchProxySessionsRequest().catch(() => null)]);
      if (!isCurrentLoad()) return;

      const activeSummary = targetSid
        ? proxyPayload?.sessions.find((s) => s.id === targetSid) || null
        : proxyPayload?.sessions.find((s) => s.id === proxyPayload.active_session_id) || proxyPayload?.sessions[0] || null;

      if (!activeSummary) {
        if (!opts.silent) { setLoading(false); visibleLoadInFlightRef.current = false; }
        return;
      }

      const session = await fetchProxySessionRequest(activeSummary.id).catch(() => activeSummary);
      if (!isCurrentLoad()) return;

      const transcript: TranscriptRecord[] = session.active_transcript || session.transcript || [];
      const nextMessages = normalizeConversation(transcript);
      const nextIsRunning = isProxyBusy(session.status, session.is_running);

      // Load workbench settings from /api/init
      if (!opts.silent) {
        const initPayload = await fetchInit().catch(() => null);
        if (!isCurrentLoad()) return;
        if (initPayload?.settings) {
          setUiLocale(normalizeSupportedLocale(initPayload.settings.user_locale));
          setThemeMode(initPayload.settings.theme_mode === 'dark' ? 'dark' : 'light');
        }
      }

      setProxySessionId(session.id);
      setIsProxyRunning(nextIsRunning);
      setMessagesIfChanged(nextMessages);
      setHistories((prev) => ({
        ...prev,
        [session.id]: session.workbench_history || prev[session.id] || [],
      }));
      setProxyUsageSummary(session.usage_summary || null);
      void refreshProxyUsage(session.id);

      // Sync title
      void syncProxySessionRequest({ session_id: session.id, title: session.title || 'Codex Context' }).catch(() => {});
    } catch (caught) {
      if (!opts.silent) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (!opts.silent) { visibleLoadInFlightRef.current = false; if (isCurrentLoad()) setLoading(false); }
    }
  }, [refreshProxyUsage, setMessagesIfChanged]);

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

  useEffect(() => {
    const ms = isProxyRunning ? LIVE_REFRESH_RUNNING_MS : LIVE_REFRESH_IDLE_MS;
    const id = window.setInterval(() => {
      if (Date.now() - lastLocalEditAtRef.current < LOCAL_EDIT_GRACE_MS) return;
      void loadInit({ silent: true });
    }, ms);
    return () => window.clearInterval(id);
  }, [isProxyRunning, loadInit]);

  const hasPendingAssistant = useMemo(() => hasPendingAssistantMessage(messages), [messages]);

  useEffect(() => {
    if (!hasPendingAssistant) return undefined;
    const startedAt = Date.now();
    const id = window.setInterval(() => {
      if (Date.now() - startedAt >= PENDING_CONTEXT_REFRESH_MAX_MS) { window.clearInterval(id); return; }
      if (Date.now() - lastLocalEditAtRef.current < LOCAL_EDIT_GRACE_MS) return;
      void loadInit({ silent: true });
    }, PENDING_CONTEXT_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [hasPendingAssistant, loadInit]);

  const currentHistory = useMemo(
    () => (proxySessionId ? histories[proxySessionId] || [] : []),
    [histories, proxySessionId],
  );

  const handleConversationChange = useCallback(
    (changedSessionId: string, conversation: MessageRecord[]) => {
      if (changedSessionId !== proxySessionId) return;
      lastLocalEditAtRef.current = Date.now();
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
        <ContextMapSidebar
          stage={2}
          messages={messages}
          onToggle={() => undefined}
          onJumpToMessage={() => undefined}
          sessionId={proxySessionId}
          isMainChatBusy={isProxyRunning}
          contextWorkbenchHistory={currentHistory}
          reasoningOptions={reasoningOptions}
          proxyUsageSummary={proxyUsageSummary}
          uiLocale={uiLocale}
          themeMode={themeMode}
          onContextWorkbenchHistoryChange={(sid, history) => {
            setHistories((prev) => ({ ...prev, [sid]: history }));
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
