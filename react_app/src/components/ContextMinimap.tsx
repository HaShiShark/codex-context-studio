import type {
  Dispatch,
  MouseEvent as ReactMouseEvent,
  Ref,
  SetStateAction,
} from 'react';

import {
  MINIMAP_CONTENT_PADDING_PX,
  contextNodeClassName,
  sidebarText,
  type MessageStat,
  type MinimapBarLayout,
} from './ContextMapSidebar.helpers';
import type { MessageRecord } from '../types';

interface ContextMinimapProps {
  messages: MessageRecord[];
  messageStats: MessageStat[];
  minimapBars: MinimapBarLayout[];
  selectedIndexes: Set<number>;
  hoveredIndex: number | null;
  uiLocale: 'zh-CN' | 'en-US';
  minimapContentHeightPx: number;
  minimapViewportTopPx: number;
  minimapViewportHeightPx: number;
  minimapRef: Ref<HTMLDivElement>;
  minimapScrollRef: Ref<HTMLDivElement>;
  setHoveredIndex: Dispatch<SetStateAction<number | null>>;
  onScrollToNode: (index: number) => void;
  onMinimapMouseDown: (event: ReactMouseEvent<HTMLDivElement>) => void;
}

export default function ContextMinimap({
  messages,
  messageStats,
  minimapBars,
  selectedIndexes,
  hoveredIndex,
  uiLocale,
  minimapContentHeightPx,
  minimapViewportTopPx,
  minimapViewportHeightPx,
  minimapRef,
  minimapScrollRef,
  setHoveredIndex,
  onScrollToNode,
  onMinimapMouseDown,
}: ContextMinimapProps) {
  return (
    <div className="context-minimap-shell">
      <div className="context-minimap" role="presentation">
        <div className="context-minimap-track" ref={minimapRef} onMouseDown={onMinimapMouseDown}>
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
                      onScrollToNode(index);
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
  );
}
