import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from 'react';

import './ContextMapSidebar.polish.css';
import ContextWorkbench from './ContextWorkbench';
import ContextMapNodeList from './ContextMapNodeList';
import ContextMinimap from './ContextMinimap';
import type {
  ContextWorkbenchHistoryEntry,
  MessageRecord,
  ProxyUsageSummary,
  ReasoningOption,
} from '../types';
import {
  buildContextMapNodeMeta,
  DEFAULT_CONTEXT_TOKEN_THRESHOLDS,
  type ContextTokenThresholds,
} from '../contextTokenWeight';
import {
  DEFAULT_SCROLL_METRICS,
  MINIMAP_CONTENT_PADDING_PX,
  MINIMAP_VIEWPORT_KEEP_OFFSET_PX,
  MINIMAP_VIEWPORT_MIN_HEIGHT_PX,
  SELECTION_AUTO_SCROLL_EDGE_PX,
  SELECTION_AUTO_SCROLL_MAX_SPEED_PX,
  SELECTION_DRAG_THRESHOLD_PX,
  areIndexSetsEqual,
  areNodeLayoutsEqual,
  areScrollMetricsEqual,
  buildFallbackMinimapBars,
  buildMessageStats,
  buildMinimapBars,
  buildRangeSelection,
  clampIndexSet,
  contextMapContentSignature,
  getFallbackMinimapContentHeightPx,
  sidebarText,
  type MessageStat,
  type MinimapBarLayout,
  type NodeLayout,
  type ScrollMetrics,
} from './ContextMapSidebar.helpers';

interface ContextMapSidebarProps {
  stage: 0 | 1 | 2;
  messages: MessageRecord[];
  onToggle: () => void;
  onJumpToMessage: (messageIndex: number) => void;
  sessionId: string;
  isMainChatBusy: boolean;
  contextWorkbenchHistory: ContextWorkbenchHistoryEntry[];
  reasoningOptions: ReasoningOption[];
  proxyUsageSummary: ProxyUsageSummary | null;
  uiLocale: 'zh-CN' | 'en-US';
  onContextWorkbenchHistoryChange: (sessionId: string, history: ContextWorkbenchHistoryEntry[]) => void;
  onContextWorkbenchConversationChange: (
    sessionId: string,
    conversation: MessageRecord[],
    options?: { resetProxyOverride?: boolean; skipProxyOverride?: boolean },
  ) => void | Promise<void>;
  onProxyUsageSummaryChange: (summary: ProxyUsageSummary | null) => void;
  onEnsureSession: () => Promise<string>;
  onUiLocaleChange?: (locale: 'zh-CN' | 'en-US') => void;
}

export default function ContextMapSidebar({
  stage,
  messages,
  onToggle,
  onJumpToMessage,
  sessionId,
  isMainChatBusy,
  contextWorkbenchHistory,
  reasoningOptions,
  proxyUsageSummary,
  uiLocale,
  onContextWorkbenchHistoryChange,
  onContextWorkbenchConversationChange,
  onProxyUsageSummaryChange,
  onEnsureSession,
  onUiLocaleChange,
}: ContextMapSidebarProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const minimapRef = useRef<HTMLDivElement>(null);
  const minimapScrollRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef<Array<HTMLDivElement | null>>([]);
  const minimapDragRef = useRef<{
    offsetPx: number;
  } | null>(null);
  const selectionDragRef = useRef<{
    startIndex: number;
    lastIndex: number;
    startClientY: number;
    pointerClientY: number;
    originSelection: Set<number>;
    mode: 'replace' | 'add';
    hasMoved: boolean;
  } | null>(null);
  const selectionAutoScrollFrameRef = useRef<number | null>(null);
  const lastContentSignatureRef = useRef('');
  const [expandedIndexes, setExpandedIndexes] = useState<Set<number>>(new Set());
  const [selectedIndexes, setSelectedIndexes] = useState<Set<number>>(new Set());
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [nodeLayouts, setNodeLayouts] = useState<NodeLayout[]>([]);
  const [previewTruncatedIndexes, setPreviewTruncatedIndexes] = useState<Set<number>>(new Set());
  const [scrollMetrics, setScrollMetrics] = useState<ScrollMetrics>(DEFAULT_SCROLL_METRICS);
  const [tokenThresholds, setTokenThresholds] = useState<ContextTokenThresholds>(DEFAULT_CONTEXT_TOKEN_THRESHOLDS);
  const showMinimap = stage === 2;
  const contentSignature = useMemo(() => contextMapContentSignature(messages), [messages]);
  const nodeMeta = useMemo(() => buildContextMapNodeMeta(messages), [messages]);
  const selectableIndexes = useMemo(
    () => new Set(nodeMeta.map((meta, index) => (meta.selectable ? index : -1)).filter((index) => index >= 0)),
    [nodeMeta],
  );

  const messageStats: MessageStat[] = useMemo(
    () => buildMessageStats(messages, nodeMeta, tokenThresholds),
    [messages, nodeMeta, tokenThresholds],
  );

  const scrollRange = Math.max(scrollMetrics.scrollHeight - scrollMetrics.clientHeight, 0);
  const scrollRatio = scrollRange <= 0 ? 0 : scrollMetrics.scrollTop / scrollRange;
  const fallbackMinimapBars: MinimapBarLayout[] = useMemo(
    () => buildFallbackMinimapBars(messages, messageStats),
    [messageStats, messages],
  );

  const fallbackMinimapContentHeightPx = useMemo(() => {
    return getFallbackMinimapContentHeightPx(fallbackMinimapBars);
  }, [fallbackMinimapBars]);

  const minimapContentHeightPx = Math.max(
    fallbackMinimapContentHeightPx,
    MINIMAP_CONTENT_PADDING_PX * 2 + MINIMAP_VIEWPORT_MIN_HEIGHT_PX,
  );
  const minimapUsableHeightPx = Math.max(minimapContentHeightPx - MINIMAP_CONTENT_PADDING_PX * 2, 1);
  const minimapViewportHeightPx = Math.min(
    minimapUsableHeightPx,
    Math.max(
      scrollMetrics.scrollHeight <= 0
        ? minimapUsableHeightPx
        : (scrollMetrics.clientHeight / scrollMetrics.scrollHeight) * minimapUsableHeightPx,
      MINIMAP_VIEWPORT_MIN_HEIGHT_PX,
    ),
  );
  const minimapViewportTravelPx = Math.max(minimapUsableHeightPx - minimapViewportHeightPx, 0);
  const minimapViewportTopPx = MINIMAP_CONTENT_PADDING_PX + scrollRatio * minimapViewportTravelPx;
  const effectiveScrollHeight = Math.max(scrollMetrics.scrollHeight, 1);
  const minimapBars: MinimapBarLayout[] = useMemo(
    () =>
      buildMinimapBars({
        effectiveScrollHeight,
        fallbackMinimapBars,
        messageStats,
        messages,
        minimapContentHeightPx,
        minimapUsableHeightPx,
        nodeLayouts,
      }),
    [
      effectiveScrollHeight,
      fallbackMinimapBars,
      messageStats,
      messages,
      minimapContentHeightPx,
      minimapUsableHeightPx,
      nodeLayouts,
    ],
  );

  useEffect(() => {
    setExpandedIndexes(new Set());
    setSelectedIndexes(new Set());
    setHoveredIndex(null);
    selectionDragRef.current = null;
    minimapDragRef.current = null;
    nodeRefs.current = [];
    setNodeLayouts([]);
    setScrollMetrics(DEFAULT_SCROLL_METRICS);
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [sessionId]);

  useEffect(() => {
    if (!lastContentSignatureRef.current) {
      lastContentSignatureRef.current = contentSignature;
      return;
    }

    if (lastContentSignatureRef.current === contentSignature) {
      return;
    }

    lastContentSignatureRef.current = contentSignature;
    setExpandedIndexes(new Set());
    setSelectedIndexes(new Set());
    setHoveredIndex(null);
    selectionDragRef.current = null;
    minimapDragRef.current = null;
    nodeRefs.current = [];
    setNodeLayouts([]);
  }, [contentSignature]);

  useEffect(() => {
    setExpandedIndexes((previous) => {
      const next = clampIndexSet(previous, messages.length);
      return next.size === previous.size ? previous : next;
    });
  }, [messages.length]);

  useEffect(() => {
    setSelectedIndexes((previous) => {
      const next = clampIndexSet(previous, messages.length, selectableIndexes);
      return next.size === previous.size ? previous : next;
    });
  }, [messages.length, selectableIndexes]);

  useEffect(() => {
    if (stage === 1) {
      setExpandedIndexes(new Set());
    }
  }, [stage]);

  useEffect(() => {
    function measureNodes() {
      const container = scrollRef.current;
      if (!container) {
        return;
      }

      const nextLayouts = messages.map((_, index) => {
        const node = nodeRefs.current[index];
        if (!node) {
          return { top: 0, height: 0 };
        }

        return {
          top: node.offsetTop,
          height: node.offsetHeight,
        };
      });
      const nextPreviewTruncatedIndexes = new Set<number>();

      nodeRefs.current.forEach((node, index) => {
        const previewNode = node?.querySelector<HTMLElement>('.map-preview-text');
        if (previewNode && previewNode.scrollWidth > previewNode.clientWidth + 1) {
          nextPreviewTruncatedIndexes.add(index);
        }
      });

      setNodeLayouts((current) => (areNodeLayoutsEqual(current, nextLayouts) ? current : nextLayouts));
      setPreviewTruncatedIndexes((current) => {
        const next = new Set(nextPreviewTruncatedIndexes);
        expandedIndexes.forEach((index) => {
          if (current.has(index)) {
            next.add(index);
          }
        });
        return areIndexSetsEqual(current, next) ? current : next;
      });
      const nextScrollMetrics = {
        clientHeight: container.clientHeight || 1,
        scrollHeight: container.scrollHeight || 1,
        scrollTop: container.scrollTop,
      };
      setScrollMetrics((current) => (
        areScrollMetricsEqual(current, nextScrollMetrics) ? current : nextScrollMetrics
      ));
    }

    const frameId = window.requestAnimationFrame(measureNodes);
    const resizeObserver =
      typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => measureNodes()) : null;

    if (scrollRef.current && resizeObserver) {
      resizeObserver.observe(scrollRef.current);
    }

    nodeRefs.current.forEach((node) => {
      if (node && resizeObserver) {
        resizeObserver.observe(node);
      }
    });

    window.addEventListener('resize', measureNodes);

    return () => {
      window.cancelAnimationFrame(frameId);
      window.removeEventListener('resize', measureNodes);
      resizeObserver?.disconnect();
    };
  }, [messages.length, expandedIndexes, stage]);

  useEffect(() => {
    const container = scrollRef.current;
    if (!container) {
      return undefined;
    }
    const activeContainer = container;

    function syncScrollMetrics() {
      const nextScrollMetrics = {
        clientHeight: activeContainer.clientHeight || 1,
        scrollHeight: activeContainer.scrollHeight || 1,
        scrollTop: activeContainer.scrollTop,
      };
      setScrollMetrics((current) => (
        areScrollMetricsEqual(current, nextScrollMetrics) ? current : nextScrollMetrics
      ));
    }

    syncScrollMetrics();
    activeContainer.addEventListener('scroll', syncScrollMetrics, { passive: true });

    return () => {
      activeContainer.removeEventListener('scroll', syncScrollMetrics);
    };
  }, [messages.length, stage]);

  useEffect(() => {
    const minimapScroller = minimapScrollRef.current;
    if (!minimapScroller) {
      return;
    }

    const maxScroll = Math.max(minimapContentHeightPx - minimapScroller.clientHeight, 0);
    const desiredScrollTop = Math.min(
      Math.max(minimapViewportTopPx - MINIMAP_VIEWPORT_KEEP_OFFSET_PX, 0),
      maxScroll,
    );

    minimapScroller.scrollTop = desiredScrollTop;
  }, [minimapContentHeightPx, minimapViewportTopPx, stage, messages.length]);

  useEffect(() => {
    function handleWindowMouseMove(event: MouseEvent) {
      syncScrollFromMinimap(event.clientY);
      updateDraggedSelection(event.clientY);
      ensureSelectionAutoScroll();
    }

    function handleWindowMouseUp() {
      minimapDragRef.current = null;
      finishSelectionDrag();
    }

    window.addEventListener('mousemove', handleWindowMouseMove);
    window.addEventListener('mouseup', handleWindowMouseUp);

    return () => {
      window.removeEventListener('mousemove', handleWindowMouseMove);
      window.removeEventListener('mouseup', handleWindowMouseUp);
    };
  });

  useEffect(() => {
    return () => {
      if (selectionAutoScrollFrameRef.current !== null) {
        window.cancelAnimationFrame(selectionAutoScrollFrameRef.current);
      }
    };
  }, []);

  function toggleMessage(index: number) {
    setExpandedIndexes((previous) => {
      const next = new Set(previous);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }

  function setNodeRef(index: number, node: HTMLDivElement | null) {
    nodeRefs.current[index] = node;
  }

  function getNodeIndexFromClientY(clientY: number) {
    const container = scrollRef.current;
    if (!container || !nodeLayouts.length) {
      return null;
    }

    const rect = container.getBoundingClientRect();
    const relativeY = clientY - rect.top + container.scrollTop;

    for (let index = 0; index < nodeLayouts.length; index += 1) {
      const layout = nodeLayouts[index];
      const middleY = layout.top + layout.height / 2;
      if (relativeY < middleY) {
        return index;
      }
    }

    return nodeLayouts.length - 1;
  }

  function stopSelectionAutoScroll() {
    if (selectionAutoScrollFrameRef.current !== null) {
      window.cancelAnimationFrame(selectionAutoScrollFrameRef.current);
      selectionAutoScrollFrameRef.current = null;
    }
  }

  function getSelectionAutoScrollDelta(clientY: number) {
    const container = scrollRef.current;
    if (!container) {
      return 0;
    }

    const rect = container.getBoundingClientRect();
    const topDistance = clientY - rect.top;
    const bottomDistance = rect.bottom - clientY;

    if (topDistance < SELECTION_AUTO_SCROLL_EDGE_PX) {
      const progress = (SELECTION_AUTO_SCROLL_EDGE_PX - topDistance) / SELECTION_AUTO_SCROLL_EDGE_PX;
      return -Math.max(1, Math.round(progress * SELECTION_AUTO_SCROLL_MAX_SPEED_PX));
    }

    if (bottomDistance < SELECTION_AUTO_SCROLL_EDGE_PX) {
      const progress = (SELECTION_AUTO_SCROLL_EDGE_PX - bottomDistance) / SELECTION_AUTO_SCROLL_EDGE_PX;
      return Math.max(1, Math.round(progress * SELECTION_AUTO_SCROLL_MAX_SPEED_PX));
    }

    return 0;
  }

  function ensureSelectionAutoScroll() {
    if (!selectionDragRef.current || selectionAutoScrollFrameRef.current !== null) {
      return;
    }

    const tick = () => {
      const dragState = selectionDragRef.current;
      const container = scrollRef.current;

      if (!dragState || !container) {
        selectionAutoScrollFrameRef.current = null;
        return;
      }

      const scrollDelta = getSelectionAutoScrollDelta(dragState.pointerClientY);
      if (scrollDelta === 0) {
        selectionAutoScrollFrameRef.current = null;
        return;
      }

      const nextScrollTop = Math.min(
        Math.max(container.scrollTop + scrollDelta, 0),
        Math.max(container.scrollHeight - container.clientHeight, 0),
      );

      if (nextScrollTop !== container.scrollTop) {
        container.scrollTop = nextScrollTop;
        updateDraggedSelection(dragState.pointerClientY, true);
      }

      selectionAutoScrollFrameRef.current = window.requestAnimationFrame(tick);
    };

    selectionAutoScrollFrameRef.current = window.requestAnimationFrame(tick);
  }

  function updateDraggedSelection(clientY: number, forceActive = false) {
    const dragState = selectionDragRef.current;
    if (!dragState) {
      return;
    }

    dragState.pointerClientY = clientY;

    const targetIndex = getNodeIndexFromClientY(clientY);
    if (targetIndex === null) {
      return;
    }

    const crossedThreshold =
      Math.abs(clientY - dragState.startClientY) > SELECTION_DRAG_THRESHOLD_PX || targetIndex !== dragState.startIndex;

    if (!dragState.hasMoved && (forceActive || crossedThreshold)) {
      dragState.hasMoved = true;
    }

    if (!dragState.hasMoved) {
      return;
    }

    if (dragState.lastIndex === targetIndex && !forceActive) {
      return;
    }

    dragState.lastIndex = targetIndex;
    setSelectedIndexes(
      buildRangeSelection(
        dragState.startIndex,
        targetIndex,
        dragState.originSelection,
        dragState.mode,
        selectableIndexes,
      ),
    );
  }

  function finishSelectionDrag() {
    const dragState = selectionDragRef.current;
    if (!dragState) {
      stopSelectionAutoScroll();
      return;
    }

    if (!dragState.hasMoved) {
      if (dragState.mode === 'add') {
        const next = new Set(dragState.originSelection);
        if (next.has(dragState.startIndex)) {
          next.delete(dragState.startIndex);
        } else if (selectableIndexes.has(dragState.startIndex)) {
          next.add(dragState.startIndex);
        }
        setSelectedIndexes(next);
      } else if (dragState.originSelection.size === 1 && dragState.originSelection.has(dragState.startIndex)) {
        setSelectedIndexes(new Set());
      } else if (!selectableIndexes.has(dragState.startIndex)) {
        setSelectedIndexes(new Set(dragState.originSelection));
      } else {
        setSelectedIndexes(new Set([dragState.startIndex]));
      }
    } else {
      updateDraggedSelection(dragState.pointerClientY, true);
    }

    selectionDragRef.current = null;
    stopSelectionAutoScroll();
  }

  function handleGutterMouseDown(index: number, event: ReactMouseEvent<HTMLButtonElement>) {
    if (event.button !== 0) {
      return;
    }
    if (!selectableIndexes.has(index)) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    const additive = event.metaKey || event.ctrlKey;
    selectionDragRef.current = {
      startIndex: index,
      lastIndex: index,
      startClientY: event.clientY,
      pointerClientY: event.clientY,
      originSelection: new Set(selectedIndexes),
      mode: additive ? 'add' : 'replace',
      hasMoved: false,
    };
  }

  function handleGutterKeyDown(index: number, event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key !== 'Enter' && event.key !== ' ') {
      return;
    }
    if (!selectableIndexes.has(index)) {
      return;
    }

    event.preventDefault();

    if (event.metaKey || event.ctrlKey) {
      setSelectedIndexes((previous) => {
        const next = new Set(previous);
        if (next.has(index)) {
          next.delete(index);
        } else if (selectableIndexes.has(index)) {
          next.add(index);
        }
        return next;
      });
      return;
    }

    setSelectedIndexes((previous) => {
      if (previous.size === 1 && previous.has(index)) {
        return new Set();
      }

      return new Set([index]);
    });
  }

  function scrollToNode(index: number) {
    const container = scrollRef.current;
    const layout = nodeLayouts[index];

    if (!container || !layout) {
      return;
    }

    const nextTop = Math.max(layout.top - 18, 0);
    container.scrollTo({
      top: nextTop,
      behavior: 'smooth',
    });
  }

  function syncScrollFromMinimap(clientY: number) {
    const dragState = minimapDragRef.current;
    const minimap = minimapRef.current;
    const minimapScroller = minimapScrollRef.current;
    const container = scrollRef.current;

    if (!dragState || !minimap || !minimapScroller || !container) {
      return;
    }

    const rect = minimap.getBoundingClientRect();
    const pointerContentY = minimapScroller.scrollTop + clientY - rect.top;
    const rawTop = pointerContentY - dragState.offsetPx;
    const clampedTop = Math.min(
      Math.max(rawTop, MINIMAP_CONTENT_PADDING_PX),
      MINIMAP_CONTENT_PADDING_PX + minimapViewportTravelPx,
    );
    const nextScrollTop =
      minimapViewportTravelPx <= 0
        ? 0
        : ((clampedTop - MINIMAP_CONTENT_PADDING_PX) / minimapViewportTravelPx) *
          Math.max(container.scrollHeight - container.clientHeight, 0);

    container.scrollTop = nextScrollTop;
  }

  function handleMinimapMouseDown(event: ReactMouseEvent<HTMLDivElement>) {
    const minimap = minimapRef.current;
    const minimapScroller = minimapScrollRef.current;

    if (!minimap || !minimapScroller) {
      return;
    }

    event.preventDefault();

    const rect = minimap.getBoundingClientRect();
    const pointerContentY = minimapScroller.scrollTop + event.clientY - rect.top;

    const target = event.target as HTMLElement;
    const pressedViewport = target.closest('.context-minimap-viewport');
    const offsetPx = pressedViewport
      ? pointerContentY - minimapViewportTopPx
      : minimapViewportHeightPx / 2;
    minimapDragRef.current = {
      offsetPx,
    };
    syncScrollFromMinimap(event.clientY);
  }

  const selectedNodeIndexes = useMemo(
    () => [...selectedIndexes].sort((left, right) => left - right),
    [selectedIndexes],
  );
  const criticalNodeIndexes = useMemo(
    () =>
      messageStats
        .map((stats, index) => (stats.editable && stats.weightClass === 'heavy' ? index : -1))
        .filter((index) => index >= 0),
    [messageStats],
  );

  return (
    <aside className={`right-panel stage-${stage}`}>
      <div className="context-map-pane">
        <div className="context-map-header">
          <div className="context-map-header-row">
            <div className="context-map-title">{sidebarText(uiLocale, 'Context Map', '上下文地图')}</div>
            {(stage === 1 || stage === 2) && (
              <button
                aria-label={sidebarText(uiLocale, 'Toggle right sidebar', '切换右侧侧边栏')}
                className="context-map-toggle"
                onClick={onToggle}
                title={sidebarText(uiLocale, 'Toggle right sidebar', '切换右侧侧边栏')}
                type="button"
              >
                <i className="ph-light ph-layout" />
              </button>
            )}
          </div>
        </div>

        <div className="context-map-list">
          <ContextMapNodeList
            messages={messages}
            stage={stage}
            nodeMeta={nodeMeta}
            messageStats={messageStats}
            expandedIndexes={expandedIndexes}
            selectedIndexes={selectedIndexes}
            hoveredIndex={hoveredIndex}
            previewTruncatedIndexes={previewTruncatedIndexes}
            uiLocale={uiLocale}
            scrollRef={scrollRef}
            setHoveredIndex={setHoveredIndex}
            setNodeRef={setNodeRef}
            onToggleMessage={toggleMessage}
            onJumpToMessage={onJumpToMessage}
            onGutterMouseDown={handleGutterMouseDown}
            onGutterKeyDown={handleGutterKeyDown}
          />

          {showMinimap ? (
            <ContextMinimap
              messages={messages}
              messageStats={messageStats}
              minimapBars={minimapBars}
              selectedIndexes={selectedIndexes}
              hoveredIndex={hoveredIndex}
              uiLocale={uiLocale}
              minimapContentHeightPx={minimapContentHeightPx}
              minimapViewportTopPx={minimapViewportTopPx}
              minimapViewportHeightPx={minimapViewportHeightPx}
              minimapRef={minimapRef}
              minimapScrollRef={minimapScrollRef}
              setHoveredIndex={setHoveredIndex}
              onScrollToNode={scrollToNode}
              onMinimapMouseDown={handleMinimapMouseDown}
            />
          ) : null}
        </div>
      </div>

      <div className="extended-pane" data-localize-skip="true">
        <ContextWorkbench
          messageTokenStats={messageStats}
          selectedNodeIndexes={selectedNodeIndexes}
          criticalNodeIndexes={criticalNodeIndexes}
          tokenThresholds={tokenThresholds}
          sessionId={sessionId}
          isMainChatBusy={isMainChatBusy}
          history={contextWorkbenchHistory}
          reasoningOptions={reasoningOptions}
          proxyUsageSummary={proxyUsageSummary}
          uiLocale={uiLocale}
          onHistoryChange={onContextWorkbenchHistoryChange}
          onConversationChange={onContextWorkbenchConversationChange}
          onProxyUsageSummaryChange={onProxyUsageSummaryChange}
          onEnsureSession={onEnsureSession}
          onTokenThresholdsChange={setTokenThresholds}
          onUiLocaleChange={onUiLocaleChange}
        />
      </div>
    </aside>
  );
}
