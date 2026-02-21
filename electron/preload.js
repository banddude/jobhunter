const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("applypilot", {
  platform: process.platform,
  isElectron: true,
});
