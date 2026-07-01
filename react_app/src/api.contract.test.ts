import { apiFetch, extractErrorMessage, streamContextChatRequest } from './api';

function assertEqual<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  }
}

async function assertRejectsMessage(action: () => Promise<unknown>, expected: string, message: string): Promise<void> {
  try {
    await action();
  } catch (error) {
    assertEqual(error instanceof Error ? error.message : String(error), expected, message);
    return;
  }

  throw new Error(`${message}: expected rejection`);
}

function jsonResponse(body: unknown, init: ResponseInit): Response {
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  return new Response(JSON.stringify(body), {
    ...init,
    headers,
  });
}

function testExtractErrorMessage(): void {
  assertEqual(
    extractErrorMessage({ error: { message: 'Auth missing', code: 'x' } }, 'HTTP 401'),
    'Auth missing',
    'prefers structured error.message',
  );
  assertEqual(
    extractErrorMessage({ error: 'simple' }, 'HTTP 400'),
    'simple',
    'keeps string error',
  );
  assertEqual(
    extractErrorMessage({ message: 'top-level' }, 'HTTP 400'),
    'top-level',
    'uses top-level message',
  );
  assertEqual(
    extractErrorMessage({ error: { code: 'auth_missing', type: 'auth_error' } }, 'HTTP 401'),
    'auth_missing / auth_error',
    'falls back to structured error metadata',
  );
  assertEqual(
    extractErrorMessage({}, 'HTTP 500'),
    'HTTP 500',
    'uses fallback when no structured message exists',
  );
}

async function testApiFetchUsesStructuredErrorMessage(): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse(
    { error: { message: 'Auth missing', code: 'x' } },
    { status: 401, statusText: 'Unauthorized' },
  );

  try {
    await assertRejectsMessage(
      () => apiFetch('/api/protected'),
      'Auth missing',
      'apiFetch surfaces structured error.message',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testStreamRequestUsesStructuredErrorMessage(): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse(
    { error: { message: 'Stream auth missing', code: 'x' } },
    { status: 401, statusText: 'Unauthorized' },
  );

  try {
    await assertRejectsMessage(
      () => streamContextChatRequest({ session_id: 's1', message: 'hello' }, () => {}),
      'Stream auth missing',
      'streamContextChatRequest surfaces structured error.message',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testApiFetchUsesStatusFallbackForNonJsonError(): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response('not json', { status: 502, statusText: 'Bad Gateway' });

  try {
    await assertRejectsMessage(
      () => apiFetch('/api/bad-gateway'),
      'Bad Gateway',
      'apiFetch uses status fallback for non-JSON errors',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function main(): Promise<void> {
  testExtractErrorMessage();
  await testApiFetchUsesStructuredErrorMessage();
  await testStreamRequestUsesStructuredErrorMessage();
  await testApiFetchUsesStatusFallbackForNonJsonError();
  console.log('ok - api error contract tests passed');
}

main().catch((error) => {
  console.error(error);
  throw error;
});
