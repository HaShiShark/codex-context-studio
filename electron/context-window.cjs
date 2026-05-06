const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const HOST = '127.0.0.1';
const BACKEND_PORT = 8765;
const FRONTEND_PORT = 5174;
const PROXY_PORT = 8787;
const CONTROL_PORT = Number(process.env.HASH_CONTEXT_CONTROL_PORT || 8790);
const USE_VITE_FRONTEND = !app.isPackaged && process.env.HASH_CONTEXT_USE_BUILT_FRONTEND !== '1';
const MIN_WINDOW_WIDTH = 760;
const MIN_WINDOW_HEIGHT = 520;

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

function requestOk(port, pathname = '/') {
  return new Promise((resolve) => {
    const req = http.get(
      {
        hostname: HOST,
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
  mainWindow.show();
  mainWindow.focus();
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

async function waitFor(port, pathname, label, timeoutMs = 30000) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    if (await requestOk(port, pathname)) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  throw new Error(`${label} did not become ready on ${HOST}:${port}.`);
}

function pythonCommand(root) {
  const localPython = path.join(root, '.venv', 'Scripts', 'python.exe');
  return fs.existsSync(localPython) ? localPython : 'python';
}

function pythonServerCommand(root, scriptName, exeName) {
  const bundledExe = path.join(root, 'python_dist', exeName, `${exeName}.exe`);
  if (fs.existsSync(bundledExe)) {
    return { command: bundledExe, args: [] };
  }

  return { command: pythonCommand(root), args: [scriptName] };
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

async function startBackend(root) {
  writeLog('checking backend');
  if (await requestOk(BACKEND_PORT, '/api/init')) {
    writeLog('backend already running');
    return;
  }

  writeLog('starting backend');
  const serverCommand = pythonServerCommand(root, 'web_server.py', 'hash-web-server');
  backendProcess = spawn(serverCommand.command, serverCommand.args, {
    cwd: root,
    env: cleanEnv({
      HASH_WEB_HOST: HOST,
      HASH_WEB_PORT: String(BACKEND_PORT),
      HASH_DATA_DIR: path.join(app.getPath('userData'), 'data'),
      PYTHONIOENCODING: 'utf-8',
    }),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  backendProcess.stdout.on('data', (chunk) => {
    console.log(`[backend] ${chunk.toString().trim()}`);
    writeLog(`[backend] ${chunk.toString().trim()}`);
  });
  backendProcess.stderr.on('data', (chunk) => {
    console.error(`[backend] ${chunk.toString().trim()}`);
    writeLog(`[backend:error] ${chunk.toString().trim()}`);
  });

  await waitFor(BACKEND_PORT, '/api/init', 'Backend');
  writeLog('backend ready');
}

async function startProxy(root) {
  writeLog('checking proxy');
  if (await requestOk(PROXY_PORT, '/api/proxy/sessions')) {
    writeLog('proxy already running');
    return;
  }

  writeLog('starting proxy');
  const serverCommand = pythonServerCommand(root, 'proxy_server.py', 'hash-proxy-server');
  proxyProcess = spawn(serverCommand.command, serverCommand.args, {
    cwd: root,
    env: cleanEnv({
      HASH_CONTEXT_PROXY_HOST: HOST,
      HASH_CONTEXT_PROXY_PORT: String(PROXY_PORT),
      HASH_CONTEXT_PROXY_DATA_DIR: path.join(app.getPath('userData'), 'data'),
      PYTHONIOENCODING: 'utf-8',
    }),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  proxyProcess.stdout.on('data', (chunk) => {
    console.log(`[proxy] ${chunk.toString().trim()}`);
    writeLog(`[proxy] ${chunk.toString().trim()}`);
  });
  proxyProcess.stderr.on('data', (chunk) => {
    console.error(`[proxy] ${chunk.toString().trim()}`);
    writeLog(`[proxy:error] ${chunk.toString().trim()}`);
  });

  await waitFor(PROXY_PORT, '/api/proxy/sessions', 'Proxy');
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
      '--strictPort',
    ],
    {
      cwd: root,
      env: cleanEnv(),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );

  frontendProcess.stdout.on('data', (chunk) => {
    console.log(`[frontend] ${chunk.toString().trim()}`);
    writeLog(`[frontend] ${chunk.toString().trim()}`);
  });
  frontendProcess.stderr.on('data', (chunk) => {
    console.error(`[frontend] ${chunk.toString().trim()}`);
    writeLog(`[frontend:error] ${chunk.toString().trim()}`);
  });

  await waitFor(FRONTEND_PORT, '/', 'Frontend');
  writeLog('frontend ready');
}

function iconPath(root) {
  const iconName = process.platform === 'win32' ? 'hash-icon.ico' : 'hash-icon.png';
  const localIcon = path.join(root, 'electron', 'assets', iconName);
  const copiedIcon = path.join(root, 'assets', 'hash-icon.png');
  return fs.existsSync(localIcon) ? localIcon : copiedIcon;
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
    width: 1221,
    height: 860,
    minWidth: MIN_WINDOW_WIDTH,
    minHeight: MIN_WINDOW_HEIGHT,
    backgroundColor: '#00000000',
    transparent: true,
    frame: false,
    roundedCorners: true,
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
  await startProxy(root);
  await startBackend(root);
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
