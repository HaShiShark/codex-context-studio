const { app, BrowserWindow, dialog, ipcMain, screen } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

function readPort(name, fallback) {
  const value = Number(process.env[name] || fallback);
  return Number.isInteger(value) && value > 0 && value < 65536 ? value : fallback;
}

const HOST = process.env.HASH_CONTEXT_HOST || 'localhost';
const PROBE_HOST = process.env.HASH_CONTEXT_PROBE_HOST || (HOST === 'localhost' ? '127.0.0.1' : HOST);
const BACKEND_PORT = readPort('HASH_WEB_PORT', 8765);
const FRONTEND_PORT = readPort('HASH_CONTEXT_FRONTEND_PORT', 5174);
const PROXY_PORT = readPort('HASH_CONTEXT_PROXY_PORT', 8787);
const CONTROL_PORT = readPort('HASH_CONTEXT_CONTROL_PORT', 8790);
const USE_VITE_FRONTEND = !app.isPackaged && process.env.HASH_CONTEXT_USE_BUILT_FRONTEND !== '1';
const MIN_WINDOW_WIDTH = 600;
const MIN_WINDOW_HEIGHT = 360;
const LIGHT_WINDOW_ACCENT_COLOR = '#f8f5f1';
const DARK_WINDOW_ACCENT_COLOR = '#211c18';

app.setPath('userData', path.join(app.getPath('appData'), 'hash-context-codex-lab'));

let mainWindow = null;
let backendProcess = null;
let frontendProcess = null;
let proxyProcess = null;
let controlServer = null;
let isQuitting = false;

function appRoot() {
  return path.resolve(__dirname, '..');
}

function writeLog(message) {
  const logDir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(logDir, { recursive: true });
  fs.appendFileSync(
    path.join(logDir, 'electron-window.log'),
    `${new Date().toISOString()} ${message}\n`,
    'utf8',
  );
}

function requestOk(port, pathname = '/', hostname = HOST) {
  return new Promise((resolve) => {
    const req = http.get(
      {
        hostname,
        port,
        path: pathname,
        timeout: 1000,
      },
      (res) => {
        res.resume();
        resolve(Boolean(res.statusCode && res.statusCode < 500));
      },
    );

    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET,POST,OPTIONS',
    'access-control-allow-headers': 'content-type',
  });
  res.end(JSON.stringify(payload));
}

function showWindow(options = {}) {
  if (!mainWindow) {
    return false;
  }

  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }

  ensureWindowOnVisibleDisplay();
  mainWindow.show();
  mainWindow.moveTop();
  mainWindow.focus();
  bringWindowToFront();
  const sessionId = typeof options.sessionId === 'string' ? options.sessionId.trim() : '';
  const detail = JSON.stringify({ sessionId });
  const sessionLiteral = JSON.stringify(sessionId);
  mainWindow.webContents.executeJavaScript(
    `
      if (${sessionLiteral}) {
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set('session_id', ${sessionLiteral});
        window.history.replaceState(null, '', nextUrl.pathname + nextUrl.search + nextUrl.hash);
      }
      window.dispatchEvent(new CustomEvent('hash-context-window-show', { detail: ${detail} }));
    `,
    true,
  ).catch((error) => {
    writeLog(`show refresh dispatch failed: ${error instanceof Error ? error.message : String(error)}`);
  });
  return true;
}

function ensureWindowOnVisibleDisplay() {
  if (!mainWindow) {
    return;
  }

  const bounds = mainWindow.getBounds();
  const displays = screen.getAllDisplays();
  const intersectsDisplay = displays.some((display) => {
    const area = display.workArea;
    return (
      bounds.x < area.x + area.width &&
      bounds.x + bounds.width > area.x &&
      bounds.y < area.y + area.height &&
      bounds.y + bounds.height > area.y
    );
  });

  if (intersectsDisplay) {
    return;
  }

  const display = screen.getDisplayNearestPoint({ x: 0, y: 0 });
  const area = display.workArea;
  const width = Math.min(Math.max(bounds.width, MIN_WINDOW_WIDTH), area.width);
  const height = Math.min(Math.max(bounds.height, MIN_WINDOW_HEIGHT), area.height);
  mainWindow.setBounds(
    {
      x: Math.round(area.x + (area.width - width) / 2),
      y: Math.round(area.y + (area.height - height) / 2),
      width,
      height,
    },
    false,
  );
  writeLog('window was off-screen; moved to nearest display');
}

function bringWindowToFront() {
  if (!mainWindow) {
    return;
  }

  mainWindow.setAlwaysOnTop(true, 'screen-saver');
  mainWindow.setAlwaysOnTop(false);
}

function startControlServer() {
  if (controlServer) {
    return;
  }

  controlServer = http.createServer((req, res) => {
    const url = new URL(req.url || '/', `http://${HOST}:${CONTROL_PORT}`);

    if (req.method === 'OPTIONS') {
      sendJson(res, 200, { ok: true });
      return;
    }

    if (req.method === 'GET' && url.pathname === '/health') {
      sendJson(res, 200, { ok: true, visible: Boolean(mainWindow && mainWindow.isVisible()) });
      return;
    }

    if (req.method === 'POST' && url.pathname === '/show') {
      const sessionId = (url.searchParams.get('session_id') || '').trim();
      sendJson(res, 200, { ok: showWindow({ sessionId }), session_id: sessionId });
      return;
    }

    if (req.method === 'POST' && url.pathname === '/hide') {
      mainWindow?.hide();
      sendJson(res, 200, { ok: true });
      return;
    }

    sendJson(res, 404, { ok: false, error: 'not found' });
  });

  controlServer.on('error', (error) => {
    writeLog(`control server error: ${error instanceof Error ? error.message : String(error)}`);
  });

  controlServer.listen(CONTROL_PORT, HOST, () => {
    writeLog(`control server ready http://${HOST}:${CONTROL_PORT}`);
  });
}

async function waitFor(port, pathname, label, timeoutMs = 30000, hostname = HOST) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    if (await requestOk(port, pathname, hostname)) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  throw new Error(`${label} did not become ready on ${hostname}:${port}.`);
}

function pythonCandidateWorks(candidate) {
  const result = spawnSync(
    candidate.command,
    [...candidate.args, '-c', 'import dotenv, zstandard'],
    { encoding: 'utf8', timeout: 5000, windowsHide: true },
  );
  return result.status === 0;
}

function localVenvPython(root) {
  return process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python');
}

function sourcePythonCandidates(root) {
  const localPython = localVenvPython(root);
  if (fs.existsSync(localPython)) {
    return [{ command: localPython, args: [] }];
  }

  const candidates = [];
  if (process.env.HASH_CONTEXT_PYTHON) {
    candidates.push({ command: process.env.HASH_CONTEXT_PYTHON, args: [] });
  }
  if (process.platform === 'win32') {
    candidates.push({ command: 'py', args: ['-3'] });
    candidates.push({ command: 'python', args: [] });
  } else {
    candidates.push({ command: 'python3', args: [] });
    candidates.push({ command: 'python', args: [] });
  }

  return candidates;
}

function pythonScriptCommand(root, scriptName) {
  for (const candidate of sourcePythonCandidates(root)) {
    if (pythonCandidateWorks(candidate)) {
      return { command: candidate.command, args: [...candidate.args, scriptName] };
    }
  }

  const localPython = localVenvPython(root);
  if (fs.existsSync(localPython)) {
    throw new Error(
      `Project .venv at ${localPython} is missing required dependencies (dotenv, zstandard). ` +
        'Run npm run setup:python to repair it, then try again.',
    );
  }
  throw new Error('No usable Python runtime found. Run npm run setup:python or set HASH_CONTEXT_PYTHON to a Python with the project dependencies installed.');
}

function pythonServerCommand(root, scriptName, exeName) {
  const preferSource =
    process.env.HASH_CONTEXT_PREFER_SOURCE_SERVERS === '1' ||
    (!app.isPackaged && process.env.HASH_CONTEXT_USE_BUNDLED_PYTHON !== '1');
  if (preferSource) {
    return pythonScriptCommand(root, scriptName);
  }

  const candidates = process.platform === 'win32'
    ? [
        path.join(root, 'python_dist', exeName, `${exeName}.exe`),
        path.join(root, 'python_dist', `${exeName}.exe`),
      ]
    : [
        path.join(root, 'python_dist', exeName),
        path.join(root, 'python_dist', exeName, exeName),
      ];

  for (const bundledExecutable of candidates) {
    if (fs.existsSync(bundledExecutable)) {
      return { command: bundledExecutable, args: [] };
    }
  }

  return pythonScriptCommand(root, scriptName);
}

function pythonPathForRoot(root) {
  const existing = process.env.PYTHONPATH;
  if (typeof existing === 'string' && existing.trim()) {
    return `${root}${path.delimiter}${existing}`;
  }
  return root;
}

function cleanEnv(extra = {}) {
  const env = {};
  for (const [key, value] of Object.entries({ ...process.env, ...extra })) {
    if (typeof value === 'string') {
      env[key] = value;
    }
  }
  return env;
}

function pipeChildLogs(child, label) {
  child.stdout.on('data', (chunk) => {
    const text = chunk.toString().trim();
    console.log(`[${label}] ${text}`);
    writeLog(`[${label}] ${text}`);
  });
  child.stderr.on('data', (chunk) => {
    const text = chunk.toString().trim();
    console.error(`[${label}] ${text}`);
    writeLog(`[${label}:error] ${text}`);
  });
}

async function startBackend(root) {
  writeLog('checking backend');
  if (await requestOk(BACKEND_PORT, '/api/health', PROBE_HOST)) {
    writeLog('backend already running');
    return;
  }

  writeLog('starting backend');
  const serverCommand = pythonServerCommand(root, path.join('backend', 'proxy_server.py'), 'hash-web-server');
  backendProcess = spawn(serverCommand.command, serverCommand.args, {
    cwd: root,
    env: cleanEnv({
      HASH_WEB_HOST: HOST,
      HASH_WEB_PORT: String(BACKEND_PORT),
      HASH_DATA_DIR: path.join(app.getPath('userData'), 'data'),
      PYTHONIOENCODING: 'utf-8',
      PYTHONPATH: pythonPathForRoot(root),
    }),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  pipeChildLogs(backendProcess, 'backend');

  await waitFor(BACKEND_PORT, '/api/health', 'Backend', 30000, PROBE_HOST);
  writeLog('backend ready');
}

async function startProxy(root) {
  writeLog('checking proxy');
  if (await requestOk(PROXY_PORT, '/api/proxy/health', PROBE_HOST)) {
    writeLog('proxy already running');
    return;
  }

  writeLog('starting proxy');
  const serverCommand = pythonServerCommand(root, path.join('backend', 'proxy_server.py'), 'hash-proxy-server');
  proxyProcess = spawn(serverCommand.command, serverCommand.args, {
    cwd: root,
    env: cleanEnv({
      HASH_CONTEXT_PROXY_HOST: HOST,
      HASH_CONTEXT_PROXY_PORT: String(PROXY_PORT),
      HASH_CONTEXT_PROXY_DATA_DIR: path.join(app.getPath('userData'), 'data'),
      PYTHONIOENCODING: 'utf-8',
      PYTHONPATH: pythonPathForRoot(root),
    }),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  pipeChildLogs(proxyProcess, 'proxy');

  await waitFor(PROXY_PORT, '/api/proxy/health', 'Proxy', 30000, PROBE_HOST);
  writeLog('proxy ready');
}

async function startFrontend(root) {
  writeLog('checking frontend');
  if (await requestOk(FRONTEND_PORT, '/')) {
    writeLog('frontend already running');
    return;
  }

  writeLog('starting frontend');
  const viteBin = path.join(root, 'node_modules', 'vite', 'bin', 'vite.js');
  frontendProcess = spawn(
    'node',
    [
      viteBin,
      '--config',
      path.join(root, 'react_app', 'vite.config.ts'),
      '--host',
      HOST,
      '--port',
      String(FRONTEND_PORT),
      '--strictPort',
    ],
    {
      cwd: root,
      env: cleanEnv(),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );

  pipeChildLogs(frontendProcess, 'frontend');

  await waitFor(FRONTEND_PORT, '/', 'Frontend');
  writeLog('frontend ready');
}

async function warmContextWorkbenchModels() {
  try {
    if (await requestOk(BACKEND_PORT, '/api/context-workbench-settings', PROBE_HOST)) {
      writeLog('context workbench models refreshed');
    }
  } catch (error) {
    writeLog(`context workbench model refresh skipped: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function iconPath(root) {
  const iconName = process.platform === 'win32' ? 'hash-icon.ico' : 'hash-icon.png';
  const localIcon = path.join(root, 'electron', 'assets', iconName);
  if (fs.existsSync(localIcon)) return localIcon;
  const localPng = path.join(root, 'electron', 'assets', 'hash-icon.png');
  if (fs.existsSync(localPng)) return localPng;
  return path.join(root, 'assets', 'hash-icon.png');
}

function normalizeWindowBounds(bounds) {
  if (!bounds || typeof bounds !== 'object') {
    return null;
  }

  const x = Number(bounds.x);
  const y = Number(bounds.y);
  const width = Math.max(MIN_WINDOW_WIDTH, Number(bounds.width));
  const height = Math.max(MIN_WINDOW_HEIGHT, Number(bounds.height));

  if (![x, y, width, height].every(Number.isFinite)) {
    return null;
  }

  return {
    x: Math.round(x),
    y: Math.round(y),
    width: Math.round(width),
    height: Math.round(height),
  };
}

function createWindow(root) {
  writeLog('creating window');
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: MIN_WINDOW_WIDTH,
    minHeight: MIN_WINDOW_HEIGHT,
    backgroundColor: '#f8f5f1',
    transparent: false,
    frame: false,
    hasShadow: true,
    roundedCorners: true,
    thickFrame: true,
    accentColor: LIGHT_WINDOW_ACCENT_COLOR,
    resizable: true,
    show: false,
    title: 'Codex Context Proxy',
    icon: iconPath(root),
    webPreferences: {
      preload: path.join(root, 'electron', 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  const publishWindowMaximizedState = () => {
    if (!mainWindow || mainWindow.webContents.isDestroyed()) {
      return;
    }

    mainWindow.webContents.send(
      'window:maximized-change',
      mainWindow.isMaximized() || mainWindow.isFullScreen(),
    );
  };

  mainWindow.on('maximize', publishWindowMaximizedState);
  mainWindow.on('unmaximize', publishWindowMaximizedState);
  mainWindow.on('enter-full-screen', publishWindowMaximizedState);
  mainWindow.on('leave-full-screen', publishWindowMaximizedState);
  mainWindow.on('restore', publishWindowMaximizedState);

  mainWindow.once('ready-to-show', () => {
    if (process.env.HASH_CONTEXT_START_HIDDEN === '1') {
      writeLog('window ready hidden');
      return;
    }

    showWindow();
  });

  mainWindow.on('close', (event) => {
    if (isQuitting || process.env.HASH_CONTEXT_CAPTURE_PATH) {
      return;
    }

    event.preventDefault();
    mainWindow.hide();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') {
      return;
    }

    if (input.key === 'Escape') {
      mainWindow?.close();
    }

    if ((input.control || input.meta) && input.shift && input.key.toLowerCase() === 'i') {
      mainWindow?.webContents.openDevTools({ mode: 'detach' });
      event.preventDefault();
    }
  });

  const frontendUrl = USE_VITE_FRONTEND
    ? `http://${HOST}:${FRONTEND_PORT}/`
    : `http://${HOST}:${BACKEND_PORT}/react/`;

  void mainWindow.loadURL(frontendUrl);
  writeLog(`loading ${frontendUrl}`);

  if (process.env.HASH_CONTEXT_CAPTURE_PATH) {
    mainWindow.webContents.once('did-finish-load', () => {
      setTimeout(async () => {
        const image = await mainWindow.webContents.capturePage();
        fs.writeFileSync(path.resolve(root, process.env.HASH_CONTEXT_CAPTURE_PATH), image.toPNG());
        app.quit();
      }, 4200);
    });
  }
}

function stopChild(child) {
  if (!child || child.killed) {
    return;
  }
  if (process.platform === 'win32' && child.pid) {
    spawnSync('taskkill', ['/pid', String(child.pid), '/t', '/f'], {
      stdio: 'ignore',
      windowsHide: true,
    });
    return;
  }
  child.kill();
}

async function boot() {
  const root = appRoot();

  writeLog(`boot root=${root}`);
  await Promise.all([startProxy(root), startBackend(root)]);
  await warmContextWorkbenchModels();
  if (USE_VITE_FRONTEND) {
    await startFrontend(root);
  } else {
    await waitFor(BACKEND_PORT, '/react/', 'React build');
  }
  createWindow(root);
  startControlServer();
}

app.whenReady().then(() => {
  writeLog('app ready');
  boot().catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    writeLog(`boot failed: ${message}`);
    dialog.showErrorBox('Codex Context Proxy failed to start', message);
    app.quit();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin' && isQuitting) {
    app.quit();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  writeLog('before quit');
  controlServer?.close();
  stopChild(frontendProcess);
  stopChild(backendProcess);
  stopChild(proxyProcess);
});

ipcMain.on('window:minimize', () => {
  mainWindow?.minimize();
});

ipcMain.on('window:maximize', () => {
  if (!mainWindow) {
    return;
  }

  if (mainWindow.isMaximized()) {
    mainWindow.unmaximize();
    return;
  }

  mainWindow.maximize();
});

ipcMain.on('window:close', () => {
  mainWindow?.hide();
});

ipcMain.handle('window:get-bounds', () => {
  return mainWindow?.getBounds() || null;
});

ipcMain.handle('window:is-maximized', () => {
  return Boolean(mainWindow && (mainWindow.isMaximized() || mainWindow.isFullScreen()));
});

ipcMain.on('window:set-theme-mode', (_event, themeMode) => {
  if (!mainWindow) {
    return;
  }

  const isDark = themeMode === 'dark';
  mainWindow.setBackgroundColor(isDark ? DARK_WINDOW_ACCENT_COLOR : LIGHT_WINDOW_ACCENT_COLOR);
  mainWindow.setAccentColor(isDark ? DARK_WINDOW_ACCENT_COLOR : LIGHT_WINDOW_ACCENT_COLOR);
});

ipcMain.on('window:set-bounds', (_event, bounds) => {
  if (!mainWindow) {
    return;
  }

  const nextBounds = normalizeWindowBounds(bounds);
  if (!nextBounds) {
    return;
  }

  if (mainWindow.isMaximized()) {
    mainWindow.unmaximize();
  }

  mainWindow.setBounds(nextBounds, false);
});
