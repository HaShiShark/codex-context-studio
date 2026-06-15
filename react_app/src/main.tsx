import React from 'react';
import ReactDOM from 'react-dom/client';

import phosphorBoldFontUrl from '@phosphor-icons/web/bold/Phosphor-Bold.woff2?url';
import phosphorLightFontUrl from '@phosphor-icons/web/light/Phosphor-Light.woff2?url';
import WorkbenchWindow from './WorkbenchWindow';
import './react-entry.css';
import './workbench-window.css';

const phosphorIconCodes: Record<string, string> = {
  'ph-brain': '\\e74e',
  'ph-broom': '\\ec54',
  'ph-caret-down': '\\e136',
  'ph-caret-right': '\\e13a',
  'ph-chart-bar': '\\e150',
  'ph-check': '\\e182',
  'ph-circle-notch': '\\eb44',
  'ph-copy': '\\e1ca',
  'ph-cpu': '\\e610',
  'ph-file-text': '\\e23a',
  'ph-gear': '\\e270',
  'ph-hand-pointing': '\\e29a',
  'ph-image': '\\e2ca',
  'ph-layout': '\\e6d6',
  'ph-lightbulb': '\\e2dc',
  'ph-lock-simple': '\\e308',
  'ph-minus': '\\e32a',
  'ph-paper-plane-tilt': '\\e398',
  'ph-square': '\\e45e',
  'ph-stop': '\\e46c',
  'ph-trash': '\\e4a6',
  'ph-x': '\\e4f6',
};

function installPhosphorSubset(): void {
  const iconRules = Object.entries(phosphorIconCodes)
    .map(([className, code]) => `.ph-light.${className}:before,.ph-bold.${className}:before{content:"${code}"}`)
    .join('');

  const style = document.createElement('style');
  style.textContent = `
@font-face{font-family:"Phosphor-Light";src:url("${phosphorLightFontUrl}") format("woff2");font-weight:normal;font-style:normal;font-display:block}
@font-face{font-family:"Phosphor-Bold";src:url("${phosphorBoldFontUrl}") format("woff2");font-weight:normal;font-style:normal;font-display:block}
.ph-light,.ph-bold{speak:never;font-style:normal;font-variant:normal;text-transform:none;line-height:1;letter-spacing:0;-webkit-font-feature-settings:"liga";font-feature-settings:"liga";-webkit-font-variant-ligatures:discretionary-ligatures;font-variant-ligatures:discretionary-ligatures;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.ph-light{font-family:"Phosphor-Light"!important;font-weight:normal}
.ph-bold{font-family:"Phosphor-Bold"!important;font-weight:normal}
${iconRules}`;
  document.head.appendChild(style);
}

installPhosphorSubset();

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <WorkbenchWindow />
  </React.StrictMode>,
);
