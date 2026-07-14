import type { ProviderItem } from './types';

export type StructuredRecord = Record<string, unknown>;

export interface AdditionalToolsProjection {
  item: StructuredRecord;
  role: string;
  tools: unknown[];
  topLevelEntryCount: number;
  directCallableCount: number;
  execDeclaredToolNames: string[];
  listedCallableCount: number;
}

export function isStructuredRecord(value: unknown): value is StructuredRecord {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

export function isAdditionalToolsProviderItem(value: unknown): value is StructuredRecord {
  return isStructuredRecord(value) && String(value.type || '').trim() === 'additional_tools';
}

export function extractExecDeclaredToolNames(description: unknown): string[] {
  if (typeof description !== 'string' || !description.trim()) {
    return [];
  }

  const names = new Set<string>();
  const headingPattern = /^###\s+`([^`]+)`\s*$/gm;
  let match: RegExpExecArray | null = headingPattern.exec(description);
  while (match) {
    const name = match[1].trim();
    if (name) {
      names.add(name);
    }
    match = headingPattern.exec(description);
  }
  return [...names];
}

export function normalizeToolDescriptionMarkdown(description: string): string {
  const lines = description.replace(/\r\n/g, '\n').split('\n');
  while (lines.length && !lines[0].trim()) {
    lines.shift();
  }
  while (lines.length && !lines[lines.length - 1].trim()) {
    lines.pop();
  }

  const indents = lines
    .filter((line) => line.trim())
    .map((line) => /^\s*/.exec(line)?.[0].length || 0);
  const commonIndent = indents.length ? Math.min(...indents) : 0;
  if (!commonIndent) {
    return lines.join('\n');
  }
  return lines.map((line) => line.slice(Math.min(commonIndent, line.length))).join('\n');
}

export function directCallableToolCount(tool: unknown): number {
  if (!isStructuredRecord(tool)) {
    return 0;
  }

  const type = String(tool.type || '').trim();
  const children = Array.isArray(tool.tools) ? tool.tools : [];
  if (type === 'namespace') {
    return children.reduce<number>((total, child) => total + directCallableToolCount(child), 0);
  }
  return 1;
}

export function collectToolDefinitionPaths(tools: unknown[], parentPath = 'tool'): string[] {
  const paths: string[] = [];
  tools.forEach((tool, index) => {
    const path = `${parentPath}-${index}`;
    paths.push(path);
    if (isStructuredRecord(tool) && Array.isArray(tool.tools)) {
      paths.push(...collectToolDefinitionPaths(tool.tools, `${path}-child`));
    }
  });
  return paths;
}

export function projectAdditionalTools(item: unknown): AdditionalToolsProjection | null {
  if (!isAdditionalToolsProviderItem(item)) {
    return null;
  }

  const tools = Array.isArray(item.tools) ? item.tools : [];
  const execDeclaredToolNames = tools.flatMap((tool) => {
    if (!isStructuredRecord(tool) || String(tool.name || '').trim() !== 'exec') {
      return [];
    }
    return extractExecDeclaredToolNames(tool.description);
  });
  const directCallableCount = tools.reduce<number>(
    (total, tool) => total + directCallableToolCount(tool),
    0,
  );

  return {
    item,
    role: String(item.role || 'developer').trim() || 'developer',
    tools,
    topLevelEntryCount: tools.length,
    directCallableCount,
    execDeclaredToolNames,
    listedCallableCount: directCallableCount + execDeclaredToolNames.length,
  };
}

export function additionalToolsFromProviderItems(providerItems: ProviderItem[] = []): StructuredRecord[] {
  return providerItems.filter(isAdditionalToolsProviderItem);
}

export function additionalToolsWeightSource(providerItems: ProviderItem[] = []): string {
  return additionalToolsFromProviderItems(providerItems)
    .map((item) => {
      try {
        return JSON.stringify(item);
      } catch {
        return String(item);
      }
    })
    .filter(Boolean)
    .join('\n');
}
