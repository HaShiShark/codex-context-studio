import { projectExecToolEvent } from './execToolDisplay';
import type { MessageBlock, ToolEvent } from './types';

export interface AssistantActivitySegment {
  kind: 'activity';
  blocks: Exclude<MessageBlock, { kind: 'text' }>[];
  toolEvents: ToolEvent[];
  startIndex: number;
}

export interface AssistantTextSegment {
  kind: 'text';
  block: Extract<MessageBlock, { kind: 'text' }>;
  index: number;
}

export type AssistantRenderSegment = AssistantActivitySegment | AssistantTextSegment;

function projectedToolEvents(block: Extract<MessageBlock, { kind: 'tool' }>): ToolEvent[] {
  return projectExecToolEvent(block.tool_event) || [block.tool_event];
}

export function buildAssistantRenderSegments(blocks: MessageBlock[]): AssistantRenderSegment[] {
  const segments: AssistantRenderSegment[] = [];
  let activityBlocks: AssistantActivitySegment['blocks'] = [];
  let activityStartIndex = 0;

  const flushActivity = () => {
    if (!activityBlocks.length) return;
    segments.push({
      kind: 'activity',
      blocks: activityBlocks,
      toolEvents: activityBlocks.flatMap((block) => block.kind === 'tool' ? projectedToolEvents(block) : []),
      startIndex: activityStartIndex,
    });
    activityBlocks = [];
  };

  blocks.forEach((block, index) => {
    if (block.kind === 'text') {
      flushActivity();
      segments.push({ kind: 'text', block, index });
      return;
    }
    if (!activityBlocks.length) activityStartIndex = index;
    activityBlocks.push(block);
  });

  flushActivity();
  return segments;
}

export function projectActivityBlockTools(
  block: Exclude<MessageBlock, { kind: 'text' }>,
): ToolEvent[] {
  return block.kind === 'tool' ? projectedToolEvents(block) : [];
}
