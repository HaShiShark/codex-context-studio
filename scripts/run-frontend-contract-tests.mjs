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
    '--outDir',
    outDir,
    'react_app/src/utils.ts',
    'react_app/src/contextTokenWeight.ts',
    'react_app/src/types.ts',
    'react_app/src/utils.contract.test.ts',
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

const run = spawnSync('node', [`${outDir}/utils.contract.test.js`], { stdio: 'inherit', shell: false });
if (run.error) {
  console.error(run.error.message);
  process.exit(1);
}
process.exit(run.status ?? 1);
