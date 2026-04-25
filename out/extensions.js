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
const child_process_1 = require("child_process");
const BRIDGE_PORT = 8080;
let bridgeProcess;
let activePanel;
function activate(context) {
    const output = vscode.window.createOutputChannel('TraceBack');
    context.subscriptions.push(output);
    startBridge(context, output);
    context.subscriptions.push(vscode.commands.registerCommand('traceback.open', () => openPanel(context, output)));
    // Auto-open sidebar on activation
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
    activePanel.webview.html = buildHtml(activePanel.webview, context.extensionPath, workspacePath);
    activePanel.onDidDispose(() => { activePanel = undefined; }, null, context.subscriptions);
    output.appendLine(`[TraceBack] Panel opened (workspace: ${workspacePath})`);
}
function buildHtml(webview, extensionPath, workspacePath) {
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
</script>`;
    html = html.replace('<head>', '<head>' + injection);
    return html;
}
function deactivate() {
    bridgeProcess?.kill();
}
//# sourceMappingURL=extensions.js.map