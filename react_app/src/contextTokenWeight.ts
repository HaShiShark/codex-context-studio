import type { MessageRecord } from './types';
import { countTokens } from './utils';

export type ContextTokenWeightClass = 'light' | 'medium' | 'heavy';

export interface ContextTokenThresholds {
  warningThreshold: number;
  criticalThreshold: number;
}

export interface ContextMessageTokenStat {
  nodeIndex: number;
  nodeNumber: number;
  role: string;
  tokens: number;
  toolTokens: number;
  weightClass: ContextTokenWeightClass;
  editable: boolean;
  internalKind?: 'instruction' | 'environment';
}

export const DEFAULT_CONTEXT_TOKEN_THRESHOLDS: ContextTokenThresholds = {
  warningThreshold: 5000,
  criticalThreshold: 10000,
};

// Mirrors Codex's resized-image estimate: 7,373 model-visible bytes at roughly 4 bytes/token.
export const CONTEXT_IMAGE_TOKEN_ESTIMATE = 1844;

export interface ContextMapNodeMeta {
  displayNodeNumber: number | null;
  editable: boolean;
  selectable: boolean;
  internalKind?: 'instruction' | 'environment';
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isInlineImageDataUrl(value: unknown): boolean {
  return typeof value === 'string' && /^data:image\/[^,;]+(?:;[^,]*)*;base64,/i.test(value.trim());
}

function isImageContentRecord(value: UnknownRecord): boolean {
  const type = String(value.type || '').trim().toLowerCase();
  if (type.includes('image') || 'image_url' in value) {
    return true;
  }

  return isInlineImageDataUrl(value.url);
}

function countImageContentItems(value: unknown): number {
  if (Array.isArray(value)) {
    return value.reduce<number>((total, item) => total + countImageContentItems(item), 0);
  }

  if (!isRecord(value)) {
    return 0;
  }

  if (isImageContentRecord(value)) {
    return 1;
  }

  return Object.values(value).reduce<number>((total, item) => total + countImageContentItems(item), 0);
}

function isEnvironmentContextMessage(message: MessageRecord) {
  return message.role === 'user' && message.text.trimStart().toLowerCase().startsWith('<environment_context>');
}

export function buildContextMapNodeMeta(messages: MessageRecord[]): ContextMapNodeMeta[] {
  const environmentIndex = messages.findIndex(isEnvironmentContextMessage);
  let displayNodeNumber = 0;

  return messages.map((message, index) => {
    const internalKind =
      message.role === 'system' || message.role === 'developer'
        ? 'instruction'
        : index === environmentIndex && isEnvironmentContextMessage(message)
          ? 'environment'
          : undefined;

    if (internalKind) {
      return {
        displayNodeNumber: null,
        editable: false,
        selectable: false,
        internalKind,
      };
    }

    displayNodeNumber += 1;
    return {
      displayNodeNumber,
      editable: true,
      selectable: true,
    };
  });
}

export function normalizeContextTokenThresholds(
  thresholds: Partial<ContextTokenThresholds> = {},
): ContextTokenThresholds {
  const warningThreshold = Math.max(
    0,
    Math.floor(Number(thresholds.warningThreshold ?? DEFAULT_CONTEXT_TOKEN_THRESHOLDS.warningThreshold) || 0),
  );
  const rawCriticalThreshold = Math.floor(
    Number(thresholds.criticalThreshold ?? DEFAULT_CONTEXT_TOKEN_THRESHOLDS.criticalThreshold) || 0,
  );

  return {
    warningThreshold,
    criticalThreshold: Math.max(warningThreshold + 1, rawCriticalThreshold),
  };
}

export function getContextTokenWeightClass(
  tokenCount: number,
  thresholds: ContextTokenThresholds = DEFAULT_CONTEXT_TOKEN_THRESHOLDS,
): ContextTokenWeightClass {
  const safeTokenCount = Math.max(0, Math.floor(tokenCount || 0));
  const normalizedThresholds = normalizeContextTokenThresholds(thresholds);

  if (safeTokenCount > normalizedThresholds.criticalThreshold) {
    return 'heavy';
  }

  if (safeTokenCount >= normalizedThresholds.warningThreshold) {
    return 'medium';
  }

  return 'light';
}

export function isContextTokenCritical(
  tokenCount: number,
  thresholds: ContextTokenThresholds = DEFAULT_CONTEXT_TOKEN_THRESHOLDS,
) {
  return Math.max(0, Math.floor(tokenCount || 0)) > normalizeContextTokenThresholds(thresholds).criticalThreshold;
}

export function getContextToolWeightSource(message: MessageRecord) {
  const parts: string[] = [];

  message.blocks.forEach((block) => {
    if (block.kind !== 'tool') {
      return;
    }

    const event = block.tool_event;
    const toolParts = [
      event.display_title,
      event.display_detail,
      event.output_preview,
      event.display_result,
      event.raw_output,
    ]
      .map((value) => String(value || '').trim())
      .filter(Boolean);

    if (toolParts.length) {
      parts.push(toolParts.join('\n'));
    }
  });

  return parts.join('\n\n');
}

export function getContextImageTokenEstimate(message: MessageRecord) {
  const providerItemImageCount = countImageContentItems(message.providerItems || []);
  const attachmentImageCount = message.attachments.filter((attachment) => attachment.kind === 'image').length;
  const imageCount = providerItemImageCount || attachmentImageCount;

  return imageCount * CONTEXT_IMAGE_TOKEN_ESTIMATE;
}

export function getContextWeightSource(message: MessageRecord) {
  const parts: string[] = [];

  if (message.blocks.length) {
    message.blocks.forEach((block) => {
      if (block.kind === 'text') {
        if (block.text.trim()) {
          parts.push(block.text);
        }
        return;
      }

      if (block.kind === 'reasoning' || block.kind === 'thinking') {
        return;
      }

      const toolSource = getContextToolWeightSource({
        ...message,
        blocks: [block],
      });

      if (toolSource) {
        parts.push(toolSource);
      }
    });
  }

  if (!parts.length && message.text.trim()) {
    parts.push(message.text);
  }

  if (message.attachments.length) {
    parts.push(message.attachments.map((attachment) => attachment.name).join('\n'));
  }

  return parts.join('\n\n');
}

export function getContextTokenCount(message: MessageRecord) {
  return countTokens(getContextWeightSource(message)) + getContextImageTokenEstimate(message);
}
