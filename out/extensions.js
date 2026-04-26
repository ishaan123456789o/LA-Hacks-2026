"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const path = __importStar(require("path"));
const fs = __importStar(require("fs"));
const http = __importStar(require("http"));
const https = __importStar(require("https"));
const child_process_1 = require("child_process");
const DEFAULT_BRIDGE_PORT = 8080;
let bridgeProcess;
let activePanel;
let latestCapturedError;
// Resolved at activation — either a hosted URL or the local 127.0.0.1 address.
let resolvedBridgeUrl = `http://127.0.0.1:${DEFAULT_BRIDGE_PORT}`;
// ── ANSI / terminal helpers ────────────────────────────────────────────────
const ANSI_RE = /\x1b\[[0-9;]*[mGKHFJA-Z]/g;
function stripAnsi(s) { return s.replace(ANSI_RE, ''); }
const TRACEBACK_RE = /Traceback \(most recent call last\)[\s\S]+?[\w.]+(?:Error|Exception):[ \t]*.+/;
const TERMINAL_ERROR_FALLBACK_RE = /File "([^"]+)", line \d+[\s\S]{0,1200}?(?:[\w.]+(?:Error|Exception)|SyntaxError):[ \t]*.+/;
// ── Post a message to the webview panel ───────────────────────────────────
function postToPanel(msg) {
    activePanel?.webview.postMessage(msg);
}
// ── Bridge proxy: extension host → bridge HTTP/HTTPS ─────────────────────
// The webview cannot reliably reach localhost directly (sandbox, IPv6, remote
// VS Code). All bridge calls are routed through the extension host instead.
// Supports both local (http://127.0.0.1) and hosted (https://...) bridges.
function handleBridgeCall(id, method, urlPath, body, output) {
    const payload = (method !== 'GET' && body) ? JSON.stringify(body) : '';
    let base;
    try {
        base = new URL(resolvedBridgeUrl);
    }
    catch {
        postToPanel({ type: 'bridge-response', id, ok: false, error: `Invalid bridgeUrl: ${resolvedBridgeUrl}` });
        return;
    }
    const isHttps = base.protocol === 'https:';
    const mod = isHttps ? https : http;
    const port = base.port ? Number(base.port) : (isHttps ? 443 : 80);
    // Strip trailing slash from pathname so urlPath (which starts with /) works cleanly.
    const fullPath = base.pathname.replace(/\/$/, '') + urlPath;
    const req = mod.request({
        hostname: base.hostname,
        port,
        path: fullPath,
        method: method.toUpperCase(),
        headers: payload
            ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
            : {},
    }, res => {
        let raw = '';
        res.on('data', (chunk) => (raw += chunk.toString()));
        res.on('end', () => {
            try {
                const parsed = JSON.parse(raw);
                const ok = (res.statusCode ?? 500) < 300;
                postToPanel({
                    type: 'bridge-response',
                    id,
                    ok,
                    data: ok ? parsed : undefined,
                    error: !ok ? _friendlyBridgeError(res.statusCode ?? 500, parsed) : undefined,
                });
            }
            catch {
                postToPanel({ type: 'bridge-response', id, ok: false, error: 'Invalid response from bridge' });
            }
        });
    });
    req.on('error', (e) => {
        output.appendLine(`[Bridge] ${method} ${urlPath} failed: ${e.message}`);
        const isLocal = resolvedBridgeUrl.includes('127.0.0.1') || resolvedBridgeUrl.includes('localhost');
        const hint = isLocal
            ? ' — is the bridge process running? Check the TraceBack output channel.'
            : ` — check that ${resolvedBridgeUrl} is reachable.`;
        postToPanel({ type: 'bridge-response', id, ok: false, error: `Bridge unreachable: ${e.message}${hint}` });
    });
    if (payload)
        req.write(payload);
    req.end();
}
function _friendlyBridgeError(statusCode, parsed) {
    const detail = parsed?.detail ?? JSON.stringify(parsed);
    if (statusCode === 401) {
        return `Auth error (401): ${detail}`;
    }
    if (statusCode === 422) {
        return `Dimension mismatch (422): ${detail}`;
    }
    if (statusCode === 503) {
        return `RPC not found (503): ${detail}`;
    }
    return detail;
}
// ── Fire-and-forget bridge call (file watcher / internal use) ─────────────
function callBridgePost(urlPath, body, output) {
    handleBridgeCall('_internal', 'POST', urlPath, body, output);
}
function _findEditRange(doc, oldCode) {
    const text = doc.getText();
    // Exact match first
    const idx = text.indexOf(oldCode);
    if (idx >= 0) {
        return new vscode.Range(doc.positionAt(idx), doc.positionAt(idx + oldCode.length));
    }
    // Line-level normalised fallback: handles CRLF and trailing whitespace differences
    const normOldLines = oldCode.replace(/\r\n/g, '\n').split('\n').map(l => l.trimEnd());
    const docLines = text.split(/\r?\n/);
    for (let i = 0; i <= docLines.length - normOldLines.length; i++) {
        if (docLines[i].trimEnd() !== normOldLines[0]) {
            continue;
        }
        let match = true;
        for (let j = 1; j < normOldLines.length; j++) {
            if (docLines[i + j].trimEnd() !== normOldLines[j]) {
                match = false;
                break;
            }
        }
        if (match) {
            const endLine = i + normOldLines.length - 1;
            return new vscode.Range(new vscode.Position(i, 0), new vscode.Position(endLine, docLines[endLine].length));
        }
    }
    return null;
}
async function applyFix(edits, output, fixRequestId) {
    if (!edits || edits.length === 0) {
        postToPanel({ type: 'fix-result', applied: 0, total: 0 });
        return;
    }
    const wsEdit = new vscode.WorkspaceEdit();
    let applied = 0;
    for (const edit of edits) {
        try {
            const uri = vscode.Uri.file(edit.file_path);
            const doc = await vscode.workspace.openTextDocument(uri);
            const range = _findEditRange(doc, edit.old_code);
            if (!range) {
                output.appendLine(`[Fix] old_code not found in ${edit.file_path}`);
                continue;
            }
            wsEdit.replace(uri, range, edit.new_code);
            applied++;
        }
        catch (e) {
            output.appendLine(`[Fix] ${edit.file_path}: ${e.message}`);
        }
    }
    if (applied > 0) {
        await vscode.workspace.applyEdit(wsEdit);
        if (fixRequestId) {
            callBridgePost('/fix-cleanup', { request_id: fixRequestId }, output);
        }
        const first = edits.find(e => {
            try {
                vscode.Uri.file(e.file_path);
                return true;
            }
            catch {
                return false;
            }
        });
        if (first) {
            const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(first.file_path));
            vscode.window.showTextDocument(doc, { preserveFocus: true });
        }
    }
    output.appendLine(`[Fix] Applied ${applied}/${edits.length} edits`);
    postToPanel({ type: 'fix-result', applied, total: edits.length });
}
// ── File watcher: re-index changed .py files ──────────────────────────────
function setupFileWatcher(context, output) {
    const watcher = vscode.workspace.createFileSystemWatcher('**/*.py');
    const debounces = new Map();
    const schedule = (uri) => {
        const key = uri.fsPath;
        const t = debounces.get(key);
        if (t)
            clearTimeout(t);
        debounces.set(key, setTimeout(() => {
            debounces.delete(key);
            output.appendLine(`[TraceBack] Re-indexing ${uri.fsPath}`);
            callBridgePost('/reindex-file', { file_path: uri.fsPath }, output);
            postToPanel({ type: 'reindex-start', file: path.basename(uri.fsPath) });
        }, 1500));
    };
    watcher.onDidChange(schedule);
    watcher.onDidCreate(schedule);
    watcher.onDidDelete(uri => callBridgePost('/reindex-file', { file_path: uri.fsPath }, output));
    context.subscriptions.push(watcher);
    output.appendLine('[TraceBack] File watcher active (*.py)');
}
// ── Terminal listener: capture Python tracebacks via shell integration ────
function setupTerminalErrorCapture(context, output) {
    const captureFromBuffer = (buf, source) => {
        const match = buf.match(TRACEBACK_RE) ?? buf.match(TERMINAL_ERROR_FALLBACK_RE);
        if (!match)
            return null;
        const errorText = match[0].trim();
        if (!errorText || errorText === latestCapturedError)
            return null;
        latestCapturedError = errorText;
        output.appendLine('[TraceBack] Error captured from terminal');
        postToPanel({ type: 'error-captured', data: errorText, source });
        return errorText;
    };
    try {
        const event = vscode.window.onDidStartTerminalShellExecution;
        if (typeof event === 'function') {
            context.subscriptions.push(event(async (e) => {
                const stream = e.execution.read();
                let buf = '';
                try {
                    for await (const chunk of stream) {
                        buf += stripAnsi(chunk).replace(/\r/g, '');
                        if (buf.length > 16384)
                            buf = buf.slice(buf.length - 16384);
                        const captured = captureFromBuffer(buf, 'terminal');
                        if (captured) {
                            const idx = buf.indexOf(captured);
                            if (idx >= 0)
                                buf = buf.slice(idx + captured.length);
                        }
                    }
                }
                catch { /* stream ended or shell integration detached */ }
            }));
        }
        else {
            output.appendLine('[TraceBack] Shell integration stream unavailable; using write-data fallback.');
        }
    }
    catch (e) {
        output.appendLine(`[TraceBack] Shell integration hook failed: ${e.message}`);
    }
    // Fallback path: captures terminal write events (works when shell integration misses output).
    try {
        const writeEvent = vscode.window.onDidWriteTerminalData;
        if (typeof writeEvent === 'function') {
            const buffers = new Map();
            context.subscriptions.push(writeEvent((e) => {
                const key = e?.terminal?.name ?? 'default';
                const prev = buffers.get(key) ?? '';
                const next = (prev + stripAnsi(e?.data ?? '').replace(/\r/g, '')).slice(-16384);
                buffers.set(key, next);
                const captured = captureFromBuffer(next, 'terminal');
                if (captured) {
                    const idx = next.indexOf(captured);
                    if (idx >= 0)
                        buffers.set(key, next.slice(idx + captured.length));
                }
            }));
        }
    }
    catch (e) {
        output.appendLine(`[TraceBack] Terminal write fallback hook failed: ${e.message}`);
    }
}
// ── Activation ────────────────────────────────────────────────────────────
function activate(context) {
    const output = vscode.window.createOutputChannel('TraceBack');
    context.subscriptions.push(output);
    const config = vscode.workspace.getConfiguration('traceback');
    const hostedUrl = config.get('bridgeUrl', '').trim();
    if (hostedUrl) {
        // Hosted mode: use a remote bridge — no subprocess needed.
        // Secrets (API keys) live on the server; only the URL is needed here.
        resolvedBridgeUrl = hostedUrl.replace(/\/$/, '');
        output.appendLine(`[TraceBack] Hosted mode — bridge: ${resolvedBridgeUrl}`);
    }
    else {
        // Local mode: spawn bridge.py subprocess on localhost.
        const port = config.get('bridgePort', DEFAULT_BRIDGE_PORT);
        resolvedBridgeUrl = `http://127.0.0.1:${port}`;
        output.appendLine(`[TraceBack] Local mode — bridge: ${resolvedBridgeUrl}`);
        startBridge(context, output, port);
    }
    setupFileWatcher(context, output);
    try {
        setupTerminalErrorCapture(context, output);
    }
    catch (e) {
        output.appendLine(`[TraceBack] Terminal capture disabled: ${e.message}`);
    }
    context.subscriptions.push(vscode.commands.registerCommand('traceback.open', () => openPanel(context, output)), vscode.commands.registerCommand('traceback.runFile', () => runActiveFile(context, output)));
    // Status bar "Run with TraceBack" button — visible when a Python file is active
    const runBtn = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    runBtn.command = 'traceback.runFile';
    runBtn.text = '$(play) Run with TraceBack';
    runBtn.tooltip = 'Run the active Python file and capture any errors';
    context.subscriptions.push(runBtn);
    const refreshBtn = () => {
        if (vscode.window.activeTextEditor?.document.fileName.endsWith('.py')) {
            runBtn.show();
        }
        else {
            runBtn.hide();
        }
    };
    refreshBtn();
    context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(refreshBtn));
    openPanel(context, output);
}
function runActiveFile(context, output) {
    const editor = vscode.window.activeTextEditor;
    if (!editor || !editor.document.fileName.endsWith('.py')) {
        vscode.window.showErrorMessage('Open a Python file to run with TraceBack.');
        return;
    }
    const filePath = editor.document.fileName;
    const config = vscode.workspace.getConfiguration('traceback');
    const pythonPath = config.get('pythonPath', 'python3');
    output.appendLine(`[TraceBack] Running ${filePath}`);
    openPanel(context, output);
    postToPanel({ type: 'run-start', file: path.basename(filePath) });
    const proc = (0, child_process_1.spawn)(pythonPath, [filePath], { cwd: path.dirname(filePath) });
    let combined = '';
    const onData = (d) => {
        const text = stripAnsi(d.toString()).replace(/\r/g, '');
        combined += text;
        if (combined.length > 16384) {
            combined = combined.slice(combined.length - 16384);
        }
    };
    proc.stdout?.on('data', onData);
    proc.stderr?.on('data', onData);
    proc.on('exit', code => {
        output.appendLine(`[TraceBack] ${path.basename(filePath)} exited (code ${code})`);
        const match = combined.match(TRACEBACK_RE);
        if (match) {
            const errorText = match[0].trim();
            latestCapturedError = errorText;
            output.appendLine('[TraceBack] Error captured from run');
            postToPanel({ type: 'error-captured', data: errorText, source: 'run' });
        }
        else if (code !== 0) {
            postToPanel({ type: 'run-error', output: combined.slice(-2000) });
        }
        else {
            postToPanel({ type: 'run-ok', file: path.basename(filePath) });
        }
    });
    proc.on('error', (e) => {
        output.appendLine(`[TraceBack] Failed to run: ${e.message}`);
        vscode.window.showErrorMessage(`TraceBack: failed to run — ${e.message}`);
    });
}
function startBridge(context, output, port) {
    const config = vscode.workspace.getConfiguration('traceback');
    const pythonPath = config.get('pythonPath', 'python3');
    const bridgePath = path.join(context.extensionPath, 'agents', 'bridge.py');
    output.appendLine(`[TraceBack] Starting bridge with ${pythonPath} ${bridgePath}`);
    bridgeProcess = (0, child_process_1.spawn)(pythonPath, [bridgePath], {
        cwd: context.extensionPath,
        env: { ...process.env, BRIDGE_PORT: String(port) },
    });
    bridgeProcess.stdout?.on('data', (d) => output.appendLine(d.toString().trim()));
    bridgeProcess.stderr?.on('data', (d) => output.appendLine('[ERR] ' + d.toString().trim()));
    bridgeProcess.on('exit', (code) => output.appendLine(`[Bridge] exited (code ${code})`));
    context.subscriptions.push({ dispose: () => bridgeProcess?.kill() });
}
function openPanel(context, output) {
    if (activePanel) {
        activePanel.reveal(vscode.ViewColumn.Beside);
        if (latestCapturedError) {
            postToPanel({ type: 'error-captured', data: latestCapturedError, source: 'terminal' });
        }
        return;
    }
    const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
    activePanel = vscode.window.createWebviewPanel('traceback', 'TraceBack', vscode.ViewColumn.Beside, {
        enableScripts: true,
        localResourceRoots: [
            vscode.Uri.file(path.join(context.extensionPath, 'src', 'frontend', 'dist')),
        ],
        retainContextWhenHidden: true,
    });
    activePanel.webview.html = buildHtml(activePanel.webview, context.extensionPath, workspacePath, latestCapturedError ?? '');
    activePanel.webview.onDidReceiveMessage(async (msg) => {
        if (msg.type === 'apply-fix') {
            await applyFix(msg.edits ?? [], output, msg.fix_request_id);
        }
        else if (msg.type === 'bridge-call') {
            handleBridgeCall(msg.id, msg.method, msg.path, msg.body, output);
        }
    }, null, context.subscriptions);
    activePanel.onDidDispose(() => { activePanel = undefined; }, null, context.subscriptions);
    output.appendLine(`[TraceBack] Panel opened (workspace: ${workspacePath})`);
}
function buildHtml(webview, extensionPath, workspacePath, initialError) {
    const distPath = path.join(extensionPath, 'src', 'frontend', 'dist');
    const indexPath = path.join(distPath, 'index.html');
    if (!fs.existsSync(indexPath)) {
        return `<!DOCTYPE html><html>
<body style="color:#ccc;font-family:monospace;padding:24px;background:#1e1e1e">
  <h3 style="color:#fff">TraceBack — Build Required</h3>
  <p>Run once to build the frontend:</p>
  <pre style="background:#252526;padding:12px;border-radius:4px">cd src/frontend
pnpm install
pnpm build</pre>
  <p>Then reload the window (<code>Cmd+Shift+P → Developer: Reload Window</code>).</p>
</body></html>`;
    }
    const distUri = webview.asWebviewUri(vscode.Uri.file(distPath)).toString();
    let html = fs.readFileSync(indexPath, 'utf8');
    html = html.replace(/(src|href)="(\.\/[^"]+)"/g, (_m, attr, p) => `${attr}="${distUri}/${p.slice(2)}"`);
    html = html.replace(/(src|href)="(\/assets\/[^"]+)"/g, (_m, attr, p) => `${attr}="${distUri}${p}"`);
    // No connect-src needed — all bridge calls go through the extension host,
    // not direct from the webview. Works locally, remotely, and in Codespaces.
    const csp = [
        `default-src 'none'`,
        `script-src 'unsafe-eval' 'unsafe-inline' ${webview.cspSource}`,
        `style-src 'unsafe-inline' ${webview.cspSource}`,
        `img-src ${webview.cspSource} data:`,
        `font-src ${webview.cspSource} data:`,
    ].join('; ');
    const injection = `
<meta http-equiv="Content-Security-Policy" content="${csp}">
<script>
  window.WORKSPACE_PATH = ${JSON.stringify(workspacePath)};
  window.INITIAL_ERROR = ${JSON.stringify(initialError)};
</script>`;
    html = html.replace('<head>', '<head>' + injection);
    return html;
}
function deactivate() {
    bridgeProcess?.kill();
}
//# sourceMappingURL=extensions.js.map