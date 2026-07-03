/// <reference types="vite/client" />

interface ElectronWindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface Window {
  electronAPI?: {
    minimize?: () => void;
    maximize?: () => void;
    close?: () => void;
    getWindowBounds?: () => Promise<ElectronWindowBounds | null>;
    isWindowMaximized?: () => Promise<boolean>;
    setWindowThemeMode?: (themeMode: 'light' | 'dark') => void;
    onWindowMaximizedChange?: (callback: (isMaximized: boolean) => void) => () => void;
    setWindowBounds?: (bounds: ElectronWindowBounds) => void;
    isElectron?: boolean;
  };
}
