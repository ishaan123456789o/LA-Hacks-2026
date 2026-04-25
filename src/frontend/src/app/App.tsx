import { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Send, FolderSearch, CheckCircle, Loader, AlertCircle, X, RefreshCw, Wrench } from 'lucide-react';

const BRIDGE = `http://localhost:${(window as any).BRIDGE_PORT || 8080}`;
const WORKSPACE: string = (window as any).WORKSPACE_PATH || '';

// Acquire VS Code API once — throws if called more than once, so module-level singleton.
const vscodeApi = (() => {
  try { return (window as any).acquireVsCodeApi?.(); }
  catch { return undefined; }
})();

type IndexState = 'idle' | 'indexing' | 'done' | 'error';
type FixState = 'idle' | 'generating' | 'applying' | 'done' | 'error';

export default function App() {
  const [errorLog, setErrorLog] = useState<string>((window as any).INITIAL_ERROR || '');
  const [analysisResult, setAnalysisResult] = useState('');
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [indexState, setIndexState] = useState<IndexState>('idle');
  const [indexedChunks, setIndexedChunks] = useState(0);
  const [indexError, setIndexError] = useState('');
  const [capturedError, setCapturedError] = useState('');
  const [reindexingFile, setReindexingFile] = useState('');
  const [fixState, setFixState] = useState<FixState>('idle');
  const [fixSummary, setFixSummary] = useState('');

  // Messages from the VS Code extension
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const msg = event.data;
      if (!msg?.type) return;
      if (msg.type === 'error-captured') {
        setCapturedError(msg.data as string);
      } else if (msg.type === 'reindex-start') {
        setReindexingFile(msg.file as string);
        setTimeout(() => setReindexingFile(''), 3000);
      } else if (msg.type === 'fix-result') {
        const { applied, total } = msg as { applied: number; total: number };
        setFixState(applied > 0 ? 'done' : 'error');
        setFixSummary(
          applied === total
            ? `${applied} edit${applied !== 1 ? 's' : ''} applied`
            : `${applied}/${total} edits applied`
        );
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, []);

  useEffect(() => {
    fetch(`${BRIDGE}/status`)
      .then(r => r.json())
      .then(data => {
        if (data.indexed_chunks > 0) {
          setIndexedChunks(data.indexed_chunks);
          setIndexState('done');
        } else if (WORKSPACE) {
          handleIndex(WORKSPACE);
        }
      })
      .catch(() => {
        if (WORKSPACE) setTimeout(() => handleIndex(WORKSPACE), 3000);
      });
  }, []);

  const handleIndex = async (repoPath: string = WORKSPACE) => {
    if (!repoPath) return;
    setIndexState('indexing');
    setIndexError('');
    try {
      const res = await fetch(`${BRIDGE}/index`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_path: repoPath }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Indexing failed');
      setIndexedChunks(data.chunks);
      setIndexState('done');
    } catch (e: any) {
      setIndexError(e.message);
      setIndexState('error');
    }
  };

  const handleAnalyze = async () => {
    if (!errorLog.trim()) return;
    setIsAnalyzing(true);
    setAnalysisResult('');
    setFixState('idle');
    setFixSummary('');
    try {
      const res = await fetch(`${BRIDGE}/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ error_log: errorLog }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Analysis failed');
      setAnalysisResult(data.result);
    } catch (e: any) {
      setAnalysisResult(`**Error:** ${e.message}`);
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleApplyFix = async () => {
    if (!errorLog.trim() || !vscodeApi) return;
    setFixState('generating');
    setFixSummary('');
    try {
      const res = await fetch(`${BRIDGE}/fix`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ error_log: errorLog }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fix generation failed');
      if (!data.edits?.length) {
        setFixState('error');
        setFixSummary('No edits returned by model');
        return;
      }
      setFixState('applying');
      vscodeApi.postMessage({ type: 'apply-fix', edits: data.edits });
      // fix-result message from extension will update fixState
    } catch (e: any) {
      setFixState('error');
      setFixSummary((e as Error).message);
    }
  };

  const useCapturedError = () => {
    setErrorLog(capturedError);
    setCapturedError('');
  };

  const indexBadge = () => {
    if (reindexingFile)
      return <span className="flex items-center gap-1 text-[10px] text-[#cca700]"><RefreshCw className="w-3 h-3 animate-spin" />{reindexingFile}</span>;
    if (indexState === 'indexing')
      return <span className="flex items-center gap-1 text-[10px] text-[#cca700]"><Loader className="w-3 h-3 animate-spin" />Indexing...</span>;
    if (indexState === 'done')
      return <span className="flex items-center gap-1 text-[10px] text-[#4ec9b0]"><CheckCircle className="w-3 h-3" />{indexedChunks} functions indexed</span>;
    if (indexState === 'error')
      return <span className="text-[10px] text-[#f44747]" title={indexError}>Index error — retry</span>;
    return null;
  };

  const fixButtonContent = () => {
    if (fixState === 'generating') return <><Loader className="w-4 h-4 animate-spin" />Generating fix...</>;
    if (fixState === 'applying')  return <><Loader className="w-4 h-4 animate-spin" />Applying...</>;
    return <><Wrench className="w-4 h-4" />Apply Fix</>;
  };

  return (
    <div className="size-full flex flex-col bg-[#1e1e1e] text-[#cccccc]">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[#2d2d2d] flex items-center justify-between">
        <div>
          <h1 className="text-sm font-semibold text-[#ffffff]">TraceBack</h1>
          <p className="text-xs text-[#858585] mt-0.5">Incident Context Engine</p>
        </div>
        <div className="flex items-center gap-2">
          {indexBadge()}
          <button
            onClick={() => handleIndex()}
            disabled={indexState === 'indexing' || !WORKSPACE}
            title="Re-index workspace"
            className="p-1.5 rounded hover:bg-[#2d2d2d] disabled:opacity-40 text-[#858585] hover:text-[#cccccc] transition-colors"
          >
            <FolderSearch className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Captured error banner */}
      {capturedError && (
        <div className="mx-4 mt-3 px-3 py-2 bg-[#2d1a1a] border border-[#6b2d2d] rounded flex items-center gap-2">
          <AlertCircle className="w-3.5 h-3.5 text-[#f44747] shrink-0" />
          <span className="flex-1 text-xs text-[#f44747] font-medium">Error captured from terminal</span>
          <button
            onClick={useCapturedError}
            className="text-[10px] px-2 py-1 bg-[#3c2424] hover:bg-[#4a2c2c] text-[#f88080] rounded transition-colors"
          >
            Use it
          </button>
          <button onClick={() => setCapturedError('')} className="text-[#858585] hover:text-[#cccccc] transition-colors" title="Dismiss">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Input */}
      <div className="px-4 py-4 border-b border-[#2d2d2d]">
        <label className="block text-xs font-medium mb-2 text-[#ffffff]">Paste Error Log</label>
        <textarea
          value={errorLog}
          onChange={(e) => setErrorLog(e.target.value)}
          placeholder="Paste your stack trace or error log here... (errors from terminal are captured automatically)"
          className="w-full h-32 px-3 py-2 bg-[#252526] border border-[#3c3c3c] rounded text-sm font-mono text-[#cccccc] placeholder-[#6a6a6a] focus:outline-none focus:border-[#007acc] resize-none"
        />
        <div className="mt-3 flex gap-2">
          <button
            onClick={handleAnalyze}
            disabled={!errorLog.trim() || isAnalyzing}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-[#0e639c] hover:bg-[#1177bb] disabled:bg-[#2d2d2d] disabled:text-[#6a6a6a] text-white text-sm font-medium rounded transition-colors"
          >
            {isAnalyzing ? <><Loader className="w-4 h-4 animate-spin" />Analyzing...</> : <><Send className="w-4 h-4" />Analyze</>}
          </button>

          {/* Apply Fix — only shown when there's an analysis result and we're running inside VS Code */}
          {analysisResult && vscodeApi && (
            <button
              onClick={handleApplyFix}
              disabled={fixState === 'generating' || fixState === 'applying'}
              className="flex items-center justify-center gap-2 px-4 py-2 bg-[#1e4d1e] hover:bg-[#256325] disabled:bg-[#2d2d2d] disabled:text-[#6a6a6a] text-[#4ec9b0] text-sm font-medium rounded transition-colors"
            >
              {fixButtonContent()}
            </button>
          )}
        </div>

        {/* Fix result status */}
        {fixState === 'done' && (
          <p className="mt-2 text-xs text-[#4ec9b0] flex items-center gap-1">
            <CheckCircle className="w-3 h-3" />{fixSummary}
          </p>
        )}
        {fixState === 'error' && (
          <p className="mt-2 text-xs text-[#f44747] flex items-center gap-1">
            <AlertCircle className="w-3 h-3" />{fixSummary || 'Fix could not be applied'}
          </p>
        )}
      </div>

      {/* Output */}
      <div className="flex-1 overflow-auto px-4 py-4">
        {analysisResult ? (
          <div className="prose prose-invert prose-sm max-w-none">
            <ReactMarkdown
              components={{
                h2: ({ node, ...props }) => <h2 className="text-base font-semibold text-[#ffffff] mt-4 mb-2" {...props} />,
                h3: ({ node, ...props }) => <h3 className="text-sm font-semibold text-[#ffffff] mt-3 mb-1.5" {...props} />,
                p: ({ node, ...props }) => <p className="text-sm text-[#cccccc] mb-2" {...props} />,
                code: ({ node, inline, ...props }: any) =>
                  inline ? (
                    <code className="px-1.5 py-0.5 bg-[#2d2d2d] text-[#d4d4d4] rounded text-xs font-mono" {...props} />
                  ) : (
                    <code className="block px-3 py-2 bg-[#1e1e1e] border border-[#2d2d2d] text-[#d4d4d4] rounded text-xs font-mono overflow-x-auto" {...props} />
                  ),
                pre: ({ node, ...props }) => <pre className="mb-3 overflow-x-auto" {...props} />,
                ul: ({ node, ...props }) => <ul className="text-sm text-[#cccccc] mb-2 list-disc list-inside" {...props} />,
                ol: ({ node, ...props }) => <ol className="text-sm text-[#cccccc] mb-2 list-decimal list-inside" {...props} />,
                strong: ({ node, ...props }) => <strong className="text-[#ffffff] font-semibold" {...props} />,
              }}
            >
              {analysisResult}
            </ReactMarkdown>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-center text-[#6a6a6a]">
            <svg className="w-12 h-12 mb-3 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <p className="text-sm">No analysis yet</p>
            <p className="text-xs mt-1">Paste an error log above, or run your code — errors are captured automatically</p>
          </div>
        )}
      </div>
    </div>
  );
}
