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
const child_process_1 = require("child_process");
const BRIDGE_PORT = 8080;
let bridgeProcess;
let activePanel;
let latestCapturedError;
// ── ANSI / terminal helpers ────────────────────────────────────────────────
const ANSI_RE = /\x1b\[[0-9;]*[mGKHFJA-Z]/g;
function stripAnsi(s) { return s.replace(ANSI_RE, ''); }
// Matches a complete Python traceback ending with an error line
const TRACEBACK_RE = /Traceback \(most recent call last\)[\s\S]+?[\w.]+(?:Error|Exception):[ \t]*.+/;
// ── Bridge HTTP helper ─────────────────────────────────────────────────────
function callBridgePost(urlPath, body, output) {
    const data = JSON.stringify(body);
    const req = http.request({
        hostname: '127.0.0.1',
        port: BRIDGE_PORT,
        path: urlPath,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
    }, res => {
        let raw = '';
        res.on('data', (chunk) => (raw += chunk.toString()));
        res.on('end', () => {
            if (res.statusCode && res.statusCode >= 300) {
                output.appendLine(`[Bridge] ${urlPath} → ${res.statusCode}: ${raw.slice(0, 120)}`);
            }
        });
    });
    req.on('error', () => { }); // bridge may not be up yet — silence retry noise
    req.write(data);
    req.end();
}
// ── Post a message to the webview panel ───────────────────────────────────
function postToPanel(msg) {
    activePanel?.webview.postMessage(msg);
}
async function applyFix(edits, output) {
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
            const text = doc.getText();
            const idx = text.indexOf(edit.old_code);
            if (idx < 0) {
                output.appendLine(`[Fix] old_code not found in ${edit.file_path}`);
                continue;
            }
            wsEdit.replace(uri, new vscode.Range(doc.positionAt(idx), doc.positionAt(idx + edit.old_code.length)), edit.new_code);
            applied++;
        }
        catch (e) {
            output.appendLine(`[Fix] ${edit.file_path}: ${e.message}`);
        }
    }
    if (applied > 0) {
        await vscode.workspace.applyEdit(wsEdit);
        // Show the first patched file in the editor
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
        // Wait 1.5 s after the last save before hitting the bridge
        debounces.set(key, setTimeout(() => {
            debounces.delete(key);
            output.appendLine(`[TraceBack] Re-indexing ${uri.fsPath}`);
            callBridgePost('/reindex-file', { file_path: uri.fsPath }, output);
            postToPanel({ type: 'reindex-start', file: path.basename(uri.fsPath) });
        }, 1500));
    };
    watcher.onDidChange(schedule);
    watcher.onDidCreate(schedule);
    // Deletion: reindex-file will delete old chunks and find no new code to insert
    watcher.onDidDelete(uri => callBridgePost('/reindex-file', { file_path: uri.fsPath }, output));
    context.subscriptions.push(watcher);
    output.appendLine('[TraceBack] File watcher active (*.py)');
}
// ── Terminal listener: capture Python tracebacks via shell integration ────
// Uses the stable onDidStartTerminalShellExecution API (VS Code 1.93+).
// Falls back silently on older versions — users can still paste errors manually.
function setupTerminalErrorCapture(context, output) {
    const event = vscode.window.onDidStartTerminalShellExecution;
    if (typeof event !== 'function') {
        output.appendLine('[TraceBack] Shell integration API unavailable; paste errors manually.');
        return;
    }
    context.subscriptions.push(event(async (e) => {
        const stream = e.execution.read();
        let buf = '';
        try {
            for await (const chunk of stream) {
                buf += stripAnsi(chunk).replace(/\r/g, '');
                if (buf.length > 8192)
                    buf = buf.slice(buf.length - 8192);
                const match = buf.match(TRACEBACK_RE);
                if (match) {
                    const errorText = match[0].trim();
                    buf = buf.slice(buf.indexOf(match[0]) + match[0].length);
                    latestCapturedError = errorText;
                    output.appendLine('[TraceBack] Error captured from terminal');
                    postToPanel({ type: 'error-captured', data: errorText, source: 'terminal' });
                }
            }
        }
        catch { /* stream ended or shell integration detached */ }
    }));
}
// ── Activation ────────────────────────────────────────────────────────────
function activate(context) {
    const output = vscode.window.createOutputChannel('TraceBack');
    context.subscriptions.push(output);
    startBridge(context, output);
    setupFileWatcher(context, output);
    setupTerminalErrorCapture(context, output);
    context.subscriptions.push(vscode.commands.registerCommand('traceback.open', () => openPanel(context, output)));
    openPanel(context, output);
}
function startBridge(context, output) {
    const config = vscode.workspace.getConfiguration('traceback');
    const pythonPath = config.get('pythonPath', 'python3');
    const bridgePath = path.join(context.extensionPath, 'agents', 'bridge.py');
    output.appendLine(`[TraceBack] Starting bridge with ${pythonPath} ${bridgePath}`);
    bridgeProcess = (0, child_process_1.spawn)(pythonPath, [bridgePath], {
        cwd: context.extensionPath,
        env: { ...process.env },
    });
    bridgeProcess.stdout?.on('data', (d) => output.appendLine(d.toString().trim()));
    bridgeProcess.stderr?.on('data', (d) => output.appendLine('[ERR] ' + d.toString().trim()));
    bridgeProcess.on('exit', (code) => output.appendLine(`[Bridge] exited (code ${code})`));
    context.subscriptions.push({ dispose: () => bridgeProcess?.kill() });
}
function openPanel(context, output) {
    if (activePanel) {
        activePanel.reveal(vscode.ViewColumn.Beside);
        // Re-send any buffered error to the already-open panel
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
            await applyFix(msg.edits ?? [], output);
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
    const csp = [
        `default-src 'none'`,
        `script-src 'unsafe-eval' 'unsafe-inline' ${webview.cspSource}`,
        `style-src 'unsafe-inline' ${webview.cspSource}`,
        `connect-src http://localhost:${BRIDGE_PORT}`,
        `img-src ${webview.cspSource} data:`,
        `font-src ${webview.cspSource} data:`,
    ].join('; ');
    const injection = `
<meta http-equiv="Content-Security-Policy" content="${csp}">
<script>
  window.WORKSPACE_PATH = ${JSON.stringify(workspacePath)};
  window.BRIDGE_PORT = ${BRIDGE_PORT};
  window.INITIAL_ERROR = ${JSON.stringify(initialError)};
</script>`;
    html = html.replace('<head>', '<head>' + injection);
    return html;
}
function deactivate() {
    bridgeProcess?.kill();
}
//# sourceMappingURL=extensions.js.map