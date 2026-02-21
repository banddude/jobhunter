const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const net = require("net");

const PREFERRED_PORT = 8888;
const POLL_INTERVAL_MS = 500;
const MAX_WAIT_MS = 30000;

let mainWindow = null;
let serverProcess = null;
let serverPort = PREFERRED_PORT;

function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(port, "0.0.0.0", () => {
      server.close(() => resolve(true));
    });
    server.on("error", () => resolve(false));
  });
}

async function findAvailablePort(start) {
  for (let port = start; port < start + 100; port++) {
    if (await isPortFree(port)) return port;
  }
  throw new Error("No available port found");
}

function findPython() {
  const venvPython = path.join(__dirname, "..", "applypilot", ".venv", "bin", "python3");
  try {
    require("fs").accessSync(venvPython, require("fs").constants.X_OK);
    return venvPython;
  } catch {
    return "python3";
  }
}

function startServer(port) {
  const pythonPath = findPython();
  const serverScript = path.join(__dirname, "..", "server.py");

  serverProcess = spawn(pythonPath, [serverScript, "--port", String(port)], {
    cwd: path.join(__dirname, ".."),
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, APPLYPILOT_PORT: String(port) },
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

function waitForServer(port) {
  const url = `http://localhost:${port}/api/system/check`;
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function poll() {
      const req = http.get(url, (res) => {
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

function createWindow(port) {
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

  mainWindow.loadURL(`http://localhost:${port}`);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.on("ready", async () => {
  try {
    serverPort = await findAvailablePort(PREFERRED_PORT);
  } catch (err) {
    dialog.showErrorBox("Startup Error", err.message);
    app.quit();
    return;
  }

  console.log(`Starting server on port ${serverPort}`);
  startServer(serverPort);

  try {
    await waitForServer(serverPort);
  } catch (err) {
    dialog.showErrorBox("Startup Error", err.message);
    app.quit();
    return;
  }

  createWindow(serverPort);
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
    createWindow(serverPort);
  }
});
