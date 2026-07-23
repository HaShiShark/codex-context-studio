import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, MouseEvent, ReactNode } from 'react';
import { flushSync } from 'react-dom';

import {
  fetchContextWorkbenchSettings,
  refreshContextWorkbenchModelsRequest,
  saveContextWorkbenchSettingsRequest,
} from '../api';
import {
  DEFAULT_CONTEXT_TOKEN_THRESHOLDS,
  normalizeContextTokenThresholds,
  type ContextTokenThresholds,
} from '../contextTokenWeight';
import { normalizeSupportedLocale, type UiLocale } from '../i18n';
import type {
  ContextWorkbenchProvider,
  ContextWorkbenchSettingsResponse,
  ResponseProviderType,
} from '../types';
import { countTokens } from '../utils';
import {
  buildWorkbenchModelOptions,
  DEFAULT_CONTEXT_WORKBENCH_MODEL,
  getThrownMessage,
  parseTokenThresholdDraft,
  preferredProviderModel,
  UI_LANGUAGE_OPTIONS,
  uiLanguageLabel,
  uiText,
} from './ContextWorkbench.helpers';
import Dropdown from './Dropdown';

interface ContextWorkbenchSettingsProps {
  tokenThresholds: ContextTokenThresholds;
  uiLocale: UiLocale;
  themeMode: 'light' | 'dark';
  onTokenThresholdsChange: (thresholds: ContextTokenThresholds) => void;
  onUiLocaleChange?: (locale: UiLocale) => void;
  onUiFontChange?: (font: string, fontSize: number) => void;
  onThemeModeChange?: (themeMode: 'light' | 'dark') => void;
}

type PromptSettingKey =
  | 'codex_system_prompt'
  | 'manual_local_compact_prompt'
  | 'auto_local_compact_prompt';

type PromptDrafts = Record<PromptSettingKey, string>;

interface PromptSettingItem {
  key: PromptSettingKey;
  title: string;
  placeholder: string;
  description?: string;
}

const DEFAULT_CONTEXT_PROVIDER_ID = 'codex-proxy';
const EMPTY_PROMPT_DRAFTS: PromptDrafts = {
  codex_system_prompt: '',
  manual_local_compact_prompt: '',
  auto_local_compact_prompt: '',
};
const PROVIDER_TYPE_OPTIONS: Array<{ value: ResponseProviderType; label: string; zhLabel: string }> = [
  { value: 'responses', label: 'Responses', zhLabel: 'Responses' },
  { value: 'chat_completion', label: 'Chat Completions', zhLabel: 'Chat Completions' },
  { value: 'claude', label: 'Anthropic Messages', zhLabel: 'Anthropic Messages' },
  { value: 'gemini', label: 'Gemini', zhLabel: 'Gemini' },
];

function SettingsRow({ title, meta, children }: { title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <div className="workbench-settings-row">
      <div className="workbench-settings-row-label">
        <div className="workbench-setting-title">{title}</div>
        {meta ? <div className="workbench-settings-row-meta">{meta}</div> : null}
      </div>
      <div className="workbench-settings-row-control">{children}</div>
    </div>
  );
}

function promptDraftsFromSettings(settings: ContextWorkbenchSettingsResponse['settings']): PromptDrafts {
  return {
    codex_system_prompt: settings.codex_system_prompt || '',
    manual_local_compact_prompt: settings.manual_local_compact_prompt || '',
    auto_local_compact_prompt: settings.auto_local_compact_prompt || '',
  };
}

function promptDefaultsFromSettings(settings: ContextWorkbenchSettingsResponse['settings']): PromptDrafts {
  return {
    codex_system_prompt: settings.codex_system_prompt_default || '',
    manual_local_compact_prompt: settings.manual_local_compact_prompt_default || '',
    auto_local_compact_prompt: settings.auto_local_compact_prompt_default || '',
  };
}

function providerTypeLabel(providerType: ResponseProviderType, uiLocale: UiLocale) {
  const item = PROVIDER_TYPE_OPTIONS.find((option) => option.value === providerType);
  return item ? uiText(uiLocale, item.label, item.zhLabel) : providerType;
}

function providerDisplayName(provider: ContextWorkbenchProvider | null, uiLocale: UiLocale) {
  if (!provider || provider.id === DEFAULT_CONTEXT_PROVIDER_ID) return 'Codex';
  return (provider.name || provider.id || '').trim() || providerTypeLabel(provider.provider_type, uiLocale);
}

function providerProtocolLabel(provider: ContextWorkbenchProvider | null, uiLocale: UiLocale) {
  if (!provider) return '';
  if (provider.id === DEFAULT_CONTEXT_PROVIDER_ID) return uiText(uiLocale, 'Codex built-in', 'Codex 自带');
  return `${providerTypeLabel(provider.provider_type, uiLocale)} ${uiText(uiLocale, 'compatible', '兼容')}`;
}

function readableProviderError(errorText: string) {
  const cleaned = errorText.trim();
  if (!cleaned.startsWith('{')) return cleaned;
  try {
    const parsed = JSON.parse(cleaned) as { message?: unknown; error?: unknown };
    if (typeof parsed.message === 'string' && parsed.message.trim()) return parsed.message.trim();
    if (typeof parsed.error === 'string' && parsed.error.trim()) return parsed.error.trim();
  } catch {
  }
  return cleaned;
}

function providerErrorPlacement(errorText: string): 'base_url' | 'api_key' | 'model' {
  const normalized = errorText.trim().toLowerCase();
  if (normalized.includes('base url')) return 'base_url';
  if (normalized.includes('api key') || normalized.includes('key')) return 'api_key';
  return 'model';
}

function providerMapFromList(providers: ContextWorkbenchProvider[]) {
  return providers.reduce<Record<string, ContextWorkbenchProvider>>((result, provider) => {
    if (provider.id) result[provider.id] = provider;
    return result;
  }, {});
}

function defaultBaseUrlForProviderType(providerType: ResponseProviderType) {
  switch (providerType) {
    case 'gemini':
      return 'https://generativelanguage.googleapis.com/v1beta';
    case 'claude':
      return 'https://api.anthropic.com/v1';
    default:
      return 'https://api.openai.com/v1';
  }
}

const DEFAULT_UI_FONT_SIZE = 16;
const MIN_UI_FONT_SIZE = 10;
const MAX_UI_FONT_SIZE = 32;

export default function ContextWorkbenchSettings({
  tokenThresholds,
  uiLocale,
  themeMode,
  onTokenThresholdsChange,
  onUiLocaleChange,
  onUiFontChange,
  onThemeModeChange,
}: ContextWorkbenchSettingsProps) {
  const [providerId, setProviderId] = useState(DEFAULT_CONTEXT_PROVIDER_ID);
  const [providers, setProviders] = useState<ContextWorkbenchProvider[]>([]);
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ContextWorkbenchProvider>>({});
  const [isApiKeySaving, setIsApiKeySaving] = useState(false);
  const [modelDraft, setModelDraft] = useState(DEFAULT_CONTEXT_WORKBENCH_MODEL);
  const [reviewAutoEnabled, setReviewAutoEnabled] = useState(true);
  const [reviewIntervalDraft, setReviewIntervalDraft] = useState('10');
  const [isReviewSettingsSaving, setIsReviewSettingsSaving] = useState(false);
  const [localeDraft, setLocaleDraft] = useState<UiLocale>(uiLocale);
  const [themeDraft, setThemeDraft] = useState<'light' | 'dark'>(themeMode);
  const [isProviderOpen, setIsProviderOpen] = useState(false);
  const [isModelOpen, setIsModelOpen] = useState(false);
  const [isModelFilterActive, setIsModelFilterActive] = useState(false);
  const [isModelsRefreshing, setIsModelsRefreshing] = useState(false);
  const [warningThresholdDraft, setWarningThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.warningThreshold),
  );
  const [criticalThresholdDraft, setCriticalThresholdDraft] = useState(
    String(DEFAULT_CONTEXT_TOKEN_THRESHOLDS.criticalThreshold),
  );
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [fontDraft, setFontDraft] = useState('Noto Serif SC');
  const [fontSizeDraft, setFontSizeDraft] = useState(String(DEFAULT_UI_FONT_SIZE));
  const [promptDrafts, setPromptDrafts] = useState<PromptDrafts>(EMPTY_PROMPT_DRAFTS);
  const [savedPromptDrafts, setSavedPromptDrafts] = useState<PromptDrafts>(EMPTY_PROMPT_DRAFTS);
  const [promptDefaults, setPromptDefaults] = useState<PromptDrafts>(EMPTY_PROMPT_DRAFTS);
  const [expandedPromptKey, setExpandedPromptKey] = useState<PromptSettingKey | null>(null);
  const modelComboboxRef = useRef<HTMLDivElement>(null);

  const selectedProvider = useMemo(
    () => providerDrafts[providerId] || providers.find((provider) => provider.id === providerId) || null,
    [providerDrafts, providerId, providers],
  );
  const orderedProviders = useMemo(
    () => [
      ...providers.filter((provider) => provider.id === DEFAULT_CONTEXT_PROVIDER_ID),
      ...providers.filter((provider) => provider.id !== DEFAULT_CONTEXT_PROVIDER_ID),
    ],
    [providers],
  );
  const providerLabel = providerDisplayName(selectedProvider, localeDraft);
  const providerGroup = providerProtocolLabel(selectedProvider, localeDraft);
  const providerType = selectedProvider?.provider_type || 'responses';
  const showExternalProviderFields = Boolean(selectedProvider && selectedProvider.id !== DEFAULT_CONTEXT_PROVIDER_ID);
  const selectedProviderModels = selectedProvider?.models || [];
  const providerSyncError = readableProviderError(selectedProvider?.last_sync_error || '');
  const providerSyncErrorField = providerErrorPlacement(providerSyncError);
  const modelOptions = useMemo(
    () => buildWorkbenchModelOptions(
      modelDraft,
      selectedProviderModels,
      providerLabel,
    ),
    [modelDraft, providerLabel, selectedProviderModels],
  );
  const filteredModelOptions = useMemo(() => {
    const prefix = isModelFilterActive ? modelDraft.trim().toLowerCase() : '';
    if (!prefix) return modelOptions;
    return modelOptions.filter((model) => {
      const id = (model.id || '').trim().toLowerCase();
      const label = (model.label || '').trim().toLowerCase();
      return id.startsWith(prefix) || label.startsWith(prefix);
    });
  }, [isModelFilterActive, modelDraft, modelOptions]);
  const nextThresholds = useMemo(() => ({
    warningThreshold: parseTokenThresholdDraft(warningThresholdDraft, tokenThresholds.warningThreshold),
    criticalThreshold: parseTokenThresholdDraft(criticalThresholdDraft, tokenThresholds.criticalThreshold),
  }), [criticalThresholdDraft, tokenThresholds, warningThresholdDraft]);
  const thresholdError = nextThresholds.warningThreshold >= nextThresholds.criticalThreshold
    ? uiText(localeDraft, 'The red threshold must be greater than the yellow threshold.', '红色阈值必须大于黄色阈值。')
    : '';
  const promptItems = useMemo<PromptSettingItem[]>(() => [
    {
      key: 'codex_system_prompt',
      title: uiText(localeDraft, 'Codex System Prompt', 'Codex 系统提示词'),
      placeholder: uiText(localeDraft, 'Waiting for the first Codex base instructions...', '等待读取第一条 Codex 基础提示词...'),
      description: uiText(localeDraft, 'For GPT-5.6, the system prompt is carried by a developer item.', 'GPT-5.6 的系统提示词以 developer 节点传递。'),
    },
    {
      key: 'manual_local_compact_prompt',
      title: uiText(localeDraft, 'Manual Compact Prompt', '手动压缩词'),
      placeholder: uiText(localeDraft, 'Manual compact prompt', '手动压缩词'),
    },
    {
      key: 'auto_local_compact_prompt',
      title: uiText(localeDraft, 'Auto Compact Prompt', '自动压缩词'),
      placeholder: uiText(localeDraft, 'Auto compact prompt', '自动压缩词'),
    },
  ], [localeDraft]);
  const expandedPromptItem = expandedPromptKey
    ? promptItems.find((item) => item.key === expandedPromptKey) || null
    : null;
  const promptTokenLabels = useMemo<PromptDrafts>(() => ({
    codex_system_prompt: `${(countTokens(promptDrafts.codex_system_prompt) / 1000).toFixed(1)}k`,
    manual_local_compact_prompt: `${(countTokens(promptDrafts.manual_local_compact_prompt) / 1000).toFixed(1)}k`,
    auto_local_compact_prompt: `${(countTokens(promptDrafts.auto_local_compact_prompt) / 1000).toFixed(1)}k`,
  }), [promptDrafts]);

  function applyProviderSettings(response: ContextWorkbenchSettingsResponse) {
    const nextProviders = response.providers || [];
    const nextProviderMap = providerMapFromList(nextProviders);
    const nextProviderId = response.settings.context_workbench_provider_id
      || nextProviders[0]?.id
      || DEFAULT_CONTEXT_PROVIDER_ID;
    const nextProvider = nextProviderMap[nextProviderId] || nextProviders[0] || null;
    setProviders(nextProviders);
    setProviderDrafts(nextProviderMap);
    setProviderId(nextProviderId);
    setModelDraft(response.settings.context_workbench_model || preferredProviderModel(nextProvider) || DEFAULT_CONTEXT_WORKBENCH_MODEL);
  }

  function applyPromptSettings(settings: ContextWorkbenchSettingsResponse['settings']) {
    const nextDrafts = promptDraftsFromSettings(settings);
    setPromptDrafts(nextDrafts);
    setSavedPromptDrafts(nextDrafts);
    setPromptDefaults(promptDefaultsFromSettings(settings));
  }

  useEffect(() => setLocaleDraft(uiLocale), [uiLocale]);
  useEffect(() => setThemeDraft(themeMode), [themeMode]);

  useEffect(() => {
    if (!isModelOpen) return;
    function handleDocumentMouseDown(event: globalThis.MouseEvent) {
      const target = event.target;
      if (target instanceof Node && modelComboboxRef.current?.contains(target)) return;
      setIsModelOpen(false);
      setIsModelFilterActive(false);
    }
    document.addEventListener('mousedown', handleDocumentMouseDown);
    return () => document.removeEventListener('mousedown', handleDocumentMouseDown);
  }, [isModelOpen]);

  useEffect(() => {
    let cancelled = false;
    async function loadSettings() {
      setIsLoading(true);
      setError('');
      try {
        const response = await fetchContextWorkbenchSettings();
        if (cancelled) return;
        const settings = response.settings;
        applyProviderSettings(response);
        setReviewAutoEnabled(settings.context_review_auto_enabled !== false);
        setReviewIntervalDraft(String(settings.context_review_interval_minutes || 10));
        const loadedThresholds = normalizeContextTokenThresholds({
          warningThreshold: settings.context_token_warning_threshold,
          criticalThreshold: settings.context_token_critical_threshold,
        });
        setWarningThresholdDraft(String(loadedThresholds.warningThreshold));
        setCriticalThresholdDraft(String(loadedThresholds.criticalThreshold));
        onTokenThresholdsChange(loadedThresholds);
        const loadedLocale = settings.user_locale ? normalizeSupportedLocale(settings.user_locale) : uiLocale;
        setLocaleDraft(loadedLocale);
        onUiLocaleChange?.(loadedLocale);
        const loadedTheme = settings.theme_mode === 'dark' ? 'dark' : 'light';
        setThemeDraft(loadedTheme);
        onThemeModeChange?.(loadedTheme);
        const loadedFont = settings.ui_font || 'Noto Serif SC';
        const loadedFontSize = settings.ui_font_size || DEFAULT_UI_FONT_SIZE;
        setFontDraft(loadedFont);
        setFontSizeDraft(String(loadedFontSize));
        onUiFontChange?.(loadedFont, loadedFontSize);
        applyPromptSettings(settings);
      } catch (loadError) {
        if (!cancelled) setError(getThrownMessage(loadError));
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }
    void loadSettings();
    return () => { cancelled = true; };
  }, [onTokenThresholdsChange]);

  function updateProviderDraft(targetProviderId: string, patch: Partial<ContextWorkbenchProvider>) {
    setProviderDrafts((previous) => {
      const current = previous[targetProviderId] || providers.find((provider) => provider.id === targetProviderId);
      return current ? { ...previous, [targetProviderId]: { ...current, ...patch } } : previous;
    });
    setError('');
  }

  function setProviderSyncError(targetProviderId: string, errorText: string) {
    updateProviderDraft(targetProviderId, { last_sync_error: readableProviderError(errorText) });
  }

  async function saveSettings(updates?: {
    model?: string;
    providerId?: string;
    provider?: Partial<ContextWorkbenchProvider>;
    surfaceProviderError?: boolean;
    thresholds?: ContextTokenThresholds;
  }): Promise<boolean> {
    const nextModel = (updates?.model ?? modelDraft).trim();
    if (!nextModel) return false;
    const nextProviderId = (updates?.providerId ?? providerId).trim() || DEFAULT_CONTEXT_PROVIDER_ID;
    const baseProvider = providerDrafts[nextProviderId]
      || providers.find((provider) => provider.id === nextProviderId)
      || selectedProvider;
    const providerPatch = updates?.provider || {};
    const nextProvider = baseProvider ? { ...baseProvider, ...providerPatch } : null;
    const thresholds = updates?.thresholds ?? nextThresholds;
    if (!updates?.thresholds && thresholdError) {
      setError(thresholdError);
      return false;
    }
    setError('');
    onTokenThresholdsChange(thresholds);
    try {
      const providerPayload = nextProvider ? {
        id: nextProvider.id,
        name: nextProvider.name,
        provider_type: nextProvider.provider_type,
        enabled: nextProvider.enabled,
        api_base_url: nextProvider.api_base_url,
        default_model: nextModel,
        ...(nextProvider.id !== DEFAULT_CONTEXT_PROVIDER_ID ? { api_key: nextProvider.api_key } : {}),
      } : null;
      const shouldSaveThresholds = !updates?.model || Boolean(updates?.thresholds);
      const response = await saveContextWorkbenchSettingsRequest({
        context_workbench_provider_id: nextProviderId,
        context_workbench_model: nextModel,
        user_locale: localeDraft,
        ...(providerPayload ? { response_providers: [providerPayload] } : {}),
        ...(shouldSaveThresholds ? {
          context_token_warning_threshold: thresholds.warningThreshold,
          context_token_critical_threshold: thresholds.criticalThreshold,
        } : {}),
      });
      applyProviderSettings(response);
      return true;
    } catch (saveError) {
      const message = readableProviderError(getThrownMessage(saveError));
      if (updates?.surfaceProviderError) setProviderSyncError(nextProviderId, message);
      else setError(message);
      return false;
    }
  }

  function selectProvider(event: MouseEvent<HTMLDivElement>, provider: ContextWorkbenchProvider) {
    event.preventDefault();
    event.stopPropagation();
    const nextProviderId = provider.id.trim();
    if (!nextProviderId) return;
    const nextModel = preferredProviderModel(provider) || modelDraft;
    flushSync(() => {
      setProviderId(nextProviderId);
      setModelDraft(nextModel);
      setIsProviderOpen(false);
      setIsModelOpen(false);
      setIsModelFilterActive(false);
      setError('');
    });
    void saveSettings({ providerId: nextProviderId, model: nextModel });
  }

  function saveModelDraft(nextModel: string) {
    if (!nextModel) return;
    flushSync(() => {
      setModelDraft(nextModel);
      setIsModelOpen(false);
      setIsModelFilterActive(false);
      setError('');
    });
    void saveSettings({ model: nextModel });
  }

  function selectModel(event: MouseEvent<HTMLButtonElement>, model: { id?: string; label?: string }) {
    event.preventDefault();
    event.stopPropagation();
    saveModelDraft((model.id || model.label || '').trim());
  }

  function handleModelKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setIsModelOpen(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      setIsModelOpen(false);
      setIsModelFilterActive(false);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const firstModel = filteredModelOptions[0];
      if (isModelOpen && firstModel) saveModelDraft((firstModel.id || firstModel.label || '').trim());
      else {
        setIsModelOpen(false);
        setIsModelFilterActive(false);
        (event.target as HTMLInputElement).blur();
      }
    }
  }

  async function saveProviderUrl() {
    if (showExternalProviderFields) {
      await saveSettings({ provider: { api_base_url: selectedProvider?.api_base_url || '' } });
    }
  }

  async function saveProviderName() {
    if (showExternalProviderFields) {
      await saveSettings({ provider: { name: selectedProvider?.name.trim() || selectedProvider?.id || '' } });
    }
  }

  async function saveApiKey() {
    if (!showExternalProviderFields || !selectedProvider) return;
    setIsApiKeySaving(true);
    try {
      await saveSettings({ provider: { api_key: selectedProvider.api_key } });
    } finally {
      setIsApiKeySaving(false);
    }
  }

  async function refreshModels() {
    if (isModelsRefreshing || !selectedProvider) return;
    const isCodexProvider = selectedProvider.id === DEFAULT_CONTEXT_PROVIDER_ID;
    const configuredApiKey = selectedProvider.api_key.trim();
    if (!isCodexProvider && !selectedProvider.api_base_url.trim()) {
      setProviderSyncError(selectedProvider.id, uiText(localeDraft, 'Base URL is required before getting models.', '获取模型前需要填写 Base URL。'));
      return;
    }
    if (!isCodexProvider && !configuredApiKey) {
      setProviderSyncError(selectedProvider.id, uiText(localeDraft, 'API Key is required before getting models.', '获取模型前需要填写 API Key。'));
      return;
    }
    setIsModelsRefreshing(true);
    setError('');
    try {
      const didPersistConnection = await saveSettings({
        surfaceProviderError: true,
      });
      if (!didPersistConnection) return;
      const response = await refreshContextWorkbenchModelsRequest(selectedProvider.id);
      applyProviderSettings(response);
      const refreshedProvider = response.providers.find((provider) => provider.id === selectedProvider.id);
      const syncError = readableProviderError(refreshedProvider?.last_sync_error || '');
      setProviderSyncError(selectedProvider.id, syncError);
      setIsModelOpen(!syncError);
      setIsModelFilterActive(false);
    } catch (refreshError) {
      setProviderSyncError(selectedProvider.id, readableProviderError(getThrownMessage(refreshError)));
    } finally {
      setIsModelsRefreshing(false);
    }
  }

  async function saveReviewAutoEnabled(nextEnabled: boolean) {
    const previous = reviewAutoEnabled;
    setReviewAutoEnabled(nextEnabled);
    setIsReviewSettingsSaving(true);
    setError('');
    try {
      const response = await saveContextWorkbenchSettingsRequest({ context_review_auto_enabled: nextEnabled });
      setReviewAutoEnabled(response.settings.context_review_auto_enabled !== false);
      setReviewIntervalDraft(String(response.settings.context_review_interval_minutes || 10));
    } catch (saveError) {
      setReviewAutoEnabled(previous);
      setError(readableProviderError(getThrownMessage(saveError)));
    } finally {
      setIsReviewSettingsSaving(false);
    }
  }

  async function saveReviewInterval() {
    const parsed = Number.parseInt(reviewIntervalDraft, 10);
    if (!Number.isFinite(parsed) || parsed < 1 || parsed > 1440) {
      setError(uiText(localeDraft, 'Suggestion interval must be between 1 and 1440 minutes.', '建议触发间隔必须在 1 到 1440 分钟之间。'));
      return;
    }
    setError('');
    setIsReviewSettingsSaving(true);
    try {
      const response = await saveContextWorkbenchSettingsRequest({ context_review_interval_minutes: parsed });
      setReviewIntervalDraft(String(response.settings.context_review_interval_minutes || parsed));
    } catch (saveError) {
      setError(readableProviderError(getThrownMessage(saveError)));
    } finally {
      setIsReviewSettingsSaving(false);
    }
  }

  async function saveLocale(nextLocale: UiLocale) {
    if (nextLocale === localeDraft) return;
    const previous = localeDraft;
    setLocaleDraft(nextLocale);
    onUiLocaleChange?.(nextLocale);
    setError('');
    try {
      await saveContextWorkbenchSettingsRequest({ user_locale: nextLocale });
    } catch (saveError) {
      setLocaleDraft(previous);
      onUiLocaleChange?.(previous);
      setError(getThrownMessage(saveError));
    }
  }

  async function saveTheme(nextTheme: 'light' | 'dark') {
    if (nextTheme === themeDraft) return;
    const previous = themeDraft;
    setThemeDraft(nextTheme);
    onThemeModeChange?.(nextTheme);
    setError('');
    try {
      await saveContextWorkbenchSettingsRequest({ theme_mode: nextTheme });
    } catch (saveError) {
      setThemeDraft(previous);
      onThemeModeChange?.(previous);
      setError(getThrownMessage(saveError));
    }
  }

  function applyFontDraft(font: string, sizeValue = fontSizeDraft) {
    const size = Math.max(
      MIN_UI_FONT_SIZE,
      Math.min(MAX_UI_FONT_SIZE, Number.parseInt(sizeValue, 10) || DEFAULT_UI_FONT_SIZE),
    );
    onUiFontChange?.(font.trim(), size);
  }

  async function saveFont() {
    const font = fontDraft.trim();
    const size = Math.max(
      MIN_UI_FONT_SIZE,
      Math.min(MAX_UI_FONT_SIZE, Number.parseInt(fontSizeDraft, 10) || DEFAULT_UI_FONT_SIZE),
    );
    setFontSizeDraft(String(size));
    onUiFontChange?.(font, size);
    setError('');
    try {
      const response = await saveContextWorkbenchSettingsRequest({ ui_font: font, ui_font_size: size });
      const savedFont = response.settings.ui_font || font;
      const savedSize = response.settings.ui_font_size || size;
      setFontDraft(savedFont);
      setFontSizeDraft(String(savedSize));
      onUiFontChange?.(savedFont, savedSize);
    } catch (saveError) {
      setError(getThrownMessage(saveError));
    }
  }

  async function savePrompt(key: PromptSettingKey, nextValue = promptDrafts[key]) {
    if (nextValue === savedPromptDrafts[key]) return;
    setError('');
    try {
      const response = await saveContextWorkbenchSettingsRequest({ [key]: nextValue });
      const savedValue = response.settings[key] || '';
      setSavedPromptDrafts((previous) => ({ ...previous, [key]: savedValue }));
      setPromptDrafts((previous) => previous[key] === nextValue ? { ...previous, [key]: savedValue } : previous);
      setPromptDefaults(promptDefaultsFromSettings(response.settings));
      applyProviderSettings(response);
    } catch (saveError) {
      setError(getThrownMessage(saveError));
    }
  }

  function renderPromptEditor(item: PromptSettingItem, expanded = false) {
    const expandLabel = expanded
      ? uiText(localeDraft, 'Close editor', '关闭输入框')
      : uiText(localeDraft, 'Expand editor', '展开输入框');
    const resetLabel = uiText(localeDraft, 'Reset', '重置');
    const toggleButton = (
      <button
        aria-label={expandLabel}
        className="workbench-prompt-icon-btn"
        disabled={isLoading}
        title={expandLabel}
        type="button"
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          if (expanded) {
            setExpandedPromptKey(null);
            void savePrompt(item.key);
          } else {
            setExpandedPromptKey(item.key);
            setError('');
          }
        }}
      >
        <i className={`ph-light ${expanded ? 'ph-arrows-in-simple' : 'ph-arrows-out-simple'}`} />
      </button>
    );
    const resetButton = (
      <button
        className="workbench-prompt-reset-btn"
        disabled={isLoading || promptDrafts[item.key] === promptDefaults[item.key]}
        type="button"
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          const nextValue = promptDefaults[item.key] || '';
          setPromptDrafts((previous) => ({ ...previous, [item.key]: nextValue }));
          void savePrompt(item.key, nextValue);
        }}
      >
        {resetLabel}
      </button>
    );
    return (
      <div className={`workbench-prompt-editor${expanded ? ' is-expanded' : ''}`} key={item.key}>
        <div className="workbench-prompt-editor-header">
          <div className="workbench-prompt-editor-title">
            <span>{item.title}</span>
            <span className="workbench-prompt-token-count">
              {localeDraft.startsWith('zh') ? '：' : ': '}{promptTokenLabels[item.key]}
            </span>
          </div>
          <div className="workbench-prompt-editor-actions">
            {expanded ? resetButton : toggleButton}
            {expanded ? toggleButton : resetButton}
          </div>
        </div>
        {item.description ? <div className="workbench-prompt-editor-description">{item.description}</div> : null}
        <textarea
          className="settings-input workbench-prompt-textarea"
          disabled={isLoading}
          placeholder={item.placeholder}
          spellCheck={false}
          value={promptDrafts[item.key]}
          onBlur={() => void savePrompt(item.key)}
          onChange={(event) => {
            setPromptDrafts((previous) => ({ ...previous, [item.key]: event.target.value }));
            setError('');
          }}
        />
        {expanded && error ? <div className="workbench-setting-feedback error">{error}</div> : null}
      </div>
    );
  }

  return (
    <section className="extended-page" data-page="settings">
      <div className={`extended-page-scroll${expandedPromptItem ? ' has-expanded-prompt' : ''}`}>
        {expandedPromptItem ? renderPromptEditor(expandedPromptItem, true) : (
          <>
            <div className="workbench-panel-title">{uiText(localeDraft, 'Prompts', '提示词')}</div>
            <div className="workbench-prompts-panel">
              <div className="workbench-prompt-list">{promptItems.map((item) => renderPromptEditor(item))}</div>
            </div>

            <div className="workbench-panel-title workbench-settings-title">{uiText(localeDraft, 'Context Model', '上下文模型')}</div>
            <div className="workbench-settings-panel workbench-context-model-panel">
              <SettingsRow meta={providerGroup} title={uiText(localeDraft, 'Provider', '服务商')}>
                <div className="workbench-setting-control-row">
                  <Dropdown
                    align="right"
                    buttonClassName="tool-btn-capsule manual-workbench-reasoning workbench-provider-picker-trigger"
                    buttonChildren={<><i className="ph-light ph-cpu" /><span>{providerLabel}</span><i className="ph-light ph-caret-down" /></>}
                    disabled={isLoading || orderedProviders.length === 0}
                    isOpen={isProviderOpen}
                    onClose={() => setIsProviderOpen(false)}
                    onToggle={() => { setIsProviderOpen((previous) => !previous); setError(''); }}
                  >
                    {orderedProviders.map((provider) => (
                      <div className={`dropdown-item ${provider.id === providerId ? 'selected' : ''}`} key={provider.id} onMouseDown={(event) => selectProvider(event, provider)}>
                        <div className="dropdown-item-left"><span>{providerDisplayName(provider, localeDraft)}</span><small>{providerProtocolLabel(provider, localeDraft)}</small></div>
                        {provider.id === providerId ? <i className="ph-bold ph-check" /> : null}
                      </div>
                    ))}
                  </Dropdown>
                </div>
              </SettingsRow>

              {showExternalProviderFields ? (
                <>
                  <SettingsRow title={uiText(localeDraft, 'Connection name', '连接名称')}>
                    <input
                      className="settings-input settings-input-url"
                      disabled={isLoading || !selectedProvider}
                      placeholder={uiText(localeDraft, 'For example: DeepSeek', '例如：DeepSeek')}
                      type="text"
                      value={selectedProvider?.name || ''}
                      onBlur={() => void saveProviderName()}
                      onChange={(event) => selectedProvider && updateProviderDraft(selectedProvider.id, { name: event.target.value })}
                      onKeyDown={(event) => { if (event.key === 'Enter') (event.target as HTMLInputElement).blur(); }}
                    />
                  </SettingsRow>
                  <SettingsRow title="Base URL">
                    <div className="workbench-field-stack">
                      <input
                        className="settings-input settings-input-url"
                        disabled={isLoading || !selectedProvider}
                        placeholder={defaultBaseUrlForProviderType(providerType)}
                        type="text"
                        value={selectedProvider?.api_base_url || ''}
                        onBlur={() => void saveProviderUrl()}
                        onChange={(event) => selectedProvider && updateProviderDraft(selectedProvider.id, { api_base_url: event.target.value, last_sync_error: '' })}
                        onKeyDown={(event) => { if (event.key === 'Enter') (event.target as HTMLInputElement).blur(); }}
                      />
                      {providerSyncError && providerSyncErrorField === 'base_url' ? <div className="workbench-field-feedback error" role="alert">{providerSyncError}</div> : null}
                    </div>
                  </SettingsRow>
                  <SettingsRow title="API Key">
                    <div className="workbench-field-stack">
                      <input
                        className="settings-input settings-input-key"
                        disabled={isLoading || isApiKeySaving || !selectedProvider}
                        placeholder="sk-..."
                        type="password"
                        value={selectedProvider?.api_key || ''}
                        onBlur={() => void saveApiKey()}
                        onChange={(event) => {
                          if (selectedProvider) {
                            updateProviderDraft(selectedProvider.id, {
                              api_key: event.target.value,
                              last_sync_error: '',
                            });
                          }
                        }}
                        onKeyDown={(event) => { if (event.key === 'Enter') (event.target as HTMLInputElement).blur(); }}
                      />
                      {providerSyncError && providerSyncErrorField === 'api_key' ? <div className="workbench-field-feedback error" role="alert">{providerSyncError}</div> : null}
                    </div>
                  </SettingsRow>
                </>
              ) : null}

              <SettingsRow title={uiText(localeDraft, 'Model', '模型')}>
                <div className="workbench-field-stack">
                  <div className="workbench-model-config-row">
                    <div className={`workbench-model-combobox${isModelOpen ? ' is-open' : ''}`} ref={modelComboboxRef}>
                      <input
                        aria-autocomplete="list"
                        aria-controls="context-workbench-model-options"
                        aria-expanded={isModelOpen}
                        className="settings-input settings-input-model"
                        disabled={isLoading}
                        placeholder={preferredProviderModel(selectedProvider) || DEFAULT_CONTEXT_WORKBENCH_MODEL}
                        role="combobox"
                        type="text"
                        value={modelDraft}
                        onBlur={() => void saveSettings()}
                        onChange={(event) => { setModelDraft(event.target.value); setIsModelOpen(true); setIsModelFilterActive(true); setError(''); }}
                        onFocus={() => { setIsModelOpen(true); setIsModelFilterActive(false); setError(''); }}
                        onKeyDown={handleModelKeyDown}
                      />
                      <i className="ph-light ph-caret-down workbench-model-combobox-icon" />
                      <div className={`dropdown-menu workbench-model-suggestion-menu ${isModelOpen ? 'show' : ''}`} id="context-workbench-model-options" role="listbox" onMouseDown={(event) => event.stopPropagation()}>
                        {filteredModelOptions.length ? filteredModelOptions.map((model) => {
                            const modelId = (model.id || model.label || '').trim();
                            return (
                              <button aria-selected={modelId === modelDraft} className={`dropdown-item workbench-model-suggestion-option ${modelId === modelDraft ? 'selected' : ''}`} key={modelId} role="option" type="button" onMouseDown={(event) => selectModel(event, model)}>
                                <div className="dropdown-item-left">
                                  <span>{model.label || modelId}</span>
                                  {model.source === 'configured' ? <small>{uiText(localeDraft, 'Current configuration · not returned by provider', '当前配置 · 服务商未返回')}</small> : null}
                                </div>
                                {modelId === modelDraft ? <i className="ph-bold ph-check" /> : null}
                              </button>
                            );
                          }) : <div className="workbench-model-suggestion-empty">{uiText(localeDraft, 'No matching models', '没有匹配的模型')}</div>}
                      </div>
                    </div>
                    <button aria-label={uiText(localeDraft, 'Get all models', '获取所有模型')} className="workbench-icon-action" disabled={isLoading || isModelsRefreshing} title={uiText(localeDraft, 'Get all models', '获取所有模型')} type="button" onClick={() => void refreshModels()}>
                      <i className={`ph-light ph-circle-notch${isModelsRefreshing ? ' is-spinning' : ''}`} />
                    </button>
                  </div>
                  {providerSyncError && providerSyncErrorField === 'model' ? <div className="workbench-field-feedback error" role="alert">{providerSyncError}</div> : null}
                </div>
              </SettingsRow>

              <SettingsRow title={uiText(localeDraft, 'Automatic context suggestions', '自动生成上下文建议')} meta={uiText(localeDraft, 'Analyze eligible conversations after they stay idle.', '对符合条件且持续闲置的对话自动生成压缩建议。')}>
                <button aria-checked={reviewAutoEnabled} aria-label={uiText(localeDraft, 'Automatic context suggestions', '自动生成上下文建议')} className={`context-review-setting-switch${reviewAutoEnabled ? ' is-on' : ''}`} disabled={isLoading || isReviewSettingsSaving} role="switch" type="button" onClick={() => void saveReviewAutoEnabled(!reviewAutoEnabled)}><span /></button>
              </SettingsRow>
              {reviewAutoEnabled ? (
                <SettingsRow title={uiText(localeDraft, 'Suggestion trigger interval', '建议触发间隔')} meta={uiText(localeDraft, "Counted independently from each conversation's latest proxy request.", '从每个对话最后一次经过代理的请求开始独立计时。')}>
                  <div className="context-review-interval-control">
                    <input aria-label={uiText(localeDraft, 'Suggestion trigger interval in minutes', '建议触发间隔（分钟）')} className="settings-input settings-input-small" disabled={isLoading || isReviewSettingsSaving} inputMode="numeric" max={1440} min={1} type="number" value={reviewIntervalDraft} onBlur={() => void saveReviewInterval()} onChange={(event) => setReviewIntervalDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') (event.target as HTMLInputElement).blur(); }} />
                    <span>{uiText(localeDraft, 'minutes', '分钟')}</span>
                  </div>
                </SettingsRow>
              ) : null}
            </div>

            <div className="workbench-panel-title workbench-settings-title">{uiText(localeDraft, 'Workspace Settings', '工作区设置')}</div>
            <div className="workbench-settings-panel">
              <SettingsRow title={uiText(localeDraft, 'Language', '语言')}>
                <div className="workbench-language-toggle" role="group" aria-label={uiText(localeDraft, 'Language', '语言')}>
                  {UI_LANGUAGE_OPTIONS.map((option) => <button aria-pressed={localeDraft === option.value} className={`workbench-language-btn${localeDraft === option.value ? ' is-active' : ''}`} disabled={isLoading} key={option.value} type="button" onClick={() => void saveLocale(option.value)}>{uiLanguageLabel(option.value, localeDraft)}</button>)}
                </div>
              </SettingsRow>
              <SettingsRow title={uiText(localeDraft, 'Theme', '主题')}>
                <div className="workbench-language-toggle" role="group" aria-label={uiText(localeDraft, 'Theme', '主题')}>
                  {(['light', 'dark'] as const).map((option) => <button aria-pressed={themeDraft === option} className={`workbench-language-btn${themeDraft === option ? ' is-active' : ''}`} disabled={isLoading} key={option} type="button" onClick={() => void saveTheme(option)}>{option === 'light' ? uiText(localeDraft, 'Light', '浅色') : uiText(localeDraft, 'Dark', '深色')}</button>)}
                </div>
              </SettingsRow>
              <SettingsRow title={uiText(localeDraft, 'Token Color Thresholds', 'Token 颜色阈值')}>
                <div className="workbench-setting-control-row">
                  <label className="workbench-token-threshold-field"><span>黄</span><input className="settings-input settings-input-small" disabled={isLoading} min={0} step={100} type="number" value={warningThresholdDraft} onChange={(event) => { setWarningThresholdDraft(event.target.value); setError(''); }} onBlur={() => { if (!thresholdError) void saveSettings(); }} /></label>
                  <label className="workbench-token-threshold-field"><span>红</span><input className="settings-input settings-input-small" disabled={isLoading} min={1} step={100} type="number" value={criticalThresholdDraft} onChange={(event) => { setCriticalThresholdDraft(event.target.value); setError(''); }} onBlur={() => { if (!thresholdError) void saveSettings(); }} /></label>
                </div>
              </SettingsRow>
              <SettingsRow title={uiText(localeDraft, 'UI Font', '界面字体')}>
                <input className="settings-input settings-input-font" disabled={isLoading} placeholder="Noto Serif SC" type="text" value={fontDraft} onChange={(event) => { setFontDraft(event.target.value); applyFontDraft(event.target.value); }} onBlur={() => void saveFont()} onKeyDown={(event) => { if (event.key === 'Enter') (event.target as HTMLInputElement).blur(); }} />
              </SettingsRow>
              <SettingsRow title={uiText(localeDraft, 'Font Size', '字体大小')}>
                <input className="settings-input settings-input-small" disabled={isLoading} max={MAX_UI_FONT_SIZE} min={MIN_UI_FONT_SIZE} step={1} type="number" value={fontSizeDraft} onChange={(event) => { setFontSizeDraft(event.target.value); applyFontDraft(fontDraft, event.target.value); }} onBlur={() => void saveFont()} />
              </SettingsRow>
            </div>
            {error ? <div className="workbench-setting-feedback error">{error}</div> : null}
            {thresholdError ? <div className="workbench-setting-feedback error">{thresholdError}</div> : null}
          </>
        )}
      </div>
    </section>
  );
}
