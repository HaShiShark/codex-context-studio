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
  stage: 0 | 1 | 2;
  nodeMeta: ContextMapNodeMeta[];
  messageStats: MessageStat[];
  expandedIndexes: Set<number>;
  selectedIndexes: Set<number>;
  previewTruncatedIndexes: Set<number>;
  uiLocale: 'zh-CN' | 'en-US';
  scrollRef: Ref<HTMLDivElement>;
  setNodeRef: (index: number, node: HTMLDivElement | null) => void;
  onToggleMessage: (index: number) => void;
  onJumpToMessage: (index: number) => void;
  onGutterMouseDown: (index: number, event: ReactMouseEvent<HTMLButtonElement>) => void;
  onGutterKeyDown: (index: number, event: ReactKeyboardEvent<HTMLButtonElement>) => void;
}

interface ContextMapNodeRowProps {
  canToggleExpand: boolean;
  displayNodeNumber: number | null | undefined;
  index: number;
  isExpanded: boolean;
  isInteractive: boolean;
  isInternal: boolean;
  isSelectable: boolean;
  isSelected: boolean;
  message: MessageRecord;
  stage: 0 | 1 | 2;
  stats: MessageStat;
  uiLocale: 'zh-CN' | 'en-US';
  setNodeRef: (index: number, node: HTMLDivElement | null) => void;
  onToggleMessage: (index: number) => void;
  onJumpToMessage: (index: number) => void;
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
  isInternal,
  isSelectable,
  isSelected,
  message,
  stage,
  stats,
  uiLocale,
  setNodeRef,
  onToggleMessage,
  onJumpToMessage,
  onGutterMouseDown,
  onGutterKeyDown,
}: ContextMapNodeRowProps) {
  const roleClass = contextNodeClassName(message.role);
  const selectedClass = isSelected ? 'selected' : '';
  const lockedClass = isInternal ? 'locked' : '';
  const canJumpToChat = stage === 1;

  return (
    <div
      className={`context-node-row ${roleClass} ${isExpanded ? 'expanded' : ''} ${selectedClass} ${lockedClass} ${stage === 1 ? 'without-gutter' : ''}`}
      ref={(node) => setNodeRef(index, node)}
    >
      {stage !== 1 && isSelectable ? (
        <button
          className="context-node-gutter"
          type="button"
          onMouseDown={(event) => onGutterMouseDown(index, event)}
          onKeyDown={(event) => onGutterKeyDown(index, event)}
          aria-label={sidebarText(uiLocale, `Select node ${index + 1}`, `选择第 ${index + 1} 个节点`)}
          aria-pressed={isSelected}
        >
          <span>{displayNodeNumber}</span>
        </button>
      ) : stage !== 1 ? (
        <div className="context-node-gutter locked" aria-hidden="true">
          <i className="ph-light ph-lock-simple" />
        </div>
      ) : null}

      <div className={`context-map-item ${roleClass} ${isExpanded ? 'expanded' : ''} ${selectedClass}`}>
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

                  onToggleMessage(index);
                }
              : undefined
          }
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
  stage,
  nodeMeta,
  messageStats,
  expandedIndexes,
  selectedIndexes,
  previewTruncatedIndexes,
  uiLocale,
  scrollRef,
  setNodeRef,
  onToggleMessage,
  onJumpToMessage,
  onGutterMouseDown,
  onGutterKeyDown,
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
            const canExpand = canExpandMessage(message, stats.previewText, previewTruncatedIndexes.has(index));
            const canToggleExpand = stage !== 1 && canExpand;
            const canJumpToChat = stage === 1;

            return (
              <ContextMapNodeRow
                canToggleExpand={canToggleExpand}
                displayNodeNumber={meta?.displayNodeNumber}
                index={index}
                isExpanded={isExpanded}
                isInteractive={canToggleExpand || canJumpToChat}
                isInternal={Boolean(meta?.internalKind)}
                isSelectable={Boolean(meta?.selectable)}
                isSelected={isSelected}
                key={`${message.role}-${index}`}
                message={message}
                stage={stage}
                stats={stats}
                uiLocale={uiLocale}
                setNodeRef={setNodeRef}
                onToggleMessage={onToggleMessage}
                onJumpToMessage={onJumpToMessage}
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
