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
    setWindowBounds?: (bounds: ElectronWindowBounds) => void;
    selectFolder?: () => Promise<{ canceled: boolean; path?: string; name?: string }>;
    openProjectParentFolder?: (path: string) => Promise<{ ok: boolean; error?: string }>;
    isElectron?: boolean;
  };
}
