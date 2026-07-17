import {
  additionalToolsWeightSource,
  collectToolDefinitionPaths,
  extractExecDeclaredToolNames,
  normalizeToolDescriptionMarkdown,
  projectAdditionalTools,
} from './additionalToolsDisplay';

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function assertEqual<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  }
}

function execDescription(names: string[]): string {
  return names.map((name) => [
    `### \`${name}\``,
    `Full description for ${name}.`,
    '',
    'exec tool declaration:',
    '```ts',
    `declare const tools: { ${name}(args: unknown): Promise<unknown>; };`,
    '```',
  ].join('\n')).join('\n\n');
}

function capturedShape() {
  return {
    type: 'additional_tools',
    role: 'developer',
    tools: [
      {
        type: 'custom',
        name: 'exec',
        description: execDescription([
          'apply_patch',
          'create_goal',
          'get_goal',
          'list_available_plugins_to_install',
          'list_mcp_resource_templates',
          'list_mcp_resources',
          'read_mcp_resource',
          'request_plugin_install',
          'shell_command',
          'update_goal',
          'update_plan',
          'view_image',
          'codex_app__load_workspace_dependencies',
          'codex_app__navigate_to_codex_page',
          'codex_app__read_thread_terminal',
          'image_gen__imagegen',
        ]),
        format: {
          type: 'grammar',
          syntax: 'lark',
          definition: 'start: SOURCE',
        },
      },
      {
        type: 'function',
        name: 'wait',
        description: 'Wait for a running cell.',
        strict: false,
        parameters: {
          type: 'object',
          properties: {
            cell_id: { type: 'string', description: 'Cell identifier.' },
          },
          required: ['cell_id'],
          additionalProperties: false,
        },
      },
      {
        type: 'function',
        name: 'request_user_input',
        description: 'Request input.',
        parameters: { type: 'object', properties: {} },
      },
      {
        type: 'namespace',
        name: 'collaboration',
        description: 'Collaboration tools.',
        tools: [
          { type: 'function', name: 'followup_task' },
          { type: 'function', name: 'interrupt_agent' },
          { type: 'function', name: 'list_agents' },
          { type: 'function', name: 'send_message' },
          { type: 'function', name: 'spawn_agent' },
          { type: 'function', name: 'wait_agent' },
        ],
      },
    ],
  };
}

function testCapturedShapeCountsProtocolLayers(): void {
  const item = capturedShape();
  const projection = projectAdditionalTools(item);

  assert(projection, 'projects an additional_tools provider item');
  assertEqual(projection.topLevelEntryCount, 4, 'counts top-level entries');
  assertEqual(projection.directCallableCount, 9, 'counts namespace children instead of the namespace');
  assertEqual(projection.execDeclaredToolNames.length, 16, 'counts exec-declared nested tools');
  assertEqual(projection.listedCallableCount, 25, 'counts all explicitly listed callable tools');
}

function testExecDeclarationExtractionIsExactAndDeduplicated(): void {
  const names = extractExecDeclaredToolNames(execDescription(['shell_command', 'view_image', 'shell_command']));

  assertEqual(names.length, 2, 'deduplicates repeated exec headings');
  assertEqual(names[0], 'shell_command', 'keeps declaration order');
  assertEqual(names[1], 'view_image', 'extracts the second declaration');
}

function testDescriptionMarkdownRemovesOnlyCommonIndent(): void {
  const normalized = normalizeToolDescriptionMarkdown([
    '',
    '    Spawns an agent.',
    '      - Nested detail',
    '',
  ].join('\n'));

  assertEqual(normalized, 'Spawns an agent.\n  - Nested detail', 'dedents provider formatting while preserving relative indentation');
}

function testDefinitionPathsIncludeNamespaceChildren(): void {
  const projection = projectAdditionalTools(capturedShape());
  assert(projection, 'projects the captured shape');
  const paths = collectToolDefinitionPaths(projection.tools);

  assertEqual(paths.length, 10, 'includes four top-level sections and six namespace children');
  assert(paths.includes('tool-3-child-5'), 'includes the final collaboration child path');
}

function testWeightSourceUsesCompleteProviderItem(): void {
  const item = capturedShape();
  const source = additionalToolsWeightSource([item]);

  assert(source.includes('Full description for shell_command.'), 'keeps complete exec descriptions');
  assert(source.includes('additionalProperties'), 'keeps complete parameter schemas');
  assert(source.includes('start: SOURCE'), 'keeps custom tool format definitions');
  assert(source.includes('wait_agent'), 'keeps namespace child tools');
  assertEqual(JSON.parse(source).tools.length, 4, 'remains valid serialized provider JSON');
}

function main(): void {
  testCapturedShapeCountsProtocolLayers();
  testExecDeclarationExtractionIsExactAndDeduplicated();
  testDescriptionMarkdownRemovesOnlyCommonIndent();
  testDefinitionPathsIncludeNamespaceChildren();
  testWeightSourceUsesCompleteProviderItem();
  console.log('ok - additional tools display contract tests passed');
}

main();
