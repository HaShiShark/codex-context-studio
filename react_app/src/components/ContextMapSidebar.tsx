import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from 'react';

import './ContextMapSidebar.polish.css';
import MessageContent, { getMessagePreviewText } from './MessageContent';
import ContextWorkbench from './ContextWorkbench';
import type {
  ContextWorkbenchHistoryEntry,
  ContextRevisionSummary,
  MessageRecord,
  PendingContextRestore,
  ReasoningOption,
} from '../types';
import {
  buildContextMapNodeMeta,
  DEFAULT_CONTEXT_TOKEN_THRESHOLDS,
  getContextToolWeightSource,
  getContextTokenWeightClass,
  getContextWeightSource,
  type ContextTokenThresholds,
  type ContextMessageTokenStat,
  type ContextTokenWeightClass,
} from '../contextTokenWeight';
import { countTokens } from '../utils';

interface ContextMapSidebarProps {
  stage: 0 | 1 | 2;
  messages: MessageRecord[];
  onToggle: () => void;
  onJumpToMessage: (messageIndex: number) => void;
  sessionId: string;
  isMainChatBusy: boolean;
  contextWorkbenchHistory: ContextWorkbenchHistoryEntry[];
  contextRevisionHistory: ContextRevisionSummary[];
  pendingContextRestore: PendingContextRestore | null;
  reasoningOptions: ReasoningOption[];
  uiLocale: 'zh-CN' | 'en-US';
  onContextWorkbenchHistoryChange: (sessionId: string, history: ContextWorkbenchHistoryEntry[]) => void;
  onContextWorkbenchConversationChange: (
    sessionId: string,
    conversation: MessageRecord[],
    options?: { resetProxyOverride?: boolean },
  ) => void | Promise<void>;
  onContextRevisionHistoryChange: (sessionId: string, revisions: ContextRevisionSummary[]) => void;
  onPendingContextRestoreChange: (sessionId: string, pendingRestore: PendingContextRestore | null) => void;
  onEnsureSession: () => Promise<string>;
  onUiLocaleChange?: (locale: 'zh-CN' | 'en-US') => void;
}

interface NodeLayout {
  top: number;
  height: number;
}

interface MinimapBarLayout {
  topPx: number;
  heightPx: number;
}

interface MessageStat extends ContextMessageTokenStat {
  label: string;
  previewText: string;
}

interface ScrollMetrics {
  clientHeight: number;
  scrollHeight: number;
  scrollTop: number;
}

const DEFAULT_SCROLL_METRICS: ScrollMetrics = {
  clientHeight: 1,
  scrollHeight: 1,
  scrollTop: 0,
};

const MINIMAP_CONTENT_PADDING_PX = 14;
const MINIMAP_BAR_GAP_PX = 8;
const MINIMAP_VIEWPORT_MIN_HEIGHT_PX = 56;
const MINIMAP_VIEWPORT_KEEP_OFFSET_PX = 14;
const SELECTION_DRAG_THRESHOLD_PX = 4;
const SELECTION_AUTO_SCROLL_EDGE_PX = 68;
const SELECTION_AUTO_SCROLL_MAX_SPEED_PX = 14;

function normalizePlainText(value: string) {
  return value.replace(/\r?\n+/g, ' ').replace(/\s+/g, ' ').trim();
}

function getMinimapBarHeightPx(role: MessageRecord['role'], weightClass: ContextTokenWeightClass) {
  if (role !== 'an') {
    return 4;
  }

  if (weightClass === 'heavy') {
    return 7;
  }

  if (weightClass === 'medium') {
    return 6;
  }

  return 5;
}

function contextNodeRoleName(role: MessageRecord['role']) {
  return role === 'an' ? 'assistant' : role;
}

function contextNodeClassName(role: MessageRecord['role']) {
  return role === 'an' ? 'assistant' : role;
}

function sidebarText(locale: ContextMapSidebarProps['uiLocale'], english: string, chinese: string) {
  return locale === 'zh-CN' ? chinese : english;
}

function buildRangeSelection(
  startIndex: number,
  endIndex: number,
  baseSelection: Set<number>,
  mode: 'replace' | 'add',
  selectableIndexes: Set<number>,
) {
  const next = mode === 'add' ? new Set(baseSelection) : new Set<number>();
  const rangeStart = Math.min(startIndex, endIndex);
  const rangeEnd = Math.max(startIndex, endIndex);

  for (let index = rangeStart; index <= rangeEnd; index += 1) {
    if (selectableIndexes.has(index)) {
      next.add(index);
    }
  }

  return next;
}

function clampIndexSet(indexes: Set<number>, length: number, selectableIndexes?: Set<number>) {
  const next = new Set<number>();
  indexes.forEach((index) => {
    if (index >= 0 && index < length && (!selectableIndexes || selectableIndexes.has(index))) {
      next.add(index);
    }
  });
  return next;
}

function areIndexSetsEqual(left: Set<number>, right: Set<number>) {
  return left.size === right.size && [...left].every((index) => right.has(index));
}

function areNodeLayoutsEqual(left: NodeLayout[], right: NodeLayout[]) {
  return (
    left.length === right.length
    && left.every((layout, index) => layout.top === right[index]?.top && layout.height === right[index]?.height)
  );
}

function areScrollMetricsEqual(left: ScrollMetrics, right: ScrollMetrics) {
  return (
    left.clientHeight === right.clientHeight
    && left.scrollHeight === right.scrollHeight
    && left.scrollTop === right.scrollTop
  );
}

function canExpandMessage(record: MessageRecord, previewText: string, isPreviewTruncated: boolean) {
  void previewText;
  const textValue = record.text || '';
  const hasStructuredContent = Boolean(
    record.attachments.length
    || record.toolEvents.length
    || record.blocks.length > 1
    || /\r?\n/.test(textValue),
  );

  return hasStructuredContent || isPreviewTruncated;
}

export default function ContextMapSidebar({
  stage,
  messages,
  onToggle,
  onJumpToMessage,
  sessionId,
  isMainChatBusy,
  contextWorkbenchHistory,
  contextRevisionHistory,
  pendingContextRestore,
  reasoningOptions,
  uiLocale,
  onContextWorkbenchHistoryChange,
  onContextWorkbenchConversationChange,
  onContextRevisionHistoryChange,
  onPendingContextRestoreChange,
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
  const [expandedIndexes, setExpandedIndexes] = useState<Set<number>>(new Set());
  const [selectedIndexes, setSelectedIndexes] = useState<Set<number>>(new Set());
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [nodeLayouts, setNodeLayouts] = useState<NodeLayout[]>([]);
  const [previewTruncatedIndexes, setPreviewTruncatedIndexes] = useState<Set<number>>(new Set());
  const [scrollMetrics, setScrollMetrics] = useState<ScrollMetrics>(DEFAULT_SCROLL_METRICS);
  const [tokenThresholds, setTokenThresholds] = useState<ContextTokenThresholds>(DEFAULT_CONTEXT_TOKEN_THRESHOLDS);
  const showMinimap = stage === 2;
  const nodeMeta = useMemo(() => buildContextMapNodeMeta(messages), [messages]);
  const selectableIndexes = useMemo(
    () => new Set(nodeMeta.map((meta, index) => (meta.selectable ? index : -1)).filter((index) => index >= 0)),
    [nodeMeta],
  );

  const messageStats: MessageStat[] = useMemo(
    () =>
      messages.map((message, index) => {
        const meta = nodeMeta[index];
        const tokens = countTokens(getContextWeightSource(message));
        const toolTokens = countTokens(getContextToolWeightSource(message));
        const roleName = contextNodeRoleName(message.role);
        const size = (tokens / 1000).toFixed(1);

        return {
          nodeIndex: index,
          nodeNumber: meta?.displayNodeNumber ?? 0,
          role: roleName,
          label: `${roleName}: ${size}k`,
          previewText: getMessagePreviewText(message),
          tokens,
          toolTokens,
          weightClass: getContextTokenWeightClass(tokens, tokenThresholds),
          editable: Boolean(meta?.editable),
          internalKind: meta?.internalKind,
        };
      }),
    [messages, nodeMeta, tokenThresholds],
  );

  const scrollRange = Math.max(scrollMetrics.scrollHeight - scrollMetrics.clientHeight, 0);
  const scrollRatio = scrollRange <= 0 ? 0 : scrollMetrics.scrollTop / scrollRange;
  const fallbackMinimapBars: MinimapBarLayout[] = useMemo(() => {
    let minimapCursorPx = MINIMAP_CONTENT_PADDING_PX;
    return messages.map((message, index) => {
      const layout = {
        topPx: minimapCursorPx,
        heightPx: getMinimapBarHeightPx(message.role, messageStats[index]?.weightClass ?? 'light'),
      };

      minimapCursorPx += layout.heightPx;
      if (index < messages.length - 1) {
        minimapCursorPx += MINIMAP_BAR_GAP_PX;
      }

      return layout;
    });
  }, [messageStats, messages]);

  const fallbackMinimapContentHeightPx = useMemo(() => {
    const lastLayout = fallbackMinimapBars[fallbackMinimapBars.length - 1];
    const fallbackHeight = lastLayout
      ? lastLayout.topPx + lastLayout.heightPx + MINIMAP_CONTENT_PADDING_PX
      : MINIMAP_CONTENT_PADDING_PX * 2;
    return Math.max(fallbackHeight, MINIMAP_CONTENT_PADDING_PX * 2 + MINIMAP_VIEWPORT_MIN_HEIGHT_PX);
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
  const minimapBars: MinimapBarLayout[] = useMemo(() => {
    const desiredMinimapBars: MinimapBarLayout[] = messages.map((message, index) => {
      const fallbackLayout = fallbackMinimapBars[index];
      const nodeLayout = nodeLayouts[index];
      const heightPx = fallbackLayout?.heightPx ?? getMinimapBarHeightPx(message.role, messageStats[index]?.weightClass ?? 'light');

      if (!nodeLayout || nodeLayout.height <= 0) {
        return fallbackLayout;
      }

      const nodeCenter = nodeLayout.top + nodeLayout.height / 2;
      const centerRatio = Math.min(Math.max(nodeCenter / effectiveScrollHeight, 0), 1);
      const centeredTopPx =
        MINIMAP_CONTENT_PADDING_PX + centerRatio * minimapUsableHeightPx - heightPx / 2;
      const maxTopPx = MINIMAP_CONTENT_PADDING_PX + minimapUsableHeightPx - heightPx;

      return {
        topPx: Math.min(Math.max(centeredTopPx, MINIMAP_CONTENT_PADDING_PX), maxTopPx),
        heightPx,
      };
    });
    const minimapBottomLimitPx = minimapContentHeightPx - MINIMAP_CONTENT_PADDING_PX;
    const nextMinimapBars: MinimapBarLayout[] = [];

    desiredMinimapBars.forEach((layout, index) => {
      const previousBar = index > 0 ? nextMinimapBars[index - 1] : null;
      const minTopPx = previousBar
        ? previousBar.topPx + previousBar.heightPx + MINIMAP_BAR_GAP_PX
        : MINIMAP_CONTENT_PADDING_PX;
      const maxTopPx = minimapBottomLimitPx - layout.heightPx;

      nextMinimapBars.push({
        ...layout,
        topPx: Math.min(Math.max(layout.topPx, minTopPx), maxTopPx),
      });
    });

    for (let index = nextMinimapBars.length - 2; index >= 0; index -= 1) {
      const nextBar = nextMinimapBars[index + 1];
      const currentBar = nextMinimapBars[index];
      const maxTopPx = nextBar.topPx - currentBar.heightPx - MINIMAP_BAR_GAP_PX;

      nextMinimapBars[index] = {
        ...currentBar,
        topPx: Math.max(MINIMAP_CONTENT_PADDING_PX, Math.min(currentBar.topPx, maxTopPx)),
      };
    }

    return nextMinimapBars;
  }, [
    effectiveScrollHeight,
    fallbackMinimapBars,
    messageStats,
    messages,
    minimapContentHeightPx,
    minimapUsableHeightPx,
    nodeLayouts,
  ]);

  useEffect(() => {
    setExpandedIndexes(new Set());
    setSelectedIndexes(new Set());
    setHoveredIndex(null);
    nodeRefs.current = [];
    setNodeLayouts([]);
    setScrollMetrics(DEFAULT_SCROLL_METRICS);
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [sessionId]);

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
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div className="context-map-title">{sidebarText(uiLocale, 'Context Map', '上下文地图')}</div>
            {(stage === 1 || stage === 2) && (
              <i
                className="ph-light ph-layout control-btn-main active"
                onClick={onToggle}
                title={sidebarText(uiLocale, 'Toggle right sidebar', '切换右侧侧边栏')}
                style={{ fontSize: '18px', cursor: 'pointer' }}
              />
            )}
          </div>
        </div>

        <div className="context-map-list">
          <div className="context-map-scroll-shell" ref={scrollRef}>
            <div className="context-map-list-inner">
              {messages.length > 0 ? (
                messages.map((message, index) => {
                  const isExpanded = expandedIndexes.has(index);
                  const isSelected = selectedIndexes.has(index);
                  const stats = messageStats[index];
                  const meta = nodeMeta[index];
                  const displayNodeNumber = meta?.displayNodeNumber;
                  const isSelectable = Boolean(meta?.selectable);
                  const isInternal = Boolean(meta?.internalKind);
                  const canExpand = canExpandMessage(message, stats.previewText, previewTruncatedIndexes.has(index));
                  const canToggleExpand = stage !== 1 && canExpand;
                  const canJumpToChat = stage === 1;
                  const isInteractive = canToggleExpand || canJumpToChat;
                  const hoverClass = hoveredIndex === index ? 'hovered' : '';
                  const selectedClass = isSelected ? 'selected' : '';
                  const lockedClass = isInternal ? 'locked' : '';

                  return (
                    <div
                      className={`context-node-row ${contextNodeClassName(message.role)} ${isExpanded ? 'expanded' : ''} ${hoverClass} ${selectedClass} ${lockedClass} ${stage === 1 ? 'without-gutter' : ''}`}
                      key={`${message.role}-${index}`}
                      onMouseEnter={() => setHoveredIndex(index)}
                      onMouseLeave={() => setHoveredIndex((previous) => (previous === index ? null : previous))}
                      ref={(node) => setNodeRef(index, node)}
                    >
                      {stage !== 1 && isSelectable ? (
                        <button
                          className="context-node-gutter"
                          type="button"
                          onMouseDown={(event) => handleGutterMouseDown(index, event)}
                          onKeyDown={(event) => handleGutterKeyDown(index, event)}
                          aria-label={sidebarText(
                            uiLocale,
                            `Select node ${index + 1}`,
                            `选择第 ${index + 1} 个节点`,
                          )}
                          aria-pressed={isSelected}
                        >
                          <span>{displayNodeNumber}</span>
                        </button>
                      ) : stage !== 1 ? (
                        <div className="context-node-gutter locked" aria-hidden="true">
                          <i className="ph-light ph-lock-simple" />
                        </div>
                      ) : null}

                      <div
                        className={`context-map-item ${contextNodeClassName(message.role)} ${isExpanded ? 'expanded' : ''} ${selectedClass}`}
                      >
                        <button
                          aria-expanded={canToggleExpand ? isExpanded : undefined}
                          aria-label={canJumpToChat
                            ? sidebarText(
                                uiLocale,
                                `Jump to main chat message ${index + 1}`,
                                `跳转到主聊天第 ${index + 1} 条消息`,
                              )
                            : undefined}
                          className={`context-map-item-button ${isInteractive ? '' : 'non-expandable'}`}
                          type="button"
                          onClick={
                            isInteractive
                              ? () => {
                                  if (canJumpToChat) {
                                    onJumpToMessage(index);
                                    return;
                                  }

                                  toggleMessage(index);
                                }
                              : undefined
                          }
                        >
                          <div className="map-metadata">
                            <span>{stats.label}</span>
                            {canToggleExpand ? (
                              <i
                                className={`ph-light ph-caret-right context-map-expand-icon ${isExpanded ? 'open' : ''}`}
                              />
                            ) : null}
                          </div>
                          {!isExpanded ? (
                            <div className="map-bubble">
                              <span className="map-preview-text">{stats.previewText}</span>
                            </div>
                          ) : null}
                        </button>

                        {canToggleExpand ? (
                          <div
                            className={`context-map-expanded-shell ${isExpanded ? 'open' : ''}`}
                            aria-hidden={!isExpanded}
                          >
                            <div className="context-map-expanded-content">
                              {isExpanded ? (
                                <div className="context-map-expanded-body">
                                  <MessageContent record={message} variant="context-map" />
                                </div>
                              ) : null}
                            </div>
                          </div>
                        ) : null}
                      </div>
                    </div>
                  );
                })
              ) : (
                <div style={{ padding: '20px', textAlign: 'center', opacity: 0.4, fontSize: '13px' }}>
                  {sidebarText(
                    uiLocale,
                    'Messages that enter this turn of context will appear here.',
                    '这里会显示本轮真正进入上下文的消息。',
                  )}
                </div>
              )}
            </div>
          </div>

          {showMinimap ? (
            <div className="context-minimap-shell">
              <div className="context-minimap" role="presentation">
                <div className="context-minimap-track" ref={minimapRef} onMouseDown={handleMinimapMouseDown}>
                  <div className="context-minimap-scroll" ref={minimapScrollRef}>
                    <div className="context-minimap-content" style={{ height: `${minimapContentHeightPx}px` }}>
                      {messages.map((message, index) => {
                        const layout = minimapBars[index];
                        const stats = messageStats[index];

                        return (
                          <button
                            className={`context-minimap-bar ${contextNodeClassName(message.role)} weight-${stats.weightClass} ${hoveredIndex === index ? 'hovered' : ''} ${selectedIndexes.has(index) ? 'selected' : ''} ${stats.internalKind ? 'locked' : ''}`}
                            key={`minimap-${message.role}-${index}`}
                            type="button"
                            style={{
                              top: `${layout?.topPx ?? MINIMAP_CONTENT_PADDING_PX}px`,
                              height: `${layout?.heightPx ?? 4}px`,
                            }}
                            onMouseDown={(event) => {
                              event.stopPropagation();
                            }}
                            onClick={(event) => {
                              event.stopPropagation();
                              scrollToNode(index);
                            }}
                            onMouseEnter={() => setHoveredIndex(index)}
                            onMouseLeave={() => setHoveredIndex((previous) => (previous === index ? null : previous))}
                            aria-label={sidebarText(
                              uiLocale,
                              `Scroll to node ${index + 1}, about ${stats.tokens} tokens`,
                              `定位到第 ${index + 1} 个节点，约 ${stats.tokens} 个 token`,
                            )}
                          />
                        );
                      })}
                      <div
                        className="context-minimap-viewport"
                        style={{
                          top: `${minimapViewportTopPx}px`,
                          height: `${minimapViewportHeightPx}px`,
                        }}
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="extended-pane" data-localize-skip="true">
        <ContextWorkbench
          messages={messages}
          messageTokenStats={messageStats}
          selectedNodeIndexes={selectedNodeIndexes}
          criticalNodeIndexes={criticalNodeIndexes}
          tokenThresholds={tokenThresholds}
          sessionId={sessionId}
          isMainChatBusy={isMainChatBusy}
          history={contextWorkbenchHistory}
          revisions={contextRevisionHistory}
          pendingRestore={pendingContextRestore}
          reasoningOptions={reasoningOptions}
          uiLocale={uiLocale}
          onHistoryChange={onContextWorkbenchHistoryChange}
          onConversationChange={onContextWorkbenchConversationChange}
          onRevisionHistoryChange={onContextRevisionHistoryChange}
          onPendingRestoreChange={onPendingContextRestoreChange}
          onEnsureSession={onEnsureSession}
          onTokenThresholdsChange={setTokenThresholds}
          onUiLocaleChange={onUiLocaleChange}
        />
      </div>
    </aside>
  );
}
