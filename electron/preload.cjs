const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  minimize: () => ipcRenderer.send('window:minimize'),
  maximize: () => ipcRenderer.send('window:maximize'),
  close: () => ipcRenderer.send('window:close'),
  getWindowBounds: () => ipcRenderer.invoke('window:get-bounds'),
  setWindowBounds: (bounds) => ipcRenderer.send('window:set-bounds', bounds),
  isElectron: true,
});
