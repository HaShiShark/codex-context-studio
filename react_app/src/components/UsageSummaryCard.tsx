import type { UiLocale } from '../i18n';
import {
  formatCostUsd,
  formatPercent,
  formatTokenCount,
  type UsageSummaryLike,
  uiText,
} from './ContextWorkbench.helpers';

interface UsageSummaryCardProps {
  title: string;
  description: string;
  summary: UsageSummaryLike;
  uiLocale: UiLocale;
  compact?: boolean;
}

export default function UsageSummaryCard({
  title,
  description,
  summary,
  uiLocale,
  compact = false,
}: UsageSummaryCardProps) {
  return (
    <div className={`workbench-setting-card usage-summary-card${compact ? ' compact' : ''}`}>
      <div className="workbench-setting-title">{title}</div>
      <div className="workbench-setting-desc">{description}</div>
      <div className="usage-summary-grid">
        <div className="usage-summary-item">
          <span>{uiText(uiLocale, 'Calls', '调用次数')}</span>
          <strong>{summary?.request_count || 0}</strong>
        </div>
        <div className="usage-summary-item">
          <span>{uiText(uiLocale, 'Input', '输入')}</span>
          <strong>{formatTokenCount(summary?.input_tokens || 0)}</strong>
        </div>
        <div className="usage-summary-item">
          <span>{uiText(uiLocale, 'Cached', '缓存')}</span>
          <strong>{formatTokenCount(summary?.cached_input_tokens || 0)}</strong>
        </div>
        <div className="usage-summary-item">
          <span>{uiText(uiLocale, 'Non-cached', '非缓存')}</span>
          <strong>{formatTokenCount(summary?.non_cached_input_tokens || 0)}</strong>
        </div>
        <div className="usage-summary-item">
          <span>{uiText(uiLocale, 'Output', '输出')}</span>
          <strong>{formatTokenCount(summary?.output_tokens || 0)}</strong>
        </div>
        <div className="usage-summary-item">
          <span>{uiText(uiLocale, 'Cache hit', '缓存命中')}</span>
          <strong>{formatPercent(summary?.cache_hit_rate)}</strong>
        </div>
      </div>
      <div className="usage-cost-row">
        <span>{uiText(uiLocale, 'Estimated API cost (GPT-5.5 reference)', '预估 API 成本（按 GPT-5.5 参考）')}</span>
        <strong>{formatCostUsd(summary?.known_cost_usd)}</strong>
      </div>
      {summary?.unknown_cost_request_count ? (
        <div className="workbench-setting-feedback">
          {uiText(
            uiLocale,
            `${summary.unknown_cost_request_count} calls used models without a local price mapping, so their cost is excluded.`,
            `${summary.unknown_cost_request_count} 次调用的模型没有本地价格映射，成本未计入。`,
          )}
        </div>
      ) : null}
    </div>
  );
}
