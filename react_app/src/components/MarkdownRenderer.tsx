import { useDeferredValue, useEffect, useId, useState } from 'react';
import type { ComponentPropsWithoutRef, ReactNode } from 'react';

import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import markdown from 'highlight.js/lib/languages/markdown';
import plaintext from 'highlight.js/lib/languages/plaintext';
import powershell from 'highlight.js/lib/languages/powershell';
import python from 'highlight.js/lib/languages/python';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import ReactMarkdown from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import rehypeSlug from 'rehype-slug';
import remarkGfm from 'remark-gfm';
import remarkGithubBlockquoteAlert from 'remark-github-blockquote-alert';

const registeredLanguages = [
  ['bash', bash],
  ['sh', bash],
  ['shell', bash],
  ['css', css],
  ['html', xml],
  ['xml', xml],
  ['javascript', javascript],
  ['js', javascript],
  ['json', json],
  ['markdown', markdown],
  ['md', markdown],
  ['plaintext', plaintext],
  ['text', plaintext],
  ['powershell', powershell],
  ['ps1', powershell],
  ['python', python],
  ['py', python],
  ['sql', sql],
  ['typescript', typescript],
  ['ts', typescript],
] as const;

registeredLanguages.forEach(([name, language]) => {
  hljs.registerLanguage(name, language);
});

let mermaidInitialized = false;
let mermaidModulePromise: Promise<typeof import('mermaid')> | null = null;

async function getMermaidModule() {
  if (!mermaidModulePromise) {
    mermaidModulePromise = import('mermaid');
  }

  const module = await mermaidModulePromise;
  const mermaidApi = module.default;

  if (!mermaidInitialized) {
    mermaidApi.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'dark',
    });
    mermaidInitialized = true;
  }

  return mermaidApi;
}

const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []),
    'div',
    'span',
    'section',
    'details',
    'summary',
    'kbd',
    'sup',
    'sub',
  ],
  attributes: {
    ...(defaultSchema.attributes || {}),
    '*': [
      ...(((defaultSchema.attributes || {})['*'] as Array<string | [string, RegExp]>) || []),
      'className',
      'id',
    ],
    a: [
      ...(((defaultSchema.attributes || {}).a as Array<string | [string, RegExp]>) || []),
      'target',
      'rel',
    ],
    code: [
      ...(((defaultSchema.attributes || {}).code as Array<string | [string, RegExp]>) || []),
      ['className', /^language-[\w-]+$/],
    ],
    div: [
      ...(((defaultSchema.attributes || {}).div as Array<string | [string, RegExp]>) || []),
      'className',
    ],
    span: [
      ...(((defaultSchema.attributes || {}).span as Array<string | [string, RegExp]>) || []),
      'className',
    ],
    blockquote: [
      ...(((defaultSchema.attributes || {}).blockquote as Array<string | [string, RegExp]>) || []),
      'className',
    ],
  },
};

function getCodeText(children: ReactNode): string {
  if (typeof children === 'string') {
    return children;
  }

  if (Array.isArray(children)) {
    return children.map((child) => getCodeText(child)).join('');
  }

  if (children && typeof children === 'object' && 'props' in children) {
    const nodeChildren = (children as { props?: { children?: ReactNode } }).props?.children;
    return getCodeText(nodeChildren || '');
  }

  return '';
}

function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState('');
  const [renderError, setRenderError] = useState('');
  const deferredCode = useDeferredValue(code);
  const graphId = useId().replace(/:/g, '-');

  useEffect(() => {
    let active = true;

    async function renderDiagram() {
      if (!deferredCode.trim()) {
        setSvg('');
        setRenderError('');
        return;
      }

      try {
        const mermaidApi = await getMermaidModule();
        const result = await mermaidApi.render(`mermaid-${graphId}`, deferredCode);
        if (!active) {
          return;
        }
        setSvg(result.svg);
        setRenderError('');
      } catch (error) {
        if (!active) {
          return;
        }
        setSvg('');
        setRenderError(error instanceof Error ? error.message : 'Mermaid 渲染失败');
      }
    }

    renderDiagram().catch(() => {
      if (active) {
        setSvg('');
        setRenderError('Mermaid 渲染失败');
      }
    });

    return () => {
      active = false;
    };
  }, [deferredCode, graphId]);

  if (svg) {
    return <div className="markdown-mermaid" dangerouslySetInnerHTML={{ __html: svg }} />;
  }

  return (
    <div className="markdown-mermaid-fallback">
      <pre>
        <code>{code}</code>
      </pre>
      {renderError ? <div className="markdown-mermaid-error">{renderError}</div> : null}
    </div>
  );
}

function CodeBlock(props: ComponentPropsWithoutRef<'code'>) {
  const { className, children, ...rest } = props;
  const rawCode = getCodeText(children).replace(/\n$/, '');
  const match = /language-([\w-]+)/.exec(className || '');
  const language = (match?.[1] || '').toLowerCase();

  if (!match) {
    return (
      <code className={className} {...rest}>
        {children}
      </code>
    );
  }

  if (language === 'mermaid') {
    return <MermaidBlock code={rawCode} />;
  }

  const highlightLanguage = hljs.getLanguage(language) ? language : 'plaintext';
  const highlighted = hljs.highlight(rawCode, {
    language: highlightLanguage,
    ignoreIllegals: true,
  }).value;

  return (
    <pre className="markdown-code-block">
      <div className="markdown-code-block-bar">
        <span>{highlightLanguage}</span>
      </div>
      <code
        className={`hljs language-${highlightLanguage}`}
        dangerouslySetInnerHTML={{ __html: highlighted }}
      />
    </pre>
  );
}

function Table({ children }: { children?: ReactNode }) {
  return (
    <div className="markdown-table-wrap">
      <table>{children}</table>
    </div>
  );
}

function unwrapMarkdownFence(content: string): string {
  const trimmed = content.trim();
  const fencedDocumentMatch = /^(`{3,}|~{3,})(markdown|md)\s*\r?\n([\s\S]*)\r?\n\1\s*$/i.exec(trimmed);

  if (!fencedDocumentMatch) {
    return content;
  }

  return fencedDocumentMatch[3];
}

export default function MarkdownRenderer({ content }: { content: string }) {
  const deferredContent = useDeferredValue(unwrapMarkdownFence(content));

  return (
    <div className="markdown-body">
      <ReactMarkdown
        components={{
          a: ({ href, children, ...props }) => (
            <a href={href} rel="noreferrer" target="_blank" {...props}>
              {children}
            </a>
          ),
          code: CodeBlock,
          table: Table,
        }}
        rehypePlugins={[
          rehypeRaw,
          [rehypeSanitize, sanitizeSchema],
          rehypeSlug,
        ]}
        remarkPlugins={[
          remarkGfm,
          remarkGithubBlockquoteAlert,
        ]}
      >
        {deferredContent}
      </ReactMarkdown>
    </div>
  );
}
