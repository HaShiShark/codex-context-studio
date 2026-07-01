import { useEffect, useState, type ReactNode } from 'react';

import MarkdownRenderer from './MarkdownRenderer';
import type { AttachmentRecord, MessageBlock, MessageRecord, ToolEvent } from '../types';
import { formatBytes } from '../utils';

export type MessageContentVariant = 'default' | 'context-map';

interface MessageContentProps {
  record: MessageRecord;
  variant?: MessageContentVariant;
}

interface ParsedToolOutput {
  prettyJson: string;
  shellOutput: string;
  exitCode?: number;
}

interface WebSearchAction {
  type?: string;
  query?: string;
  queries?: unknown;
  url?: string;
  pattern?: string;
  [key: string]: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function humanizeToolName(name?: string) {
  if (!name) {
    return '工具调用';
  }

  const knownNames: Record<string, string> = {
    parallel_tools: 'parallel_tools',
    get_current_time: '获取当前时间',
    list_project_files: '列出项目文件',
    read_project_file: '读取文件',
    list_dir: '列出目录',
    read_file: '读取文件',
    shell_command: '执行本地命令',
    exec_command: 'Exec 命令',
    write_stdin: '写入 stdin',
    apply_patch: 'Apply Patch',
    view_image: '查看图片',
    js_repl: 'JS REPL',
    js_repl_reset: '重置 JS REPL',
  };

  if (knownNames[name]) {
    return knownNames[name];
  }

  return name.replace(/[._-]+/g, ' ').trim() || '工具调用';
}

function parseToolOutput(event: ToolEvent): ParsedToolOutput {
  const rawOutput = event.raw_output?.trim() || '';
  if (!rawOutput) {
    return {
      prettyJson: event.output_preview || '{}',
      shellOutput: event.display_result || '',
    };
  }

  try {
    const parsed = JSON.parse(rawOutput) as {
      stdout?: string;
      stderr?: string;
      output?: string;
      exit_code?: number;
    };
    const stdout = typeof parsed.stdout === 'string' ? parsed.stdout.trimEnd() : '';
    const output = typeof parsed.output === 'string' ? parsed.output.trimEnd() : '';
    const stderr = typeof parsed.stderr === 'string' ? parsed.stderr.trimEnd() : '';
    const shellOutput = [stdout || output, stderr ? `[stderr]\n${stderr}` : ''].filter(Boolean).join('\n\n');

    return {
      prettyJson: JSON.stringify(parsed, null, 2),
      shellOutput,
      exitCode: typeof parsed.exit_code === 'number' ? parsed.exit_code : undefined,
    };
  } catch {
    return {
      prettyJson: rawOutput,
      shellOutput: rawOutput,
    };
  }
}

function truncateSingleLine(value: string, limit = 72) {
  if (value.length <= limit) {
    return value;
  }

  return `${value.slice(0, Math.max(0, limit - 3))}...`;
}

function isShellToolEvent(event: ToolEvent) {
  if (isWebSearchEvent(event)) {
    return false;
  }

  return event.name === 'shell_command' || event.name === 'exec_command' || event.name === 'write_stdin';
}

function parseWebSearchAction(event: ToolEvent): WebSearchAction | null {
  const { arguments: eventArguments } = event;
  let value: unknown = eventArguments;

  if (typeof eventArguments === 'string') {
    const trimmed = eventArguments.trim();
    if (!trimmed) {
      return null;
    }
    try {
      value = JSON.parse(trimmed) as unknown;
    } catch {
      return null;
    }
  }

  if (!isRecord(value)) {
    return null;
  }

  return value as WebSearchAction;
}

function isWebSearchEvent(event: ToolEvent, action = parseWebSearchAction(event)) {
  if (event.display_title === 'web_search' || event.name === 'web_search' || event.name === 'web_search_call') {
    return true;
  }

  const actionType = typeof action?.type === 'string' ? action.type : '';
  return actionType === 'search' || actionType === 'open_page' || actionType === 'find_in_page';
}

function webSearchQueriesText(queries: unknown) {
  if (!Array.isArray(queries)) {
    return '';
  }

  return queries
    .map((query) => (typeof query === 'string' ? query.trim() : ''))
    .filter(Boolean)
    .join(', ');
}

function webSearchDetailText(event: ToolEvent, action: WebSearchAction | null) {
  const detail = (event.display_detail || '').trim();
  if (!action) {
    return detail;
  }

  if (action.type === 'search') {
    return action.query?.trim() || webSearchQueriesText(action.queries) || detail;
  }

  if (action.type === 'open_page') {
    return action.url?.trim() || detail;
  }

  if (action.type === 'find_in_page') {
    const pattern = action.pattern?.trim() || '';
    const url = action.url?.trim() || '';
    if (pattern && url) {
      return `${pattern} in ${url}`;
    }
    return pattern || url || detail;
  }

  return action.query?.trim() || action.url?.trim() || detail || action.type || '';
}

function webSearchLabel(action: WebSearchAction | null) {
  if (action?.type === 'open_page') {
    return '已打开网页';
  }
  if (action?.type === 'find_in_page') {
    return '页内查找';
  }
  return '已搜索网页';
}

function webSearchActionJson(action: WebSearchAction | null, detail: string) {
  if (action) {
    return JSON.stringify(action, null, 2);
  }
  return detail || 'web_search_call';
}

function toolGroupLabel(events: ToolEvent[]) {
  const allShellEvents = events.every(isShellToolEvent);
  const allWebSearchEvents = events.every((event) => isWebSearchEvent(event));

  if (allWebSearchEvents) {
    return `网页搜索 ${events.length} 次`;
  }

  if (allShellEvents) {
    return `已运行 ${events.length} 条命令`;
  }

  return `调用了 ${events.length} 个工具`;
}

function shouldOpenToolGroupInitially(events: ToolEvent[]) {
  return events.some((event) => {
    const status = String(event.status || '').toLowerCase();
    return status && status !== 'completed' && status !== 'error';
  });
}

function normalizePreviewWhitespace(value: string) {
  return value.replace(/\r?\n+/g, ' ').replace(/\s+/g, ' ').trim();
}

function stripMarkdownSyntax(value: string) {
  return normalizePreviewWhitespace(
    value
      .replace(/```[\w-]*\r?\n([\s\S]*?)```/g, '$1 ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/^>\s?/gm, '')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/^\s*[-*+]\s+/gm, '')
      .replace(/^\s*\d+\.\s+/gm, '')
      .replace(/[*_~]+/g, '')
      .replace(/\|/g, ' ')
      .replace(/---+/g, ' ')
      .replace(/<[^>]+>/g, ' '),
  );
}

function getRecordText(record: MessageRecord) {
  const textBlocks = record.blocks
    .filter((block): block is Extract<MessageBlock, { kind: 'text' }> => block.kind === 'text')
    .map((block) => block.text)
    .join('\n');

  return textBlocks || record.text || '';
}

export function getMessagePreviewText(record: MessageRecord) {
  if (record.pending && !record.text && !record.blocks.length) {
    return '正在思考...';
  }

  if (record.pending && record.blocks.some((block) => block.kind === 'thinking')) {
    return '正在思考...';
  }

  const plainText = stripMarkdownSyntax(getRecordText(record));
  if (plainText) {
    return plainText;
  }

  if (record.toolEvents.length) {
    const webSearchPreviews = record.toolEvents
      .filter((event) => isWebSearchEvent(event))
      .map((event) => webSearchDetailText(event, parseWebSearchAction(event)))
      .filter(Boolean);

    if (webSearchPreviews.length) {
      return webSearchPreviews.length === 1
        ? `网页搜索：${truncateSingleLine(webSearchPreviews[0], 96)}`
        : `网页搜索 ${webSearchPreviews.length} 次：${truncateSingleLine(webSearchPreviews[0], 80)}`;
    }

    return `调用了 ${record.toolEvents.length} 个工具`;
  }

  if (record.attachments.length) {
    return `附件：${record.attachments.map((attachment) => attachment.name).join('，')}`;
  }

  return '仅附件消息';
}

function renderAttachments(attachments: AttachmentRecord[]) {
  if (!attachments.length) {
    return null;
  }

  return (
    <div className="message-attachments">
      {attachments.map((attachment) => {
        const key = attachment.id || `${attachment.name}-${attachment.url || 'local'}`;
        const isImage = attachment.kind === 'image' && attachment.url;

        return (
          <div className={`message-attachment-card ${attachment.kind}`} key={key}>
            {isImage ? (
              <a className="message-attachment-image-link" href={attachment.url} rel="noreferrer" target="_blank">
                <img alt={attachment.name} className="message-attachment-image" src={attachment.url} />
              </a>
            ) : null}

            <div className="message-attachment-meta">
              <div className="message-attachment-name">
                <i className={`ph-light ${attachment.kind === 'image' ? 'ph-image' : 'ph-file-text'}`} />
                <span>{attachment.name}</span>
              </div>
              <div className="message-attachment-subtitle">
                {attachment.kind === 'image' ? '图片' : '文件'}
                {attachment.size_bytes ? ` · ${formatBytes(attachment.size_bytes)}` : ''}
              </div>
            </div>

            {attachment.url && !isImage ? (
              <a className="message-attachment-open" href={attachment.url} rel="noreferrer" target="_blank">
                打开
              </a>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function ThinkingState({ record }: { record: MessageRecord }) {
  const hasBlocks = record.blocks.length > 0;
  const hasText = Boolean(record.text && record.text.trim());

  if (!record.pending || hasBlocks || hasText) {
    return null;
  }

  return <ThinkingBlock />;

  return (
    <div className="thinking-inline-line" role="status">
      <span className="thinking-inline-text">正在思考...</span>
    </div>
  );
}

function ThinkingBlock() {
  return (
    <div className="thinking-inline-line" role="status">
      <span className="thinking-inline-text">正在思考...</span>
    </div>
  );
}

function ReasoningBlock({ block }: { block: Extract<MessageBlock, { kind: 'reasoning' }> }) {
  const isStreaming = block.status === 'streaming';
  const [isOpen, setIsOpen] = useState(false);

  useEffect(() => {
    if (!isStreaming) {
      setIsOpen(false);
    }
  }, [isStreaming]);

  if (!block.text.trim() && !isStreaming) {
    return null;
  }

  return (
    <div className={`reasoning-block ${isOpen ? 'open' : ''} ${isStreaming ? 'streaming' : 'completed'}`}>
      <button className="reasoning-block-toggle" type="button" onClick={() => setIsOpen((previous) => !previous)}>
        <span className="reasoning-block-label">{isStreaming ? '正在思考...' : '思考完成'}</span>
        <i className="ph-light ph-caret-right reasoning-block-chevron" />
      </button>
      <div className={`reasoning-block-panel ${isOpen ? 'open' : ''}`}>
        <div className="reasoning-block-inner">
          <div className="reasoning-block-content">
            {block.text || '正在生成思考内容...'}
          </div>
        </div>
      </div>
    </div>
  );
}

function ToolInvocationItem({
  event,
}: {
  event: ToolEvent;
}) {
  const [isDetailOpen, setIsDetailOpen] = useState(false);
  const title = event.display_title || humanizeToolName(event.name);
  const detail = (event.display_detail || '').trim();
  const isShell = isShellToolEvent(event);
  const webSearchAction = parseWebSearchAction(event);
  const isWebSearch = isWebSearchEvent(event, webSearchAction);
  const { prettyJson, shellOutput, exitCode } = parseToolOutput(event);
  const shellStatusText = event.status === 'error' ? '失败' : '成功';
  const safeShellOutput = shellOutput || event.display_result || '命令已执行，但没有输出。';
  const shellPreview = truncateSingleLine(detail);
  const webSearchDetail = webSearchDetailText(event, webSearchAction);
  const webSearchPreview = truncateSingleLine(webSearchDetail);
  const itemLabel = isWebSearch ? webSearchLabel(webSearchAction) : isShell ? '已运行' : title;

  return (
    <div className="inline-tool-item">
      <button
        className={`inline-tool-summary ${isDetailOpen ? 'open' : ''}`}
        type="button"
        onClick={() => setIsDetailOpen((previous) => !previous)}
      >
        <span className="inline-tool-summary-left">
          <span>{itemLabel}</span>
          {(isWebSearch || isShell) && !isDetailOpen && (isWebSearch ? webSearchDetail : detail) ? (
            <span className="inline-tool-command-preview">{isWebSearch ? webSearchPreview : shellPreview}</span>
          ) : null}
        </span>
        <i className="ph-light ph-caret-right inline-tool-summary-chevron" />
      </button>

      <div className={`inline-tool-detail-panel ${isDetailOpen ? 'open' : ''}`}>
        <div className="inline-tool-detail-inner">
          {!isWebSearch && !isShell && detail ? <div className="inline-tool-detail-text">{detail}</div> : null}

          {isWebSearch ? (
            <>
              {webSearchDetail ? <div className="inline-tool-detail-text">{webSearchDetail}</div> : null}
              <div className="tool-json-box">
                <div className="tool-json-box-label">action</div>
                <div className="tool-json-scroll">
                  <pre>{webSearchActionJson(webSearchAction, webSearchDetail)}</pre>
                </div>
              </div>
            </>
          ) : isShell ? (
            <div className="tool-shell-box">
              <div className="tool-shell-box-label">Shell</div>
              <div className="tool-shell-command">$ {detail || 'powershell command'}</div>
              <div className="tool-shell-scroll">
                <pre>{safeShellOutput}</pre>
              </div>
              <div className={`tool-shell-footer ${event.status === 'error' ? 'error' : 'success'}`}>
                {typeof exitCode === 'number' ? `退出码 ${exitCode} · ${shellStatusText}` : shellStatusText}
              </div>
            </div>
          ) : (
            <div className="tool-json-box">
              <div className="tool-json-box-label">json</div>
              <div className="tool-json-scroll">
                <pre>{prettyJson}</pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ToolInvocationGroup({
  events,
  variant = 'default',
}: {
  events: ToolEvent[];
  variant?: MessageContentVariant;
}) {
  const [isGroupOpen, setIsGroupOpen] = useState(() => shouldOpenToolGroupInitially(events));
  const compactClassName = variant === 'context-map' ? ' inline-tool-block-compact' : '';

  return (
    <div className={`inline-tool-block${compactClassName}`}>
      <button
        className={`inline-tool-call-count ${isGroupOpen ? 'open' : ''}`}
        type="button"
        onClick={() => setIsGroupOpen((previous) => !previous)}
      >
        <span>{toolGroupLabel(events)}</span>
        <i className="ph-light ph-caret-right inline-tool-group-chevron" />
      </button>

      <div className={`inline-tool-group-panel ${isGroupOpen ? 'open' : ''}`}>
        <div className="inline-tool-group-inner">
          {events.map((event, index) => (
            <ToolInvocationItem
              event={event}
              key={event.call_id || `${event.name || 'tool'}-${index}`}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function renderAssistantBlocks(record: MessageRecord, variant: MessageContentVariant) {
  if (record.blocks.length > 0) {
    const renderedBlocks: ReactNode[] = [];
    let pendingToolEvents: ToolEvent[] = [];
    let pendingToolStartIndex = 0;

    const flushToolEvents = () => {
      if (!pendingToolEvents.length) {
        return;
      }

      renderedBlocks.push(
        <ToolInvocationGroup
          events={pendingToolEvents}
          key={`tool-group-${pendingToolStartIndex}-${pendingToolEvents.length}`}
          variant={variant}
        />,
      );
      pendingToolEvents = [];
    };

    record.blocks.forEach((block, index) => {
      if (block.kind === 'tool') {
        if (!pendingToolEvents.length) {
          pendingToolStartIndex = index;
        }
        pendingToolEvents.push(block.tool_event);
        return;
      }

      flushToolEvents();

      if (block.kind === 'text') {
        renderedBlocks.push(<MarkdownRenderer content={block.text} key={`text-${index}`} />);
        return;
      }

      if (block.kind === 'reasoning') {
        renderedBlocks.push(<ReasoningBlock block={block} key={`reasoning-${index}`} />);
        return;
      }

      if (block.kind === 'thinking') {
        renderedBlocks.push(<ThinkingBlock key={`thinking-${index}`} />);
      }
    });

    flushToolEvents();
    return renderedBlocks;
  }

  if (record.text.trim()) {
    return <MarkdownRenderer content={record.text} />;
  }

  return null;
}

function renderUserBlocks(record: MessageRecord, variant: MessageContentVariant) {
  const textBlocks = record.blocks.filter((block): block is Extract<MessageBlock, { kind: 'text' }> => block.kind === 'text');

  if (textBlocks.length > 0) {
    if (variant === 'context-map') {
      return textBlocks.map((block, index) => <MarkdownRenderer content={block.text} key={`user-text-${index}`} />);
    }

    return textBlocks.map((block, index) => <div key={`user-text-${index}`}>{block.text}</div>);
  }

  if (record.text.trim()) {
    if (variant === 'context-map') {
      return <MarkdownRenderer content={record.text} />;
    }

    return record.text;
  }

  return <span className="message-empty-text">这条消息只带了附件。</span>;
}

export default function MessageContent({
  record,
  variant = 'default',
}: MessageContentProps) {
  const isAssistant = record.role === 'an';

  return (
    <>
      <ThinkingState record={record} />
      {renderAttachments(record.attachments)}
      {isAssistant ? renderAssistantBlocks(record, variant) : renderUserBlocks(record, variant)}
    </>
  );
}
