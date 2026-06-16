const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  minimize: () => ipcRenderer.send('window:minimize'),
  maximize: () => ipcRenderer.send('window:maximize'),
  close: () => ipcRenderer.send('window:close'),
  getWindowBounds: () => ipcRenderer.invoke('window:get-bounds'),
  isWindowMaximized: () => ipcRenderer.invoke('window:is-maximized'),
  setWindowThemeMode: (themeMode) => ipcRenderer.send('window:set-theme-mode', themeMode),
  onWindowMaximizedChange: (callback) => {
    if (typeof callback !== 'function') {
      return () => {};
    }
    const handler = (_event, isMaximized) => callback(Boolean(isMaximized));
    ipcRenderer.on('window:maximized-change', handler);
    return () => ipcRenderer.removeListener('window:maximized-change', handler);
  },
  setWindowBounds: (bounds) => ipcRenderer.send('window:set-bounds', bounds),
  isElectron: true,
});
