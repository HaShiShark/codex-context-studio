import { getMessagePreviewText } from './MessageContent';
import type { MessageRecord } from '../types';
import {
  getContextToolWeightSource,
  getContextTokenCount,
  getContextTokenWeightClass,
  type ContextMapNodeMeta,
  type ContextMessageTokenStat,
  type ContextTokenThresholds,
  type ContextTokenWeightClass,
} from '../contextTokenWeight';
import { countTokens } from '../utils';

export interface NodeLayout {
  top: number;
  height: number;
}

export interface MinimapBarLayout {
  topPx: number;
  heightPx: number;
}

export interface MessageStat extends ContextMessageTokenStat {
  label: string;
  previewText: string;
}

export interface ScrollMetrics {
  clientHeight: number;
  scrollHeight: number;
  scrollTop: number;
}

export const DEFAULT_SCROLL_METRICS: ScrollMetrics = {
  clientHeight: 1,
  scrollHeight: 1,
  scrollTop: 0,
};

export const MINIMAP_CONTENT_PADDING_PX = 14;
export const MINIMAP_BAR_GAP_PX = 8;
export const MINIMAP_VIEWPORT_MIN_HEIGHT_PX = 56;
export const MINIMAP_VIEWPORT_KEEP_OFFSET_PX = 14;
export const SELECTION_DRAG_THRESHOLD_PX = 4;
export const SELECTION_AUTO_SCROLL_EDGE_PX = 68;
export const SELECTION_AUTO_SCROLL_MAX_SPEED_PX = 14;

function isAssistantStyleRole(role: MessageRecord['role']) {
  return role === 'an' || role === 'subagent';
}

function providerItemType(item: unknown) {
  return item && typeof item === 'object' && !Array.isArray(item)
    ? String((item as Record<string, unknown>).type || '').trim()
    : '';
}

function subagentAuthorFromMessage(message: MessageRecord) {
  const agentMessage = (message.providerItems || []).find((item) => providerItemType(item) === 'agent_message');
  if (!agentMessage || typeof agentMessage !== 'object' || Array.isArray(agentMessage)) {
    return '';
  }
  return String((agentMessage as Record<string, unknown>).author || '').trim();
}

export function getMinimapBarHeightPx(role: MessageRecord['role'], weightClass: ContextTokenWeightClass) {
  if (!isAssistantStyleRole(role)) {
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

export function contextNodeRoleName(role: MessageRecord['role']) {
  return role === 'an' ? 'assistant' : role;
}

export function contextNodeClassName(role: MessageRecord['role']) {
  return isAssistantStyleRole(role) ? 'assistant' : role;
}

export function sidebarText(locale: 'zh-CN' | 'en-US', english: string, chinese: string) {
  return locale === 'zh-CN' ? chinese : english;
}

export function buildRangeSelection(
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

export function clampIndexSet(indexes: Set<number>, length: number, selectableIndexes?: Set<number>) {
  const next = new Set<number>();
  indexes.forEach((index) => {
    if (index >= 0 && index < length && (!selectableIndexes || selectableIndexes.has(index))) {
      next.add(index);
    }
  });
  return next;
}

export function areIndexSetsEqual(left: Set<number>, right: Set<number>) {
  return left.size === right.size && [...left].every((index) => right.has(index));
}

export function contextMapContentSignature(messages: MessageRecord[]) {
  return JSON.stringify(
    messages.map((message) => ({
      role: message.role,
      text: message.text,
      attachments: message.attachments.map((attachment) => ({
        name: attachment.name,
        mime_type: attachment.mime_type,
      })),
      blocks: message.blocks,
      toolEvents: message.toolEvents,
      providerItems: message.providerItems,
    })),
  );
}

export function areNodeLayoutsEqual(left: NodeLayout[], right: NodeLayout[]) {
  return (
    left.length === right.length
    && left.every((layout, index) => layout.top === right[index]?.top && layout.height === right[index]?.height)
  );
}

export function areScrollMetricsEqual(left: ScrollMetrics, right: ScrollMetrics) {
  return (
    left.clientHeight === right.clientHeight
    && left.scrollHeight === right.scrollHeight
    && left.scrollTop === right.scrollTop
  );
}

export function canExpandMessage(record: MessageRecord, previewText: string, isPreviewTruncated: boolean) {
  void previewText;
  const textValue = record.text || '';
  const trimmedTextValue = textValue.trim();
  const hasAttachmentsOrTools = Boolean(record.attachments.length || record.toolEvents.length);
  if (record.role === 'user') {
    return hasAttachmentsOrTools || /\r?\n/.test(trimmedTextValue) || isPreviewTruncated;
  }

  const hasStructuredContent = Boolean(
    hasAttachmentsOrTools
    || record.blocks.length > 1
    || /\r?\n/.test(trimmedTextValue),
  );

  return hasStructuredContent || isPreviewTruncated;
}

export function buildMessageStats(
  messages: MessageRecord[],
  nodeMeta: ContextMapNodeMeta[],
  tokenThresholds: ContextTokenThresholds,
): MessageStat[] {
  return messages.map((message, index) => {
    const meta = nodeMeta[index];
    const tokens = getContextTokenCount(message);
    const toolTokens = countTokens(getContextToolWeightSource(message));
    const roleName = message.role === 'subagent'
      ? ['subagent', subagentAuthorFromMessage(message)].filter(Boolean).join(' ')
      : contextNodeRoleName(message.role);
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
  });
}

export function buildFallbackMinimapBars(messages: MessageRecord[], messageStats: MessageStat[]) {
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
}

export function getFallbackMinimapContentHeightPx(fallbackMinimapBars: MinimapBarLayout[]) {
  const lastLayout = fallbackMinimapBars[fallbackMinimapBars.length - 1];
  const fallbackHeight = lastLayout
    ? lastLayout.topPx + lastLayout.heightPx + MINIMAP_CONTENT_PADDING_PX
    : MINIMAP_CONTENT_PADDING_PX * 2;
  return Math.max(fallbackHeight, MINIMAP_CONTENT_PADDING_PX * 2 + MINIMAP_VIEWPORT_MIN_HEIGHT_PX);
}

export function buildMinimapBars(options: {
  effectiveScrollHeight: number;
  fallbackMinimapBars: MinimapBarLayout[];
  messageStats: MessageStat[];
  messages: MessageRecord[];
  minimapContentHeightPx: number;
  minimapUsableHeightPx: number;
  nodeLayouts: NodeLayout[];
}) {
  const {
    effectiveScrollHeight,
    fallbackMinimapBars,
    messageStats,
    messages,
    minimapContentHeightPx,
    minimapUsableHeightPx,
    nodeLayouts,
  } = options;
  const desiredMinimapBars: MinimapBarLayout[] = messages.map((message, index) => {
    const fallbackLayout = fallbackMinimapBars[index];
    const nodeLayout = nodeLayouts[index];
    const heightPx = fallbackLayout?.heightPx ?? getMinimapBarHeightPx(message.role, messageStats[index]?.weightClass ?? 'light');

    if (!nodeLayout || nodeLayout.height <= 0) {
      return fallbackLayout;
    }

    const nodeCenter = nodeLayout.top + nodeLayout.height / 2;
    const centerRatio = Math.min(Math.max(nodeCenter / effectiveScrollHeight, 0), 1);
    const centeredTopPx = MINIMAP_CONTENT_PADDING_PX + centerRatio * minimapUsableHeightPx - heightPx / 2;
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
}
