import type {
  ContextWorkbenchChatMessage,
  ContextWorkbenchToolCatalogItem,
  ProxyUsageBucket,
  ProxyUsageSummary,
  ResponseProviderDraft,
  ResponseProviderModel,
  ResponseProviderSettings,
} from '../types';
import type { UiLocale } from '../i18n';

export type WorkbenchTab = 'suggestions' | 'manual' | 'usage' | 'settings';

export interface ManualWorkbenchMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  pending?: boolean;
  statusText?: string;
}

export type UsageSummaryLike = ProxyUsageBucket | ProxyUsageSummary | null;

export const DEFAULT_WORKBENCH_MODELS = ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.2'];
export const DEFAULT_WORKBENCH_PROVIDER_ID = 'codex-proxy';

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

export function statusLabel(status: ContextWorkbenchToolCatalogItem['status'], locale: UiLocale) {
  return status === 'available'
    ? uiText(locale, 'Available', '可用')
    : uiText(locale, 'Preview', '预览');
}

export function toWorkbenchProviderDraft(provider: ResponseProviderSettings): ResponseProviderDraft {
  return {
    id: provider.id,
    name: provider.name,
    provider_type: provider.provider_type,
    enabled: provider.enabled,
    supports_model_fetch: provider.supports_model_fetch,
    supports_responses: provider.supports_responses,
    api_base_url: provider.api_base_url || '',
    api_key_input: '',
    clear_api_key: false,
    default_model: provider.default_model || '',
    models: Array.isArray(provider.models) ? provider.models : [],
    last_sync_at: provider.last_sync_at || '',
    last_sync_error: provider.last_sync_error || '',
  };
}

export function inferWorkbenchProviderId(modelId: string, providers: ResponseProviderDraft[]) {
  const cleanedModelId = modelId.trim();
  if (cleanedModelId) {
    const matchedProvider = providers.find((provider) =>
      provider.models.some((model) => (model.id || '').trim() === cleanedModelId),
    );
    if (matchedProvider) {
      return matchedProvider.id;
    }
  }

  return providers.find((provider) => provider.enabled && provider.models.length > 0)?.id || 'openai';
}

export function resolveWorkbenchSelection(modelId: string, providerId: string, providers: ResponseProviderDraft[]) {
  const cleanedModelId = modelId.trim();
  const cleanedProviderId = providerId.trim();
  const matchedProvider = providers.find((provider) => provider.id === cleanedProviderId);
  const matchedModel =
    matchedProvider?.models.find((model) => (model.id || '').trim() === cleanedModelId) ||
    providers.find((provider) => provider.models.some((model) => (model.id || '').trim() === cleanedModelId))
      ?.models.find((model) => (model.id || '').trim() === cleanedModelId);

  if (matchedModel) {
    return {
      providerId: matchedProvider?.models.some((model) => (model.id || '').trim() === cleanedModelId)
        ? matchedProvider.id
        : inferWorkbenchProviderId(cleanedModelId, providers),
      modelId: matchedModel.id || matchedModel.label || DEFAULT_WORKBENCH_MODELS[0],
    };
  }

  const fallbackProvider = providers.find((provider) => provider.enabled && provider.models.length > 0);
  return {
    providerId: fallbackProvider?.id || cleanedProviderId || DEFAULT_WORKBENCH_PROVIDER_ID,
    modelId: cleanedModelId || fallbackProvider?.default_model || fallbackProvider?.models[0]?.id || DEFAULT_WORKBENCH_MODELS[0],
  };
}

export function workbenchProviderName(provider: ResponseProviderDraft | undefined) {
  if (!provider) {
    return 'No provider selected';
  }
  return provider.name.trim() || provider.id;
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

export function localizeToolCatalogItem(tool: ContextWorkbenchToolCatalogItem, locale: UiLocale) {
  switch (tool.id) {
    case 'get_context_node_details':
      return {
        label: uiText(locale, 'Expand node details', '展开节点详情'),
        description: uiText(locale, 'Expand nodes into full content and editable items before deciding whether to edit.', '将节点展开为完整内容和可编辑条目，再决定是否编辑。'),
      };
    case 'find_context_items':
      return {
        label: uiText(locale, 'Find items', '查找条目'),
        description: uiText(locale, 'Search provider items by metadata and previews without loading full node content.', '通过元数据和预览搜索条目，不加载完整节点内容。'),
      };
    case 'edit_context_items':
      return {
        label: uiText(locale, 'Edit items', '编辑条目'),
        description: uiText(locale, 'Batch delete, replace, or compress items selected by node, item, or type filters.', '按节点、条目或类型筛选后，批量删除、替换或压缩条目。'),
      };
    case 'delete_context_item':
      return {
        label: uiText(locale, 'Delete one item', '删除单个条目'),
        description: uiText(locale, 'Delete one item inside a node.', '删除某个节点里的一个条目。'),
      };
    case 'replace_context_item':
      return {
        label: uiText(locale, 'Replace one item', '替换单个条目'),
        description: uiText(locale, 'Replace one item inside a node with new content.', '把某个节点里的一个条目替换成新的内容。'),
      };
    case 'compress_context_item':
      return {
        label: uiText(locale, 'Compress one item', '压缩单个条目'),
        description: uiText(locale, 'Compress one item while keeping its item type.', '把某个条目压缩成更短的版本，同时保留原来的条目类型。'),
      };
    case 'compress_context_nodes':
      return {
        label: uiText(locale, 'Compress nodes', '压缩节点'),
        description: uiText(locale, 'Compress one or more nodes into summary nodes in the working snapshot.', '把一个或多个节点压缩成新的摘要节点。'),
      };
    case 'delete_context_nodes':
      return {
        label: uiText(locale, 'Delete nodes', '删除节点'),
        description: uiText(locale, 'Delete one or more nodes from the working snapshot.', '从当前工作快照里删除一个或多个节点。'),
      };
    case 'confirm_working_snapshot':
      return {
        label: uiText(locale, 'Confirm snapshot', '确认快照'),
        description: uiText(locale, 'Review the final active nodes after the planned edits are complete.', '在计划内编辑完成后，确认最终生效的节点概览。'),
      };
    default:
      return {
        label: tool.label,
        description: tool.description,
      };
  }
}

export function buildWorkbenchModelOptions(
  selectedProvider: ResponseProviderDraft | undefined,
  modelDraft: string,
): ResponseProviderModel[] {
  const seen = new Set<string>();
  const options: ResponseProviderModel[] = [];

  function pushModel(model: Partial<ResponseProviderModel> | string) {
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
        group: 'Codex',
        provider: 'Codex',
      });
      return;
    }

    options.push({
      id: cleanedId,
      label: (model.label || cleanedId).trim(),
      group: (model.group || selectedProvider?.name || 'Codex').trim(),
      provider: (model.provider || selectedProvider?.name || 'Codex').trim(),
    });
  }

  (selectedProvider?.models || []).forEach(pushModel);
  pushModel(modelDraft);
  DEFAULT_WORKBENCH_MODELS.forEach(pushModel);

  return options;
}
