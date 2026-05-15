import { useState } from "react";
import AnalysisSelector from "./components/AnalysisSelector";
import type { AnalysisType } from "./api-types";
import AnalysisForm from "./components/AnalysisForm";
import ProgressTracker from "./components/ProgressTracker";
import JobListPanel from "./components/JobListPanel";
import ViewerApp from "./viewer/ViewerApp";
import "./App.css";

type View = "selector" | "form" | "progress";

export default function App() {
  const searchParams = new URLSearchParams(window.location.search);
  const isViewer = searchParams.get("view") === "results";

  if (isViewer) {
    return <ViewerApp />;
  }

  return <MainApp />;
}

function MainApp() {
  const [view, setView] = useState<View>("selector");
  const [analysisType, setAnalysisType] = useState<AnalysisType | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  function handleSelectType(type: AnalysisType) {
    setAnalysisType(type);
    setView("form");
  }

  function handleSubmitStart(jobId: string) {
    setActiveJobId(jobId);
    setSelectedJobId(jobId);
    setView("progress");
  }

  function handleBack() {
    setView("selector");
    setAnalysisType(null);
    setActiveJobId(null);
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <div>
          <p className="app-eyebrow">OmicsPrism 在线分析平台</p>
          <h1 className="app-title">组学数据分析</h1>
        </div>
      </header>

      <div className="app-body">
        {view === "selector" && (
          <AnalysisSelector onSelect={handleSelectType} />
        )}

        {view === "form" && analysisType && (
          <AnalysisForm
            type={analysisType}
            onBack={handleBack}
            onSubmitStart={handleSubmitStart}
          />
        )}

        {view === "progress" && activeJobId && (
          <ProgressTracker jobId={activeJobId} onBack={handleBack} />
        )}
      </div>

      <JobListPanel onSelectJob={setSelectedJobId} selectedJobId={selectedJobId} />
    </main>
  );
}
