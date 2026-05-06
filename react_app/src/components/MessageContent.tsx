import { useEffect, useState } from 'react';

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

function ToolInvocationBlock({
  event,
  variant = 'default',
}: {
  event: ToolEvent;
  variant?: MessageContentVariant;
}) {
  const [isGroupOpen, setIsGroupOpen] = useState(true);
  const [isDetailOpen, setIsDetailOpen] = useState(false);
  const title = event.display_title || humanizeToolName(event.name);
  const detail = (event.display_detail || '').trim();
  const isShell = event.name === 'shell_command' || event.name === 'exec_command' || event.name === 'write_stdin';
  const { prettyJson, shellOutput, exitCode } = parseToolOutput(event);
  const shellStatusText = event.status === 'error' ? '失败' : '成功';
  const safeShellOutput = shellOutput || event.display_result || '命令已执行，但没有输出。';
  const shellPreview = truncateSingleLine(detail);
  const groupLabel = isShell ? '运行命令' : '调用了 1 个工具';
  const itemLabel = isShell ? '已运行命令' : title;
  const compactClassName = variant === 'context-map' ? ' inline-tool-block-compact' : '';

  return (
    <div className={`inline-tool-block${compactClassName}`}>
      <button
        className={`inline-tool-call-count ${isGroupOpen ? 'open' : ''}`}
        type="button"
        onClick={() => setIsGroupOpen((previous) => !previous)}
      >
        <span>{groupLabel}</span>
        <i className="ph-light ph-caret-right inline-tool-group-chevron" />
      </button>

      <div className={`inline-tool-group-panel ${isGroupOpen ? 'open' : ''}`}>
        <div className="inline-tool-group-inner">
          <button
            className={`inline-tool-summary ${isDetailOpen ? 'open' : ''}`}
            type="button"
            onClick={() => setIsDetailOpen((previous) => !previous)}
          >
            <span className="inline-tool-summary-left">
              <span>{itemLabel}</span>
              {isShell && !isDetailOpen && detail ? (
                <span className="inline-tool-command-preview">{shellPreview}</span>
              ) : null}
            </span>
            <i className="ph-light ph-caret-right inline-tool-summary-chevron" />
          </button>

          <div className={`inline-tool-detail-panel ${isDetailOpen ? 'open' : ''}`}>
            <div className="inline-tool-detail-inner">
              {!isShell && detail ? <div className="inline-tool-detail-text">{detail}</div> : null}

              {isShell ? (
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
      </div>
    </div>
  );
}

function renderAssistantBlocks(record: MessageRecord, variant: MessageContentVariant) {
  if (record.blocks.length > 0) {
    return record.blocks.map((block, index) => {
      if (block.kind === 'text') {
        return <MarkdownRenderer content={block.text} key={`text-${index}`} />;
      }

      if (block.kind === 'reasoning') {
        return <ReasoningBlock block={block} key={`reasoning-${index}`} />;
      }

      if (block.kind === 'thinking') {
        return <ThinkingBlock key={`thinking-${index}`} />;
      }

      return (
        <ToolInvocationBlock
          event={block.tool_event}
          key={`tool-${index}-${block.tool_event.name || 'tool'}`}
          variant={variant}
        />
      );
    });
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
