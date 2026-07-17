import { buildAssistantRenderSegments } from './assistantActivityDisplay';
import type { MessageBlock } from './types';

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function assertEqual<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected) throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
}

function shellBlock(command: string): MessageBlock {
  return {
    kind: 'tool',
    tool_event: {
      name: 'exec',
      status: 'completed',
      arguments: `await tools.shell_command({ command: ${JSON.stringify(command)} });`,
    },
  };
}

function testReasoningDoesNotSplitToolActivity(): void {
  const segments = buildAssistantRenderSegments([
    shellBlock('rg --files'),
    { kind: 'reasoning', text: 'checking status', status: 'completed' },
    shellBlock('git status --short'),
  ]);

  assertEqual(segments.length, 1, 'reasoning and tools share one activity segment');
  const activity = segments[0];
  assert(activity.kind === 'activity', 'segment is activity');
  assertEqual(activity.toolEvents.length, 2, 'activity count includes tools on both sides of reasoning');
}

function testVisibleTextRemainsABoundary(): void {
  const segments = buildAssistantRenderSegments([
    shellBlock('rg --files'),
    { kind: 'text', text: 'First check complete.' },
    shellBlock('npm test'),
  ]);

  assertEqual(segments.length, 3, 'visible text separates activity segments');
  assertEqual(segments[0].kind, 'activity', 'first segment is activity');
  assertEqual(segments[1].kind, 'text', 'middle segment is visible text');
  assertEqual(segments[2].kind, 'activity', 'last segment is activity');
}

function main(): void {
  testReasoningDoesNotSplitToolActivity();
  testVisibleTextRemainsABoundary();
  console.log('ok - assistant activity display contract tests passed');
}

main();
