import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  Ref,
} from 'react';
import { memo } from 'react';

import MessageContent from './MessageContent';
import {
  canExpandMessage,
  contextNodeClassName,
  sidebarText,
  type MessageStat,
} from './ContextMapSidebar.helpers';
import type { ContextMapNodeMeta } from '../contextTokenWeight';
import type { MessageRecord } from '../types';

interface ContextMapNodeListProps {
  messages: MessageRecord[];
  nodeMeta: ContextMapNodeMeta[];
  messageStats: MessageStat[];
  expandedIndexes: Set<number>;
  selectedIndexes: Set<number>;
  previewTruncatedIndexes: Set<number>;
  uiLocale: 'zh-CN' | 'en-US';
  scrollRef: Ref<HTMLDivElement>;
  setNodeRef: (index: number, node: HTMLDivElement | null) => void;
  onToggleMessage: (index: number) => void;
  onGutterMouseDown: (index: number, event: ReactMouseEvent<HTMLButtonElement>) => void;
  onGutterKeyDown: (index: number, event: ReactKeyboardEvent<HTMLButtonElement>) => void;
  isNodeLockDisabled: boolean;
  nodeLockPendingIds: Set<string>;
}

interface ContextMapNodeRowProps {
  canToggleExpand: boolean;
  displayNodeNumber: number | null | undefined;
  index: number;
  isExpanded: boolean;
  isInteractive: boolean;
  isLocked: boolean;
  isLockDisabled: boolean;
  isLockPending: boolean;
  isSelected: boolean;
  message: MessageRecord;
  stats: MessageStat;
  uiLocale: 'zh-CN' | 'en-US';
  setNodeRef: (index: number, node: HTMLDivElement | null) => void;
  onToggleMessage: (index: number) => void;
  onGutterMouseDown: (index: number, event: ReactMouseEvent<HTMLButtonElement>) => void;
  onGutterKeyDown: (index: number, event: ReactKeyboardEvent<HTMLButtonElement>) => void;
}

const MemoizedMessageContent = memo(MessageContent);

const ContextMapNodeRow = memo(function ContextMapNodeRow({
  canToggleExpand,
  displayNodeNumber,
  index,
  isExpanded,
  isInteractive,
  isLocked,
  isLockDisabled,
  isLockPending,
  isSelected,
  message,
  stats,
  uiLocale,
  setNodeRef,
  onToggleMessage,
  onGutterMouseDown,
  onGutterKeyDown,
}: ContextMapNodeRowProps) {
  const roleClass = contextNodeClassName(message.role);
  const selectedClass = isSelected ? 'selected' : '';
  const lockedClass = isLocked ? 'locked' : '';
  const nodeNumberLabel = displayNodeNumber ?? index + 1;
  const lockTooltip = sidebarText(uiLocale, 'Double-click to lock/unlock', '双击锁定/解锁');

  return (
    <div
      className={`context-node-row ${roleClass} ${isExpanded ? 'expanded' : ''} ${selectedClass} ${lockedClass}`}
      ref={(node) => setNodeRef(index, node)}
    >
      <button
        className={`context-node-gutter ${isLocked ? 'locked' : ''} ${isLockPending ? 'pending' : ''}`}
        type="button"
        onMouseDown={(event) => onGutterMouseDown(index, event)}
        onKeyDown={(event) => onGutterKeyDown(index, event)}
        disabled={isLockPending}
        aria-disabled={isLockDisabled || isLockPending}
        aria-label={
          isLocked
            ? sidebarText(uiLocale, `Unlock node ${nodeNumberLabel}`, `解锁第 ${nodeNumberLabel} 个节点`)
            : sidebarText(
              uiLocale,
              `Select node ${nodeNumberLabel}; double-click to lock`,
              `选择第 ${nodeNumberLabel} 个节点；双击锁定`,
            )
        }
        aria-pressed={isSelected}
        title={lockTooltip}
      >
        {isLocked ? <i className="ph-light ph-lock-simple" /> : <span>{nodeNumberLabel}</span>}
      </button>

      <div className={`context-map-item ${roleClass} ${isExpanded ? 'expanded' : ''} ${selectedClass}`}>
        <button
          aria-expanded={canToggleExpand ? isExpanded : undefined}
          className={`context-map-item-button ${isInteractive ? '' : 'non-expandable'}`}
          type="button"
          onClick={isInteractive ? () => onToggleMessage(index) : undefined}
        >
          <div className="map-metadata">
            <span>{stats.label}</span>
            {canToggleExpand ? (
              <i className={`ph-light ph-caret-right context-map-expand-icon ${isExpanded ? 'open' : ''}`} />
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
                  <MemoizedMessageContent record={message} variant="context-map" />
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
});

function ContextMapNodeList({
  messages,
  nodeMeta,
  messageStats,
  expandedIndexes,
  selectedIndexes,
  previewTruncatedIndexes,
  uiLocale,
  scrollRef,
  setNodeRef,
  onToggleMessage,
  onGutterMouseDown,
  onGutterKeyDown,
  isNodeLockDisabled,
  nodeLockPendingIds,
}: ContextMapNodeListProps) {
  return (
    <div className="context-map-scroll-shell" ref={scrollRef}>
      <div className="context-map-list-inner">
        {messages.length > 0 ? (
          messages.map((message, index) => {
            const isExpanded = expandedIndexes.has(index);
            const isSelected = selectedIndexes.has(index);
            const stats = messageStats[index];
            const meta = nodeMeta[index];
            const canToggleExpand = canExpandMessage(message, previewTruncatedIndexes.has(index));

            return (
              <ContextMapNodeRow
                canToggleExpand={canToggleExpand}
                displayNodeNumber={meta?.displayNodeNumber}
                index={index}
                isExpanded={isExpanded}
                isInteractive={canToggleExpand}
                isLocked={Boolean(meta?.locked)}
                isLockDisabled={isNodeLockDisabled}
                isLockPending={Boolean(message.nodeId && nodeLockPendingIds.has(message.nodeId))}
                isSelected={isSelected}
                key={`${message.role}-${index}`}
                message={message}
                stats={stats}
                uiLocale={uiLocale}
                setNodeRef={setNodeRef}
                onToggleMessage={onToggleMessage}
                onGutterMouseDown={onGutterMouseDown}
                onGutterKeyDown={onGutterKeyDown}
              />
            );
          })
        ) : (
          <div className="context-map-empty">
            {sidebarText(
              uiLocale,
              'Messages that enter this turn of context will appear here.',
              '这里会显示本轮真正进入上下文的消息。',
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(ContextMapNodeList);
