import type {
  AttachmentRecord,
  MessageBlock,
  MessageRecord,
  ProviderItem,
  ReasoningOption,
  ToolEvent,
  TranscriptEntry,
  TranscriptNode,
} from './types';

type TokenEncoder = {
  encode(text: string): unknown[];
};

let encoding: TokenEncoder | null = null;
let encoderLoadPromise: Promise<TokenEncoder | null> | null = null;
let encoderUnavailable = false;

const _tokenResults = new Map<string, { exact: boolean; value: number }>();
const _TOKEN_CACHE_LIMIT = 4096;
const _TOKEN_ENCODER_LOAD_THRESHOLD = 2000;

function estimateTokens(text: string): number {
  return Math.ceil(text.length / 3.5);
}

function shouldLoadPreciseEncoder(text: string): boolean {
  return text.length >= _TOKEN_ENCODER_LOAD_THRESHOLD;
}

function loadEncoder(): Promise<TokenEncoder | null> {
  if (encoding || encoderUnavailable) {
    return Promise.resolve(encoding);
  }

  if (!encoderLoadPromise) {
    encoderLoadPromise = import('js-tiktoken')
      .then(({ getEncoding }) => {
        encoding = getEncoding('cl100k_base');
        _tokenResults.clear();
        return encoding;
      })
      .catch(() => {
        encoderUnavailable = true;
        return null;
      })
      .finally(() => {
        encoderLoadPromise = null;
      });
  }

  return encoderLoadPromise;
}

export function countTokens(text: string): number {
  if (!text) {
    return 0;
  }

  const cached = _tokenResults.get(text);
  if (cached && (cached.exact || !encoding)) {
    if (!encoding && shouldLoadPreciseEncoder(text)) {
      void loadEncoder();
    }
    return cached.value;
  }

  let result = 0;
  let exact = false;
  if (encoding) {
    try {
      result = encoding.encode(text).length;
      exact = true;
    } catch {
      result = estimateTokens(text);
    }
  } else {
    result = estimateTokens(text);
    if (shouldLoadPreciseEncoder(text)) {
      void loadEncoder();
    }
  }

  if (_tokenResults.size >= _TOKEN_CACHE_LIMIT) {
    _tokenResults.clear();
  }
  _tokenResults.set(text, { exact, value: result });
  return result;
}

const FALLBACK_REASONING_LABELS: Record<string, string> = {
  default: '自动',
  none: '关闭',
  low: '低',
  medium: '中',
  high: '高',
};

export const DEFAULT_REASONING_OPTIONS: ReasoningOption[] = [
  { value: 'default', label: '自动' },
  { value: 'none', label: '关闭' },
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
];

export function normalizeAttachments(value: AttachmentRecord[] | undefined): AttachmentRecord[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.reduce<AttachmentRecord[]>((normalized, item) => {
    const name = String(item?.name || '').trim();
    const mimeType = String(item?.mime_type || '').trim() || 'application/octet-stream';
    const kind = item?.kind === 'image' ? 'image' : 'file';
    const url = typeof item?.url === 'string' ? item.url : undefined;
    const id = typeof item?.id === 'string' ? item.id : undefined;
    const relativePath = typeof item?.relative_path === 'string' ? item.relative_path : undefined;
    const rawSize = item?.size_bytes;
    const sizeBytes = typeof rawSize === 'number' ? rawSize : Number(rawSize || 0);

    if (!name) {
      return normalized;
    }

    normalized.push({
      id,
      name,
      mime_type: mimeType,
      kind,
      size_bytes: Number.isFinite(sizeBytes) ? sizeBytes : 0,
      url,
      relative_path: relativePath,
    } satisfies AttachmentRecord);
    return normalized;
  }, []);
}

type ProviderItemRecord = Record<string, unknown>;

const NON_DICT_PROVIDER_ITEM_MARKER = '__hash_context_non_dict_provider_item__';

const TOOL_CALL_ITEM_TYPES = new Set([
  'functioncall',
  'customtoolcall',
  'localshellcall',
  'toolsearchcall',
  'websearchcall',
  'imagegenerationcall',
]);

const TOOL_OUTPUT_ITEM_TYPES = new Set([
  'functioncalloutput',
  'customtoolcalloutput',
  'localshellcalloutput',
  'mcptoolcalloutput',
  'toolsearchoutput',
]);

const PAIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE: Record<string, Set<string>> = {
  functioncall: new Set(['functioncalloutput', 'mcptoolcalloutput']),
  localshellcall: new Set(['functioncalloutput', 'localshellcalloutput']),
  customtoolcall: new Set(['customtoolcalloutput']),
  toolsearchcall: new Set(['toolsearchoutput']),
};

const COMPACTION_ITEM_TYPES = new Set(['compaction', 'contextcompaction', 'compactionsummary']);

function isRecord(value: unknown): value is ProviderItemRecord {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isProviderItem(value: unknown): value is ProviderItem {
  return isRecord(value);
}

function isTranscriptNode(record: TranscriptEntry): record is TranscriptNode {
  return Array.isArray((record as TranscriptNode).items);
}

function mapTranscriptRole(rawRole: unknown): MessageRecord['role'] {
  const role = String(rawRole || '').trim().toLowerCase();
  if (role === 'assistant' || role === 'an') {
    return 'an';
  }
  if (role === 'subagent' || role === 'agent_message' || role === 'agentmessage') {
    return 'subagent';
  }
  if (role === 'system' || role === 'developer' || role === 'context' || role === 'compaction') {
    return role;
  }
  if (role === 'context_compaction' || role === 'contextcompaction') {
    return 'compaction';
  }
  if (role === 'unknown') {
    return 'context';
  }
  return 'user';
}

function providerItemsFromTranscriptNode(node: TranscriptNode): ProviderItem[] {
  if (!Array.isArray(node.items)) {
    return [];
  }

  return node.items
    .map((item) => (isRecord(item) ? item.providerItem : undefined))
    .filter(isProviderItem);
}

function providerItemType(item: ProviderItemRecord | undefined): string {
  return String(item?.type || '').trim();
}

function providerItemCanonicalType(item: ProviderItemRecord | undefined): string {
  return providerItemType(item).toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function providerItemCallId(item: ProviderItemRecord | undefined, allowIdFallback = true): string {
  const callId = String(item?.call_id || '').trim();
  if (callId) {
    return callId;
  }
  return allowIdFallback ? String(item?.id || '').trim() : '';
}

function safeStringify(value: unknown, space = 2): string {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }

  try {
    const result = JSON.stringify(value, null, space);
    return typeof result === 'string' ? result : '';
  } catch {
    return String(value);
  }
}

function isInlineImageDataUrl(value: string): boolean {
  return /^data:image\/[^,;]+(?:;[^,]*)*;base64,/i.test(value.trim());
}

function isImageContentRecord(value: ProviderItemRecord): boolean {
  const type = String(value.type || '').trim().toLowerCase();
  if (type.includes('image') || 'image_url' in value) {
    return true;
  }

  const url = typeof value.url === 'string' ? value.url : '';
  return Boolean(url && isInlineImageDataUrl(url));
}

function imageContentText(value: ProviderItemRecord): string {
  const rawUrl = typeof value.image_url === 'string'
    ? value.image_url
    : typeof value.url === 'string'
      ? value.url
      : '';
  const imageUrl = rawUrl.trim();

  if (!imageUrl || isInlineImageDataUrl(imageUrl)) {
    return '[image]';
  }

  return `[image] ${imageUrl}`;
}

function parseJsonish(value: unknown): unknown {
  if (typeof value !== 'string') {
    return value;
  }

  const trimmed = value.trim();
  if (!trimmed) {
    return '';
  }

  if (!/^[{\[]/.test(trimmed)) {
    return value;
  }

  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return value;
  }
}

function providerPayloadText(value: unknown): string {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value
      .map((entry) => providerPayloadText(entry))
      .map((entry) => entry.trim())
      .filter(Boolean)
      .join('\n');
  }
  if (isRecord(value)) {
    if (isImageContentRecord(value)) {
      return imageContentText(value);
    }

    for (const key of ['text', 'content', 'summary', 'output_text', 'input_text', 'reasoning_text']) {
      if (key in value) {
        const text = providerPayloadText(value[key]);
        if (text.trim()) {
          return text;
        }
      }
    }
  }
  return safeStringify(value);
}

function previewText(value: unknown, limit = 160): string {
  return truncateText(providerPayloadText(value).replace(/\s+/g, ' ').trim(), limit);
}

function messageContentPartText(part: unknown): string {
  if (!isRecord(part)) {
    return providerPayloadText(part);
  }

  if (isImageContentRecord(part)) {
    return imageContentText(part);
  }

  const directText = providerPayloadText(part.text ?? part.output_text ?? part.input_text);
  if (directText.trim()) {
    return directText;
  }

  const nestedContent = providerPayloadText(part.content);
  if (nestedContent.trim()) {
    return nestedContent;
  }

  const type = String(part.type || '').trim();
  if (type.includes('image') || 'image_url' in part) {
    return imageContentText(part);
  }
  if (type.includes('file') || 'file_id' in part || 'filename' in part) {
    return ['[file]', providerPayloadText(part.filename || part.file_id || part.name).trim()].filter(Boolean).join(' ');
  }

  return safeStringify(part);
}

function providerMessageContentText(content: unknown): string {
  if (Array.isArray(content)) {
    return content
      .map(messageContentPartText)
      .map((part) => part.trim())
      .filter(Boolean)
      .join('\n');
  }
  return providerPayloadText(content);
}

function messageTextFromProviderItem(item: ProviderItemRecord): string {
  return providerMessageContentText(item.content);
}

function agentMessageTextFromProviderItem(item: ProviderItemRecord): string {
  const text = providerMessageContentText(item.content);
  if (text.trim()) {
    return text;
  }
  const encrypted = Array.isArray(item.content)
    && item.content.some((part) => isRecord(part) && String(part.type || '').trim() === 'encrypted_content');
  return encrypted ? '[encrypted subagent message]' : '';
}

function reasoningTextFromProviderItem(item: ProviderItemRecord): string {
  const text = providerPayloadText(item.summary || item.content || item.text || item.reasoning_text);
  if (text.trim()) {
    return text;
  }
  if (String(item.encrypted_content || '').trim()) {
    return 'Reasoning item (encrypted content)';
  }
  return '';
}

function compactionTextFromProviderItem(item: ProviderItemRecord): string {
  const text = providerPayloadText(item.summary || item.content || item.text || item.output || item.input);
  if (text.trim()) {
    return text;
  }
  if (String(item.encrypted_content || '').trim()) {
    return 'Compaction item (encrypted content)';
  }
  return '';
}

function toolOutputTextFromProviderItem(item: ProviderItemRecord | undefined): string {
  if (!item) {
    return '';
  }

  const itemType = providerItemCanonicalType(item);
  if (itemType === 'toolsearchoutput') {
    return providerPayloadText(item.tools);
  }
  if (itemType === 'imagegenerationcall') {
    return providerPayloadText(item.result);
  }
  return providerPayloadText(item.output ?? item.result);
}

function webSearchActionDetail(action: unknown): string {
  if (!isRecord(action)) {
    return previewText(action);
  }

  const actionType = String(action.type || '').trim();
  if (actionType === 'search') {
    const query = String(action.query || '').trim();
    if (query) {
      return query;
    }
    const queries = Array.isArray(action.queries)
      ? action.queries.map((query) => String(query || '').trim()).filter(Boolean)
      : [];
    return queries.join(', ');
  }
  if (actionType === 'open_page') {
    return String(action.url || '').trim();
  }
  if (actionType === 'find_in_page') {
    const pattern = String(action.pattern || '').trim();
    const url = String(action.url || '').trim();
    return pattern && url ? `${pattern} in ${url}` : pattern || url;
  }

  return previewText(action.query || action.url || actionType || action);
}

function toolCallArgumentsValue(item: ProviderItemRecord | undefined): unknown {
  if (!item) {
    return '';
  }

  const itemType = providerItemCanonicalType(item);
  if (itemType === 'functioncall') {
    return parseJsonish(item.arguments ?? '{}');
  }
  if (itemType === 'customtoolcall') {
    return parseJsonish(item.input ?? '');
  }
  if (itemType === 'localshellcall' || itemType === 'websearchcall') {
    return item.action ?? '';
  }
  if (itemType === 'toolsearchcall') {
    return parseJsonish(item.arguments ?? '');
  }
  if (itemType === 'imagegenerationcall') {
    return String(item.revised_prompt || item.prompt || '').trim();
  }
  return parseJsonish(item.arguments ?? item.input ?? item.action ?? '');
}

function toolDisplayTitleFromProviderItem(item: ProviderItemRecord | undefined): string {
  if (!item) {
    return 'tool';
  }

  const itemType = providerItemCanonicalType(item);
  if (itemType === 'functioncall' || itemType === 'customtoolcall') {
    return String(item.name || '').trim() || 'tool';
  }
  if (itemType === 'localshellcall') {
    return 'local_shell';
  }
  if (itemType === 'toolsearchcall') {
    return 'tool_search';
  }
  if (itemType === 'websearchcall') {
    return 'web_search';
  }
  if (itemType === 'imagegenerationcall') {
    return 'image_generation';
  }
  if (TOOL_OUTPUT_ITEM_TYPES.has(itemType)) {
    return String(item.name || providerItemType(item) || 'tool_output').trim();
  }
  return providerItemType(item) || 'tool';
}

function toolEventNameFromProviderItems(
  callItem: ProviderItemRecord | undefined,
  outputItem: ProviderItemRecord | undefined,
): string {
  const source = callItem || outputItem;
  if (!source) {
    return 'tool';
  }

  const itemType = providerItemCanonicalType(source);
  if (itemType === 'websearchcall') {
    return 'web_search';
  }
  if (itemType === 'imagegenerationcall') {
    return 'image_generation';
  }
  if (itemType === 'toolsearchcall') {
    return 'tool_search';
  }
  if (itemType === 'localshellcall') {
    return 'local_shell';
  }
  return String(source.name || toolDisplayTitleFromProviderItem(source)).trim() || 'tool';
}

function toolDisplayDetailFromProviderItem(item: ProviderItemRecord | undefined): string {
  if (!item) {
    return '';
  }

  const itemType = providerItemCanonicalType(item);
  const callName = String(item.name || '').trim();
  const argumentsValue = toolCallArgumentsValue(item);

  if ((callName === 'shell_command' || callName === 'exec_command') && isRecord(argumentsValue)) {
    const command = argumentsValue.command;
    if (Array.isArray(command)) {
      return command.map((part) => String(part)).join(' ');
    }
    if (command !== undefined && command !== null) {
      return providerPayloadText(command).trim();
    }
  }
  if (callName === 'write_stdin' && isRecord(argumentsValue)) {
    return providerPayloadText(argumentsValue.stdin || argumentsValue.input || '').trim();
  }
  if (itemType === 'localshellcall') {
    const action = item.action;
    if (isRecord(action)) {
      const command = action.command;
      if (Array.isArray(command)) {
        return command.map((part) => String(part)).join(' ');
      }
      if (command !== undefined && command !== null) {
        return providerPayloadText(command).trim();
      }
    }
    return previewText(action);
  }
  if (itemType === 'websearchcall') {
    return webSearchActionDetail(item.action);
  }
  if (itemType === 'imagegenerationcall') {
    return previewText(item.revised_prompt || item.prompt);
  }

  const detail = providerPayloadText(argumentsValue).trim();
  return detail && detail !== '{}' && detail !== '[]' ? previewText(detail) : '';
}

function statusFromToolOutput(output: string, fallback = 'completed'): string {
  const firstLine = output.trim().split(/\r?\n/, 1)[0] || '';
  if (!firstLine.toLowerCase().startsWith('exit code:')) {
    return fallback;
  }

  const rawCode = firstLine.split(':', 2)[1]?.trim().split(/\s+/, 1)[0] || '';
  const code = Number.parseInt(rawCode, 10);
  if (!Number.isFinite(code)) {
    return fallback;
  }
  return code === 0 ? 'completed' : 'error';
}

function buildToolEventFromProviderItems(
  callItem: ProviderItemRecord | undefined,
  outputItem: ProviderItemRecord | undefined,
): ToolEvent {
  const source = callItem || outputItem;
  const output = outputItem ? toolOutputTextFromProviderItem(outputItem) : toolOutputTextFromProviderItem(callItem);
  const fallbackStatus = String(callItem?.status || outputItem?.status || 'completed').trim() || 'completed';
  const callId = providerItemCallId(callItem) || providerItemCallId(outputItem, false);

  return {
    name: toolEventNameFromProviderItems(callItem, outputItem),
    arguments: callItem ? toolCallArgumentsValue(callItem) : '',
    call_id: callId || undefined,
    output_preview: truncateText(output, 500),
    raw_output: output,
    display_title: toolDisplayTitleFromProviderItem(source),
    display_detail: callItem ? toolDisplayDetailFromProviderItem(callItem) : '',
    display_result: truncateText(output, 500),
    status: output ? statusFromToolOutput(output, fallbackStatus) : fallbackStatus,
  };
}

function fallbackProviderItemText(item: ProviderItemRecord): string {
  if (item[NON_DICT_PROVIDER_ITEM_MARKER] === true) {
    return providerPayloadText(item.value);
  }

  const itemType = providerItemType(item) || 'provider_item';
  const payload = safeStringify(item);
  return payload ? `${itemType}\n${payload}` : itemType;
}

function readableTextFromProviderItem(item: ProviderItem): string {
  const itemRecord = item as ProviderItemRecord;
  const itemType = providerItemCanonicalType(itemRecord);

  if (itemType === 'message') {
    return messageTextFromProviderItem(itemRecord);
  }
  if (itemType === 'agentmessage') {
    return agentMessageTextFromProviderItem(itemRecord);
  }
  if (itemType === 'reasoning') {
    return reasoningTextFromProviderItem(itemRecord);
  }
  if (COMPACTION_ITEM_TYPES.has(itemType)) {
    return compactionTextFromProviderItem(itemRecord);
  }
  if (TOOL_OUTPUT_ITEM_TYPES.has(itemType)) {
    return toolOutputTextFromProviderItem(itemRecord);
  }
  if (TOOL_CALL_ITEM_TYPES.has(itemType)) {
    const title = toolDisplayTitleFromProviderItem(itemRecord);
    const detail = toolDisplayDetailFromProviderItem(itemRecord);
    return [title, detail].filter(Boolean).join(': ');
  }

  return fallbackProviderItemText(itemRecord);
}

function buildTextBlocksFromProviderItems(providerItems: ProviderItem[]): { text: string; blocks: MessageBlock[] } {
  const blocks = providerItems
    .map(readableTextFromProviderItem)
    .map((text) => text.trim())
    .filter(Boolean)
    .map((text) => ({ kind: 'text', text }) satisfies MessageBlock);

  return {
    text: blocks.map((block) => block.text).join('\n\n'),
    blocks,
  };
}

function buildAssistantDisplayFromProviderItems(
  providerItems: ProviderItem[],
): { text: string; toolEvents: ToolEvent[]; blocks: MessageBlock[] } {
  const outputIndexesByCallId = new Map<string, number[]>();
  providerItems.forEach((item, index) => {
    const itemRecord = item as ProviderItemRecord;
    if (!TOOL_OUTPUT_ITEM_TYPES.has(providerItemCanonicalType(itemRecord))) {
      return;
    }
    const callId = providerItemCallId(itemRecord, false);
    if (!callId) {
      return;
    }
    outputIndexesByCallId.set(callId, [...(outputIndexesByCallId.get(callId) || []), index]);
  });

  const consumedOutputIndexes = new Set<number>();
  const textParts: string[] = [];
  const toolEvents: ToolEvent[] = [];
  const blocks: MessageBlock[] = [];

  const addText = (text: string) => {
    const trimmed = text.trim();
    if (trimmed) {
      textParts.push(trimmed);
    }
  };

  const findOutputItem = (callItem: ProviderItemRecord): ProviderItemRecord | undefined => {
    const callId = providerItemCallId(callItem);
    if (!callId) {
      return undefined;
    }

    const allowedTypes = PAIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE[providerItemCanonicalType(callItem)];
    if (!allowedTypes) {
      return undefined;
    }

    for (const outputIndex of outputIndexesByCallId.get(callId) || []) {
      if (consumedOutputIndexes.has(outputIndex)) {
        continue;
      }
      const outputItem = providerItems[outputIndex] as ProviderItemRecord;
      if (!allowedTypes.has(providerItemCanonicalType(outputItem))) {
        continue;
      }
      consumedOutputIndexes.add(outputIndex);
      return outputItem;
    }

    return undefined;
  };

  providerItems.forEach((item, index) => {
    const itemRecord = item as ProviderItemRecord;
    const itemType = providerItemCanonicalType(itemRecord);

    if (itemType === 'message') {
      const text = messageTextFromProviderItem(itemRecord);
      addText(text);
      if (text.trim()) {
        blocks.push({ kind: 'text', text });
      }
      return;
    }

    if (itemType === 'reasoning') {
      const text = reasoningTextFromProviderItem(itemRecord);
      addText(text);
      if (text.trim()) {
        blocks.push({ kind: 'reasoning', text, status: 'completed' });
      }
      return;
    }

    if (COMPACTION_ITEM_TYPES.has(itemType)) {
      const text = compactionTextFromProviderItem(itemRecord);
      addText(text);
      if (text.trim()) {
        blocks.push({ kind: 'text', text });
      }
      return;
    }

    if (TOOL_CALL_ITEM_TYPES.has(itemType)) {
      const outputItem = findOutputItem(itemRecord);
      const toolEvent = buildToolEventFromProviderItems(itemRecord, outputItem);
      toolEvents.push(toolEvent);
      blocks.push({ kind: 'tool', tool_event: toolEvent });
      addText(toolOutputTextFromProviderItem(outputItem) || toolOutputTextFromProviderItem(itemRecord));
      return;
    }

    if (TOOL_OUTPUT_ITEM_TYPES.has(itemType)) {
      if (consumedOutputIndexes.has(index)) {
        return;
      }
      const toolEvent = buildToolEventFromProviderItems(undefined, itemRecord);
      toolEvents.push(toolEvent);
      blocks.push({ kind: 'tool', tool_event: toolEvent });
      addText(toolOutputTextFromProviderItem(itemRecord));
      return;
    }

    const fallbackText = fallbackProviderItemText(itemRecord);
    addText(fallbackText);
    if (fallbackText.trim()) {
      blocks.push({ kind: 'text', text: fallbackText });
    }
  });

  return {
    text: textParts.join('\n\n'),
    toolEvents,
    blocks,
  };
}

function buildTranscriptNodeDisplay(
  role: MessageRecord['role'],
  providerItems: ProviderItem[],
): { text: string; toolEvents: ToolEvent[]; blocks: MessageBlock[] } {
  if (role !== 'an') {
    return {
      ...buildTextBlocksFromProviderItems(providerItems),
      toolEvents: [],
    };
  }
  return buildAssistantDisplayFromProviderItems(providerItems);
}

export function normalizeConversation(records: TranscriptEntry[] = []): MessageRecord[] {
  let lastUserText = '';

  return records.map((record) => {
    const role = mapTranscriptRole(record.role);
    const providerItems = providerItemsFromTranscriptNode(record);
    const nodeDisplay = buildTranscriptNodeDisplay(role, providerItems);
    const text = nodeDisplay.text;
    const toolEvents = nodeDisplay.toolEvents;
    const blocks = normalizeBlocks(
      nodeDisplay.blocks,
      text,
      toolEvents,
      role,
    );
    const normalized: MessageRecord = {
      role,
      text,
      attachments: [],
      toolEvents,
      blocks,
      providerItems,
      pending: role === 'an' && !text.trim() && toolEvents.length === 0,
      sourceText: role === 'an' ? lastUserText : '',
    };

    if (role === 'user') {
      lastUserText = text;
    }

    return normalized;
  });
}

function normalizeBlocks(
  rawBlocks: MessageBlock[] | undefined,
  text: string,
  toolEvents: ToolEvent[] | undefined,
  role: MessageRecord['role'],
): MessageBlock[] {
  if (Array.isArray(rawBlocks) && rawBlocks.length > 0) {
    return rawBlocks
      .map((block): MessageBlock | null => {
        if (!block || typeof block !== 'object' || !('kind' in block)) {
          return null;
        }

        if (block.kind === 'text') {
          const blockText = String(block.text || '');
          if (!blockText) {
            return null;
          }

          return {
            kind: 'text',
            text: blockText,
          } satisfies MessageBlock;
        }

        if (block.kind === 'reasoning') {
          const blockText = String(block.text || '');
          const status = String(block.status || '').trim() || 'completed';
          if (!blockText && status !== 'streaming') {
            return null;
          }

          return {
            kind: 'reasoning',
            text: blockText,
            status: status === 'streaming' ? 'streaming' : 'completed',
          } satisfies MessageBlock;
        }

        if (block.kind === 'tool' && block.tool_event) {
          return {
            kind: 'tool',
            tool_event: block.tool_event,
          } satisfies MessageBlock;
        }

        return null;
      })
      .filter((block): block is MessageBlock => Boolean(block));
  }

  if (role === 'user') {
    return text
      ? [
          {
            kind: 'text',
            text,
          },
        ]
      : [];
  }

  const fallbackBlocks: MessageBlock[] = [];
  if (text) {
    fallbackBlocks.push({
      kind: 'text',
      text,
    });
  }

  if (Array.isArray(toolEvents)) {
    toolEvents.forEach((toolEvent) => {
      fallbackBlocks.push({
        kind: 'tool',
        tool_event: toolEvent,
      });
    });
  }

  return fallbackBlocks;
}

export function normalizeReasoningOptions(options?: ReasoningOption[]): ReasoningOption[] {
  if (!Array.isArray(options) || options.length === 0) {
    return DEFAULT_REASONING_OPTIONS;
  }

  const normalizedOptions = options
    .map((option) => {
      const value = String(option.value || '').trim();
      if (!value) {
        return null;
      }

      const label = String(option.label || '').trim() || FALLBACK_REASONING_LABELS[value] || value;
      return { value, label };
    })
    .filter((option): option is ReasoningOption => Boolean(option))
    .map((option) => (option.value === 'none' && option.label === '快速' ? { ...option, label: '关闭' } : option));

  return normalizedOptions.some((option) => option.value === 'default')
    ? normalizedOptions
    : [{ value: 'default', label: '自动' }, ...normalizedOptions];
}

export function getReasoningLabel(value: string, options: ReasoningOption[]): string {
  const match = options.find((option) => option.value === value);
  return match ? match.label : FALLBACK_REASONING_LABELS[value] || value;
}




export function getConversation(
  conversations: Record<string, MessageRecord[]>,
  sessionId: string,
): MessageRecord[] {
  if (!sessionId) {
    return [];
  }

  return conversations[sessionId] || [];
}

export function formatBytes(sizeBytes: number | undefined): string {
  const safeValue = typeof sizeBytes === 'number' && Number.isFinite(sizeBytes) ? Math.max(sizeBytes, 0) : 0;
  if (safeValue < 1024) {
    return `${safeValue} B`;
  }
  if (safeValue < 1024 * 1024) {
    return `${(safeValue / 1024).toFixed(1)} KB`;
  }
  return `${(safeValue / (1024 * 1024)).toFixed(1)} MB`;
}

export function truncateText(value: string, limit = 120): string {
  if (value.length <= limit) {
    return value;
  }
  return `${value.slice(0, limit - 3)}...`;
}

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textArea = document.createElement('textarea');
  textArea.value = text;
  document.body.appendChild(textArea);
  textArea.select();
  document.execCommand('copy');
  document.body.removeChild(textArea);
}
