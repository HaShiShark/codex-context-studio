import { analyzeExecSource, projectExecToolEvent } from './execToolDisplay';

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function assertEqual<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  }
}

function testDirectParallelCalls(): void {
  const analysis = analyzeExecSource(`
    const results = await Promise.all([
      tools.shell_command({ command: "rg --files", workdir: "." }),
      tools.shell_command({ command: "git status --short", workdir: "." }),
    ]);
    text(results.length);
  `);

  assert(analysis, 'parses direct parallel calls');
  assert(analysis.exact, 'direct parallel count is exact');
  assertEqual(analysis.calls.length, 2, 'finds both direct calls');
  assertEqual(
    (analysis.calls[1].arguments as Record<string, unknown>).command,
    'git status --short',
    'keeps command arguments',
  );
}

function testStaticArrayMap(): void {
  const analysis = analyzeExecSource(`
    const commands = ["rg --files", "git status --short", "npm run typecheck"];
    const results = await Promise.all(commands.map(command =>
      tools.shell_command({ command, workdir: ".", timeout_ms: 20000 })
    ));
    results.forEach(text);
  `);

  assert(analysis, 'parses static array map');
  assert(analysis.exact, 'static map count is exact');
  assertEqual(analysis.calls.length, 3, 'expands every static map entry');
  assertEqual(
    (analysis.calls[2].arguments as Record<string, unknown>).command,
    'npm run typecheck',
    'binds map callback identifier',
  );
}

function testStaticTupleMap(): void {
  const analysis = analyzeExecSource(`
    const tasks = [
      ["files", "rg --files"],
      ["status", "git status --short"],
    ];
    const results = await Promise.all(tasks.map(async ([name, command]) => ({
      name,
      result: await tools.shell_command({ command, workdir: "." }),
    })));
    results.forEach(result => text(result.name));
  `);

  assert(analysis, 'parses static tuple map');
  assert(analysis.exact, 'tuple map count is exact');
  assertEqual(analysis.calls.length, 2, 'expands tuple map entries');
  assertEqual(
    (analysis.calls[0].arguments as Record<string, unknown>).command,
    'rg --files',
    'binds destructured tuple command',
  );
}

function testMixedNestedTools(): void {
  const analysis = analyzeExecSource(`
    await tools.shell_command({ command: "git status --short" });
    await tools.apply_patch("*** Begin Patch\\n*** End Patch");
  `);

  assert(analysis, 'parses mixed nested tools');
  assertEqual(analysis.calls.length, 2, 'keeps every nested tool type');
  assertEqual(analysis.calls[1].name, 'apply_patch', 'keeps freeform tool name');
}

function testProjectionUsesAggregateOutputOnlyForSingleCall(): void {
  const projected = projectExecToolEvent({
    name: 'exec',
    arguments: 'await tools.shell_command({ command: "rg --files" });',
    call_id: 'call-exec',
    raw_output: 'file-a.ts',
    display_result: 'file-a.ts',
    status: 'completed',
  });

  assert(projected, 'projects a static exec call');
  assertEqual(projected.length, 1, 'projects one nested event');
  assertEqual(projected[0].name, 'shell_command', 'uses nested tool name');
  assertEqual(projected[0].display_detail, 'rg --files', 'builds command preview');
  assertEqual(projected[0].raw_output, 'file-a.ts', 'single call safely reuses aggregate output');
}

function testDynamicLoopFallsBack(): void {
  const source = `
    for (const command of getCommands()) {
      await tools.shell_command({ command });
    }
  `;
  const analysis = analyzeExecSource(source);
  assert(analysis, 'detects nested call inside dynamic loop');
  assert(!analysis.exact, 'dynamic loop does not claim an exact count');
  assertEqual(
    projectExecToolEvent({ name: 'exec', arguments: source }),
    null,
    'dynamic loop keeps the outer exec display',
  );
}

function testDynamicBranchesFallBack(): void {
  const sources = [
    'enabled && await tools.shell_command({ command: "rg --files" });',
    'enabled ? await tools.shell_command({ command: "rg --files" }) : null;',
    'try { await tools.shell_command({ command: "rg --files" }); } catch {}',
  ];

  sources.forEach((source) => {
    const analysis = analyzeExecSource(source);
    assert(analysis, 'detects a nested call in dynamic control flow');
    assert(!analysis.exact, 'dynamic control flow does not claim an exact count');
    assertEqual(
      projectExecToolEvent({ name: 'exec', arguments: source, status: 'completed' }),
      null,
      'dynamic control flow keeps the outer exec display',
    );
  });
}

function testUncalledFunctionDoesNotCount(): void {
  const source = `
    const run = () => tools.shell_command({ command: "rg --files" });
    text("function prepared");
  `;
  assertEqual(analyzeExecSource(source), null, 'an uncalled function body is not execution evidence');
}

function testIncompleteExecFallsBack(): void {
  const source = 'await tools.shell_command({ command: "rg --files" });';
  assertEqual(
    projectExecToolEvent({ name: 'exec', arguments: source, status: 'error' }),
    null,
    'a failed exec does not claim every static command ran',
  );
  assertEqual(
    projectExecToolEvent({ name: 'exec', arguments: source, status: 'running' }),
    null,
    'a running exec does not claim commands have completed',
  );
}

function testInvalidSourceFallsBack(): void {
  assertEqual(analyzeExecSource('const = broken'), null, 'invalid JavaScript is not projected');
}

function main(): void {
  testDirectParallelCalls();
  testStaticArrayMap();
  testStaticTupleMap();
  testMixedNestedTools();
  testProjectionUsesAggregateOutputOnlyForSingleCall();
  testDynamicLoopFallsBack();
  testDynamicBranchesFallBack();
  testUncalledFunctionDoesNotCount();
  testIncompleteExecFallsBack();
  testInvalidSourceFallsBack();
  console.log('ok - exec display contract tests passed');
}

main();
