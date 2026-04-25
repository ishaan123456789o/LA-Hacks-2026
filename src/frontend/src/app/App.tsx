import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Send } from 'lucide-react';

export default function App() {
  const [errorLog, setErrorLog] = useState('');
  const [analysisResult, setAnalysisResult] = useState('');
  const [isAnalyzing, setIsAnalyzing] = useState(false);

  const handleAnalyze = async () => {
    if (!errorLog.trim()) return;

    setIsAnalyzing(true);

    // Simulate API call to Fetch.ai agents
    setTimeout(() => {
      // Mock response for demo purposes
      const mockResult = `## Incident Context Kit

**Error Type:** \`TypeError\`
**Failing Function:** \`processUserData()\`

### Root Cause Chain

1. **File:** \`src/services/userService.ts:42\`
   \`\`\`typescript
   async function processUserData(userId: string) {
     const user = await fetchUser(userId);
     return user.profile.email; // Error: Cannot read 'email' of undefined
   }
   \`\`\`

2. **File:** \`src/api/userApi.ts:18\`
   \`\`\`typescript
   async function fetchUser(id: string) {
     const response = await fetch(\`/api/users/\${id}\`);
     return response.json(); // Missing profile field validation
   }
   \`\`\`

### Recommended Fix

Add null checks and validation:
\`\`\`typescript
async function processUserData(userId: string) {
  const user = await fetchUser(userId);
  if (!user?.profile?.email) {
    throw new Error('User profile incomplete');
  }
  return user.profile.email;
}
\`\`\`

**Dependencies Traced:** 2 files, 3 functions
**Confidence:** 94%`;

      setAnalysisResult(mockResult);
      setIsAnalyzing(false);
    }, 1500);
  };

  return (
    <div className="size-full flex flex-col bg-[#1e1e1e] text-[#cccccc]">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[#2d2d2d]">
        <h1 className="text-sm font-semibold text-[#ffffff]">TraceBack</h1>
        <p className="text-xs text-[#858585] mt-0.5">Incident Context Engine</p>
      </div>

      {/* Input Section */}
      <div className="px-4 py-4 border-b border-[#2d2d2d]">
        <label className="block text-xs font-medium mb-2 text-[#ffffff]">
          Paste Error Log
        </label>
        <textarea
          value={errorLog}
          onChange={(e) => setErrorLog(e.target.value)}
          placeholder="Paste your stack trace or error log here..."
          className="w-full h-32 px-3 py-2 bg-[#252526] border border-[#3c3c3c] rounded text-sm font-mono text-[#cccccc] placeholder-[#6a6a6a] focus:outline-none focus:border-[#007acc] resize-none"
        />

        <button
          onClick={handleAnalyze}
          disabled={!errorLog.trim() || isAnalyzing}
          className="mt-3 w-full flex items-center justify-center gap-2 px-4 py-2 bg-[#0e639c] hover:bg-[#1177bb] disabled:bg-[#2d2d2d] disabled:text-[#6a6a6a] text-white text-sm font-medium rounded transition-colors"
        >
          {isAnalyzing ? (
            <>
              <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              Analyzing...
            </>
          ) : (
            <>
              <Send className="w-4 h-4" />
              Analyze Trace
            </>
          )}
        </button>
      </div>

      {/* Output Section */}
      <div className="flex-1 overflow-auto px-4 py-4">
        {analysisResult ? (
          <div className="prose prose-invert prose-sm max-w-none">
            <ReactMarkdown
              components={{
                h2: ({node, ...props}) => <h2 className="text-base font-semibold text-[#ffffff] mt-4 mb-2" {...props} />,
                h3: ({node, ...props}) => <h3 className="text-sm font-semibold text-[#ffffff] mt-3 mb-1.5" {...props} />,
                p: ({node, ...props}) => <p className="text-sm text-[#cccccc] mb-2" {...props} />,
                code: ({node, inline, ...props}: any) =>
                  inline ? (
                    <code className="px-1.5 py-0.5 bg-[#2d2d2d] text-[#d4d4d4] rounded text-xs font-mono" {...props} />
                  ) : (
                    <code className="block px-3 py-2 bg-[#1e1e1e] border border-[#2d2d2d] text-[#d4d4d4] rounded text-xs font-mono overflow-x-auto" {...props} />
                  ),
                pre: ({node, ...props}) => <pre className="mb-3 overflow-x-auto" {...props} />,
                ul: ({node, ...props}) => <ul className="text-sm text-[#cccccc] mb-2 list-disc list-inside" {...props} />,
                ol: ({node, ...props}) => <ol className="text-sm text-[#cccccc] mb-2 list-decimal list-inside" {...props} />,
                strong: ({node, ...props}) => <strong className="text-[#ffffff] font-semibold" {...props} />,
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
            <p className="text-xs mt-1">Paste an error log above to get started</p>
          </div>
        )}
      </div>
    </div>
  );
}