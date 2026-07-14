import { createWriteStream, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const packagingRoot = path.join(root, 'packaging');
const runtimeRoot = path.join(packagingRoot, 'dist', 'python');
const cacheRoot = path.join(packagingRoot, '.cache');
const releaseDir = path.join(root, 'release');
const pythonVersion = process.env.CODEX_CONTEXT_STUDIO_PACKAGE_PYTHON_VERSION || '3.11.9';
const pythonTag = pythonVersion.replace(/\./g, '');
const pythonZipName = `python-${pythonVersion}-embed-amd64.zip`;
const pythonZipUrl = `https://www.python.org/ftp/python/${pythonVersion}/${pythonZipName}`;
const getPipUrl = 'https://bootstrap.pypa.io/get-pip.py';

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    stdio: 'inherit',
    ...options,
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with exit code ${result.status}`);
  }
}

function assertWorkspacePath(target) {
  const resolvedRoot = path.resolve(root);
  const resolvedTarget = path.resolve(target);
  const relative = path.relative(resolvedRoot, resolvedTarget);
  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`Refusing to touch path outside workspace: ${resolvedTarget}`);
  }
  return resolvedTarget;
}

function removeWorkspacePath(target) {
  rmSync(assertWorkspacePath(target), { recursive: true, force: true });
}

function parseTarget() {
  const targetIndex = process.argv.indexOf('--target');
  const target = targetIndex >= 0 ? process.argv[targetIndex + 1] : 'installer';
  if (!['installer', 'dir'].includes(target)) {
    throw new Error('Usage: node packaging/build.mjs --target <installer|dir>');
  }
  return target;
}

async function downloadFile(url, destination, redirects = 0) {
  if (existsSync(destination)) {
    return;
  }
  if (redirects > 5) {
    throw new Error(`Too many redirects while downloading ${url}`);
  }

  mkdirSync(path.dirname(destination), { recursive: true });
  await new Promise((resolve, reject) => {
    const request = https.get(url, (response) => {
      if (
        response.statusCode &&
        response.statusCode >= 300 &&
        response.statusCode < 400 &&
        response.headers.location
      ) {
        response.resume();
        const nextUrl = new URL(response.headers.location, url).toString();
        downloadFile(nextUrl, destination, redirects + 1).then(resolve, reject);
        return;
      }

      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`Download failed ${response.statusCode}: ${url}`));
        return;
      }

      const file = createWriteStream(destination);
      response.pipe(file);
      file.on('finish', () => file.close(resolve));
      file.on('error', reject);
    });

    request.on('error', reject);
  });
}

function expandZip(zipPath, destination) {
  mkdirSync(destination, { recursive: true });
  const zipLiteral = `'${zipPath.replace(/'/g, "''")}'`;
  const destinationLiteral = `'${destination.replace(/'/g, "''")}'`;
  run('powershell', [
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-Command',
    `Expand-Archive -LiteralPath ${zipLiteral} -DestinationPath ${destinationLiteral} -Force`,
  ]);
}

function configureEmbeddedPython() {
  const pthPath = path.join(runtimeRoot, `python${pythonTag.slice(0, 3)}._pth`);
  if (!existsSync(pthPath)) {
    throw new Error(`Python path file was not found: ${pthPath}`);
  }

  const lines = readFileSync(pthPath, 'utf8')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && line !== '#import site' && line !== 'import site');

  for (const entry of ['Lib/site-packages', '..']) {
    if (!lines.includes(entry)) {
      lines.push(entry);
    }
  }
  lines.push('import site');
  writeFileSync(pthPath, `${lines.join('\r\n')}\r\n`, 'utf8');
}

async function preparePythonRuntime() {
  if (process.platform !== 'win32') {
    throw new Error('The new packaging pipeline currently targets Windows only.');
  }

  const pythonZipPath = path.join(cacheRoot, pythonZipName);
  const getPipPath = path.join(cacheRoot, 'get-pip.py');
  await downloadFile(pythonZipUrl, pythonZipPath);
  await downloadFile(getPipUrl, getPipPath);

  removeWorkspacePath(runtimeRoot);
  expandZip(pythonZipPath, runtimeRoot);
  configureEmbeddedPython();

  const pythonExe = path.join(runtimeRoot, 'python.exe');
  run(pythonExe, [getPipPath, '--no-warn-script-location']);
  run(pythonExe, [
    '-m',
    'pip',
    'install',
    '--disable-pip-version-check',
    '--no-cache-dir',
    '--no-warn-script-location',
    '-r',
    path.join(root, 'requirements.txt'),
  ]);
  run(pythonExe, [
    '-c',
    'import dotenv, zstandard, brotli, fastapi, uvicorn, httpx, openai, tiktoken',
  ]);
}

function buildReact() {
  run(process.execPath, [
    path.join(root, 'node_modules', 'vite', 'bin', 'vite.js'),
    'build',
    '--config',
    path.join(root, 'react_app', 'vite.config.ts'),
  ]);
}

function buildElectron(target) {
  const builder = path.join(root, 'node_modules', 'electron-builder', 'cli.js');
  const args = [
    '--config',
    path.join('packaging', 'electron-builder.json'),
    '--win',
    '--x64',
    '--publish',
    'never',
  ];

  if (target === 'dir') {
    args.push('--dir');
  }

  run(process.execPath, [builder, ...args]);
}

async function main() {
  const target = parseTarget();
  removeWorkspacePath(releaseDir);
  await preparePythonRuntime();
  buildReact();
  buildElectron(target);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
