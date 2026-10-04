'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  getConfig: () => ipcRenderer.invoke('config:get'),
  saveConfig: (cfg) => ipcRenderer.invoke('config:save', cfg),

  startTask: (params) => ipcRenderer.invoke('task:start', params),
  stopTask: () => ipcRenderer.invoke('task:stop'),

  checkLogin: (opts) => ipcRenderer.invoke('login:check', { ...(opts || {}), mode: 'check' }),
  waitLogin: (opts) => ipcRenderer.invoke('login:check', { ...(opts || {}), mode: 'wait-login' }),

  openResult: () => ipcRenderer.invoke('result:open'),
  openFolder: () => ipcRenderer.invoke('folder:open'),
  openExternal: (url) => ipcRenderer.invoke('external:open', url),

  chooseDir: () => ipcRenderer.invoke('dialog:chooseDir'),
  chooseFile: (filters) => ipcRenderer.invoke('dialog:chooseFile', filters),

  envCheck: () => ipcRenderer.invoke('env:check'),
  refreshData: () => ipcRenderer.invoke('data:refresh'),

  generateLocator: (opts) => ipcRenderer.invoke('locator:generate', opts),
  openLocatorHtml: () => ipcRenderer.invoke('locator:open'),

  onTaskLine: (cb) => ipcRenderer.on('task:line', (_e, v) => cb(v)),
  onTaskStatus: (cb) => ipcRenderer.on('task:status', (_e, v) => cb(v)),
  onTaskExit: (cb) => ipcRenderer.on('task:exit', (_e, v) => cb(v)),
  onData: (cb) => ipcRenderer.on('data:snapshot', (_e, v) => cb(v)),
  onLoginStatus: (cb) => ipcRenderer.on('login:status', (_e, v) => cb(v)),
});
