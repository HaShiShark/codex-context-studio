import type {
  AttachmentRecord,
  MessageBlock,
  MessageRecord,
  ReasoningOption,
  TranscriptRecord,
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

export function normalizeConversation(records: TranscriptRecord[] = []): MessageRecord[] {
  let lastUserText = '';

  return records.map((record) => {
    const rawRole = String(record.role || '').trim();
    const role: MessageRecord['role'] = rawRole === 'assistant'
      ? 'an'
      : (['system', 'developer', 'compaction', 'context'].includes(rawRole)
        ? rawRole as MessageRecord['role']
        : 'user');
    const text = String(record.text || '');
    const toolEvents = Array.isArray(record.toolEvents) ? record.toolEvents : [];
    const attachments = normalizeAttachments(record.attachments);
    const blocks = normalizeBlocks(record.blocks, text, toolEvents, role);
    const normalized: MessageRecord = {
      role,
      text,
      attachments,
      toolEvents,
      blocks,
      providerItems: Array.isArray(record.providerItems) ? record.providerItems : [],
      pending: Boolean(record.pending),
      sourceText: role === 'an' ? lastUserText : '',
    };

    if (role === 'user') {
      lastUserText = text;
    }

    return normalized;
  });
}

function normalizeBlocks(
  rawBlocks: TranscriptRecord['blocks'],
  text: string,
  toolEvents: TranscriptRecord['toolEvents'] | undefined,
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
