import {
  buildWorkbenchModelOptions,
  DEFAULT_CONTEXT_WORKBENCH_MODEL,
  preferredProviderModel,
} from './ContextWorkbench.helpers';

function assertEqual<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  }
}

function model(id: string) {
  return { id, label: id, group: 'Codex', provider: 'Codex' };
}

function testSavedProviderModelWinsOverFetchedListOrder(): void {
  assertEqual(
    preferredProviderModel({
      default_model: 'gpt-5.6-sol',
      models: [model('gpt-5.2'), model('gpt-5.6-sol')],
    }),
    'gpt-5.6-sol',
    'first-use Codex default is not replaced by fetched list order',
  );

  assertEqual(
    preferredProviderModel({
      default_model: 'gpt-5.5',
      models: [model('gpt-5.2'), model('gpt-5.6-sol')],
    }),
    'gpt-5.5',
    'an explicitly saved provider model remains selected',
  );
}

function testFetchedModelIsOnlyFallbackWithoutSavedSelection(): void {
  assertEqual(
    preferredProviderModel({
      default_model: '',
      models: [model('gpt-5.6-sol'), model('gpt-5.5')],
    }),
    'gpt-5.6-sol',
    'provider list supplies a model only when no saved selection exists',
  );
  assertEqual(preferredProviderModel(null), '', 'missing provider has no preferred model');
}

function testExternalProviderModelsNeverReceiveCodexDefaults(): void {
  const options = buildWorkbenchModelOptions(
    'deepseek-v4-pro',
    [
      { id: 'deepseek-v4-flash', label: 'deepseek-v4-flash', group: 'DeepSeek', provider: 'DeepSeek' },
      { id: 'deepseek-v4-pro', label: 'deepseek-v4-pro', group: 'DeepSeek', provider: 'DeepSeek' },
    ],
    'DeepSeek',
  );
  assertEqual(
    options.map((option) => option.id).join(','),
    'deepseek-v4-flash,deepseek-v4-pro',
    'an external provider only shows models returned by that provider',
  );
  assertEqual(
    options.some((option) => option.id.startsWith('gpt-')),
    false,
    'Codex defaults do not leak into an external provider',
  );
}

function testMissingConfiguredModelIsMarkedInsteadOfDisguisedAsFetched(): void {
  const options = buildWorkbenchModelOptions(
    'claude-sonnet-4-5',
    [{ id: 'deepseek-v4-pro', label: 'deepseek-v4-pro', group: 'DeepSeek', provider: 'DeepSeek' }],
    'DeepSeek',
  );
  assertEqual(options[0]?.id, 'claude-sonnet-4-5', 'missing configured model remains visible');
  assertEqual(options[0]?.source, 'configured', 'missing configured model is explicitly marked');
  assertEqual(options[1]?.source, 'provider', 'provider-returned models keep their source');
}

function testCodexFirstUseDefaultRemains56Sol(): void {
  assertEqual(DEFAULT_CONTEXT_WORKBENCH_MODEL, 'gpt-5.6-sol', 'frontend first-use default matches Codex');
}

testSavedProviderModelWinsOverFetchedListOrder();
testFetchedModelIsOnlyFallbackWithoutSavedSelection();
testExternalProviderModelsNeverReceiveCodexDefaults();
testMissingConfiguredModelIsMarkedInsteadOfDisguisedAsFetched();
testCodexFirstUseDefaultRemains56Sol();
console.log('ok - context workbench model preference contract tests passed');
