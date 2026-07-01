import { normalizeConversation } from './utils';
import codexItemRegistry from '../../shared/codex-item-registry.json';
import {
  CONTEXT_IMAGE_TOKEN_ESTIMATE,
  getContextTokenCount,
  getContextWeightSource,
} from './contextTokenWeight';
import type { ProviderItem, TranscriptNode } from './types';

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

function assertIncludes(value: string, expected: string, message: string): void {
  if (!value.includes(expected)) {
    throw new Error(`${message}: expected ${JSON.stringify(value)} to include ${JSON.stringify(expected)}`);
  }
}

function node(id: string, role: string, providerItems: ProviderItem[]): TranscriptNode {
  return {
    id,
    role,
    items: providerItems.map((providerItem, index) => ({
      kind: String((providerItem as Record<string, unknown>).type || 'unknown'),
      providerItem,
      inputIndex: index,
    })),
    source_map: {},
  };
}

function testNormalizeConversationKeepsProviderItemContract(): void {
  const callItem: ProviderItem = {
    type: 'function_call',
    call_id: 'call-lookup',
    name: 'lookup_context',
    arguments: '{"query":"alpha"}',
  };
  const outputItem: ProviderItem = {
    type: 'function_call_output',
    call_id: 'call-lookup',
    output: 'lookup result',
  };
  const unknownItem: ProviderItem = {
    type: 'unexpected_provider_item',
    role: 'context',
    payload: { marker: 'kept' },
  };

  const conversation = normalizeConversation([
    node('node-user', 'user', [
      {
        type: 'message',
        role: 'user',
        content: 'hello',
      },
    ]),
    node('node-assistant', 'assistant', [
      {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: 'I will look it up.' }],
      },
      {
        type: 'reasoning',
        summary: [{ type: 'summary_text', text: 'Need a lookup.' }],
      },
      callItem,
      outputItem,
    ]),
    node('node-unknown', 'unknown', [unknownItem]),
  ]);

  assertEqual(conversation.length, 3, 'normalizes every transcript node');

  const user = conversation[0];
  assertEqual(user.role, 'user', 'keeps user role');
  assertEqual(user.text, 'hello', 'reads message item text');
  assertEqual(user.blocks.length, 1, 'creates a text block for message item');
  assertEqual(user.providerItems?.length, 1, 'keeps original user provider item');

  const assistant = conversation[1];
  assertEqual(assistant.role, 'an', 'maps assistant transcript role to UI assistant role');
  assertIncludes(assistant.text, 'I will look it up.', 'includes assistant message text');
  assertIncludes(assistant.text, 'Need a lookup.', 'includes reasoning text');
  assertIncludes(assistant.text, 'lookup result', 'includes paired tool output text');
  assertEqual(assistant.providerItems?.length, 4, 'keeps all assistant provider items');

  const reasoningBlock = assistant.blocks.find((block) => block.kind === 'reasoning');
  assert(reasoningBlock?.kind === 'reasoning', 'creates a reasoning block');
  assertEqual(reasoningBlock.text, 'Need a lookup.', 'reasoning block exposes summary text');

  const toolBlocks = assistant.blocks.filter((block) => block.kind === 'tool');
  assertEqual(toolBlocks.length, 1, 'pairs one tool call with its output');
  assertEqual(assistant.toolEvents.length, 1, 'creates one tool event');
  assertEqual(assistant.toolEvents[0].name, 'lookup_context', 'uses function call name as tool event name');
  assertEqual(assistant.toolEvents[0].call_id, 'call-lookup', 'keeps tool call id');
  assertEqual(assistant.toolEvents[0].raw_output, 'lookup result', 'keeps tool output');
  assertEqual(assistant.toolEvents[0].status, 'completed', 'marks successful tool output completed');

  const unknown = conversation[2];
  assertEqual(unknown.role, 'context', 'maps unknown transcript role to context display role');
  assertEqual(unknown.providerItems?.length, 1, 'keeps unknown provider item');
  assertIncludes(unknown.text, 'unexpected_provider_item', 'renders unknown item type');
  assertIncludes(unknown.text, '"marker": "kept"', 'renders unknown item payload');
}

function testProviderItemRegistryDrivesToolPairingAndDisplayHints(): void {
  const registry = codexItemRegistry as {
    tool_call_item_types: string[];
    tool_output_item_types: string[];
    paired_tool_output_types_by_call_type: Record<string, string[]>;
    compaction_item_types: string[];
    display_hints_by_item_type: Record<string, { title?: string; event_name?: string }>;
  };

  assert(
    registry.tool_call_item_types.includes('local_shell_call'),
    'registry declares local_shell_call as a tool call',
  );
  assert(
    registry.tool_output_item_types.includes('local_shell_call_output'),
    'registry declares local_shell_call_output as a tool output',
  );
  assert(
    registry.paired_tool_output_types_by_call_type.local_shell_call.includes('local_shell_call_output'),
    'registry declares local shell output pairing',
  );
  assertEqual(
    registry.display_hints_by_item_type.local_shell_call.event_name,
    'local_shell',
    'registry declares local shell display event name',
  );

  const conversation = normalizeConversation([
    node('node-assistant-shell', 'assistant', [
      {
        type: 'local_shell_call',
        call_id: 'call-shell',
        action: { command: ['echo', 'hello'] },
      },
      {
        type: 'local_shell_call_output',
        call_id: 'call-shell',
        output: 'exit code: 0\nhello',
      },
    ]),
  ]);

  const assistant = conversation[0];
  assertEqual(assistant.toolEvents.length, 1, 'pairs local shell call with registry output type');
  assertEqual(assistant.toolEvents[0].name, 'local_shell', 'uses registry display event name');
  assertEqual(assistant.toolEvents[0].display_title, 'local_shell', 'uses registry display title');
  assertEqual(assistant.toolEvents[0].raw_output, 'exit code: 0\nhello', 'keeps paired local shell output');
  assertEqual(assistant.toolEvents[0].status, 'completed', 'keeps local shell status parsing');
}

function testProviderItemTypesAreCaseAndSeparatorSensitive(): void {
  const conversation = normalizeConversation([
    node('node-assistant-wrong-type', 'assistant', [
      {
        type: 'Local-Shell-Call',
        call_id: 'call-shell',
        action: { command: ['echo', 'hello'] },
      },
      {
        type: 'local_shell_call_output',
        call_id: 'call-shell',
        output: 'exit code: 0\nhello',
      },
    ]),
  ]);

  const assistant = conversation[0];
  assertEqual(assistant.toolEvents.length, 1, 'does not pair output with wrong call type');
  assertEqual(assistant.toolEvents[0].name, 'local_shell_call_output', 'renders the valid output as standalone');
  assertIncludes(assistant.text, 'Local-Shell-Call', 'renders wrong call type as raw provider item');
}

function testNormalizeConversationSupportsSubagentNodes(): void {
  const conversation = normalizeConversation([
    node('node-subagent', 'subagent', [
      {
        type: 'agent_message',
        author: 'worker',
        recipient: 'root',
        content: [{ type: 'input_text', text: 'worker result is ready' }],
      },
    ]),
  ]);

  const subagent = conversation[0];
  assertEqual(subagent.role, 'subagent', 'keeps subagent display role');
  assertIncludes(subagent.text, 'worker result is ready', 'renders agent_message content as readable text');
  assert(!subagent.text.includes('"author"'), 'does not render agent_message as raw JSON');
  assertEqual(subagent.providerItems?.length, 1, 'keeps original agent_message provider item');
}

function testImageDataUrlsDoNotEnterContextWeightText(): void {
  const payload = 'A'.repeat(120_000);
  const imageUrl = `data:image/png;base64,${payload}`;
  const conversation = normalizeConversation([
    node('node-user-image', 'user', [
      {
        type: 'message',
        role: 'user',
        content: [
          { type: 'input_text', text: 'Please inspect this screenshot.' },
          { type: 'input_image', image_url: imageUrl },
        ],
      },
    ]),
  ]);

  const user = conversation[0];
  const weightSource = getContextWeightSource(user);
  const tokenCount = getContextTokenCount(user);

  assertIncludes(user.text, '[image]', 'renders image content as a short placeholder');
  assert(!user.text.includes(payload), 'does not expose image base64 in normalized message text');
  assert(!weightSource.includes(payload), 'does not count image base64 as text in context map weight source');
  assert(
    tokenCount >= CONTEXT_IMAGE_TOKEN_ESTIMATE && tokenCount < CONTEXT_IMAGE_TOKEN_ESTIMATE + 100,
    'uses a bounded image token estimate instead of base64 text length',
  );
}

function main(): void {
  testNormalizeConversationKeepsProviderItemContract();
  testProviderItemRegistryDrivesToolPairingAndDisplayHints();
  testProviderItemTypesAreCaseAndSeparatorSensitive();
  testNormalizeConversationSupportsSubagentNodes();
  testImageDataUrlsDoNotEnterContextWeightText();
  console.log('ok - normalizeConversation contract tests passed');
}

main();
