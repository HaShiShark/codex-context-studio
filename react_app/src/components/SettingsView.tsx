import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, CSSProperties, KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react';
import type {
  OpenAISettings,
  ProviderType,
  ResponseProviderDraft,
  ResponseProviderModel,
  SettingsDraft,
  ToolSetting,
  ViewName,
} from '../types';
import ChatModelPicker from './ChatModelPicker';
import SettingsProvidersPanel from './SettingsProvidersPanel';
import './SettingsView.interface.css';
import { PAPER_INK_WHITE_THEME } from '../constants';
import hashIconUrl from '../assets/hash-icon.png';

type AppearanceMode = 'light' | 'dark' | 'system';
type ResolvedAppearanceMode = 'light' | 'dark';

const DEFAULT_UI_FONT_SIZE = 16;
const DEFAULT_CODE_FONT_SIZE = 14;

interface SettingsViewProps {
  availableModels: string[];
  availableCodeFonts: string[];
  availableUiFonts: string[];
  appearanceContrast: number;
  backgroundColor: string;
  codeFont: string;
  codeFontSize: number;
  fetchingProviderId: string;
  isSaving: boolean;
  openAISettings: OpenAISettings;
  resolvedThemeMode: ResolvedAppearanceMode;
  savingProviderId: string;
  settingsDraft: SettingsDraft;
  serviceHintsEnabled: boolean;
  themeColor: string;
  themeMode: AppearanceMode;
  themeOptions: { label: string; value: string }[];
  uiFont: string;
  uiFontSize: number;
  view: ViewName;
  onClearApiKey: () => void;
  onDraftChange: (patch: Partial<SettingsDraft>) => void;
  onProviderDraftChange: (providerId: string, patch: Partial<ResponseProviderDraft>) => void;
  onProviderAdd: (providerType: ProviderType, providerName?: string) => string;
  onProviderDelete: (providerId: string) => Promise<string>;
  onProviderLoadModels: (providerId: string) => Promise<ResponseProviderModel[]>;
  onProviderPersist: (
    providerId: string,
    options?: {
      activate?: boolean;
      clearApiKey?: boolean;
      silent?: boolean;
    },
  ) => void;
  onSaveOpenAISettings: () => void;
  onAppearanceContrastChange: (value: number) => void;
  onBackgroundColorChange: (value: string) => void;
  onCodeFontChange: (value: string) => void;
  onCodeFontSizeChange: (value: number) => void;
  onSwitchView: (view: ViewName) => void;
  onThemeChange: (value: string) => void;
  onThemeModeChange: (value: AppearanceMode) => void;
  onToggleServiceHints: (enabled: boolean) => void;
  onUiFontChange: (value: string) => void;
  onUiFontSizeChange: (value: number) => void;
}

type SettingsCategory = 'assistant' | 'me' | 'interface' | 'providers' | 'tools' | 'about';
type CompanionId = 'butter' | 'hanako' | 'ming';

interface InterfaceThemeCard {
  value: string;
  label: string;
  description: string;
  accent: string;
  surfaceClassName: string;
  availability: 'live' | 'preview';
}

const categories: Array<{ id: SettingsCategory; label: string; icon: string }> = [
  { id: 'assistant', label: '助手', icon: 'ph-user-circle' },
  { id: 'me', label: '我', icon: 'ph-user' },
  { id: 'interface', label: '外观', icon: 'ph-sun' },
  { id: 'providers', label: '供应商', icon: 'ph-pulse' },
  { id: 'tools', label: '工具', icon: 'ph-wrench' },
  { id: 'about', label: '关于', icon: 'ph-info' },
];

const companions: Array<{
  id: CompanionId;
  name: string;
  subtitle: string;
  summary: string;
  icon: string;
  color: string;
}> = [
  {
    id: 'butter',
    name: 'butter',
    subtitle: '更富有感情',
    summary: '会更主动接住情绪，适合陪你把想法慢慢理顺。',
    icon: 'ph-heart-straight',
    color: '#7ebc8f',
  },
  {
    id: 'hanako',
    name: 'hanako',
    subtitle: '均衡的助手',
    summary: '像一个可靠搭档，情绪、效率和判断力比较平衡。',
    icon: 'ph-star-four',
    color: '#6f8fa5',
  },
  {
    id: 'ming',
    name: 'ming',
    subtitle: '更理性冷静',
    summary: '更像一个安静的执行者，适合偏任务型的工作流。',
    icon: 'ph-moon',
    color: '#98a4b4',
  },
];

const languageOptions = [
  { value: 'zh-CN', label: '简体中文', keywords: '中文 Chinese China Mandarin zh-CN' },
  { value: 'en-US', label: 'English', keywords: '英文 English United States en-US' },
];

const timezoneOptions = [
  { value: 'Asia/Shanghai', label: 'Asia/Shanghai', keywords: '上海 北京 中国 China UTC+8' },
  { value: 'Asia/Tokyo', label: 'Asia/Tokyo', keywords: '东京 日本 Japan UTC+9' },
  { value: 'America/Los_Angeles', label: 'America/Los_Angeles', keywords: '洛杉矶 Los Angeles Pacific UTC-8 UTC-7' },
];

/* ---------- helper components ---------- */

interface ShowcaseSectionProps {
  title: string;
  subtitle?: string;
  children: ReactNode;
}

function ShowcaseSection({ title, subtitle, children }: ShowcaseSectionProps) {
  return (
    <section className="settings-showcase-section">
      <div className="settings-ornament-title">
        <span>{title}</span>
      </div>
      {subtitle && <p className="settings-section-subtitle">{subtitle}</p>}
      {children}
    </section>
  );
}

interface AssistantSettingRowProps {
  icon: string;
  label: string;
  hint?: string;
  children: ReactNode;
}

function AssistantSettingRow({ icon, label, hint, children }: AssistantSettingRowProps) {
  return (
    <div className="settings-assistant-setting-row">
      <div className="settings-assistant-setting-meta">
        <div className="settings-assistant-setting-icon">
          <i className={`ph-light ${icon}`} />
        </div>
        <div className="settings-assistant-setting-copy">
          <strong>{label}</strong>
          {hint ? <small>{hint}</small> : null}
        </div>
      </div>
      <div className="settings-assistant-setting-control">{children}</div>
    </div>
  );
}

interface ToggleButtonProps {
  checked: boolean;
  onClick: () => void;
}

function ToggleButton({ checked, onClick }: ToggleButtonProps) {
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

interface OptionalNumberControlProps {
  value: string;
  enabled: boolean;
  min?: number;
  max?: number;
  step?: number;
  onValueChange: (value: string) => void;
  onEnabledChange: (enabled: boolean) => void;
}

function OptionalNumberControl({
  value,
  enabled,
  min,
  max,
  step,
  onValueChange,
  onEnabledChange,
}: OptionalNumberControlProps) {
  return (
    <div className="settings-optional-number-control">
      <input
        className="settings-assistant-mini-input"
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={!enabled}
        onChange={(e) => onValueChange(e.target.value)}
      />
      <ToggleButton checked={enabled} onClick={() => onEnabledChange(!enabled)} />
    </div>
  );
}

interface SettingsComboboxOption {
  value: string;
  label: string;
  keywords?: string;
}

interface SettingsComboboxProps {
  value: string;
  options: SettingsComboboxOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
  allowCustom?: boolean;
  placeholder?: string;
}

function normalizeSearchText(value: string) {
  return value.trim().toLowerCase();
}

function SettingsCombobox({
  value,
  options,
  onChange,
  ariaLabel,
  className = '',
  allowCustom = false,
  placeholder,
}: SettingsComboboxProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const selectedOption = options.find((option) => option.value === value);
  const displayValue = selectedOption?.label || value;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(displayValue);
  const normalizedQuery = normalizeSearchText(query);
  const shouldShowAllOptions =
    !normalizedQuery ||
    (selectedOption &&
      (normalizedQuery === normalizeSearchText(selectedOption.label) ||
        normalizedQuery === normalizeSearchText(selectedOption.value))) ||
    (!selectedOption && normalizedQuery === normalizeSearchText(value));
  const filteredOptions = shouldShowAllOptions
    ? options
    : options.filter((option) =>
        normalizeSearchText(`${option.label} ${option.value} ${option.keywords || ''}`).includes(normalizedQuery),
      );

  useEffect(() => {
    if (!open) {
      setQuery(displayValue);
    }
  }, [displayValue, open]);

  useEffect(() => {
    if (!open) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }

    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  const commitOption = (option: SettingsComboboxOption) => {
    onChange(option.value);
    setQuery(option.label);
    setOpen(false);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
      setQuery(displayValue);
      return;
    }

    if (event.key === 'Enter' && filteredOptions.length) {
      event.preventDefault();
      commitOption(filteredOptions[0]);
    }
  };

  return (
    <div ref={containerRef} className={`settings-combobox ${className}${open ? ' is-open' : ''}`}>
      <input
        className="settings-combobox-input"
        aria-label={ariaLabel}
        role="combobox"
        aria-expanded={open}
        value={query}
        placeholder={placeholder}
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        onChange={(event) => {
          const nextValue = event.target.value;
          setQuery(nextValue);
          setOpen(true);
          if (allowCustom) {
            onChange(nextValue);
          }
        }}
        onKeyDown={handleKeyDown}
      />
      <button
        type="button"
        className="settings-combobox-caret"
        aria-label={`展开${ariaLabel}`}
        onClick={() => setOpen((previous) => !previous)}
      >
        <i className="ph-light ph-caret-down" />
      </button>

      {open ? (
        <div className="settings-combobox-menu" role="listbox">
          {filteredOptions.length ? (
            filteredOptions.map((option) => {
              const selected = option.value === value;
              return (
                <button
                  key={option.value}
                  type="button"
                  className={`settings-combobox-option${selected ? ' is-selected' : ''}`}
                  onClick={() => commitOption(option)}
                  role="option"
                  aria-selected={selected}
                >
                  <span>{option.label}</span>
                  {selected ? <i className="ph-bold ph-check" /> : null}
                </button>
              );
            })
          ) : (
            <div className="settings-combobox-empty">
              {allowCustom ? '继续输入即可保留自定义值' : '没有匹配项'}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

/* ---------- main component ---------- */

export default function SettingsView({
  availableModels,
  availableCodeFonts,
  availableUiFonts,
  appearanceContrast,
  backgroundColor,
  codeFont,
  codeFontSize,
  fetchingProviderId,
  isSaving,
  openAISettings,
  resolvedThemeMode,
  savingProviderId,
  settingsDraft,
  themeColor,
  themeMode,
  themeOptions,
  uiFont,
  uiFontSize,
  view,
  onClearApiKey,
  onDraftChange,
  onProviderDraftChange,
  onProviderAdd,
  onProviderDelete,
  onProviderLoadModels,
  onProviderPersist,
  onSaveOpenAISettings,
  onAppearanceContrastChange,
  onBackgroundColorChange,
  onCodeFontChange,
  onCodeFontSizeChange,
  onSwitchView,
  onThemeChange,
  onThemeModeChange,
  onUiFontChange,
  onUiFontSizeChange,
}: SettingsViewProps) {
  const [activeCategory, setActiveCategory] = useState<SettingsCategory>('assistant');
  const [selectedCompanion, setSelectedCompanion] = useState<CompanionId>('hanako');
  const assistantDraft = {
    name: settingsDraft.assistant_name,
    greeting: settingsDraft.assistant_greeting,
    persona: settingsDraft.assistant_prompt,
    temperatureEnabled: settingsDraft.temperature_enabled,
    temperature: settingsDraft.temperature,
    topPEnabled: settingsDraft.top_p_enabled,
    topP: settingsDraft.top_p,
    contextMessagesEnabled: settingsDraft.context_message_limit_enabled,
    contextMessages: settingsDraft.context_message_limit,
    streaming: settingsDraft.streaming,
  };
  const meDraft = {
    name: settingsDraft.user_name,
    profile: settingsDraft.user_profile,
    locale: settingsDraft.user_locale,
    timezone: settingsDraft.user_timezone,
  };
  const [interfacePreview, setInterfacePreview] = useState({
    motion: true,
    serifTitle: true,
    density: 'balanced',
    navStyle: 'floating',
    themeMode: 'dark',
    contrast: 45,
    uiFontSize: 16,
    codeFontSize: 14,
  });
  const appearanceImportInputRef = useRef<HTMLInputElement | null>(null);
  const [appearanceNotice, setAppearanceNotice] = useState('');
  const [settingsModelPickerOpen, setSettingsModelPickerOpen] = useState(false);

  const modelOptions = Array.from(new Set([settingsDraft.default_model, ...availableModels].filter(Boolean)));
  const activeResponseProvider =
    settingsDraft.response_providers.find((provider) => provider.id === settingsDraft.active_provider_id) ||
    settingsDraft.response_providers[0];
  const activeResponseModel = activeResponseProvider?.models.find((model) => {
    const modelId = (model.id || model.label || '').trim();
    return modelId === settingsDraft.default_model;
  });
  const settingsModelLabel = activeResponseModel?.label || settingsDraft.default_model || modelOptions[0] || '选择模型';
  const timezoneComboboxOptions = useMemo(() => {
    const intlWithTimezones = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] };
    const supportedTimezones = intlWithTimezones.supportedValuesOf?.('timeZone') || [];
    const values = Array.from(
      new Set([meDraft.timezone, ...timezoneOptions.map((option) => option.value), ...supportedTimezones].filter(Boolean)),
    );

    return values.map((value) => {
      const preset = timezoneOptions.find((option) => option.value === value);
      return {
        value,
        label: preset?.label || value,
        keywords: `${preset?.keywords || ''} ${value.replace(/[_/]/g, ' ')}`,
      };
    });
  }, [meDraft.timezone]);
  const assistantAvatarText = assistantDraft.name.trim().slice(0, 1).toUpperCase() || 'A';
  const meAvatarText = meDraft.name.trim().slice(0, 1) || '我';

  const interfaceThemeCards: InterfaceThemeCard[] = [
    ...themeOptions.map((option) => {
      const isPaperInk = option.value === PAPER_INK_WHITE_THEME;

      return {
        value: option.value,
        label: option.label,
        description: isPaperInk
          ? '只把默认强调换成白色，整体还是现在这套暗色界面。'
          : '沿用当前主题配置，直接切换整体界面氛围。',
        accent: isPaperInk ? '#ffffff' : option.value,
        surfaceClassName: isPaperInk
          ? 'settings-interface-theme-surface is-paper-ink'
          : 'settings-interface-theme-surface is-live',
        availability: 'live' as const,
      };
    }),
  ];

  const isPaperInkSelected = themeColor === PAPER_INK_WHITE_THEME;
  const appearanceAccentColor = isPaperInkSelected ? '#ffffff' : themeColor;
  const appearanceThemeOptions = interfaceThemeCards.some((themeCard) => themeCard.value === themeColor)
    ? interfaceThemeCards
    : [
        {
          value: themeColor,
          label: '自定义主题',
          description: '从颜色选择器临时选出的强调色。',
          accent: appearanceAccentColor,
          surfaceClassName: 'settings-interface-theme-surface is-live',
          availability: 'live' as const,
        },
        ...interfaceThemeCards,
      ];
  const appearanceUiFontOptions = availableUiFonts.includes(uiFont) ? availableUiFonts : [uiFont, ...availableUiFonts];
  const appearanceCodeFontOptions = availableCodeFonts.includes(codeFont) ? availableCodeFonts : [codeFont, ...availableCodeFonts];
  const selectedAppearanceTheme = appearanceThemeOptions.find((themeCard) => themeCard.value === themeColor);
  const resolvedAppearanceMode = resolvedThemeMode;
  const appearanceSurfaceColor = backgroundColor;
  const appearanceForegroundColor = resolvedAppearanceMode === 'light' ? '#1F1F1F' : '#FFFFFF';
  const appearanceShellStyle = {
    '--appearance-color': appearanceAccentColor,
    '--appearance-surface': appearanceSurfaceColor,
    '--appearance-foreground': appearanceForegroundColor,
    '--appearance-ui-font-size': `${uiFontSize}px`,
    '--appearance-code-font-size': `${codeFontSize}px`,
    '--appearance-contrast-filter': `${Math.max(85, Math.min(140, appearanceContrast + 55))}%`,
  } as CSSProperties;

  const handleSettingsModelSelect = (providerId: string, model: ResponseProviderModel) => {
    const modelId = (model.id || model.label || '').trim();
    if (!modelId) {
      return;
    }

    onDraftChange({
      active_provider_id: providerId,
      default_model: modelId,
      response_providers: settingsDraft.response_providers.map((provider) =>
        provider.id === providerId ? { ...provider, default_model: modelId } : provider,
      ),
    });
    setSettingsModelPickerOpen(false);
  };

  const renderSettingsModelButton = (className = 'settings-assistant-model-button') => (
    <button
      type="button"
      className={className}
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => {
        if (event.button !== 0) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        setSettingsModelPickerOpen(true);
      }}
      aria-label="选择聊天模型"
    >
      <span>{settingsModelLabel}</span>
      <i className="ph-light ph-caret-down" />
    </button>
  );

  const clampAppearanceNumber = (value: number, min: number, max: number, fallback: number) => {
    if (!Number.isFinite(value)) return fallback;
    return Math.min(max, Math.max(min, value));
  };

  const isAppearanceColor = (value: unknown): value is string =>
    typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value);

  const handleAppearanceImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;

    try {
      const raw = (await file.text()).trim();
      const payload = raw.startsWith('{') ? JSON.parse(raw) : { accent: raw };
      const nextPreview: Partial<typeof interfacePreview> = {};
      const importedAccent = typeof payload.accent === 'string' ? payload.accent : payload.themeColor;

      if (isAppearanceColor(importedAccent) || importedAccent === PAPER_INK_WHITE_THEME) {
        onThemeChange(importedAccent);
      }

      if (isAppearanceColor(payload.background)) {
        onBackgroundColorChange(payload.background);
      }

      if (typeof payload.uiFont === 'string') {
        onUiFontChange(payload.uiFont);
      }

      if (typeof payload.codeFont === 'string') {
        onCodeFontChange(payload.codeFont);
      }

      if (['light', 'dark', 'system'].includes(payload.themeMode)) {
        onThemeModeChange(payload.themeMode as AppearanceMode);
        nextPreview.themeMode = payload.themeMode;
      }

      if (typeof payload.contrast === 'number') {
        const nextContrast = clampAppearanceNumber(payload.contrast, 30, 80, appearanceContrast);
        onAppearanceContrastChange(nextContrast);
        nextPreview.contrast = nextContrast;
      }

      if (typeof payload.uiFontSize === 'number') {
        const nextUiFontSize = clampAppearanceNumber(payload.uiFontSize, 12, 22, uiFontSize);
        onUiFontSizeChange(nextUiFontSize);
        nextPreview.uiFontSize = nextUiFontSize;
      }

      if (typeof payload.codeFontSize === 'number') {
        const nextCodeFontSize = clampAppearanceNumber(payload.codeFontSize, 11, 20, codeFontSize);
        onCodeFontSizeChange(nextCodeFontSize);
        nextPreview.codeFontSize = nextCodeFontSize;
      }

      if (Object.keys(nextPreview).length) {
        setInterfacePreview((prev) => ({ ...prev, ...nextPreview }));
      }

      setAppearanceNotice('已导入主题配置');
    } catch {
      setAppearanceNotice('导入失败，只支持 JSON 或 #RRGGBB');
    } finally {
      event.currentTarget.value = '';
    }
  };

  const handleAppearanceCopy = async () => {
    const payload = {
      theme: selectedAppearanceTheme?.label || '自定义主题',
      accent: appearanceAccentColor,
      background: backgroundColor,
      uiFont,
      codeFont,
      themeMode,
      contrast: appearanceContrast,
      uiFontSize,
      codeFontSize,
    };

    try {
      await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
      setAppearanceNotice('主题配置已复制');
    } catch {
      setAppearanceNotice('复制失败，当前环境没有开放剪贴板权限');
    }
  };

  /* ---- assistant ---- */
  const renderAssistant = () => (
    <div className="settings-assistant-shell">
      <section className="settings-assistant-card settings-assistant-identity-card">
        <div className="settings-assistant-avatar">{assistantAvatarText}</div>
        <div className="settings-assistant-identity-main">
          <div className="settings-assistant-card-head compact">
            <div>
              <h3>助手名称</h3>
              <p>这一页直接把基础设置和提示词放在一起，不再做分页。</p>
            </div>
          </div>

          <input
            className="settings-assistant-name-input"
            value={assistantDraft.name}
            onChange={(e) => onDraftChange({ assistant_name: e.target.value })}
            placeholder="给助手起个名字"
          />

          <div className="settings-assistant-preset-row">
            {companions.map((companion) => (
              <button
                key={companion.id}
                type="button"
                className={`settings-assistant-preset-chip${selectedCompanion === companion.id ? ' active' : ''}`}
                style={{ '--preset-accent': companion.color } as CSSProperties}
                onClick={() => setSelectedCompanion(companion.id)}
              >
                <i className={`ph-fill ${companion.icon}`} />
                <span>{companion.name}</span>
              </button>
            ))}
          </div>
        </div>
      </section>

      <section className="settings-assistant-card">
        <div className="settings-assistant-model-row">
          <div className="settings-assistant-model-copy">
            <strong>默认聊天模型</strong>
            <span>未单独设置时，就按这里作为助手默认模型。</span>
          </div>
          {renderSettingsModelButton()}
        </div>
      </section>

      <section className="settings-assistant-card">
        <div className="settings-assistant-card-head">
          <div>
            <h3>生成设置</h3>
            <p>这些参数会真实进入模型请求；推理强度仍然由输入框控制。</p>
          </div>
        </div>

        <div className="settings-assistant-settings-list">
          <AssistantSettingRow icon="ph-thermometer" label="Temperature" hint="控制输出随机性，关闭时使用模型默认值">
            <OptionalNumberControl
              value={assistantDraft.temperature}
              enabled={assistantDraft.temperatureEnabled}
              min={0}
              max={2}
              step={0.1}
              onValueChange={(value) => onDraftChange({ temperature: value })}
              onEnabledChange={(enabled) => onDraftChange({ temperature_enabled: enabled })}
            />
          </AssistantSettingRow>

          <AssistantSettingRow icon="ph-funnel" label="Top P" hint="控制候选词采样范围，关闭时使用模型默认值">
            <OptionalNumberControl
              value={assistantDraft.topP}
              enabled={assistantDraft.topPEnabled}
              min={0}
              max={1}
              step={0.05}
              onValueChange={(value) => onDraftChange({ top_p: value })}
              onEnabledChange={(enabled) => onDraftChange({ top_p_enabled: enabled })}
            />
          </AssistantSettingRow>

          <AssistantSettingRow icon="ph-stack" label="上下文消息数量" hint="只限制发给模型的历史消息，不影响侧边栏和聊天记录显示">
            <OptionalNumberControl
              value={assistantDraft.contextMessages}
              enabled={assistantDraft.contextMessagesEnabled}
              min={1}
              step={1}
              onValueChange={(value) => onDraftChange({ context_message_limit: value })}
              onEnabledChange={(enabled) => onDraftChange({ context_message_limit_enabled: enabled })}
            />
          </AssistantSettingRow>


          <AssistantSettingRow icon="ph-waveform" label="流式输出" hint="关闭后等待完整回答再显示">
            <ToggleButton checked={assistantDraft.streaming} onClick={() => onDraftChange({ streaming: !assistantDraft.streaming })} />
          </AssistantSettingRow>
        </div>
      </section>

      <section className="settings-assistant-card">
        <div className="settings-assistant-card-head">
          <div>
            <h3>提示词</h3>
          </div>
        </div>

        <div className="settings-assistant-prompt-stack">
          <label className="settings-assistant-prompt-block">
            <textarea
              className="settings-textarea settings-assistant-textarea is-large"
              rows={8}
              value={assistantDraft.persona}
              onChange={(e) => onDraftChange({ assistant_prompt: e.target.value })}
              placeholder="写清楚助手的语气、角色、做事方式和边界。"
            />
          </label>
        </div>
      </section>
    </div>
  );

  const renderMeMinimal = () => (
    <div className="settings-assistant-shell settings-me-shell">
      <section className="settings-assistant-card settings-assistant-identity-card settings-me-identity-card">
        <div className="settings-me-avatar">
          <span>{meAvatarText}</span>
        </div>
        <div className="settings-assistant-identity-main">
          <input
            className="settings-assistant-name-input"
            value={meDraft.name}
            onChange={(e) => onDraftChange({ user_name: e.target.value })}
            placeholder="你的称呼"
          />
        </div>
      </section>

      <section className="settings-assistant-card">
        <div className="settings-assistant-settings-list">
          <AssistantSettingRow icon="ph-user" label="名字" hint="聊天和个性化里显示的名字">
            <input
              className="settings-assistant-mini-input"
              value={meDraft.name}
              onChange={(e) => onDraftChange({ user_name: e.target.value })}
            />
          </AssistantSettingRow>

          <AssistantSettingRow icon="ph-translate" label="语言" hint="应用界面的显示语言">
            <SettingsCombobox
              className="settings-assistant-mini-input"
              value={meDraft.locale}
              options={languageOptions}
              ariaLabel="语言"
              placeholder="输入语言"
              onChange={(value) => onDraftChange({ user_locale: value })}
            />
          </AssistantSettingRow>

          <AssistantSettingRow icon="ph-globe-hemisphere-east" label="时区" hint="用于时间相关的默认上下文">
            <SettingsCombobox
              className="settings-assistant-mini-input"
              value={meDraft.timezone}
              options={timezoneComboboxOptions}
              allowCustom
              ariaLabel="时区"
              placeholder="输入时区"
              onChange={(value) => onDraftChange({ user_timezone: value })}
            />
          </AssistantSettingRow>
        </div>
      </section>

      <section className="settings-assistant-card">
        <div className="settings-assistant-prompt-stack">
          <label className="settings-assistant-prompt-block">
            <span>关于你</span>
            <textarea
              className="settings-textarea settings-assistant-textarea is-large"
              rows={7}
              value={meDraft.profile}
              onChange={(e) => onDraftChange({ user_profile: e.target.value })}
              placeholder="写一些你的性格、偏好和说话方式，方便助手更贴着你回答。"
            />
          </label>
        </div>
      </section>
    </div>
  );

  const renderInterfaceRedesign = () => (
    <div className={`settings-appearance-shell mode-${resolvedAppearanceMode}`} style={appearanceShellStyle}>
      <h1>外观</h1>

      <section className="settings-appearance-panel compact">
        <div className="appearance-row stacked">
          <div>
            <span>语言</span>
            <small>应用界面的显示语言</small>
          </div>
          <SettingsCombobox
            className="appearance-combobox appearance-select-pill"
            value={meDraft.locale}
            options={languageOptions}
            ariaLabel="语言"
            placeholder="输入语言"
            onChange={(value) => onDraftChange({ user_locale: value })}
          />
        </div>
      </section>

      <section className="settings-appearance-panel">
        <div className="appearance-panel-head">
          <div>
            <strong>主题</strong>
            <span>使用浅色、深色，或匹配系统设置</span>
          </div>

          <div className="appearance-mode-switch" role="group" aria-label="主题模式">
            {[
              { value: 'light', label: '浅色', icon: 'ph-sun' },
              { value: 'dark', label: '深色', icon: 'ph-moon' },
              { value: 'system', label: '系统', icon: 'ph-monitor' },
            ].map((option) => (
              <button
                key={option.value}
                type="button"
                className={themeMode === option.value ? 'active' : ''}
                onClick={() => onThemeModeChange(option.value as AppearanceMode)}
                aria-pressed={themeMode === option.value}
              >
                <i className={`ph-light ${option.icon}`} />
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="appearance-row is-toolbar">
          <span>深色主题</span>
          <div className="appearance-toolbar-actions">
            <button type="button" onClick={() => appearanceImportInputRef.current?.click()}>导入</button>
            <input
              ref={appearanceImportInputRef}
              className="appearance-import-input"
              type="file"
              accept=".json,text/plain"
              onChange={handleAppearanceImport}
            />
            <button type="button" onClick={handleAppearanceCopy}>复制主题</button>
            <SettingsCombobox
              className="appearance-combobox appearance-theme-combobox"
              value={themeColor}
              options={appearanceThemeOptions.map((theme) => ({
                value: theme.value,
                label: theme.label,
                keywords: theme.description,
              }))}
              ariaLabel="选择深色主题"
              onChange={onThemeChange}
            />
          </div>
        </div>

        {appearanceNotice ? <div className="appearance-notice">{appearanceNotice}</div> : null}

        <div className="appearance-row">
          <span>强调色</span>
          <label className="appearance-color-pill">
            <input
              type="color"
              value={appearanceAccentColor}
              aria-label="强调色"
              onChange={(event) => onThemeChange(event.target.value)}
            />
            <span>{appearanceAccentColor.toUpperCase()}</span>
          </label>
        </div>

        <div className="appearance-row">
          <span>背景</span>
          <label
            className="appearance-color-pill"
            style={{ '--appearance-pill-color': appearanceSurfaceColor } as CSSProperties}
          >
            <input
              type="color"
              value={appearanceSurfaceColor}
              aria-label="背景"
              onChange={(event) => onBackgroundColorChange(event.target.value)}
            />
            {appearanceSurfaceColor}
          </label>
        </div>

        <div className="appearance-row">
          <span>UI 字体</span>
          <SettingsCombobox
            className="appearance-combobox appearance-select-pill is-ui-font"
            value={uiFont}
            options={appearanceUiFontOptions.map((fontName) => ({ value: fontName, label: fontName }))}
            allowCustom
            ariaLabel="UI 字体"
            onChange={onUiFontChange}
          />
        </div>

        <div className="appearance-row">
          <span>代码字体</span>
          <SettingsCombobox
            className="appearance-combobox appearance-select-pill is-code-font"
            value={codeFont}
            options={appearanceCodeFontOptions.map((fontName) => ({ value: fontName, label: fontName }))}
            allowCustom
            ariaLabel="代码字体"
            onChange={onCodeFontChange}
          />
        </div>

        <div className="appearance-row">
          <span>对比度</span>
          <div className="appearance-range-control">
            <input
              type="range"
              min="30"
              max="80"
              value={appearanceContrast}
              aria-label="对比度"
              onChange={(event) => onAppearanceContrastChange(Number(event.target.value))}
            />
            <strong>{appearanceContrast}</strong>
          </div>
        </div>
      </section>

      <section className="settings-appearance-panel compact">
        <div className="appearance-row stacked">
          <div>
            <span>UI 字号</span>
            <small>调整 Codex UI 使用的基准字号</small>
          </div>
          <label className="appearance-number-field">
            <input
              type="number"
              min="12"
              max="22"
              value={uiFontSize}
              aria-label="UI 字号"
              onChange={(event) =>
                onUiFontSizeChange(clampAppearanceNumber(Number(event.target.value), 12, 22, DEFAULT_UI_FONT_SIZE))
              }
            />
            <span>px</span>
          </label>
        </div>

        <div className="appearance-row stacked">
          <div>
            <span>代码字体大小</span>
            <small>调整聊天和差异视图中代码使用的基础字号</small>
          </div>
          <label className="appearance-number-field">
            <input
              type="number"
              min="11"
              max="20"
              value={codeFontSize}
              aria-label="代码字体大小"
              onChange={(event) =>
                onCodeFontSizeChange(clampAppearanceNumber(Number(event.target.value), 11, 20, DEFAULT_CODE_FONT_SIZE))
              }
            />
            <span>px</span>
          </label>
        </div>
      </section>
    </div>
  );

  /* ---- about ---- */
  const handleToolToggle = (toolName: string) => {
    const nextTools = settingsDraft.tool_settings.map((tool): ToolSetting => (
      tool.name === toolName ? { ...tool, enabled: !tool.enabled } : tool
    ));
    onDraftChange({ tool_settings: nextTools });
  };

  const renderTools = () => {
    const enabledCount = settingsDraft.tool_settings.filter((tool) => tool.enabled).length;

    return (
      <div className="settings-tools-shell">
        <div className="settings-page-head settings-tools-head">
          <div>
            <h1>工具</h1>
            <p>这里控制会发给模型的工具列表。保存后，OpenAI Responses、Chat Completions、Claude 和 Gemini 都会使用同一份开关。</p>
          </div>
          <div className="settings-tools-counter">
            <strong>{enabledCount}</strong>
            <span>已开启</span>
          </div>
        </div>

        <div className="settings-tools-grid">
          {settingsDraft.tool_settings.map((tool) => (
            <section key={tool.name} className={`settings-tool-card${tool.enabled ? ' is-on' : ''}`}>
              <div className="settings-tool-card-main">
                <div className="settings-tool-icon">
                  <i className="ph-light ph-wrench" />
                </div>
                <div className="settings-tool-copy">
                  <div className="settings-tool-title-row">
                    <strong>{tool.name}</strong>
                  </div>
                  <p>{tool.description}</p>
                </div>
              </div>
              <ToggleButton checked={tool.enabled} onClick={() => handleToolToggle(tool.name)} />
            </section>
          ))}
        </div>

        <div className="settings-tools-footer">
          <button type="button" className="settings-primary-btn" disabled={isSaving} onClick={onSaveOpenAISettings}>
            {isSaving ? '保存中...' : '保存工具设置'}
          </button>
        </div>
      </div>
    );
  };

  const renderAbout = () => (
    <>
      <ShowcaseSection title="关于">
        <div className="settings-about-hero">
          <div className="settings-about-icon">
            <img src={hashIconUrl} alt="hashcode" />
          </div>
          <h2>hashcode</h2>
          <p>真正的上下文工程</p>
          <span className="settings-about-version">v0.1.0 Preview</span>
        </div>
      </ShowcaseSection>

      <ShowcaseSection title="信息">
        <div className="settings-paper-card settings-info-card">
          <div className="settings-info-row">
            <span>GitHub</span>
            <a href="https://github.com/HaShiShark/context-editor-agent" target="_blank" rel="noreferrer">github.com/HaShiShark/context-editor-agent</a>
          </div>
          <div className="settings-info-row">
            <span>加入我们</span>
            <a href="mailto:3455744878@qq.com">3455744878@qq.com</a>
          </div>
        </div>
      </ShowcaseSection>
    </>
  );

  /* ---- content switch ---- */
  const renderContent = () => {
    switch (activeCategory) {
      case 'assistant':
        return renderAssistant();
      case 'me':
        return renderMeMinimal();
      case 'interface':
        return renderInterfaceRedesign();
      case 'providers':
        return (
          <SettingsProvidersPanel
            fetchingProviderId={fetchingProviderId}
            openAISettings={openAISettings}
            savingProviderId={savingProviderId}
            settingsDraft={settingsDraft}
            onClearApiKey={onClearApiKey}
            onProviderDraftChange={onProviderDraftChange}
            onProviderAdd={onProviderAdd}
            onProviderDelete={onProviderDelete}
            onProviderLoadModels={onProviderLoadModels}
            onProviderPersist={onProviderPersist}
          />
        );
      case 'tools':
        return renderTools();
      case 'about':
        return renderAbout();
      default:
        return null;
    }
  };

  /* ---- layout ---- */
  return (
    <div
      className={`settings-page ${view === 'settings' ? 'active' : ''}${activeCategory === 'providers' ? ' is-providers' : ''}${activeCategory === 'assistant' ? ' is-assistant' : ''}${activeCategory === 'me' ? ' is-me' : ''}${activeCategory === 'interface' ? ' is-interface' : ''}${activeCategory === 'tools' ? ' is-tools' : ''}${activeCategory === 'about' ? ' is-about' : ''}`}
    >
      <aside className="settings-sidebar">
        <div className="settings-sidebar-header">
          <h2>设置</h2>
          <p>配置你的助手和工作环境。</p>
        </div>

        <nav className="settings-nav">
          {categories.map((cat) => (
            <button
              key={cat.id}
              type="button"
              className={`settings-nav-item ${activeCategory === cat.id ? 'active' : ''}`}
              onClick={() => setActiveCategory(cat.id)}
            >
              <i className={`ph-light ${cat.icon}`} />
              <span>{cat.label}</span>
            </button>
          ))}
        </nav>

        <div className="settings-sidebar-footer">
          <button type="button" className="back-btn" onClick={() => onSwitchView('chat')}>
            <i className="ph-light ph-arrow-left" />
            返回聊天
          </button>
        </div>
      </aside>

      <main className="settings-content">
        <div className="settings-content-scroll">
          <div key={activeCategory} className="settings-tab-panel">
            {renderContent()}
          </div>
        </div>
      </main>

      <ChatModelPicker
        activeProviderId={settingsDraft.active_provider_id}
        currentModel={settingsDraft.default_model}
        open={settingsModelPickerOpen}
        providers={settingsDraft.response_providers}
        title="选择聊天模型"
        description="这里使用和主聊天框一样的模型选择器，切换后会更新设置里的默认聊天模型。"
        selectedContextLabel="设置默认"
        onClose={() => setSettingsModelPickerOpen(false)}
        onSelectModel={handleSettingsModelSelect}
      />
    </div>
  );
}
