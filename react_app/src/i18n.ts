export type UiLocale = 'zh-CN' | 'en-US';

const SUPPORTED_LOCALES = new Set<UiLocale>(['zh-CN', 'en-US']);

export function normalizeSupportedLocale(locale: unknown): UiLocale {
  return typeof locale === 'string' && SUPPORTED_LOCALES.has(locale as UiLocale) ? (locale as UiLocale) : 'en-US';
}
