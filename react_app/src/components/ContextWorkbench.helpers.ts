import type {
  ContextWorkbenchProvider,
  ContextWorkbenchChatMessage,
  MessageBlock,
  ProxyUsageBucket,
  ProxyUsageSummary,
  ResponseProviderModel,
  ToolEvent,
} from '../types';
import type { UiLocale } from '../i18n';

export type WorkbenchTab = 'suggestions' | 'manual' | 'usage' | 'settings';

export interface ManualWorkbenchMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  toolEvents?: ToolEvent[];
  blocks?: MessageBlock[];
  pending?: boolean;
  statusText?: string;
}

export type UsageSummaryLike = ProxyUsageBucket | ProxyUsageSummary | null;

export const DEFAULT_CONTEXT_WORKBENCH_MODEL = 'gpt-5.6-sol';

export function preferredProviderModel(
  provider: Pick<ContextWorkbenchProvider, 'default_model' | 'models'> | null,
) {
  const savedModel = provider?.default_model?.trim() || '';
  if (savedModel) return savedModel;
  return provider?.models?.find((model) => model.id?.trim())?.id.trim() || '';
}

export const WORKBENCH_TABS: Array<{
  id: WorkbenchTab;
  label: string;
  icon: string;
}> = [
  { id: 'suggestions', label: 'Suggestions', icon: 'ph-lightbulb' },
  { id: 'manual', label: 'Manual', icon: 'ph-hand-pointing' },
  { id: 'usage', label: 'Usage', icon: 'ph-chart-bar' },
  { id: 'settings', label: 'Settings', icon: 'ph-gear' },
];

export const UI_LANGUAGE_OPTIONS: Array<{ value: UiLocale }> = [
  { value: 'en-US' },
  { value: 'zh-CN' },
];

export function uiText(locale: UiLocale, english: string, chinese: string) {
  return locale === 'zh-CN' ? chinese : english;
}

export function uiLanguageLabel(value: UiLocale, locale: UiLocale) {
  if (value === 'en-US') {
    return uiText(locale, 'English', '英文');
  }
  return uiText(locale, 'Chinese', '简体中文');
}

export function workbenchTabLabel(tab: WorkbenchTab, locale: UiLocale) {
  switch (tab) {
    case 'suggestions':
      return uiText(locale, 'Suggestions', '建议');
    case 'manual':
      return uiText(locale, 'Manual', '手动');
    case 'usage':
      return uiText(locale, 'Usage', '用量');
    case 'settings':
      return uiText(locale, 'Settings', '设置');
    default:
      return tab;
  }
}

export function reasoningDisplayLabel(value: string, fallbackLabel: string, locale: UiLocale) {
  switch (value) {
    case 'default':
      return uiText(locale, 'Auto', '自动');
    case 'none':
      return uiText(locale, 'Off', '关闭');
    case 'minimal':
      return uiText(locale, 'Minimal', '极简');
    case 'low':
      return uiText(locale, 'Low', '低');
    case 'medium':
      return uiText(locale, 'Medium', '中');
    case 'high':
      return uiText(locale, 'High', '高');
    case 'xhigh':
      return uiText(locale, 'Extra High', '超高');
    default:
      return fallbackLabel;
  }
}

export function createManualMessage(
  role: ManualWorkbenchMessage['role'],
  content: string,
  options: Partial<ManualWorkbenchMessage> = {},
): ManualWorkbenchMessage {
  return {
    id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    role,
    content,
    pending: false,
    ...options,
  };
}

export function buildManualMessagesFromChat(chat: ContextWorkbenchChatMessage[]): ManualWorkbenchMessage[] {
  if (!chat.length) {
    return [];
  }

  return chat.map((entry, index) =>
    createManualMessage(entry.role, entry.content, {
      id: `context-chat-${index}-${entry.role}`,
      toolEvents: Array.isArray(entry.toolEvents) ? entry.toolEvents : undefined,
      blocks: Array.isArray(entry.blocks) ? entry.blocks : undefined,
    }),
  );
}

export function getThrownMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export function formatCostUsd(value: number | undefined) {
  const safeValue = Number.isFinite(value) ? Number(value) : 0;
  if (safeValue <= 0) {
    return '$0.0000';
  }
  if (safeValue < 0.0001) {
    return '<$0.0001';
  }
  return `$${safeValue.toFixed(4)}`;
}

export function formatPercent(value: number | undefined) {
  const safeValue = Number.isFinite(value) ? Number(value) : 0;
  return `${(Math.max(0, Math.min(safeValue, 1)) * 100).toFixed(1)}%`;
}

export function formatNodeReferenceSegments(nodeNumbers: number[]) {
  if (!nodeNumbers.length) {
    return [];
  }

  const segments: string[] = [];
  let rangeStart = nodeNumbers[0];
  let previous = nodeNumbers[0];

  for (let index = 1; index < nodeNumbers.length; index += 1) {
    const current = nodeNumbers[index];
    if (current === previous + 1) {
      previous = current;
      continue;
    }

    segments.push(rangeStart === previous ? `${rangeStart}` : `${rangeStart}-${previous}`);
    rangeStart = current;
    previous = current;
  }

  segments.push(rangeStart === previous ? `${rangeStart}` : `${rangeStart}-${previous}`);
  return segments;
}

export function formatTokenCount(value: number) {
  return value.toLocaleString('zh-CN');
}

export function parseTokenThresholdDraft(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : fallback;
}

export function formatSuggestionRoleLabel(role: string, locale: UiLocale) {
  return role === 'user' ? uiText(locale, 'User', '用户') : uiText(locale, 'Assistant', '助手');
}

export function isAbortError(error: unknown) {
  return error instanceof Error && error.name === 'AbortError';
}

export function buildWorkbenchModelOptions(
  modelDraft: string,
  models: Array<ResponseProviderModel | string> = [],
  fallbackProvider = 'Codex',
): ResponseProviderModel[] {
  const seen = new Set<string>();
  const options: ResponseProviderModel[] = [];

  function pushModel(model: Partial<ResponseProviderModel> | string, source: ResponseProviderModel['source']) {
    const modelId = typeof model === 'string' ? model : (model.id || model.label || '');
    const cleanedId = modelId.trim();
    if (!cleanedId || seen.has(cleanedId)) {
      return;
    }

    seen.add(cleanedId);
    if (typeof model === 'string') {
      options.push({
        id: cleanedId,
        label: cleanedId,
        group: fallbackProvider,
        provider: fallbackProvider,
        source,
      });
      return;
    }

    options.push({
      id: cleanedId,
      label: (model.label || cleanedId).trim(),
      group: (model.group || fallbackProvider).trim(),
      provider: (model.provider || fallbackProvider).trim(),
      source,
    });
  }

  models.forEach((model) => pushModel(model, 'provider'));
  const configuredModel = modelDraft.trim();
  if (configuredModel && !seen.has(configuredModel)) {
    pushModel(configuredModel, 'configured');
    const currentOption = options.pop();
    if (currentOption) options.unshift(currentOption);
  }

  return options;
}
