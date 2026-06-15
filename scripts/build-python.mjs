import { rmSync, existsSync } from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const buildVenv = path.join(root, '.build-venv');
const pythonDist = path.join(root, 'python_dist');
const webWork = path.join(root, '.tmp-pyinstaller-web');
const proxyWork = path.join(root, '.tmp-pyinstaller-proxy');
const isWindows = process.platform === 'win32';
const venvPython = isWindows
  ? path.join(buildVenv, 'Scripts', 'python.exe')
  : path.join(buildVenv, 'bin', 'python');
const systemPython = process.env.PYTHON || (isWindows ? 'python' : 'python3');

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    stdio: 'inherit',
    shell: false,
    ...options,
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with exit code ${result.status}`);
  }
}

function removeWorkspacePath(target) {
  const resolvedRoot = path.resolve(root);
  const resolvedTarget = path.resolve(target);
  const relative = path.relative(resolvedRoot, resolvedTarget);

  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`Refusing to remove path outside workspace: ${resolvedTarget}`);
  }

  rmSync(resolvedTarget, { recursive: true, force: true });
}

function pyInstallerArgs(name, scriptPath, workPath) {
  const args = [
    '-m',
    'PyInstaller',
    '--noconfirm',
    '--clean',
    '--distpath',
    pythonDist,
    '--workpath',
    workPath,
    '--specpath',
    workPath,
    '--name',
    name,
  ];

  if (!isWindows) {
    args.push('--onefile');
  }

  args.push(scriptPath);
  return args;
}

if (!existsSync(venvPython)) {
  run(systemPython, ['-m', 'venv', buildVenv]);
}

run(venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip']);
run(venvPython, ['-m', 'pip', 'install', '-r', path.join(root, 'requirements.txt'), 'pyinstaller']);

removeWorkspacePath(pythonDist);
removeWorkspacePath(webWork);
removeWorkspacePath(proxyWork);

run(venvPython, pyInstallerArgs('hash-web-server', path.join(root, 'backend', 'web_server.py'), webWork));
run(venvPython, pyInstallerArgs('hash-proxy-server', path.join(root, 'backend', 'proxy_server.py'), proxyWork));
