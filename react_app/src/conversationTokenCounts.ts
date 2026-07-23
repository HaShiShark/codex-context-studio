import { fetchTokenCountsRequest } from './api';
import { getContextToolWeightSource, getContextWeightSource } from './contextTokenWeight';
import type { MessageRecord } from './types';


export async function enrichConversationTokenCounts(
  messages: MessageRecord[],
): Promise<MessageRecord[]> {
  if (!messages.length) return messages;
  const response = await fetchTokenCountsRequest(
    messages.map((message, index) => ({
      id: message.nodeId || String(index),
      text: getContextWeightSource(message),
      tool_text: getContextToolWeightSource(message),
    })),
  );
  const counts = new Map(response.items.map((item) => [item.id, item]));
  return messages.map((message, index) => {
    const count = counts.get(message.nodeId || String(index));
    return count
      ? {
          ...message,
          tokenEstimate: count.tokens,
          toolTokenEstimate: count.tool_tokens,
        }
      : message;
  });
}
