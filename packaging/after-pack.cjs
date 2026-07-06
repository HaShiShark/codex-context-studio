const { existsSync } = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function assertFile(filePath, label) {
  if (!existsSync(filePath)) {
    throw new Error(`${label} was not found: ${filePath}`);
  }
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit' });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${command} failed with exit code ${result.status}`);
  }
}

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== 'win32') {
    return;
  }

  const projectDir = context.packager.projectDir;
  const appInfo = context.packager.appInfo;
  const productName = appInfo.productName || 'Codex Context Proxy';
  const productFilename = appInfo.productFilename || productName;
  const version = appInfo.version;
  const exePath = path.join(context.appOutDir, `${productFilename}.exe`);
  const iconPath = path.join(projectDir, 'electron', 'assets', 'hash-icon.ico');
  const rceditPath = path.join(projectDir, 'node_modules', 'electron-winstaller', 'vendor', 'rcedit.exe');

  assertFile(exePath, 'Packaged executable');
  assertFile(iconPath, 'Executable icon');
  assertFile(rceditPath, 'rcedit executable');

  run(rceditPath, [
    exePath,
    '--set-icon',
    iconPath,
    '--set-version-string',
    'ProductName',
    productName,
    '--set-version-string',
    'FileDescription',
    productName,
    '--set-version-string',
    'CompanyName',
    productName,
    '--set-file-version',
    version,
    '--set-product-version',
    version,
  ]);
};
