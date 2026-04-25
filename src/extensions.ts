import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as http from 'http';
import * as https from 'https';
import { spawn, ChildProcess } from 'child_process';

const DEFAULT_BRIDGE_PORT = 8080;
let bridgeProcess: ChildProcess | undefined;
let activePanel: vscode.WebviewPanel | undefined;
let latestCapturedError: string | undefined;

// Resolved at activation — either a hosted URL or the local 127.0.0.1 address.
let resolvedBridgeUrl = `http://127.0.0.1:${DEFAULT_BRIDGE_PORT}`;

// ── ANSI / terminal helpers ────────────────────────────────────────────────

const ANSI_RE = /\x1b\[[0-9;]*[mGKHFJA-Z]/g;
function stripAnsi(s: string): string { return s.replace(ANSI_RE, ''); }

const TRACEBACK_RE = /Traceback \(most recent call last\)[\s\S]+?[\w.]+(?:Error|Exception):[ \t]*.+/;

// ── Post a message to the webview panel ───────────────────────────────────

function postToPanel(msg: object) {
    activePanel?.webview.postMessage(msg);
}

// ── Bridge proxy: extension host → bridge HTTP/HTTPS ─────────────────────
// The webview cannot reliably reach localhost directly (sandbox, IPv6, remote
// VS Code). All bridge calls are routed through the extension host instead.
// Supports both local (http://127.0.0.1) and hosted (https://...) bridges.

function handleBridgeCall(
    id: string,
    method: string,
    urlPath: string,
    body: object | undefined,
    output: vscode.OutputChannel
) {
    const payload = (method !== 'GET' && body) ? JSON.stringify(body) : '';

    let base: URL;
    try {
        base = new URL(resolvedBridgeUrl);
    } catch {
        postToPanel({ type: 'bridge-response', id, ok: false, error: `Invalid bridgeUrl: ${resolvedBridgeUrl}` });
        return;
    }

    const isHttps = base.protocol === 'https:';
    const mod = isHttps ? https : http;
    const port = base.port ? Number(base.port) : (isHttps ? 443 : 80);
    // Strip trailing slash from pathname so urlPath (which starts with /) works cleanly.
    const fullPath = base.pathname.replace(/\/$/, '') + urlPath;

    const req = mod.request(
        {
            hostname: base.hostname,
            port,
            path: fullPath,
            method: method.toUpperCase(),
            headers: payload
                ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
                : {},
        },
        res => {
            let raw = '';
            res.on('data', (chunk: Buffer) => (raw += chunk.toString()));
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
                } catch {
                    postToPanel({ type: 'bridge-response', id, ok: false, error: 'Invalid response from bridge' });
                }
            });
        }
    );
    req.on('error', (e: Error) => {
        output.appendLine(`[Bridge] ${method} ${urlPath} failed: ${e.message}`);
        const isLocal = resolvedBridgeUrl.includes('127.0.0.1') || resolvedBridgeUrl.includes('localhost');
        const hint = isLocal
            ? ' — is the bridge process running? Check the TraceBack output channel.'
            : ` — check that ${resolvedBridgeUrl} is reachable.`;
        postToPanel({ type: 'bridge-response', id, ok: false, error: `Bridge unreachable: ${e.message}${hint}` });
    });
    if (payload) req.write(payload);
    req.end();
}

function _friendlyBridgeError(statusCode: number, parsed: any): string {
    const detail: string = parsed?.detail ?? JSON.stringify(parsed);
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

function callBridgePost(urlPath: string, body: object, output: vscode.OutputChannel) {
    handleBridgeCall('_internal', 'POST', urlPath, body, output);
}

// ── Apply edits received from the webview ─────────────────────────────────

interface FileEdit { file_path: string; old_code: string; new_code: string; }

async function applyFix(edits: FileEdit[], output: vscode.OutputChannel) {
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
            wsEdit.replace(
                uri,
                new vscode.Range(doc.positionAt(idx), doc.positionAt(idx + edit.old_code.length)),
                edit.new_code
            );
            applied++;
        } catch (e: any) {
            output.appendLine(`[Fix] ${edit.file_path}: ${e.message}`);
        }
    }

    if (applied > 0) {
        await vscode.workspace.applyEdit(wsEdit);
        const first = edits.find(e => {
            try { vscode.Uri.file(e.file_path); return true; } catch { return false; }
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

function setupFileWatcher(context: vscode.ExtensionContext, output: vscode.OutputChannel) {
    const watcher = vscode.workspace.createFileSystemWatcher('**/*.py');
    const debounces = new Map<string, ReturnType<typeof setTimeout>>();

    const schedule = (uri: vscode.Uri) => {
        const key = uri.fsPath;
        const t = debounces.get(key);
        if (t) clearTimeout(t);
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

function setupTerminalErrorCapture(context: vscode.ExtensionContext, output: vscode.OutputChannel) {
    const event = (vscode.window as any).onDidStartTerminalShellExecution;
    if (typeof event !== 'function') {
        output.appendLine('[TraceBack] Shell integration API unavailable; paste errors manually.');
        return;
    }

    context.subscriptions.push(
        event(async (e: { execution: { read(): AsyncIterable<string> } }) => {
            const stream = e.execution.read();
            let buf = '';
            try {
                for await (const chunk of stream) {
                    buf += stripAnsi(chunk).replace(/\r/g, '');
                    if (buf.length > 8192) buf = buf.slice(buf.length - 8192);
                    const match = buf.match(TRACEBACK_RE);
                    if (match) {
                        const errorText = match[0].trim();
                        buf = buf.slice(buf.indexOf(match[0]) + match[0].length);
                        latestCapturedError = errorText;
                        output.appendLine('[TraceBack] Error captured from terminal');
                        postToPanel({ type: 'error-captured', data: errorText, source: 'terminal' });
                    }
                }
            } catch { /* stream ended or shell integration detached */ }
        })
    );
}

// ── Activation ────────────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
    const output = vscode.window.createOutputChannel('TraceBack');
    context.subscriptions.push(output);

    const config = vscode.workspace.getConfiguration('traceback');
    const hostedUrl = config.get<string>('bridgeUrl', '').trim();

    if (hostedUrl) {
        // Hosted mode: use a remote bridge — no subprocess needed.
        // Secrets (API keys) live on the server; only the URL is needed here.
        resolvedBridgeUrl = hostedUrl.replace(/\/$/, '');
        output.appendLine(`[TraceBack] Hosted mode — bridge: ${resolvedBridgeUrl}`);
    } else {
        // Local mode: spawn bridge.py subprocess on localhost.
        const port = config.get<number>('bridgePort', DEFAULT_BRIDGE_PORT);
        resolvedBridgeUrl = `http://127.0.0.1:${port}`;
        output.appendLine(`[TraceBack] Local mode — bridge: ${resolvedBridgeUrl}`);
        startBridge(context, output, port);
    }

    setupFileWatcher(context, output);
    setupTerminalErrorCapture(context, output);

    context.subscriptions.push(
        vscode.commands.registerCommand('traceback.open', () => openPanel(context, output))
    );

    openPanel(context, output);
}

function startBridge(context: vscode.ExtensionContext, output: vscode.OutputChannel, port: number) {
    const config = vscode.workspace.getConfiguration('traceback');
    const pythonPath = config.get<string>('pythonPath', 'python3');
    const bridgePath = path.join(context.extensionPath, 'agents', 'bridge.py');

    output.appendLine(`[TraceBack] Starting bridge with ${pythonPath} ${bridgePath}`);

    bridgeProcess = spawn(pythonPath, [bridgePath], {
        cwd: context.extensionPath,
        env: { ...process.env, BRIDGE_PORT: String(port) },
    });

    bridgeProcess.stdout?.on('data', (d: Buffer) => output.appendLine(d.toString().trim()));
    bridgeProcess.stderr?.on('data', (d: Buffer) => output.appendLine('[ERR] ' + d.toString().trim()));
    bridgeProcess.on('exit', (code: number | null) => output.appendLine(`[Bridge] exited (code ${code})`));

    context.subscriptions.push({ dispose: () => bridgeProcess?.kill() });
}

function openPanel(context: vscode.ExtensionContext, output: vscode.OutputChannel) {
    if (activePanel) {
        activePanel.reveal(vscode.ViewColumn.Beside);
        if (latestCapturedError) {
            postToPanel({ type: 'error-captured', data: latestCapturedError, source: 'terminal' });
        }
        return;
    }

    const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';

    activePanel = vscode.window.createWebviewPanel(
        'traceback',
        'TraceBack',
        vscode.ViewColumn.Beside,
        {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.file(path.join(context.extensionPath, 'src', 'frontend', 'dist')),
            ],
            retainContextWhenHidden: true,
        }
    );

    activePanel.webview.html = buildHtml(
        activePanel.webview,
        context.extensionPath,
        workspacePath,
        latestCapturedError ?? ''
    );

    activePanel.webview.onDidReceiveMessage(
        async (msg: any) => {
            if (msg.type === 'apply-fix') {
                await applyFix(msg.edits ?? [], output);
            } else if (msg.type === 'bridge-call') {
                handleBridgeCall(msg.id, msg.method, msg.path, msg.body, output);
            }
        },
        null,
        context.subscriptions
    );

    activePanel.onDidDispose(() => { activePanel = undefined; }, null, context.subscriptions);

    output.appendLine(`[TraceBack] Panel opened (workspace: ${workspacePath})`);
}

function buildHtml(
    webview: vscode.Webview,
    extensionPath: string,
    workspacePath: string,
    initialError: string
): string {
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

    html = html.replace(/(src|href)="(\.\/[^"]+)"/g, (_m, attr, p) =>
        `${attr}="${distUri}/${p.slice(2)}"`
    );
    html = html.replace(/(src|href)="(\/assets\/[^"]+)"/g, (_m, attr, p) =>
        `${attr}="${distUri}${p}"`
    );

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

export function deactivate() {
    bridgeProcess?.kill();
}
