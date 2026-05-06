import MessageContent from './MessageContent';
import type { MessageRecord } from '../types';

interface MessageBubbleProps {
  messageIndex: number;
  record: MessageRecord;
  onCopy: (text: string) => void;
  onDelete: (messageIndex: number) => void;
  onEdit: (messageIndex: number) => void;
  onRegenerate: (sourceText: string) => void;
}

export default function MessageBubble({
  messageIndex,
  record,
  onCopy,
  onDelete,
  onEdit,
  onRegenerate,
}: MessageBubbleProps) {
  const canShowActions = !record.pending;

  return (
    <div className={`message ${record.role}${record.pending ? ' pending' : ''}`} data-message-index={messageIndex}>
      <div className="message-body">
        <div className="content">
          <MessageContent record={record} />
        </div>
        {canShowActions ? (
          <div className="message-actions">
            <button className="action-btn" type="button" onClick={() => onCopy(record.text)}>
              <i className="ph-light ph-copy" />
            </button>
            {record.role === 'user' ? (
              <button className="action-btn" type="button" onClick={() => onEdit(messageIndex)}>
                <i className="ph-light ph-pencil-simple" />
              </button>
            ) : (
              <button className="action-btn" type="button" onClick={() => onRegenerate(record.sourceText)}>
                <i className="ph-light ph-arrows-clockwise" />
              </button>
            )}
            <button className="action-btn" type="button" onClick={() => onDelete(messageIndex)}>
              <i className="ph-light ph-trash" />
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
