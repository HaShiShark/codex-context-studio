import React from 'react';
import ReactDOM from 'react-dom/client';

import WorkbenchWindow from './WorkbenchWindow';
import './react-entry.css';
import './workbench-window.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <WorkbenchWindow />
  </React.StrictMode>,
);
