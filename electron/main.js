const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");

const SERVER_PORT = 8888;
const SERVER_URL = `http://localhost:${SERVER_PORT}`;
const POLL_INTERVAL_MS = 500;
const MAX_WAIT_MS = 30000;

let mainWindow = null;
let serverProcess = null;

function findPython() {
  const venvPython = path.join(__dirname, "..", "applypilot", ".venv", "bin", "python3");
  try {
    require("fs").accessSync(venvPython, require("fs").constants.X_OK);
    return venvPython;
  } catch {
    return "python3";
  }
}

function startServer() {
  const pythonPath = findPython();
  const serverScript = path.join(__dirname, "..", "server.py");

  serverProcess = spawn(pythonPath, [serverScript], {
    cwd: path.join(__dirname, ".."),
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env },
  });

  serverProcess.stdout.on("data", (data) => {
    process.stdout.write(`[server] ${data}`);
  });

  serverProcess.stderr.on("data", (data) => {
    process.stderr.write(`[server] ${data}`);
  });

  serverProcess.on("error", (err) => {
    dialog.showErrorBox(
      "Server Error",
      `Failed to start the Python server.\n\n${err.message}\n\nMake sure Python 3.11+ is installed and the virtual environment is set up:\n  cd applypilot && python3 -m venv .venv && source .venv/bin/activate && pip install -e .`
    );
    app.quit();
  });

  serverProcess.on("exit", (code, signal) => {
    if (mainWindow && !app.isQuitting) {
      dialog.showErrorBox(
        "Server Stopped",
        `The Python server exited unexpectedly (code: ${code}, signal: ${signal}).\n\nCheck the terminal output for details.`
      );
      app.quit();
    }
  });
}

function waitForServer() {
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function poll() {
      const req = http.get(`${SERVER_URL}/api/system/check`, (res) => {
        if (res.statusCode === 200) {
          resolve();
        } else {
          retry();
        }
      });

      req.on("error", () => retry());
      req.setTimeout(2000, () => {
        req.destroy();
        retry();
      });
    }

    function retry() {
      if (Date.now() - start > MAX_WAIT_MS) {
        reject(new Error(`Server did not respond within ${MAX_WAIT_MS / 1000} seconds`));
        return;
      }
      setTimeout(poll, POLL_INTERVAL_MS);
    }

    poll();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    title: "ApplyPilot",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(SERVER_URL);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.on("ready", async () => {
  startServer();

  try {
    await waitForServer();
  } catch (err) {
    dialog.showErrorBox("Startup Error", err.message);
    app.quit();
    return;
  }

  createWindow();
});

app.on("before-quit", () => {
  app.isQuitting = true;
});

app.on("will-quit", () => {
  if (serverProcess && !serverProcess.killed) {
    serverProcess.kill("SIGTERM");
    setTimeout(() => {
      if (serverProcess && !serverProcess.killed) {
        serverProcess.kill("SIGKILL");
      }
    }, 3000);
  }
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("activate", () => {
  if (mainWindow === null) {
    createWindow();
  }
});
