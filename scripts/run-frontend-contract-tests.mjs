import { rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const outDir = '.tmp-tests/frontend-contract';

rmSync(outDir, { recursive: true, force: true });

const compile = spawnSync(
  'node',
  [
    'node_modules/typescript/bin/tsc',
    '--module',
    'NodeNext',
    '--moduleResolution',
    'NodeNext',
    '--target',
    'ES2020',
    '--lib',
    'ES2020,DOM,DOM.Iterable',
    '--skipLibCheck',
    '--strict',
    '--esModuleInterop',
    '--allowSyntheticDefaultImports',
    '--resolveJsonModule',
    '--rootDir',
    '.',
    '--outDir',
    outDir,
    'react_app/src/utils.ts',
    'react_app/src/contextTokenWeight.ts',
    'react_app/src/conversationTokenCounts.ts',
    'react_app/src/additionalToolsDisplay.ts',
    'react_app/src/types.ts',
    'react_app/src/api.ts',
    'react_app/src/execToolDisplay.ts',
    'react_app/src/assistantActivityDisplay.ts',
    'react_app/src/components/ContextWorkbench.helpers.ts',
    'react_app/src/api.contract.test.ts',
    'react_app/src/execToolDisplay.contract.test.ts',
    'react_app/src/assistantActivityDisplay.contract.test.ts',
    'react_app/src/additionalToolsDisplay.contract.test.ts',
    'react_app/src/utils.contract.test.ts',
    'react_app/src/components/ContextWorkbench.helpers.contract.test.ts',
  ],
  { stdio: 'inherit', shell: false },
);

if (compile.error) {
  console.error(compile.error.message);
  process.exit(1);
}
if ((compile.status ?? 1) !== 0) {
  process.exit(compile.status ?? 1);
}

const run = spawnSync('node', [`${outDir}/react_app/src/utils.contract.test.js`], { stdio: 'inherit', shell: false });
if (run.error) {
  console.error(run.error.message);
  process.exit(1);
}
if ((run.status ?? 1) !== 0) {
  process.exit(run.status ?? 1);
}

const runApi = spawnSync('node', [`${outDir}/react_app/src/api.contract.test.js`], { stdio: 'inherit', shell: false });
if (runApi.error) {
  console.error(runApi.error.message);
  process.exit(1);
}
if ((runApi.status ?? 1) !== 0) {
  process.exit(runApi.status ?? 1);
}

const runContextWorkbenchHelpers = spawnSync(
  'node',
  [`${outDir}/react_app/src/components/ContextWorkbench.helpers.contract.test.js`],
  { stdio: 'inherit', shell: false },
);
if (runContextWorkbenchHelpers.error) {
  console.error(runContextWorkbenchHelpers.error.message);
  process.exit(1);
}
if ((runContextWorkbenchHelpers.status ?? 1) !== 0) {
  process.exit(runContextWorkbenchHelpers.status ?? 1);
}

const runAssistantActivity = spawnSync(
  'node',
  [`${outDir}/react_app/src/assistantActivityDisplay.contract.test.js`],
  { stdio: 'inherit', shell: false },
);
if (runAssistantActivity.error) {
  console.error(runAssistantActivity.error.message);
  process.exit(1);
}
if ((runAssistantActivity.status ?? 1) !== 0) {
  process.exit(runAssistantActivity.status ?? 1);
}

const runAdditionalToolsDisplay = spawnSync(
  'node',
  [`${outDir}/react_app/src/additionalToolsDisplay.contract.test.js`],
  { stdio: 'inherit', shell: false },
);
if (runAdditionalToolsDisplay.error) {
  console.error(runAdditionalToolsDisplay.error.message);
  process.exit(1);
}
if ((runAdditionalToolsDisplay.status ?? 1) !== 0) {
  process.exit(runAdditionalToolsDisplay.status ?? 1);
}

const runExecDisplay = spawnSync(
  'node',
  [`${outDir}/react_app/src/execToolDisplay.contract.test.js`],
  { stdio: 'inherit', shell: false },
);
if (runExecDisplay.error) {
  console.error(runExecDisplay.error.message);
  process.exit(1);
}
process.exit(runExecDisplay.status ?? 1);
