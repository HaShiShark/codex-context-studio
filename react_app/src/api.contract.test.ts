import {
  apiFetch,
  cancelContextChatRequest,
  extractErrorMessage,
  fetchContextWorkbenchSettings,
  proxyRealtimeUrl,
  refreshContextWorkbenchModelsRequest,
  resetProxyUsageRequest,
  streamContextChatRequest,
} from './api';

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

async function testResetProxyUsageUsesBackendFacade(): Promise<void> {
  const originalFetch = globalThis.fetch;
  let seenPath = '';
  let seenMethod = '';
  let seenBody = '';
  globalThis.fetch = async (input, init) => {
    seenPath = String(input);
    seenMethod = String(init?.method || 'GET');
    seenBody = String(init?.body || '');
    return jsonResponse({ cleared_count: 1, summary: {} }, { status: 200, statusText: 'OK' });
  };

  try {
    const result = await resetProxyUsageRequest('session-1');
    assertEqual(seenPath, '/api/proxy-session-usage-reset', 'reset usage goes through backend facade');
    assertEqual(seenMethod, 'POST', 'reset usage uses POST');
    assertEqual(seenBody, JSON.stringify({ session_id: 'session-1' }), 'reset usage sends session id body');
    assertEqual(result.cleared_count, 1, 'reset usage returns proxy payload');
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testCancelContextChatTargetsTheActiveRequest(): Promise<void> {
  const originalFetch = globalThis.fetch;
  let seenPath = '';
  let seenBody = '';
  globalThis.fetch = async (input, init) => {
    seenPath = String(input);
    seenBody = String(init?.body || '');
    return jsonResponse(
      { cancelled: true, completed: true, request_id: 'request-7', status: 'cancelled' },
      { status: 200, statusText: 'OK' },
    );
  };

  try {
    const result = await cancelContextChatRequest('session-3', 'request-7');
    assertEqual(seenPath, '/api/cancel-request', 'context cancellation uses the cancellation endpoint');
    assertEqual(
      seenBody,
      JSON.stringify({ session_id: 'session-3', request_id: 'request-7', mode: 'context' }),
      'context cancellation targets the exact active request',
    );
    assertEqual(result.completed, true, 'context cancellation waits for server completion confirmation');
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testWorkbenchSettingsAndModelRefreshUseSeparateGlobalEndpoints(): Promise<void> {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ path: string; method: string; body: string }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({
      path: String(input),
      method: String(init?.method || 'GET'),
      body: String(init?.body || ''),
    });
    return jsonResponse({ scope: 'global', settings: {}, providers: [] }, { status: 200, statusText: 'OK' });
  };

  try {
    await fetchContextWorkbenchSettings();
    await refreshContextWorkbenchModelsRequest('anthropic');
    assertEqual(requests[0]?.path, '/api/context-workbench-settings', 'settings load uses the global settings endpoint');
    assertEqual(requests[0]?.method, 'GET', 'settings load is read-only');
    assertEqual(
      requests[1]?.path,
      '/api/context-workbench-settings/refresh-models',
      'model refresh has a dedicated global endpoint',
    );
    assertEqual(requests[1]?.method, 'POST', 'model refresh uses POST');
    assertEqual(requests[1]?.body, JSON.stringify({ provider_id: 'anthropic' }), 'model refresh identifies one provider');
  } finally {
    globalThis.fetch = originalFetch;
  }
}

function testProxyRealtimeUrlUsesRuntimePort(): void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      location: {
        protocol: 'http:',
        hostname: '127.0.0.1',
      },
    },
  });

  try {
    assertEqual(
      proxyRealtimeUrl({ proxy_port: 9876, proxy_realtime_path: '/api/proxy/ws' }),
      'ws://127.0.0.1:9876/api/proxy/ws',
      'realtime url uses runtime proxy port',
    );
  } finally {
    if (originalWindow) {
      Object.defineProperty(globalThis, 'window', originalWindow);
    } else {
      delete (globalThis as { window?: Window }).window;
    }
  }
}

async function main(): Promise<void> {
  testExtractErrorMessage();
  await testApiFetchUsesStructuredErrorMessage();
  await testStreamRequestUsesStructuredErrorMessage();
  await testApiFetchUsesStatusFallbackForNonJsonError();
  await testResetProxyUsageUsesBackendFacade();
  await testCancelContextChatTargetsTheActiveRequest();
  await testWorkbenchSettingsAndModelRefreshUseSeparateGlobalEndpoints();
  testProxyRealtimeUrlUsesRuntimePort();
  console.log('ok - api error contract tests passed');
}

main().catch((error) => {
  console.error(error);
  throw error;
});
