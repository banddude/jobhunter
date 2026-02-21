const { app, BrowserWindow, dialog } = require("electron");
const { spawn, execFileSync, execSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const http = require("http");
const net = require("net");

const PREFERRED_PORT = 8888;
const POLL_INTERVAL_MS = 500;
const MAX_WAIT_MS = 30000;
const VENV_DIR = path.join(os.homedir(), ".applypilot", ".venv");
const VENV_PYTHON = path.join(VENV_DIR, "bin", "python3");

let mainWindow = null;
let serverProcess = null;
let serverPort = PREFERRED_PORT;

function getResourcePath(...parts) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, ...parts);
  }
  return path.join(__dirname, "..", ...parts);
}

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
  const candidates = [
    VENV_PYTHON,
    getResourcePath("applypilot", ".venv", "bin", "python3"),
    "python3",
  ];
  for (const p of candidates) {
    try {
      fs.accessSync(p, fs.constants.X_OK);
      return p;
    } catch {}
  }
  return "python3";
}

function hasDeps(pythonPath) {
  try {
    execFileSync(pythonPath, ["-c", "import fastapi, uvicorn, yaml"], {
      timeout: 10000,
      stdio: "ignore",
    });
    return true;
  } catch {
    return false;
  }
}

function ensurePythonEnv() {
  const python = findPython();
  if (hasDeps(python)) return python;

  if (fs.existsSync(VENV_PYTHON) && hasDeps(VENV_PYTHON)) return VENV_PYTHON;

  console.log("Setting up Python environment at ~/.applypilot/.venv ...");

  try {
    fs.mkdirSync(path.join(os.homedir(), ".applypilot"), { recursive: true });

    execFileSync("python3", ["-m", "venv", VENV_DIR], {
      timeout: 30000,
      stdio: "inherit",
    });

    const reqFile = getResourcePath("requirements.txt");
    if (fs.existsSync(reqFile)) {
      execFileSync(VENV_PYTHON, ["-m", "pip", "install", "-r", reqFile], {
        timeout: 120000,
        stdio: "inherit",
      });
    } else {
      execFileSync(VENV_PYTHON, ["-m", "pip", "install", "fastapi", "uvicorn[standard]", "pyyaml"], {
        timeout: 120000,
        stdio: "inherit",
      });
    }

    const applypilotDir = getResourcePath("applypilot");
    if (fs.existsSync(applypilotDir)) {
      execFileSync(VENV_PYTHON, ["-m", "pip", "install", "-e", applypilotDir], {
        timeout: 120000,
        stdio: "inherit",
      });
    }

    if (hasDeps(VENV_PYTHON)) return VENV_PYTHON;

    dialog.showErrorBox("Setup Failed", "Dependencies were installed but verification failed.\n\nTry running manually in Terminal:\n  python3 -m venv ~/.applypilot/.venv\n  ~/.applypilot/.venv/bin/pip install fastapi uvicorn[standard] pyyaml");
    return null;
  } catch (err) {
    dialog.showErrorBox("Setup Failed", `Could not set up Python environment.\n\n${err.message}\n\nMake sure Python 3.11+ is installed (brew install python3), then relaunch.`);
    return null;
  }
}

function startServer(port, pythonPath) {
  const serverScript = getResourcePath("server.py");
  const cwd = getResourcePath();

  serverProcess = spawn(pythonPath, [serverScript, "--port", String(port)], {
    cwd: cwd,
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
      `Failed to start the Python server.\n\n${err.message}`
    );
    app.quit();
  });

  serverProcess.on("exit", (code, signal) => {
    if (mainWindow && !app.isQuitting) {
      dialog.showErrorBox(
        "Server Stopped",
        `The Python server exited unexpectedly (code: ${code}, signal: ${signal}).`
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
  const pythonPath = ensurePythonEnv();
  if (!pythonPath) {
    app.quit();
    return;
  }

  try {
    serverPort = await findAvailablePort(PREFERRED_PORT);
  } catch (err) {
    dialog.showErrorBox("Startup Error", err.message);
    app.quit();
    return;
  }

  console.log(`Starting server on port ${serverPort} with ${pythonPath}`);
  startServer(serverPort, pythonPath);

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
