import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';

import type {
  OpenAISettings,
  ProviderType,
  ResponseProviderDraft,
  ResponseProviderModel,
  SettingsDraft,
} from '../types';
import { getProviderModelCapabilities, type ProviderModelCapability } from '../modelCapabilities';
import { compareProviderModelGroups, getProviderModelGroup } from '../modelGroups';
import { getProviderModelInitial, getProviderModelLogo } from '../modelLogos';

interface SettingsProvidersPanelProps {
  fetchingProviderId: string;
  openAISettings: OpenAISettings;
  savingProviderId: string;
  settingsDraft: SettingsDraft;
  onClearApiKey: () => void;
  onProviderAdd: (providerType: ProviderType, providerName?: string) => string;
  onProviderDelete: (providerId: string) => Promise<string>;
  onProviderDraftChange: (providerId: string, patch: Partial<ResponseProviderDraft>) => void;
  onProviderLoadModels: (providerId: string) => Promise<ResponseProviderModel[]>;
  onProviderPersist: (
    providerId: string,
    options?: {
      activate?: boolean;
      clearApiKey?: boolean;
      silent?: boolean;
    },
  ) => void;
}

interface ProviderTypeMeta {
  value: ProviderType;
  label: string;
  shortLabel: string;
  description: string;
  defaultBaseUrl: string;
  accent: string;
  icon: string;
}

interface ProviderModelCard extends ResponseProviderModel {
  note: string;
  capabilities: ProviderModelCapability[];
}

const providerTypeOptions: ProviderTypeMeta[] = [
  {
    value: 'responses',
    label: 'Responses',
    shortLabel: 'Responses',
    description: 'OpenAI Responses 接口，主聊天的真实链路会优先走这一类。',
    defaultBaseUrl: 'https://api.openai.com/v1',
    accent: '#7fa7ff',
    icon: 'O',
  },
  {
    value: 'chat_completion',
    label: 'Chat Completion',
    shortLabel: 'Chat',
    description: 'OpenAI 兼容的 /chat/completions 供应商，模型列表从 /models 拉取。',
    defaultBaseUrl: 'https://api.example.com/v1',
    accent: '#7fd59c',
    icon: 'C',
  },
  {
    value: 'gemini',
    label: 'Gemini',
    shortLabel: 'Gemini',
    description: 'Google Gemini 接口，按 Gemini 的模型列表格式拉取。',
    defaultBaseUrl: 'https://generativelanguage.googleapis.com/v1beta',
    accent: '#70b5ff',
    icon: 'G',
  },
  {
    value: 'claude',
    label: 'Claude',
    shortLabel: 'Claude',
    description: 'Anthropic Claude 接口，按 Claude 的模型列表格式拉取。',
    defaultBaseUrl: 'https://api.anthropic.com/v1',
    accent: '#d48f62',
    icon: 'A',
  },
];

const BUILTIN_PROVIDER_IDS = new Set(['openai', 'codex-proxy', 'anthropic', 'gemini']);

function providerTypeMeta(providerType: ProviderType) {
  return providerTypeOptions.find((option) => option.value === providerType) || providerTypeOptions[0];
}

function providerDisplayName(provider: ResponseProviderDraft) {
  if (provider.name.trim()) {
    return provider.name.trim();
  }
  if (provider.id === 'anthropic') {
    return 'Claude';
  }
  if (provider.id === 'gemini') {
    return 'Gemini';
  }
  if (provider.id === 'openai') {
    return 'OpenAI';
  }
  return provider.id;
}

function providerInitial(provider: ResponseProviderDraft) {
  return (
    providerDisplayName(provider).slice(0, 1) ||
    providerTypeMeta(provider.provider_type).icon
  ).toUpperCase();
}

function isBuiltinProvider(provider: ResponseProviderDraft) {
  return BUILTIN_PROVIDER_IDS.has(provider.id);
}

function modelDisplayName(model: ResponseProviderModel) {
  return (model.label || model.id || 'Unnamed model').trim() || 'Unnamed model';
}

function modelIdentifier(model: ResponseProviderModel) {
  return (model.id || model.label || 'unknown-model').trim() || 'unknown-model';
}

function buildSecondaryModelText(model: ResponseProviderModel, fallbackText: string) {
  const label = modelDisplayName(model);
  const id = modelIdentifier(model);
  return id !== label ? id : fallbackText;
}

function normalizeProviderBaseUrl(rawValue: string, providerType: ProviderType) {
  const cleaned = rawValue.trim().replace(/\/+$/, '');
  if (!cleaned) {
    return '';
  }

  const suffixesByType: Record<ProviderType, string[]> = {
    responses: ['/responses', '/chat/completions', '/completions', '/models'],
    chat_completion: ['/chat/completions', '/completions', '/models'],
    gemini: ['/models'],
    claude: ['/messages', '/models'],
  };

  for (const suffix of suffixesByType[providerType]) {
    if (cleaned.endsWith(suffix)) {
      return cleaned.slice(0, -suffix.length);
    }
  }

  return cleaned;
}

function buildProviderPreviewUrl(rawValue: string, providerType: ProviderType) {
  const normalized = normalizeProviderBaseUrl(rawValue, providerType);
  if (!normalized) {
    return '';
  }

  if (providerType === 'responses') {
    return `${normalized}/responses`;
  }
  if (providerType === 'chat_completion') {
    return `${normalized}/chat/completions`;
  }
  if (providerType === 'claude') {
    return `${normalized}/messages`;
  }
  return `${normalized}/models`;
}

function sortModels<T extends ResponseProviderModel>(models: T[]) {
  return [...models].sort((left, right) =>
    modelIdentifier(left).localeCompare(modelIdentifier(right), 'en', { sensitivity: 'base' }),
  );
}

function buildFetchedModelCards(provider: ResponseProviderDraft): ProviderModelCard[] {
  return sortModels(provider.models).map((model) => {
    const group = getProviderModelGroup(model, provider.provider_type);

    return {
      ...model,
      label: modelDisplayName(model),
      group,
      provider: model.provider || providerTypeMeta(provider.provider_type).shortLabel,
      note: group,
      capabilities: getProviderModelCapabilities(model, provider.provider_type),
    };
  });
}

function groupModels(models: ProviderModelCard[]) {
  const groups = new Map<string, ProviderModelCard[]>();

  models.forEach((model) => {
    const groupName = model.group || 'Other';
    const items = groups.get(groupName) || [];
    items.push(model);
    groups.set(groupName, items);
  });

  return Array.from(groups.entries())
    .map(([groupName, items]) => [groupName, sortModels(items)] as const)
    .sort(([left], [right]) => compareProviderModelGroups(left, right));
}

function groupRemoteModels(models: ResponseProviderModel[], providerType: ProviderType) {
  const groups = new Map<string, ResponseProviderModel[]>();

  sortModels(models).forEach((model) => {
    const groupName = getProviderModelGroup(model, providerType);
    const items = groups.get(groupName) || [];
    items.push(model);
    groups.set(groupName, items);
  });

  return Array.from(groups.entries()).sort(([left], [right]) => compareProviderModelGroups(left, right));
}

function isGroupCollapsed(collapsedState: Record<string, boolean>, groupName: string) {
  return collapsedState[groupName] ?? true;
}

function ToggleButton({ checked, onClick }: { checked: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className={`settings-toggle${checked ? ' is-on' : ''}`}
      onClick={onClick}
      aria-pressed={checked}
    >
      <span className="settings-toggle-knob" />
    </button>
  );
}

function CapabilityBadges({
  capabilities,
  modelId,
}: {
  capabilities: ProviderModelCapability[];
  modelId: string;
}) {
  if (!capabilities.length) {
    return null;
  }

  return (
    <div className="settings-provider-model-badges">
      {capabilities.map((capability) => (
        <span
          key={`${modelId}-${capability.key}`}
          className={`settings-provider-model-badge tone-${capability.tone}`}
          title={capability.label}
          aria-label={capability.label}
        >
          <i className={`ph-fill ${capability.icon}`} />
        </span>
      ))}
    </div>
  );
}

export default function SettingsProvidersPanel({
  fetchingProviderId,
  openAISettings,
  savingProviderId,
  settingsDraft,
  onClearApiKey,
  onProviderAdd,
  onProviderDelete,
  onProviderDraftChange,
  onProviderLoadModels,
  onProviderPersist,
}: SettingsProvidersPanelProps) {
  const safeDraftProviders = Array.isArray(settingsDraft.response_providers) ? settingsDraft.response_providers : [];
  const safeSavedProviders = Array.isArray(openAISettings.response_providers) ? openAISettings.response_providers : [];

  const [selectedProviderId, setSelectedProviderId] = useState(settingsDraft.active_provider_id || 'openai');
  const [providerQuery, setProviderQuery] = useState('');
  const [providerModelQuery, setProviderModelQuery] = useState('');
  const [showApiKey, setShowApiKey] = useState(false);
  const [showAddTypes, setShowAddTypes] = useState(false);
  const [createProviderName, setCreateProviderName] = useState('');
  const [modelManagerOpen, setModelManagerOpen] = useState(false);
  const [modelCandidateQuery, setModelCandidateQuery] = useState('');
  const [modelCandidates, setModelCandidates] = useState<ResponseProviderModel[]>([]);
  const [modelCandidateError, setModelCandidateError] = useState('');
  const [collapsedModelGroups, setCollapsedModelGroups] = useState<Record<string, boolean>>({});
  const [collapsedCandidateGroups, setCollapsedCandidateGroups] = useState<Record<string, boolean>>({});

  const selectedProviderIdRef = useRef(selectedProviderId);
  const addTypeMenuRef = useRef<HTMLDivElement>(null);
  const pendingNameFocusProviderIdRef = useRef('');
  const pendingDeleteProviderIdRef = useRef('');
  const providerNameInputRef = useRef<HTMLInputElement>(null);
  const providerNameRestoreRef = useRef('');

  const selectedProvider = safeDraftProviders.find((provider) => provider.id === selectedProviderId) || null;

  useEffect(() => {
    if (
      !selectedProvider &&
      safeDraftProviders[0] &&
      pendingNameFocusProviderIdRef.current !== selectedProviderId
    ) {
      setSelectedProviderId(safeDraftProviders[0].id);
    }
  }, [safeDraftProviders, selectedProvider, selectedProviderId]);

  useEffect(() => {
    selectedProviderIdRef.current = selectedProviderId;
  }, [selectedProviderId]);

  useEffect(() => {
    setProviderModelQuery('');
    setShowApiKey(false);
    setModelManagerOpen(false);
    setModelCandidateQuery('');
    setModelCandidates([]);
    setModelCandidateError('');
    setCollapsedModelGroups({});
    setCollapsedCandidateGroups({});
  }, [selectedProviderId]);

  useEffect(() => {
    if (!showAddTypes) {
      return undefined;
    }

    function handlePointerDown(event: MouseEvent) {
      if (addTypeMenuRef.current?.contains(event.target as Node)) {
        return;
      }
      setShowAddTypes(false);
    }

    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [showAddTypes]);

  useEffect(() => {
    if (!modelManagerOpen && !showAddTypes) {
      return undefined;
    }

    function handleEscape(event: KeyboardEvent) {
      if (event.key !== 'Escape') {
        return;
      }

      if (modelManagerOpen) {
        setModelManagerOpen(false);
        return;
      }

      if (showAddTypes) {
        setShowAddTypes(false);
      }
    }

    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [modelManagerOpen, showAddTypes]);

  useEffect(() => {
    if (pendingNameFocusProviderIdRef.current !== selectedProvider?.id) {
      return undefined;
    }

    const frameId = window.requestAnimationFrame(() => {
      providerNameInputRef.current?.focus();
      providerNameInputRef.current?.select();
      pendingNameFocusProviderIdRef.current = '';
    });

    return () => window.cancelAnimationFrame(frameId);
  }, [selectedProvider?.id]);

  if (!selectedProvider) {
    return null;
  }

  const activeProvider = selectedProvider;
  const selectedTypeMeta = providerTypeMeta(selectedProvider.provider_type);
  const savedProvider = safeSavedProviders.find((provider) => provider.id === selectedProvider.id);
  const providerHasSavedApiKey = Boolean(savedProvider?.has_api_key);
  const providerApiPreview = savedProvider?.api_key_preview || '';
  const providerBaseUrl = selectedProvider.api_base_url || '';
  const providerPreviewUrl = buildProviderPreviewUrl(providerBaseUrl, selectedProvider.provider_type);
  const isFetchingModels = fetchingProviderId === selectedProvider.id;
  const isSavingProvider = savingProviderId === selectedProvider.id;
  const canDeleteSelectedProvider = !isBuiltinProvider(selectedProvider);

  const filteredProviders = useMemo(() => {
    const query = providerQuery.trim().toLowerCase();
    if (!query) {
      return safeDraftProviders;
    }

    return safeDraftProviders.filter((provider) => {
      const typeMeta = providerTypeMeta(provider.provider_type);
      return `${providerDisplayName(provider)} ${provider.id} ${typeMeta.label}`.toLowerCase().includes(query);
    });
  }, [providerQuery, safeDraftProviders]);

  const visibleModels = useMemo(() => {
    const models = buildFetchedModelCards(selectedProvider);
    const query = providerModelQuery.trim().toLowerCase();
    if (!query) {
      return models;
    }

    return models.filter((model) =>
      `${model.label} ${model.id} ${model.group} ${model.note}`.toLowerCase().includes(query),
    );
  }, [providerModelQuery, selectedProvider]);

  const modelGroups = useMemo(() => groupModels(visibleModels), [visibleModels]);

  const addedModelIds = useMemo(
    () => new Set(selectedProvider.models.map((model) => model.id)),
    [selectedProvider.models],
  );

  const visibleModelCandidates = useMemo(() => {
    const query = modelCandidateQuery.trim().toLowerCase();
    if (!query) {
      return modelCandidates;
    }

    return modelCandidates.filter((model) => {
      const group = getProviderModelGroup(model, selectedProvider.provider_type);
      return `${model.id} ${model.label} ${group} ${model.provider || ''}`.toLowerCase().includes(query);
    });
  }, [modelCandidateQuery, modelCandidates, selectedProvider.provider_type]);

  const visibleModelCandidateGroups = useMemo(
    () => groupRemoteModels(visibleModelCandidates, selectedProvider.provider_type),
    [selectedProvider.provider_type, visibleModelCandidates],
  );

  const showCandidateErrorState = Boolean(modelCandidateError && !modelCandidates.length);

  function handleProviderEnableToggle() {
    onProviderDraftChange(activeProvider.id, { enabled: !activeProvider.enabled });
    onProviderPersist(activeProvider.id, { silent: true });
  }

  function handleApiKeyAction() {
    const hasDraftKey = Boolean(activeProvider.api_key_input.trim());

    if (hasDraftKey || providerHasSavedApiKey) {
      onProviderDraftChange(activeProvider.id, {
        api_key_input: '',
        clear_api_key: true,
      });

      if (activeProvider.id === settingsDraft.active_provider_id) {
        onClearApiKey();
        return;
      }

      onProviderPersist(activeProvider.id, { clearApiKey: true, silent: true });
      return;
    }

    void openModelManager();
  }

  function handleAddProvider(providerType: ProviderType) {
    const nextProviderId = onProviderAdd(providerType, createProviderName.trim() || undefined);
    pendingNameFocusProviderIdRef.current = nextProviderId;
    setCreateProviderName('');
    setShowAddTypes(false);
    setSelectedProviderId(nextProviderId);
  }

  async function handleDeleteProvider() {
    if (!canDeleteSelectedProvider || isSavingProvider) {
      pendingDeleteProviderIdRef.current = '';
      return;
    }

    const remainingProviders = safeDraftProviders.filter((provider) => provider.id !== activeProvider.id);
    const optimisticNextProviderId =
      remainingProviders.find((provider) => provider.id === settingsDraft.active_provider_id)?.id ||
      remainingProviders[0]?.id ||
      'openai';

    setSelectedProviderId(optimisticNextProviderId);
    setShowAddTypes(false);
    setModelManagerOpen(false);

    const nextProviderId = await onProviderDelete(activeProvider.id);
    pendingDeleteProviderIdRef.current = '';
    setSelectedProviderId(nextProviderId);
  }

  async function openModelManager() {
    const providerId = activeProvider.id;
    setModelManagerOpen(true);
    setModelCandidateQuery('');
    setModelCandidateError('');

    try {
      const candidates = await onProviderLoadModels(providerId);
      if (providerId !== selectedProviderIdRef.current) {
        return;
      }

      setModelCandidates(candidates);
      setCollapsedCandidateGroups({});
      if (!candidates.length) {
        setModelCandidateError('这个地址没有返回可添加的模型。');
      }
    } catch (error) {
      if (providerId !== selectedProviderIdRef.current) {
        return;
      }
      setModelCandidateError(error instanceof Error ? error.message : String(error));
      setModelCandidates([]);
    }
  }

  function handleAddModels(models: ResponseProviderModel[]) {
    const nextAdditions = models
      .filter((model) => model.id && !addedModelIds.has(model.id))
      .map((model) => {
        const group = getProviderModelGroup(model, activeProvider.provider_type);
        return {
          id: model.id,
          label: modelDisplayName(model),
          group,
          provider: model.provider || selectedTypeMeta.shortLabel,
        };
      });

    if (!nextAdditions.length) {
      return;
    }

    onProviderDraftChange(activeProvider.id, {
      models: [...activeProvider.models, ...nextAdditions],
      default_model: activeProvider.default_model || nextAdditions[0].id,
      last_sync_error: '',
    });
    onProviderPersist(activeProvider.id, { silent: true });
  }

  function handleAddModel(model: ResponseProviderModel) {
    handleAddModels([model]);
  }

  function handleRemoveModels(modelIds: string[]) {
    const removedIds = new Set(modelIds);
    const nextModels = activeProvider.models.filter((model) => !removedIds.has(model.id));
    const nextDefaultModel = removedIds.has(activeProvider.default_model)
      ? nextModels[0]?.id || ''
      : activeProvider.default_model;

    onProviderDraftChange(activeProvider.id, {
      models: nextModels,
      default_model: nextDefaultModel,
      last_sync_error: '',
    });
    onProviderPersist(activeProvider.id, { silent: true });
  }

  function handleRemoveModel(modelId: string) {
    handleRemoveModels([modelId]);
  }

  function handleClearModels() {
    if (!activeProvider.models.length) {
      return;
    }

    onProviderDraftChange(activeProvider.id, {
      models: [],
      default_model: '',
      last_sync_error: '',
    });
    onProviderPersist(activeProvider.id, { silent: true });
  }

  function toggleModelGroup(groupName: string) {
    setCollapsedModelGroups((previous) => ({
      ...previous,
      [groupName]: !isGroupCollapsed(previous, groupName),
    }));
  }

  function toggleCandidateGroup(groupName: string) {
    setCollapsedCandidateGroups((previous) => ({
      ...previous,
      [groupName]: !isGroupCollapsed(previous, groupName),
    }));
  }

  return (
    <div className="settings-provider-workbench">
      <aside className="settings-provider-sidepane">
        <div className="settings-provider-search">
          <i className="ph-light ph-magnifying-glass" />
          <input
            type="text"
            value={providerQuery}
            onChange={(event) => setProviderQuery(event.target.value)}
            placeholder="搜索供应商..."
          />
          <button type="button" className="settings-provider-icon-btn" aria-label="筛选供应商">
            <i className="ph-light ph-funnel" />
          </button>
        </div>

        <div className="settings-provider-list-scroller">
          {filteredProviders.length ? (
            filteredProviders.map((provider) => {
              const typeMeta = providerTypeMeta(provider.provider_type);

              return (
                <button
                  key={provider.id}
                  type="button"
                  className={`settings-provider-list-item${selectedProviderId === provider.id ? ' active' : ''}`}
                  onClick={() => setSelectedProviderId(provider.id)}
                >
                  <span
                    className="settings-provider-avatar"
                    style={{ '--provider-accent': typeMeta.accent } as CSSProperties}
                  >
                    {providerInitial(provider)}
                  </span>
                  <div className="settings-provider-list-copy">
                    <strong>{providerDisplayName(provider)}</strong>
                    <small>{typeMeta.shortLabel}</small>
                  </div>
                  <span className={`settings-provider-status${provider.enabled ? ' is-on' : ''}`}>
                    {provider.enabled ? 'ON' : 'OFF'}
                  </span>
                </button>
              );
            })
          ) : (
            <div className="settings-provider-empty-state side">
              <p className="settings-provider-empty-title">没有匹配的供应商</p>
              <p>换个关键词，或者直接新建一个。</p>
            </div>
          )}
        </div>

        <div ref={addTypeMenuRef} className="settings-provider-add-wrap">
          {showAddTypes && (
            <div className="settings-provider-type-menu">
              <div className="settings-provider-type-name-field">
                <input
                  type="text"
                  value={createProviderName}
                  onChange={(event) => setCreateProviderName(event.target.value)}
                  placeholder="供应商名称（可选）"
                />
              </div>
              {providerTypeOptions.map((option) => (
                <button key={option.value} type="button" onClick={() => handleAddProvider(option.value)}>
                  <span style={{ '--provider-accent': option.accent } as CSSProperties}>{option.icon}</span>
                  <div>
                    <strong>{option.label}</strong>
                    <small>{option.description}</small>
                  </div>
                </button>
              ))}
            </div>
          )}

          <button
            type="button"
            className="settings-provider-add-btn"
            onClick={() => setShowAddTypes((previous) => !previous)}
          >
            <i className="ph-light ph-plus" />
            添加供应商
          </button>
        </div>
      </aside>

      <div className="settings-provider-mainpane">
        <div className="settings-provider-panel">
          <div className="settings-provider-panel-topbar">
            <div className="settings-provider-panel-meta">
              <div
                className="settings-provider-brandmark"
                style={{ '--provider-accent': selectedTypeMeta.accent } as CSSProperties}
              >
                {providerInitial(selectedProvider)}
              </div>

              <div className="settings-provider-panel-copy">
                <div className="settings-provider-panel-title-row">
                  <input
                    ref={providerNameInputRef}
                    className="settings-provider-name-input"
                    value={selectedProvider.name}
                    onChange={(event) => onProviderDraftChange(selectedProvider.id, { name: event.target.value })}
                    onFocus={() => {
                      providerNameRestoreRef.current = providerDisplayName(selectedProvider);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.currentTarget.blur();
                      }
                    }}
                    onBlur={(event) => {
                      if (pendingDeleteProviderIdRef.current === selectedProvider.id) {
                        return;
                      }
                      const rawName = event.currentTarget.value.trim();
                      const normalizedName =
                        rawName || providerNameRestoreRef.current || providerDisplayName(selectedProvider);
                      if (normalizedName !== event.currentTarget.value) {
                        event.currentTarget.value = normalizedName;
                      }
                      if (normalizedName !== selectedProvider.name) {
                        onProviderDraftChange(selectedProvider.id, { name: normalizedName });
                      }
                      onProviderPersist(selectedProvider.id, { silent: true });
                    }}
                    placeholder="供应商名称"
                  />
                  <span className="settings-provider-mini-chip">{selectedTypeMeta.shortLabel}</span>
                  {isFetchingModels && <span className="settings-provider-mini-chip">拉取中</span>}
                </div>
                <p>{selectedTypeMeta.description}</p>
              </div>
            </div>

            <div className="settings-provider-panel-actions">
              {canDeleteSelectedProvider ? (
                <button
                  type="button"
                  className="settings-provider-toolbar-btn danger-subtle"
                  onMouseDown={(event) => {
                    pendingDeleteProviderIdRef.current = selectedProvider.id;
                    event.preventDefault();
                  }}
                  onClick={() => void handleDeleteProvider()}
                  disabled={isSavingProvider}
                  title="删除当前供应商"
                >
                  <i className="ph-light ph-trash" />
                  删除供应商
                </button>
              ) : null}
              <ToggleButton checked={selectedProvider.enabled} onClick={handleProviderEnableToggle} />
            </div>
          </div>

          <div className="settings-provider-block">
            <div className="settings-provider-block-head">
              <div>
                <h4>API 密钥</h4>
                <p>密钥只保存在本地，用来拉取模型列表和后续请求。</p>
              </div>
              <button
                type="button"
                className="settings-provider-icon-btn"
                aria-label={showApiKey ? '隐藏 API 密钥' : '显示 API 密钥'}
                onClick={() => setShowApiKey((previous) => !previous)}
              >
                <i className={`ph-light ${showApiKey ? 'ph-eye-slash' : 'ph-eye'}`} />
              </button>
            </div>

            <div className="settings-provider-field-row">
              <input
                className="settings-field-input settings-provider-wide-input"
                type={showApiKey ? 'text' : 'password'}
                value={selectedProvider.api_key_input}
                onChange={(event) =>
                  onProviderDraftChange(selectedProvider.id, {
                    api_key_input: event.target.value,
                    clear_api_key: !event.target.value.trim() && providerHasSavedApiKey,
                  })
                }
                onBlur={() => onProviderPersist(selectedProvider.id, { silent: true })}
                placeholder={providerHasSavedApiKey ? providerApiPreview : '输入 API Key'}
              />
              <button
                type="button"
                className="settings-provider-inline-btn"
                onClick={handleApiKeyAction}
                disabled={isSavingProvider || isFetchingModels}
              >
                {selectedProvider.api_key_input.trim() || providerHasSavedApiKey ? '清除' : '检测'}
              </button>
            </div>
          </div>

          <div className="settings-provider-block">
            <div className="settings-provider-block-head">
              <div>
                <h4>API 地址</h4>
                <p>这里填基础地址就行，会按接口类型自动拼出真实请求路径。</p>
              </div>
              <span className="settings-provider-mini-chip">{selectedTypeMeta.label}</span>
            </div>
            <input
              className="settings-field-input settings-provider-wide-input"
              value={providerBaseUrl}
              onChange={(event) => onProviderDraftChange(selectedProvider.id, { api_base_url: event.target.value })}
              onBlur={() => onProviderPersist(selectedProvider.id, { silent: true })}
              placeholder={selectedTypeMeta.defaultBaseUrl}
            />
            <p className="settings-provider-preview-note">
              预览：{providerPreviewUrl || '填写后这里会显示实际请求路径'}
            </p>
          </div>

          <div className="settings-provider-block settings-provider-models-block">
            <div className="settings-provider-models-head">
              <div className="settings-provider-models-title">
                <h4>模型</h4>
                <span className="settings-provider-count">{visibleModels.length}</span>
              </div>

              <div className="settings-provider-models-actions">
                <div className="settings-provider-model-search">
                  <i className="ph-light ph-magnifying-glass" />
                  <input
                    type="text"
                    value={providerModelQuery}
                    onChange={(event) => setProviderModelQuery(event.target.value)}
                    placeholder="搜索已添加模型"
                  />
                </div>
                <button
                  type="button"
                  className="settings-provider-danger-btn"
                  onClick={handleClearModels}
                  disabled={!selectedProvider.models.length}
                >
                  <i className="ph-light ph-trash" />
                  清空全部
                </button>
              </div>
            </div>

            <div className="settings-provider-models-content">
              {modelGroups.length ? (
                modelGroups.map(([groupName, models]) => {
                  const collapsed = isGroupCollapsed(collapsedModelGroups, groupName);

                  return (
                    <div key={groupName} className="settings-provider-model-group">
                      <div className="settings-provider-model-group-head">
                        <button
                          type="button"
                          className="settings-provider-model-group-toggle"
                          aria-expanded={!collapsed}
                          onClick={() => toggleModelGroup(groupName)}
                        >
                          <i className={`ph-light ph-caret-right${collapsed ? '' : ' is-open'}`} />
                          <strong>{groupName}</strong>
                          <span>{models.length} 个模型</span>
                        </button>
                        <button
                          type="button"
                          className="settings-provider-candidate-group-action is-added"
                          onClick={() => handleRemoveModels(models.map((model) => model.id))}
                        >
                          <i className="ph-light ph-trash" />
                          删除本组
                        </button>
                      </div>

                      <div className={`settings-provider-group-body${collapsed ? ' is-collapsed' : ''}`}>
                        <div className="settings-provider-group-body-inner">
                          {models.map((model) => {
                            const modelLogo = getProviderModelLogo(model, selectedProvider.provider_type);
                            const secondaryText = buildSecondaryModelText(model, model.note);

                            return (
                              <div key={model.id} className="settings-provider-model-item">
                                <div className="settings-provider-model-select">
                                  <div className="settings-provider-model-main">
                                    <span className="settings-provider-model-logo">
                                      {modelLogo ? (
                                        <img src={modelLogo} alt="" aria-hidden="true" />
                                      ) : (
                                        getProviderModelInitial(model)
                                      )}
                                    </span>
                                    <div className="settings-provider-model-copy">
                                      <strong>{model.label}</strong>
                                      <span>{secondaryText}</span>
                                    </div>
                                  </div>

                                  <CapabilityBadges capabilities={model.capabilities} modelId={model.id} />
                                </div>

                                <button
                                  type="button"
                                  className="settings-provider-model-remove"
                                  aria-label={`删除模型 ${model.label}`}
                                  title="删除模型"
                                  onClick={() => handleRemoveModel(model.id)}
                                >
                                  <i className="ph-light ph-trash" />
                                </button>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="settings-provider-empty-state">
                  <p className="settings-provider-empty-title">
                    {selectedProvider.last_sync_error || '还没有添加模型'}
                  </p>
                  <p>点“管理模型”后，会先拉远端列表，再由你手动把需要的模型加进来。</p>
                </div>
              )}
            </div>

            <div className="settings-provider-bottom">
              <div className="settings-provider-action-group">
                <button
                  type="button"
                  className="settings-provider-toolbar-btn"
                  onClick={() => void openModelManager()}
                  disabled={isFetchingModels}
                >
                  <i className={`ph-light ${isFetchingModels ? 'ph-spinner-gap' : 'ph-list-bullets'}`} />
                  {isFetchingModels ? '拉取中' : '管理模型'}
                </button>
                <button
                  type="button"
                  className="settings-provider-toolbar-btn"
                  onClick={() => void openModelManager()}
                  disabled={isFetchingModels}
                >
                  <i className="ph-light ph-plus" />
                  添加模型
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {modelManagerOpen && (
        <div
          className="settings-provider-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setModelManagerOpen(false);
            }
          }}
        >
          <section className="settings-provider-model-dialog" aria-label="管理供应商模型">
            <div className="settings-provider-dialog-head">
              <div>
                <strong>管理模型</strong>
                <p>{providerPreviewUrl || selectedTypeMeta.defaultBaseUrl}</p>
              </div>
              <button
                type="button"
                className="settings-provider-icon-btn"
                aria-label="关闭模型管理"
                onClick={() => setModelManagerOpen(false)}
              >
                <i className="ph-light ph-x" />
              </button>
            </div>

            <div className="settings-provider-dialog-toolbar">
              <div className="settings-provider-model-search">
                <i className="ph-light ph-magnifying-glass" />
                <input
                  type="text"
                  value={modelCandidateQuery}
                  onChange={(event) => setModelCandidateQuery(event.target.value)}
                  placeholder="搜索远端模型"
                />
              </div>
              <button
                type="button"
                className="settings-provider-toolbar-btn"
                onClick={() => void openModelManager()}
                disabled={isFetchingModels}
              >
                <i className={`ph-light ${isFetchingModels ? 'ph-spinner-gap' : 'ph-arrow-clockwise'}`} />
                {isFetchingModels ? '拉取中' : '重新拉取'}
              </button>
            </div>

            <div className="settings-provider-candidate-list">
              {modelCandidateError && modelCandidates.length ? (
                <div className="settings-provider-inline-error">{modelCandidateError}</div>
              ) : null}

              {isFetchingModels && !modelCandidates.length ? (
                <div className="settings-provider-empty-state">
                  <p className="settings-provider-empty-title">正在请求模型列表</p>
                  <p>这一步只会拿到候选模型，不会自动把整批模型全塞进供应商。</p>
                </div>
              ) : showCandidateErrorState ? (
                <div className="settings-provider-empty-state">
                  <p className="settings-provider-empty-title">{modelCandidateError}</p>
                </div>
              ) : visibleModelCandidateGroups.length ? (
                visibleModelCandidateGroups.map(([groupName, models]) => {
                  const allAdded = models.every((model) => addedModelIds.has(model.id));
                  const collapsed = isGroupCollapsed(collapsedCandidateGroups, groupName);

                  return (
                    <div key={groupName} className="settings-provider-candidate-group">
                      <div className="settings-provider-candidate-group-head">
                        <button
                          type="button"
                          className="settings-provider-candidate-group-toggle"
                          aria-expanded={!collapsed}
                          onClick={() => toggleCandidateGroup(groupName)}
                        >
                          <i className={`ph-light ph-caret-right${collapsed ? '' : ' is-open'}`} />
                          <strong>{groupName}</strong>
                          <span>{models.length}</span>
                        </button>
                        <button
                          type="button"
                          className={`settings-provider-candidate-group-action${allAdded ? ' is-added' : ''}`}
                          onClick={() =>
                            allAdded
                              ? handleRemoveModels(models.map((model) => model.id))
                              : handleAddModels(models)
                          }
                        >
                          <i className={`ph-light ${allAdded ? 'ph-minus' : 'ph-plus'}`} />
                          {allAdded ? '移除本组' : '添加本组'}
                        </button>
                      </div>

                      <div className={`settings-provider-group-body${collapsed ? ' is-collapsed' : ''}`}>
                        <div className="settings-provider-group-body-inner">
                          {models.map((model) => {
                            const isAdded = addedModelIds.has(model.id);
                            const modelLogo = getProviderModelLogo(model, selectedProvider.provider_type);
                            const capabilities = getProviderModelCapabilities(model, selectedProvider.provider_type);
                            const candidateModelId = modelIdentifier(model);

                            return (
                              <div key={candidateModelId} className="settings-provider-candidate-row">
                                <div className="settings-provider-candidate-main">
                                  <span className="settings-provider-model-logo">
                                    {modelLogo ? (
                                      <img src={modelLogo} alt="" aria-hidden="true" />
                                    ) : (
                                      getProviderModelInitial(model)
                                    )}
                                  </span>
                                  <div className="settings-provider-model-copy">
                                    <strong>{modelDisplayName(model)}</strong>
                                    <span>{candidateModelId}</span>
                                  </div>
                                </div>

                                <div className="settings-provider-candidate-actions">
                                  <CapabilityBadges capabilities={capabilities} modelId={candidateModelId} />
                                  <button
                                    type="button"
                                    className={`settings-provider-candidate-add${isAdded ? ' is-added' : ''}`}
                                    onClick={() => (isAdded ? handleRemoveModel(model.id) : handleAddModel(model))}
                                  >
                                    <i className={`ph-light ${isAdded ? 'ph-minus' : 'ph-plus'}`} />
                                    {isAdded ? '移除' : '添加'}
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="settings-provider-empty-state">
                  <p className="settings-provider-empty-title">没有匹配的模型</p>
                  <p>换个关键词，或者重新拉取一次。</p>
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
