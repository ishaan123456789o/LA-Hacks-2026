import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { spawn, ChildProcess } from 'child_process';

const BRIDGE_PORT = 8080;
let bridgeProcess: ChildProcess | undefined;
let activePanel: vscode.WebviewPanel | undefined;

export function activate(context: vscode.ExtensionContext) {
    const output = vscode.window.createOutputChannel('TraceBack');
    context.subscriptions.push(output);

    startBridge(context, output);

    context.subscriptions.push(
        vscode.commands.registerCommand('traceback.open', () => openPanel(context, output))
    );

    // Auto-open sidebar on activation
    openPanel(context, output);
}

function startBridge(context: vscode.ExtensionContext, output: vscode.OutputChannel) {
    const config = vscode.workspace.getConfiguration('traceback');
    const pythonPath = config.get<string>('pythonPath', 'python3');
    const bridgePath = path.join(context.extensionPath, 'agents', 'bridge.py');

    output.appendLine(`[TraceBack] Starting bridge with ${pythonPath} ${bridgePath}`);

    bridgeProcess = spawn(pythonPath, [bridgePath], {
        cwd: context.extensionPath,
        env: { ...process.env },
    });

    bridgeProcess.stdout?.on('data', (d: Buffer) =>
        output.appendLine(d.toString().trim())
    );
    bridgeProcess.stderr?.on('data', (d: Buffer) =>
        output.appendLine('[ERR] ' + d.toString().trim())
    );
    bridgeProcess.on('exit', (code: number | null) =>
        output.appendLine(`[Bridge] exited (code ${code})`)
    );

    context.subscriptions.push({ dispose: () => bridgeProcess?.kill() });
}

function openPanel(context: vscode.ExtensionContext, output: vscode.OutputChannel) {
    if (activePanel) {
        activePanel.reveal(vscode.ViewColumn.Beside);
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

    activePanel.webview.html = buildHtml(activePanel.webview, context.extensionPath, workspacePath);
    activePanel.onDidDispose(() => { activePanel = undefined; }, null, context.subscriptions);

    output.appendLine(`[TraceBack] Panel opened (workspace: ${workspacePath})`);
}

function buildHtml(webview: vscode.Webview, extensionPath: string, workspacePath: string): string {
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

    // Rewrite relative asset paths to webview URIs
    html = html.replace(/(src|href)="(\.\/[^"]+)"/g, (_m, attr, p) =>
        `${attr}="${distUri}/${p.slice(2)}"`
    );
    html = html.replace(/(src|href)="(\/assets\/[^"]+)"/g, (_m, attr, p) =>
        `${attr}="${distUri}${p}"`
    );

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
</script>`;

    html = html.replace('<head>', '<head>' + injection);
    return html;
}

export function deactivate() {
    bridgeProcess?.kill();
}
