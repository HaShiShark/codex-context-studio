import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';

import { getProviderModelCapabilities, type ProviderModelCapability } from '../modelCapabilities';
import { compareProviderModelGroups, getProviderModelGroup } from '../modelGroups';
import { getProviderModelInitial, getProviderModelLogo } from '../modelLogos';
import type { ProviderType, ResponseProviderDraft, ResponseProviderModel } from '../types';

interface ChatModelPickerProps {
  activeProviderId: string;
  currentModel: string;
  open: boolean;
  providers: ResponseProviderDraft[];
  title?: string;
  description?: string;
  selectedContextLabel?: string;
  onClose: () => void;
  onSelectModel: (providerId: string, model: ResponseProviderModel) => void | Promise<void>;
}

type FilterKey = ProviderModelCapability['key'];

interface ProviderTypeMeta {
  accent: string;
  shortLabel: string;
}

interface ProviderModelCard extends ResponseProviderModel {
  capabilities: ProviderModelCapability[];
  group: string;
  secondary: string;
}

const FILTER_OPTIONS: Array<{ key: FilterKey; icon: string; label: string }> = [
  { key: 'vision', icon: 'ph-eye', label: '视觉' },
  { key: 'web_search', icon: 'ph-globe-hemisphere-west', label: '联网' },
  { key: 'reasoning', icon: 'ph-brain', label: '推理' },
  { key: 'tools', icon: 'ph-wrench', label: '工具' },
];

const PROVIDER_TYPE_META: Record<ProviderType, ProviderTypeMeta> = {
  responses: { accent: '#7fa7ff', shortLabel: 'Responses' },
  chat_completion: { accent: '#7fd59c', shortLabel: 'Chat' },
  gemini: { accent: '#70b5ff', shortLabel: 'Gemini' },
  claude: { accent: '#d48f62', shortLabel: 'Claude' },
};

function providerDisplayName(provider: ResponseProviderDraft) {
  if (provider.name.trim()) {
    return provider.name.trim();
  }
  if (provider.id === 'openai') {
    return 'OpenAI';
  }
  if (provider.id === 'anthropic') {
    return 'Claude';
  }
  if (provider.id === 'gemini') {
    return 'Gemini';
  }
  return provider.id;
}

function providerInitial(provider: ResponseProviderDraft) {
  return providerDisplayName(provider).slice(0, 1).toUpperCase() || PROVIDER_TYPE_META[provider.provider_type].shortLabel.slice(0, 1);
}

function modelDisplayName(model: ResponseProviderModel) {
  return (model.label || model.id || 'Unnamed model').trim() || 'Unnamed model';
}

function modelIdentifier(model: ResponseProviderModel) {
  return (model.id || model.label || 'unknown-model').trim() || 'unknown-model';
}

function sortModels<T extends ResponseProviderModel>(models: T[]) {
  return [...models].sort((left, right) =>
    modelIdentifier(left).localeCompare(modelIdentifier(right), 'en', { sensitivity: 'base', numeric: true }),
  );
}

function buildSecondaryModelText(model: ResponseProviderModel, fallbackText: string) {
  const label = modelDisplayName(model);
  const id = modelIdentifier(model);
  return id !== label ? id : fallbackText;
}

function modelSearchText(model: ProviderModelCard, providerName: string) {
  return [providerName, model.label, model.id, model.group, model.provider, model.secondary].filter(Boolean).join(' ').toLowerCase();
}

function modelMatchesFilters(model: ProviderModelCard, filterKeys: FilterKey[]) {
  return filterKeys.every((filterKey) => model.capabilities.some((capability) => capability.key === filterKey));
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

function ChatModelPickerDialog({
  activeProviderId,
  currentModel,
  open: _open,
  providers,
  title = '选择模型',
  description = '这里展示所有已启用供应商里已经添加好的模型，点击后会直接切到对应供应商。',
  selectedContextLabel = '当前会话',
  onClose,
  onSelectModel,
}: ChatModelPickerProps) {
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [searchText, setSearchText] = useState('');
  const deferredSearchText = useDeferredValue(searchText.trim().toLowerCase());
  const [activeFilters, setActiveFilters] = useState<FilterKey[]>([]);
  const [collapsedProviders, setCollapsedProviders] = useState<Record<string, boolean>>({});

  const enabledProviders = useMemo(
    () => providers.filter((provider) => provider.enabled && Array.isArray(provider.models) && provider.models.length > 0),
    [providers],
  );

  const providerCards = useMemo(
    () =>
      enabledProviders.map((provider) => ({
        provider,
        providerName: providerDisplayName(provider),
        meta: PROVIDER_TYPE_META[provider.provider_type],
        cards: sortModels(provider.models).map((model): ProviderModelCard => {
          const group = getProviderModelGroup(model, provider.provider_type);
          return {
            ...model,
            label: modelDisplayName(model),
            group,
            provider: model.provider || PROVIDER_TYPE_META[provider.provider_type].shortLabel,
            secondary: buildSecondaryModelText(model, group),
            capabilities: getProviderModelCapabilities(model, provider.provider_type),
          };
        }),
      })),
    [enabledProviders],
  );

  const availableFilters = useMemo(() => {
    const supported = new Set<FilterKey>();
    providerCards.forEach(({ cards }) => {
      cards.forEach((card) => {
        card.capabilities.forEach((capability) => supported.add(capability.key));
      });
    });
    return FILTER_OPTIONS.filter((option) => supported.has(option.key));
  }, [providerCards]);

  const hasActiveQuery = Boolean(deferredSearchText);
  const hasActiveFilters = activeFilters.length > 0;

  const providerSections = useMemo(
    () =>
      providerCards
        .map((entry) => {
          const visibleCards = entry.cards.filter((card) => {
            if (hasActiveQuery && !modelSearchText(card, entry.providerName).includes(deferredSearchText)) {
              return false;
            }
            if (hasActiveFilters && !modelMatchesFilters(card, activeFilters)) {
              return false;
            }
            return true;
          });

          return {
            ...entry,
            filteredCount: visibleCards.length,
            visibleCards,
            selected: entry.provider.id === activeProviderId && entry.cards.some((card) => modelIdentifier(card) === currentModel),
          };
        })
        .filter((entry) => entry.filteredCount > 0 || (!hasActiveQuery && !hasActiveFilters)),
    [activeFilters, activeProviderId, currentModel, deferredSearchText, hasActiveFilters, hasActiveQuery, providerCards],
  );

  useEffect(() => {
    setSearchText('');
    setActiveFilters([]);
    setCollapsedProviders(Object.fromEntries(enabledProviders.map((provider) => [provider.id, true])));

    const frameId = window.requestAnimationFrame(() => {
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    });

    return () => window.cancelAnimationFrame(frameId);
  }, [enabledProviders]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="chat-model-picker-backdrop" onClick={onClose}>
      <section
        aria-label="选择聊天模型"
        aria-modal="true"
        className="chat-model-picker-dialog"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="chat-model-picker-head">
          <div className="chat-model-picker-title">
            <strong>{title}</strong>
            <p>{description}</p>
          </div>
          <button className="chat-model-picker-close" onClick={onClose} type="button" aria-label="关闭模型选择">
            <i className="ph-light ph-x" />
          </button>
        </div>

        <div className="chat-model-picker-toolbar">
          <label className="settings-provider-model-search chat-model-picker-search">
            <i className="ph-light ph-magnifying-glass" />
            <input
              ref={searchInputRef}
              type="search"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="搜索模型或供应商..."
            />
          </label>

          {availableFilters.length ? (
            <div className="chat-model-picker-filter-row">
              {availableFilters.map((filter) => {
                const active = activeFilters.includes(filter.key);
                return (
                  <button
                    key={filter.key}
                    type="button"
                    className={`chat-model-picker-filter${active ? ' active' : ''}`}
                    onClick={() =>
                      setActiveFilters((previous) =>
                        previous.includes(filter.key)
                          ? previous.filter((item) => item !== filter.key)
                          : [...previous, filter.key],
                      )
                    }
                  >
                    <i className={`ph-light ${filter.icon}`} />
                    <span>{filter.label}</span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>

        <div className="chat-model-picker-list">
          {providerSections.length ? (
            providerSections.map((section) => {
              const providerIsExpanded = hasActiveQuery || hasActiveFilters || !collapsedProviders[section.provider.id];
              const groupedCards = providerIsExpanded ? groupModels(section.visibleCards) : [];
              const providerStyle = { '--provider-accent': section.meta.accent } as CSSProperties;

              return (
                <div
                  key={section.provider.id}
                  className={`chat-model-picker-provider${providerIsExpanded ? ' is-expanded' : ''}`}
                  style={providerStyle}
                >
                  <button
                    type="button"
                    className={`chat-model-picker-provider-head${section.selected ? ' is-current' : ''}`}
                    onClick={() =>
                      setCollapsedProviders((previous) => ({
                        ...previous,
                        [section.provider.id]: !previous[section.provider.id],
                      }))
                    }
                  >
                    <span className="settings-provider-avatar chat-model-picker-provider-avatar">{providerInitial(section.provider)}</span>
                    <span className="chat-model-picker-provider-copy">
                      <strong>{section.providerName}</strong>
                      <small>
                        {section.meta.shortLabel}
                        {section.selected ? ` · ${selectedContextLabel}` : ''}
                      </small>
                    </span>
                    <span className="chat-model-picker-provider-count">
                      {hasActiveQuery || hasActiveFilters ? `${section.filteredCount} / ${section.cards.length}` : `${section.cards.length} 个模型`}
                    </span>
                    <i className="ph-light ph-caret-down chat-model-picker-provider-caret" />
                  </button>

                  <div className="chat-model-picker-provider-body-wrap">
                    {providerIsExpanded ? (
                    <div className="chat-model-picker-provider-body">
                      {groupedCards.map(([groupName, cards]) => (
                        <div key={`${section.provider.id}-${groupName}`} className="chat-model-picker-family">
                          <div className="chat-model-picker-family-label">{groupName}</div>
                          <div className="chat-model-picker-family-list">
                            {cards.map((model) => {
                              const modelId = modelIdentifier(model);
                              const logo = getProviderModelLogo(model, section.provider.provider_type);
                              const active = activeProviderId === section.provider.id && currentModel === modelId;

                              return (
                                <button
                                  key={`${section.provider.id}-${modelId}`}
                                  type="button"
                                  className={`chat-model-picker-model-row${active ? ' active' : ''}`}
                                  onClick={(event) => event.stopPropagation()}
                                  onMouseDown={(event) => {
                                    if (event.button !== 0) {
                                      return;
                                    }
                                    event.preventDefault();
                                    event.stopPropagation();
                                    onSelectModel(section.provider.id, model);
                                  }}
                                >
                                  <span className="settings-provider-model-logo">
                                    {logo ? <img src={logo} alt="" /> : <span>{getProviderModelInitial(model)}</span>}
                                  </span>
                                  <span className="settings-provider-model-copy chat-model-picker-model-copy">
                                    <strong>{model.label}</strong>
                                    <span>{model.secondary}</span>
                                  </span>
                                  <span className="settings-provider-model-badges chat-model-picker-model-badges">
                                    {model.capabilities.map((capability) => (
                                      <span
                                        key={`${modelId}-${capability.key}`}
                                        className={`settings-provider-model-badge tone-${capability.tone}`}
                                        title={capability.label}
                                      >
                                        <i className={`ph-light ${capability.icon}`} />
                                      </span>
                                    ))}
                                  </span>
                                  <span className={`chat-model-picker-model-check${active ? ' is-visible' : ''}`}>
                                    <i className="ph-bold ph-check" />
                                  </span>
                                </button>
                              );
                            })}
                          </div>
                        </div>
                      ))}
                    </div>
                    ) : null}
                  </div>
                </div>
              );
            })
          ) : (
            <div className="chat-model-picker-empty">
              <strong>没有找到可选模型</strong>
              <p>先去供应商里启用一个供应商，并把模型添加进去，这里才会出现真正能用的聊天模型。</p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

export default function ChatModelPicker(props: ChatModelPickerProps) {
  if (!props.open) {
    return null;
  }

  return <ChatModelPickerDialog {...props} />;
}
