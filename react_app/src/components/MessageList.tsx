import type { RefObject } from 'react';

import type { MessageRecord } from '../types';
import MessageBubble from './MessageBubble';

interface MessageListProps {
  listRef: RefObject<HTMLDivElement | null>;
  messages: MessageRecord[];
  onCopy: (text: string) => void;
  onDeleteMessage: (messageIndex: number) => void;
  onEdit: (messageIndex: number) => void;
  onRegenerate: (sourceText: string) => void;
}

export default function MessageList({
  listRef,
  messages,
  onCopy,
  onDeleteMessage,
  onEdit,
  onRegenerate,
}: MessageListProps) {
  return (
    <div className="message-list" id="message-list" ref={listRef}>
      {messages.map((record, index) => (
        <MessageBubble
          key={`${record.role}-${index}-${record.text.slice(0, 24)}-${record.blocks.length}`}
          messageIndex={index}
          record={record}
          onCopy={onCopy}
          onDelete={onDeleteMessage}
          onEdit={onEdit}
          onRegenerate={onRegenerate}
        />
      ))}
    </div>
  );
}
