import type { ProviderType, ResponseProviderModel } from './types';

const TECHNICAL_GROUPS = new Set([
  'api',
  'chat',
  'chat_completion',
  'chat-completion',
  'chat completions',
  'completion',
  'completions',
  'custom',
  'model',
  'models',
  'openai',
  'response',
  'responses',
  'system',
]);

function getBaseModelName(id: string, delimiter = '/') {
  const parts = id.split(delimiter);
  return parts[parts.length - 1] || id;
}

function getLowerBaseModelName(id: string, delimiter = '/') {
  const normalizedId = id.toLowerCase().startsWith('accounts/fireworks/models/')
    ? id.replace(/(\d)p(?=\d)/g, '$1.')
    : id;

  let baseModelName = getBaseModelName(normalizedId, delimiter).toLowerCase();
  if (baseModelName.endsWith(':free')) {
    baseModelName = baseModelName.replace(':free', '');
  }
  if (baseModelName.endsWith('(free)')) {
    baseModelName = baseModelName.replace('(free)', '');
  }
  if (baseModelName.endsWith(':cloud')) {
    baseModelName = baseModelName.replace(':cloud', '');
  }
  return baseModelName;
}

function getDefaultGroupName(id: string, provider?: string) {
  const str = id.toLowerCase();
  let firstDelimiters = ['/', ' ', ':'];
  let secondDelimiters = ['-', '_'];

  if (provider && ['aihubmix', 'silicon', 'ocoolai', 'o3', 'dmxapi'].includes(provider.toLowerCase())) {
    firstDelimiters = ['/', ' ', '-', '_', ':'];
    secondDelimiters = [];
  }

  for (const delimiter of firstDelimiters) {
    if (str.includes(delimiter)) {
      return str.split(delimiter)[0];
    }
  }

  for (const delimiter of secondDelimiters) {
    if (str.includes(delimiter)) {
      const parts = str.split(delimiter);
      return parts.length > 1 ? `${parts[0]}-${parts[1]}` : parts[0];
    }
  }

  return str;
}

function prettifyTokenGroup(group: string) {
  const cleaned = group.trim().replace(/[_-]+/g, ' ');
  if (!cleaned) {
    return 'Other';
  }

  return cleaned
    .split(/\s+/)
    .map((part) => {
      const lower = part.toLowerCase();
      if (['ai', 'api', 'glm', 'gpt', 'vl'].includes(lower)) {
        return lower.toUpperCase();
      }
      if (lower === 'qwen') {
        return 'Qwen';
      }
      if (lower === 'kimi') {
        return 'Kimi';
      }
      return part.slice(0, 1).toUpperCase() + part.slice(1);
    })
    .join(' ');
}

function modelSearchText(model: ResponseProviderModel) {
  return [model.id, model.label].filter(Boolean).join(' ');
}

function inferKnownModelGroup(model: ResponseProviderModel) {
  const text = modelSearchText(model);
  const lowerText = text.toLowerCase();
  const base = getLowerBaseModelName(model.id || model.label || text);

  if (/(^|[/\s:_-])gpt[-_]?image/.test(lowerText)) {
    return 'GPT Image';
  }

  if (/(^|[/\s:_-])gpt[-_]?oss/.test(lowerText)) {
    return 'GPT OSS';
  }

  const gpt4oMatch = lowerText.match(/(^|[/\s:_-])(gpt[-_]?4o)(?:[-_\s]|$)/);
  if (gpt4oMatch) {
    return 'GPT 4o';
  }

  const gptMatch = lowerText.match(/(^|[/\s:_-])gpt[-_]?(\d+(?:\.\d+)?)(?:[-_\s]|$)/);
  if (gptMatch) {
    return `GPT ${gptMatch[2]}`;
  }

  const openAiReasoningMatch = lowerText.match(/(^|[/\s:_-])o(\d)(?:[-_\s]|$)/);
  if (openAiReasoningMatch) {
    return `OpenAI o${openAiReasoningMatch[2]}`;
  }

  const geminiMatch = lowerText.match(/(^|[/\s:_-])gemini[-_]?(\d+)(?:\.(\d+))?/);
  if (geminiMatch) {
    const major = geminiMatch[2];
    const minor = geminiMatch[3];
    return major === '3' ? 'Gemini 3' : `Gemini ${minor ? `${major}.${minor}` : major}`;
  }

  const claudeMatch =
    lowerText.match(/claude[-_](?:opus|sonnet|haiku)[-_](\d)(?:[-_.](\d))?/) ||
    lowerText.match(/claude[-_](\d)(?:[-_.](\d))?/);
  if (claudeMatch) {
    return `Claude ${claudeMatch[1]}${claudeMatch[2] ? `.${claudeMatch[2]}` : ''}`;
  }

  if (lowerText.includes('deepseek')) {
    const deepseekVersion = lowerText.match(/deepseek[-_\s]?(?:v|r)(\d+(?:\.\d+)?)/);
    if (deepseekVersion) {
      return `DeepSeek ${deepseekVersion[0].includes('r') ? 'R' : 'V'}${deepseekVersion[1]}`;
    }
    if (lowerText.includes('reasoner')) {
      return 'DeepSeek Reasoner';
    }
    if (lowerText.includes('chat')) {
      return 'DeepSeek Chat';
    }
    return 'DeepSeek';
  }

  const qwenMatch = base.match(/^(qwen)(\d+(?:\.\d+)?)(?:[-_]?([a-z]+))?/);
  if (qwenMatch) {
    const suffix = qwenMatch[3] && ['vl', 'coder', 'omni', 'max'].includes(qwenMatch[3]) ? ` ${qwenMatch[3].toUpperCase()}` : '';
    return `Qwen ${qwenMatch[2]}${suffix}`;
  }
  if (/^(qwq|qvq)(?:[-_]|$)/.test(base)) {
    return 'Qwen Reasoning';
  }
  if (base.includes('qwen')) {
    return 'Qwen';
  }

  const glmMatch = base.match(/glm[-_]?(\d+(?:\.\d+)?[a-z]?)/);
  if (glmMatch) {
    return `GLM ${glmMatch[1].toUpperCase()}`;
  }

  const kimiMatch = base.match(/kimi[-_]?k?(\d+(?:\.\d+)?)/);
  if (kimiMatch) {
    return `Kimi K${kimiMatch[1]}`;
  }
  if (base.includes('kimi') || base.includes('moonshot')) {
    return 'Kimi';
  }

  const llamaMatch = base.match(/llama[-_]?(\d+(?:\.\d+)?)/);
  if (llamaMatch) {
    return `Llama ${llamaMatch[1]}`;
  }
  if (base.includes('llama')) {
    return 'Llama';
  }

  const gemmaMatch = base.match(/gemma[-_]?(\d+(?:\.\d+)?)/);
  if (gemmaMatch) {
    return `Gemma ${gemmaMatch[1]}`;
  }
  if (base.includes('gemma')) {
    return 'Gemma';
  }

  if (base.includes('mistral') || base.includes('mixtral') || base.includes('codestral')) {
    return base.includes('codestral') ? 'Codestral' : 'Mistral';
  }
  if (base.includes('minimax') || base.includes('abab')) {
    return 'MiniMax';
  }
  if (base.includes('doubao') || base.includes('seedream')) {
    return 'Doubao';
  }
  if (base.includes('embedding') || base.includes('bge-') || base.includes('voyage-')) {
    return 'Embedding';
  }
  if (base.includes('rerank')) {
    return 'Rerank';
  }
  if (base.includes('dall-e') || base.includes('dalle')) {
    return 'DALL-E';
  }

  return '';
}

function shouldUseReturnedGroup(model: ResponseProviderModel) {
  const group = (model.group || '').trim().toLowerCase();
  if (!group || TECHNICAL_GROUPS.has(group)) {
    return false;
  }
  if (model.provider && model.provider.trim().toLowerCase() === group) {
    return false;
  }
  return true;
}

export function getProviderModelGroup(model: ResponseProviderModel, providerType?: ProviderType) {
  const knownGroup = inferKnownModelGroup(model);
  if (knownGroup) {
    return knownGroup;
  }

  if (shouldUseReturnedGroup(model)) {
    return prettifyTokenGroup(model.group);
  }

  const defaultGroup = getDefaultGroupName(model.id || model.label || '', providerType);
  return prettifyTokenGroup(defaultGroup);
}

const GROUP_ORDER = [
  'GPT 5.4',
  'GPT 5.2',
  'GPT 5.1',
  'GPT 5',
  'GPT 4.1',
  'GPT 4o',
  'GPT 4',
  'OpenAI o',
  'GPT Image',
  'GPT OSS',
  'Gemini 3',
  'Gemini 2.5',
  'Gemini 2',
  'Claude 4.6',
  'Claude 4.5',
  'Claude 4',
  'Claude 3.7',
  'Claude 3.5',
  'DeepSeek',
  'Qwen',
  'Kimi',
  'Llama',
  'Gemma',
  'GLM',
  'Mistral',
  'Embedding',
  'Rerank',
];

function groupRank(groupName: string) {
  const index = GROUP_ORDER.findIndex((prefix) => groupName.toLowerCase().startsWith(prefix.toLowerCase()));
  return index === -1 ? GROUP_ORDER.length : index;
}

export function compareProviderModelGroups(left: string, right: string) {
  const leftRank = groupRank(left);
  const rightRank = groupRank(right);
  if (leftRank !== rightRank) {
    return leftRank - rightRank;
  }
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: 'base' });
}
