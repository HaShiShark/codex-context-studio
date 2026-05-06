import type { ProviderType, ResponseProviderModel } from './types';

export type ModelCapabilityTone = 'green' | 'blue' | 'violet' | 'orange';

export interface ProviderModelCapability {
  key: 'vision' | 'web_search' | 'reasoning' | 'tools';
  icon: string;
  label: string;
  tone: ModelCapabilityTone;
}

const NON_CHAT_MODEL_REGEX =
  /\b(tts-|whisper-|embedding|text-embedding|rerank|bge-|voyage-|omni-moderation|text-moderation|gpt-image|dall-e|dalle|imagen|flux|midjourney|mj-)\b/i;

const VISION_REGEX =
  /\b(llava|moondream|minicpm|gemini-1\.5|gemini-2\.0|gemini-2\.5|gemini-3(?:\.\d+)?-(?:flash|pro)|gemini-(?:flash|pro|flash-lite)-latest|claude-3|claude-(?:haiku|sonnet|opus)-4|vision|glm-4(?:\.\d+)?v(?:-[\w-]+)?|glm-5v(?:-[\w-]+)?|qwen(?:2(?:\.5)?|3(?:\.\d+)?)?-(?:vl|omni)|qvq|internvl|grok-vision-beta|grok-4(?:-[\w-]+)?|pixtral|gpt-4(?:o|\.1|\.5)(?:-[\w-]+)?|gpt-5(?:-[\w-]+)?|chatgpt-4o(?:-[\w-]+)?|o[134](?:-[\w-]+)?|deepseek-vl(?:[\w-]+)?|kimi-k2\.5|kimi-vl|gemma-?[34](?:[-.\w]+)?|llama-4(?:-[\w-]+)?|step-1(?:o|v)(?:-[\w-]+)?|mistral-(?:large|medium|small)-(?:latest|\d+)|mimo-v2-omni)\b/i;

const WEB_SEARCH_REGEX =
  /\b(gpt-4o-search-preview|gpt-4o-mini-search-preview|gpt-4\.1(?!-nano)|gpt-4o(?!-image)|gpt-5(?:\.\d+)?(?!.*chat)|o[34](?:-[\w-]+)?|claude-(?:haiku|sonnet|opus)-4(?:-[\w-]+)?|claude-3(?:[.-](?:5|7))-(?:sonnet|haiku)(?:-[\w-]+)?|gemini-(?:2(?!.*image).*(?:-latest)?|3(?:\.\d+)?-(?:flash|pro)(?:-(?:image-)?preview)?|flash-latest|pro-latest|flash-lite-latest)(?:-[\w-]+)*|sonar(?:-[\w-]+)?|perplexity|qwen-(?:max|plus|turbo|flash)(?:-[\w-]+)?|qwq(?:-[\w-]+)?|grok(?:-[\w-]+)?)\b/i;

const REASONING_REGEX =
  /\b(o\d+(?:-[\w-]+)?|reasoning|reasoner|thinking|think\b|qwq(?:-[\w-]+)?|qvq(?:-[\w-]+)?|hunyuan-t1(?:-[\w-]+)?|glm-zero-preview|grok-(?:3-mini|4|4-fast)(?:-[\w-]+)?|deepseek-r\d+(?:\.\d+)?|deepseek-reasoner|gpt-5(?:\.\d+)?(?!.*chat)|gpt-oss(?:-[\w-]+)?|claude-[\w.-]*thinking)\b/i;

const TOOLS_REGEX =
  /\b(gpt-4o(?:-[\w-]+)?|gpt-4\.1(?:-[\w-]+)?|gpt-4(?:-[\w-]+)?|gpt-4\.5(?:-[\w-]+)?|gpt-5(?:\.\d+)?(?!.*chat)|gpt-oss(?:-[\w-]+)?|o[134](?:-[\w-]+)?|claude(?:-[\w.-]+)?|qwen(?:[\w.-]+)?|hunyuan(?:-[\w-]+)?|deepseek(?:-[\w-]+)?|glm-(?:4(?:\.\d+)?|5)(?:-[\w-]+)?|gemini(?!-1)(?:-[\w.-]+)?|gemma-?4(?:[-.\w]+)?|grok-(?:3|4)(?:-[\w-]+)?|doubao-seed(?:-[\w.-]+)?|kimi-k2(?:\.\d+)?(?:-[\w-]+)?|ling-\w+(?:-[\w-]+)?|ring-\w+(?:-[\w-]+)?|minimax-m2(?:\.\d+)?(?:-[\w-]+)?|mimo-v2-(?:flash|pro|omni)|llama-4(?:-[\w-]+)?|step-1(?:o|v)(?:-[\w-]+)?)\b/i;

const OPENAI_RESPONSE_WEB_SEARCH_REGEX = /\b(gpt-4\.1(?!-nano)|gpt-4o(?!-image)|gpt-5(?:\.\d+)?(?!.*chat)|o[34](?:-[\w-]+)?)\b/i;
const CLAUDE_WEB_SEARCH_REGEX = /\bclaude-(?:haiku|sonnet|opus)-4(?:-[\w-]+)?|claude-3(?:[.-](?:5|7))-(?:sonnet|haiku)(?:-[\w-]+)?\b/i;
const GEMINI_WEB_SEARCH_REGEX = /\bgemini-(?:2(?!.*image).*(?:-latest)?|3(?:\.\d+)?-(?:flash|pro)(?:-(?:image-)?preview)?|flash-latest|pro-latest|flash-lite-latest)(?:-[\w-]+)*\b/i;

function modelSearchText(model: ResponseProviderModel) {
  return [model.id, model.label, model.group, model.provider].filter(Boolean).join(' ');
}

function canUseChatCapabilities(model: ResponseProviderModel) {
  return !NON_CHAT_MODEL_REGEX.test(modelSearchText(model));
}

function isVisionModel(model: ResponseProviderModel) {
  return canUseChatCapabilities(model) && VISION_REGEX.test(modelSearchText(model));
}

function isWebSearchModel(model: ResponseProviderModel, providerType: ProviderType) {
  if (!canUseChatCapabilities(model)) {
    return false;
  }

  const searchText = modelSearchText(model);
  if (providerType === 'responses' || providerType === 'chat_completion') {
    return OPENAI_RESPONSE_WEB_SEARCH_REGEX.test(searchText) || WEB_SEARCH_REGEX.test(searchText);
  }
  if (providerType === 'claude') {
    return CLAUDE_WEB_SEARCH_REGEX.test(searchText);
  }
  if (providerType === 'gemini') {
    return GEMINI_WEB_SEARCH_REGEX.test(searchText);
  }
  return WEB_SEARCH_REGEX.test(searchText);
}

function isReasoningModel(model: ResponseProviderModel) {
  return canUseChatCapabilities(model) && REASONING_REGEX.test(modelSearchText(model));
}

function isFunctionCallingModel(model: ResponseProviderModel) {
  return canUseChatCapabilities(model) && TOOLS_REGEX.test(modelSearchText(model));
}

export function getProviderModelCapabilities(
  model: ResponseProviderModel,
  providerType: ProviderType,
): ProviderModelCapability[] {
  const capabilities: ProviderModelCapability[] = [];

  if (isVisionModel(model)) {
    capabilities.push({
      key: 'vision',
      icon: 'ph-eye',
      label: '视觉',
      tone: 'green',
    });
  }

  if (isWebSearchModel(model, providerType)) {
    capabilities.push({
      key: 'web_search',
      icon: 'ph-globe-hemisphere-west',
      label: '联网',
      tone: 'blue',
    });
  }

  if (isReasoningModel(model)) {
    capabilities.push({
      key: 'reasoning',
      icon: 'ph-brain',
      label: '推理',
      tone: 'violet',
    });
  }

  if (isFunctionCallingModel(model)) {
    capabilities.push({
      key: 'tools',
      icon: 'ph-wrench',
      label: '工具',
      tone: 'orange',
    });
  }

  return capabilities;
}
