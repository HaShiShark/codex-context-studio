import type { ProviderType, ResponseProviderModel } from './types';

const modelLogoModules = import.meta.glob('./assets/images/models/*', {
  eager: true,
  import: 'default',
  query: '?url',
});

const modelLogoUrls = Object.fromEntries(
  Object.entries(modelLogoModules).map(([path, url]) => [path.split('/').pop() || path, String(url)]),
);

function asset(filename: string) {
  return modelLogoUrls[filename];
}

const logoRules: Array<[RegExp, string]> = [
  [/pixtral/i, 'pixtral.png'],
  [/jina/i, 'jina.png'],
  [/(abab|minimax|m2-her)/i, 'minimax.png'],
  [/veo/i, 'gemini.png'],
  [/(^|[-_/])o[134](?:[-_/]|$)/i, 'gpt_o1.png'],
  [/gpt-image/i, 'gpt_image_1.png'],
  [/gpt-3/i, 'gpt_3.5.png'],
  [/gpt-4/i, 'gpt_4.png'],
  [/gpt-5\.1-codex-mini/i, 'gpt-5.1-codex-mini.png'],
  [/gpt-5\.1-codex/i, 'gpt-5.1-codex.png'],
  [/gpt-5\.1-chat/i, 'gpt-5.1-chat.png'],
  [/gpt-5\.1/i, 'gpt-5.1.png'],
  [/gpt-5-mini/i, 'gpt-5-mini.png'],
  [/gpt-5-nano/i, 'gpt-5-nano.png'],
  [/gpt-5-chat/i, 'gpt-5-chat.png'],
  [/gpt-5-codex/i, 'gpt-5-codex.png'],
  [/gpt-5/i, 'gpt-5.png'],
  [/(gpts|gpt-oss|text-moderation|babbage-|davinci-|sora-|sora_|(^|\/)omni-|tts-1|whisper-)/i, 'chatgpt.jpeg'],
  [/(glm|chatglm)/i, 'chatglm.png'],
  [/deepseek/i, 'deepseek.png'],
  [/(qwen|qwq|qvq|wan-)/i, 'qwen.png'],
  [/gemma/i, 'gemma.png'],
  [/yi-/i, 'yi.png'],
  [/llama/i, 'llama.png'],
  [/mixtral|mistral|codestral|ministral|magistral/i, 'mixtral.png'],
  [/(moonshot|kimi)/i, 'moonshot.webp'],
  [/phi|microsoft|wizardlm/i, 'microsoft.png'],
  [/baichuan/i, 'baichuan.png'],
  [/(claude|anthropic-)/i, 'claude.png'],
  [/gemini/i, 'gemini.png'],
  [/bison|palm/i, 'palm.png'],
  [/step/i, 'step.png'],
  [/hailuo/i, 'hailuo.png'],
  [/(doubao|seedream|ep-202)/i, 'doubao.png'],
  [/(cohere|command)/i, 'cohere.png'],
  [/minicpm/i, 'minicpm.webp'],
  [/360/i, '360.png'],
  [/aimass/i, 'aimass.png'],
  [/codegeex/i, 'codegeex.png'],
  [/(copilot|creative|balanced|precise)/i, 'copilot.png'],
  [/dall?e/i, 'dalle.png'],
  [/dbrx/i, 'dbrx.png'],
  [/flashaudio|voice/i, 'flashaudio.png'],
  [/flux/i, 'flux.png'],
  [/grok/i, 'grok.png'],
  [/hunyuan/i, 'hunyuan.png'],
  [/internlm/i, 'internlm.png'],
  [/internvl/i, 'internvl.png'],
  [/llava/i, 'llava.png'],
  [/magic/i, 'magic.png'],
  [/midjourney|mj-/i, 'midjourney.png'],
  [/tao-|ernie-|Embedding-V1/i, 'wenxin.png'],
  [/stable-|sd2|sd3|sdxl/i, 'stability.png'],
  [/sparkdesk|generalv/i, 'sparkdesk.png'],
  [/hermes/i, 'nousresearch.png'],
  [/gryphe|mythomax/i, 'gryphe.png'],
  [/suno|chirp/i, 'suno.png'],
  [/luma/i, 'luma.png'],
  [/keling/i, 'keling.png'],
  [/vidu-/i, 'vidu.png'],
  [/ai21|jamba-/i, 'ai21.png'],
  [/nvidia/i, 'nvidia.png'],
  [/dianxin/i, 'dianxin.png'],
  [/tele/i, 'tele.png'],
  [/adept/i, 'adept.png'],
  [/aisingapore/i, 'aisingapore.png'],
  [/bigcode/i, 'bigcode.webp'],
  [/mediatek/i, 'mediatek.png'],
  [/upstage/i, 'upstage.png'],
  [/rakutenai/i, 'rakutenai.png'],
  [/ibm/i, 'ibm.png'],
  [/google\//i, 'google.png'],
  [/xirang/i, 'xirang.png'],
  [/hugging/i, 'huggingface.png'],
  [/embedding-3|cogview|zhipu/i, 'zhipu.png'],
  [/embedding|text-embedding/i, 'embedding.png'],
  [/perplexity|sonar/i, 'perplexity.png'],
  [/bge-/i, 'bge.webp'],
  [/voyage-/i, 'voyageai.png'],
  [/tokenflux/i, 'tokenflux.png'],
  [/nomic-/i, 'nomic.png'],
  [/longcat/i, 'longcat.svg'],
  [/bytedance/i, 'byte_dance.svg'],
  [/ling|ring/i, 'ling.png'],
  [/(V_1|V_1_TURBO|V_2|V_2A|V_2_TURBO|DESCRIBE|UPSCALE)/i, 'ideogram.svg'],
  [/mimo/i, 'mimo.svg'],
];

const providerFallbacks: Record<ProviderType, string> = {
  responses: 'chatgpt.jpeg',
  chat_completion: 'chatgpt.jpeg',
  gemini: 'gemini.png',
  claude: 'claude.png',
};

export function getProviderModelLogo(model: ResponseProviderModel, providerType: ProviderType) {
  const searchable = [model.id, model.label, model.provider, model.group].filter(Boolean).join(' ');
  for (const [regex, filename] of logoRules) {
    if (regex.test(searchable)) {
      return asset(filename);
    }
  }
  return asset(providerFallbacks[providerType]);
}

export function getProviderModelInitial(model: ResponseProviderModel) {
  const source = model.label || model.id || model.provider || model.group || 'M';
  return source.trim().slice(0, 1).toUpperCase();
}
