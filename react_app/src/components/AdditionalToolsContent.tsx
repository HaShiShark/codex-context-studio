import { useMemo, useState } from 'react';

import MarkdownRenderer from './MarkdownRenderer';
import {
  collectToolDefinitionPaths,
  extractExecDeclaredToolNames,
  isStructuredRecord,
  normalizeToolDescriptionMarkdown,
  projectAdditionalTools,
  type StructuredRecord,
} from '../additionalToolsDisplay';
import type { ProviderItem } from '../types';
import type { UiLocale } from '../i18n';

interface AdditionalToolsContentProps {
  item: ProviderItem;
  uiLocale: UiLocale;
}

interface ToolDefinitionSectionProps {
  depth: number;
  openPaths: Set<string>;
  path: string;
  tool: unknown;
  uiLocale: UiLocale;
  onToggle: (path: string) => void;
}

const TOOL_KNOWN_KEYS = new Set(['type', 'name', 'description', 'strict', 'parameters', 'format', 'tools']);
const FORMAT_KNOWN_KEYS = new Set(['type', 'syntax', 'definition']);
const ITEM_KNOWN_KEYS = new Set(['type', 'role', 'tools']);
const SCHEMA_KNOWN_KEYS = new Set([
  'type',
  'description',
  'default',
  'enum',
  'const',
  'properties',
  'required',
  'items',
  'additionalProperties',
]);

function text(uiLocale: UiLocale, english: string, chinese: string) {
  return uiLocale === 'zh-CN' ? chinese : english;
}

function displayPrimitive(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (value === undefined) {
    return 'undefined';
  }
  return JSON.stringify(value);
}

function StructuredValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (!value.length) {
      return <code className="additional-tools-inline-value">[]</code>;
    }
    return (
      <div className="additional-tools-structured-list">
        {value.map((item, index) => (
          <div className="additional-tools-structured-list-item" key={index}>
            <span className="additional-tools-structured-index">{index}</span>
            <StructuredValue value={item} />
          </div>
        ))}
      </div>
    );
  }

  if (isStructuredRecord(value)) {
    const entries = Object.entries(value);
    if (!entries.length) {
      return <code className="additional-tools-inline-value">{'{}'}</code>;
    }
    return (
      <div className="additional-tools-structured-object">
        {entries.map(([key, child]) => (
          <div className="additional-tools-field-row" key={key}>
            <div className="additional-tools-field-key">{key}</div>
            <div className="additional-tools-field-value">
              <StructuredValue value={child} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return <span className="additional-tools-primitive">{displayPrimitive(value)}</span>;
}

function UnknownFields({ record, knownKeys }: { record: StructuredRecord; knownKeys: Set<string> }) {
  const unknownEntries = Object.entries(record).filter(([key]) => !knownKeys.has(key));
  if (!unknownEntries.length) {
    return null;
  }

  return (
    <div className="additional-tools-unknown-fields">
      {unknownEntries.map(([key, value]) => (
        <div className="additional-tools-field-row" key={key}>
          <div className="additional-tools-field-key">{key}</div>
          <div className="additional-tools-field-value">
            <StructuredValue value={value} />
          </div>
        </div>
      ))}
    </div>
  );
}

function schemaTypeLabel(schema: StructuredRecord): string {
  const rawType = schema.type;
  if (Array.isArray(rawType)) {
    return rawType.map(String).join(' | ');
  }
  if (rawType !== undefined && rawType !== null) {
    return String(rawType);
  }
  if (isStructuredRecord(schema.properties)) {
    return 'object';
  }
  if (schema.items !== undefined) {
    return 'array';
  }
  return 'any';
}

function SchemaNode({
  name,
  required = false,
  schema,
  uiLocale,
}: {
  name: string;
  required?: boolean;
  schema: unknown;
  uiLocale: UiLocale;
}) {
  if (!isStructuredRecord(schema)) {
    return (
      <div className="additional-tools-schema-row">
        <div className="additional-tools-schema-heading">
          <code>{name}</code>
        </div>
        <StructuredValue value={schema} />
      </div>
    );
  }

  const properties = isStructuredRecord(schema.properties) ? schema.properties : null;
  const requiredNames = new Set(
    Array.isArray(schema.required) ? schema.required.map((value) => String(value)) : [],
  );
  const unknownEntries = Object.entries(schema).filter(([key]) => !SCHEMA_KNOWN_KEYS.has(key));

  return (
    <div className="additional-tools-schema-row">
      <div className="additional-tools-schema-heading">
        <code>{name}</code>
        <span className="additional-tools-type-badge">{schemaTypeLabel(schema)}</span>
        {required ? (
          <span className="additional-tools-required-badge">{text(uiLocale, 'required', '必填')}</span>
        ) : (
          <span className="additional-tools-optional-badge">{text(uiLocale, 'optional', '可选')}</span>
        )}
      </div>

      {typeof schema.description === 'string' && schema.description ? (
        <div className="additional-tools-schema-description">{schema.description}</div>
      ) : null}

      {schema.default !== undefined ? (
        <div className="additional-tools-schema-meta">
          <span>{text(uiLocale, 'Default', '默认值')}</span>
          <StructuredValue value={schema.default} />
        </div>
      ) : null}
      {schema.enum !== undefined ? (
        <div className="additional-tools-schema-meta">
          <span>enum</span>
          <StructuredValue value={schema.enum} />
        </div>
      ) : null}
      {schema.const !== undefined ? (
        <div className="additional-tools-schema-meta">
          <span>const</span>
          <StructuredValue value={schema.const} />
        </div>
      ) : null}
      {schema.additionalProperties !== undefined ? (
        <div className="additional-tools-schema-meta">
          <span>additionalProperties</span>
          <StructuredValue value={schema.additionalProperties} />
        </div>
      ) : null}
      {schema.required !== undefined ? (
        <div className="additional-tools-schema-meta">
          <span>required</span>
          <StructuredValue value={schema.required} />
        </div>
      ) : null}

      {properties ? (
        <div className="additional-tools-schema-children">
          {Object.entries(properties).map(([propertyName, propertySchema]) => (
            <SchemaNode
              key={propertyName}
              name={propertyName}
              required={requiredNames.has(propertyName)}
              schema={propertySchema}
              uiLocale={uiLocale}
            />
          ))}
        </div>
      ) : null}

      {schema.items !== undefined ? (
        <div className="additional-tools-schema-children">
          <SchemaNode name="items" schema={schema.items} uiLocale={uiLocale} />
        </div>
      ) : null}

      {unknownEntries.length ? (
        <div className="additional-tools-schema-extra">
          {unknownEntries.map(([key, value]) => (
            <div className="additional-tools-schema-meta" key={key}>
              <span>{key}</span>
              <StructuredValue value={value} />
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ParametersView({ parameters, uiLocale }: { parameters: unknown; uiLocale: UiLocale }) {
  if (!isStructuredRecord(parameters)) {
    return <StructuredValue value={parameters} />;
  }

  return <SchemaNode name={text(uiLocale, 'parameters', '参数')} required schema={parameters} uiLocale={uiLocale} />;
}

function FormatView({ format, uiLocale }: { format: unknown; uiLocale: UiLocale }) {
  if (!isStructuredRecord(format)) {
    return <StructuredValue value={format} />;
  }

  return (
    <div className="additional-tools-format">
      <div className="additional-tools-meta-grid">
        {format.type !== undefined ? (
          <div><span>type</span><code>{displayPrimitive(format.type)}</code></div>
        ) : null}
        {format.syntax !== undefined ? (
          <div><span>syntax</span><code>{displayPrimitive(format.syntax)}</code></div>
        ) : null}
      </div>
      {format.definition !== undefined ? (
        <div className="additional-tools-definition">
          <div className="additional-tools-section-label">{text(uiLocale, 'Definition', '定义')}</div>
          <pre><code>{displayPrimitive(format.definition)}</code></pre>
        </div>
      ) : null}
      <UnknownFields record={format} knownKeys={FORMAT_KNOWN_KEYS} />
    </div>
  );
}

function ToolDefinitionSection({
  depth,
  openPaths,
  path,
  tool,
  uiLocale,
  onToggle,
}: ToolDefinitionSectionProps) {
  const isOpen = openPaths.has(path);
  if (!isStructuredRecord(tool)) {
    return (
      <div className="additional-tools-tool-section malformed">
        <StructuredValue value={tool} />
      </div>
    );
  }

  const name = String(tool.name || tool.type || text(uiLocale, 'Unnamed tool', '未命名工具'));
  const type = String(tool.type || 'tool');
  const children = Array.isArray(tool.tools) ? tool.tools : [];
  const execDeclaredNames = name === 'exec' ? extractExecDeclaredToolNames(tool.description) : [];

  return (
    <section className="additional-tools-tool-section" data-depth={depth}>
      <button
        aria-expanded={isOpen}
        className="additional-tools-tool-toggle"
        type="button"
        onClick={() => onToggle(path)}
      >
        <span className="additional-tools-tool-heading">
          <code>{name}</code>
          <span className="additional-tools-type-badge">{type}</span>
          {children.length ? (
            <span className="additional-tools-count-label">
              {text(uiLocale, `${children.length} tools`, `${children.length} 个工具`)}
            </span>
          ) : null}
          {execDeclaredNames.length ? (
            <span className="additional-tools-count-label">
              {text(uiLocale, `${execDeclaredNames.length} nested tools`, `内含 ${execDeclaredNames.length} 个工具`)}
            </span>
          ) : null}
        </span>
        <i className={`ph-light ph-caret-right additional-tools-chevron ${isOpen ? 'open' : ''}`} />
      </button>

      {isOpen ? (
        <div className="additional-tools-tool-body">
          {typeof tool.description === 'string' && tool.description ? (
            <div className="additional-tools-description">
              <div className="additional-tools-section-label">{text(uiLocale, 'Description', '完整描述')}</div>
              <MarkdownRenderer content={normalizeToolDescriptionMarkdown(tool.description)} />
            </div>
          ) : null}

          {tool.strict !== undefined ? (
            <div className="additional-tools-single-meta">
              <span>strict</span>
              <code>{displayPrimitive(tool.strict)}</code>
            </div>
          ) : null}

          {tool.format !== undefined ? (
            <div className="additional-tools-subsection">
              <div className="additional-tools-section-label">format</div>
              <FormatView format={tool.format} uiLocale={uiLocale} />
            </div>
          ) : null}

          {tool.parameters !== undefined ? (
            <div className="additional-tools-subsection">
              <div className="additional-tools-section-label">{text(uiLocale, 'Parameters', '参数')}</div>
              <ParametersView parameters={tool.parameters} uiLocale={uiLocale} />
            </div>
          ) : null}

          {children.length ? (
            <div className="additional-tools-children">
              <div className="additional-tools-section-label">{text(uiLocale, 'Namespace tools', '命名空间工具')}</div>
              {children.map((child, index) => (
                <ToolDefinitionSection
                  depth={depth + 1}
                  key={`${path}-child-${index}`}
                  openPaths={openPaths}
                  path={`${path}-child-${index}`}
                  tool={child}
                  uiLocale={uiLocale}
                  onToggle={onToggle}
                />
              ))}
            </div>
          ) : null}

          <UnknownFields record={tool} knownKeys={TOOL_KNOWN_KEYS} />
        </div>
      ) : null}
    </section>
  );
}

export default function AdditionalToolsContent({ item, uiLocale }: AdditionalToolsContentProps) {
  const projection = useMemo(() => projectAdditionalTools(item), [item]);
  const allPaths = useMemo(
    () => collectToolDefinitionPaths(projection?.tools || []),
    [projection],
  );
  const [openPaths, setOpenPaths] = useState<Set<string>>(() => new Set());
  const [showRaw, setShowRaw] = useState(false);

  if (!projection) {
    return null;
  }

  const togglePath = (path: string) => {
    setOpenPaths((previous) => {
      const next = new Set(previous);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  return (
    <div className="additional-tools-view">
      <div className="additional-tools-header">
        <div>
          <div className="additional-tools-title">
            <code>additional_tools</code>
            <span>{projection.role}</span>
          </div>
          <div className="additional-tools-summary">
            {text(uiLocale, `${projection.topLevelEntryCount} top-level entries`, `${projection.topLevelEntryCount} 个顶层入口`)}
            <span>·</span>
            {text(uiLocale, `${projection.directCallableCount} direct tools`, `${projection.directCallableCount} 个直接工具`)}
            {projection.execDeclaredToolNames.length ? (
              <>
                <span>·</span>
                {text(uiLocale, `${projection.execDeclaredToolNames.length} inside exec`, `exec 内 ${projection.execDeclaredToolNames.length} 个`)}
              </>
            ) : null}
            <span>·</span>
            <strong>{text(uiLocale, `${projection.listedCallableCount} listed tools`, `${projection.listedCallableCount} 个已列出工具`)}</strong>
          </div>
        </div>

        <div className="additional-tools-actions">
          <button type="button" onClick={() => setOpenPaths(new Set(allPaths))}>
            {text(uiLocale, 'Expand all', '全部展开')}
          </button>
          <button type="button" onClick={() => setOpenPaths(new Set())}>
            {text(uiLocale, 'Collapse all', '全部收起')}
          </button>
          <button aria-pressed={showRaw} type="button" onClick={() => setShowRaw((previous) => !previous)}>
            {text(uiLocale, 'Raw structure', '原始结构')}
          </button>
        </div>
      </div>

      {projection.execDeclaredToolNames.length ? (
        <div className="additional-tools-runtime-note">
          {text(
            uiLocale,
            'The listed total excludes deferred runtime tools that are only discoverable through ALL_TOOLS.',
            '已列出数量不包含只能通过运行时 ALL_TOOLS 发现的 deferred 工具。',
          )}
        </div>
      ) : null}

      <UnknownFields record={projection.item} knownKeys={ITEM_KNOWN_KEYS} />

      <div className="additional-tools-tool-list">
        {projection.tools.map((tool, index) => (
          <ToolDefinitionSection
            depth={0}
            key={`tool-${index}`}
            openPaths={openPaths}
            path={`tool-${index}`}
            tool={tool}
            uiLocale={uiLocale}
            onToggle={togglePath}
          />
        ))}
      </div>

      {showRaw ? (
        <div className="additional-tools-raw">
          <div className="additional-tools-section-label">JSON</div>
          <pre><code>{JSON.stringify(projection.item, null, 2)}</code></pre>
        </div>
      ) : null}
    </div>
  );
}
