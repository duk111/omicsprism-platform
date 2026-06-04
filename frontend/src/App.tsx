import { useEffect, useMemo, useState } from "react";
import type { AnalysisType, ImageInfo, JobFilesResponse, JobResponse, JobStatus } from "./api-types";
import { apiFetchJson, ApiRequestError } from "./api";
import { formatSeconds } from "./app/jobs/progress";
import { ANALYSIS_LABELS, STATUS_LABELS, formatDateTime } from "./app/jobs/jobUtils";
import { useJobProgressSubscription } from "./app/jobs/useJobProgressSubscription";
import FigureViewer from "./viewer/FigureViewer";
import "./App.css";

type View = { name: "form" } | { name: "progress"; jobId: string } | { name: "results"; jobId: string };

export default function App() {
  const [view, setView] = useState<View>({ name: "form" });
  const [jobListKey, setJobListKey] = useState(0);

  useEffect(() => {
    if (view.name === "form") document.title = "OmicsPrism";
    else if (view.name === "progress") document.title = "Running · OmicsPrism";
    else document.title = "Results · OmicsPrism";
  }, [view]);

  function goForm() { setView({ name: "form" }); }
  function goProgress(jobId: string) { setView({ name: "progress", jobId }); }
  function goResults(jobId: string) { setView({ name: "results", jobId }); }

  return (
    <div className="platform-shell">
      <header className="topbar">
        <button className="brand" type="button" onClick={goForm}>OmicsPrism</button>
        <nav className="topnav">
          <button type="button" className={view.name === "form" ? "active-nav" : ""} onClick={goForm}>New Analysis</button>
          <button type="button" className={view.name === "form" ? "" : "active-nav"} onClick={() => { setView({ name: "form" }); setJobListKey(k => k + 1); }}>My Jobs</button>
        </nav>
      </header>
      {view.name === "form" && (
        <AnalysisPage
          onProgress={goProgress}
          jobListKey={jobListKey}
        />
      )}
      {view.name === "progress" && (
        <ProgressPage jobId={view.jobId} onResults={() => goResults(view.jobId)} onBack={goForm} />
      )}
      {view.name === "results" && (
        <ResultsPage jobId={view.jobId} onBack={goForm} onProgress={() => goProgress(view.jobId)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Analysis Page: form + job list                                     */
/* ------------------------------------------------------------------ */

function DropZone({ label, required, file, onFile }: {
  label: string; required: boolean; file: File | null; onFile: (f: File | null) => void;
}) {
  const [over, setOver] = useState(false);

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) onFile(f);
  }

  return (
    <label
      className={`drop-zone${over ? " drop-active" : ""}${file ? " drop-has-file" : ""}`}
      onDragOver={e => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={handleDrop}
    >
      <span className="drop-label">{label}{required && " *"}</span>
      {file ? (
        <span className="drop-file">
          <span className="drop-file-name">{file.name}</span>
          <span className="drop-file-size">{(file.size / 1024).toFixed(0)} KB</span>
          <button type="button" className="drop-clear" onClick={e => { e.preventDefault(); onFile(null); }}>
            &times;
          </button>
        </span>
      ) : (
        <span className="drop-hint">Drop CSV file or click to browse</span>
      )}
      <input
        type="file"
        accept=".csv,.tsv,.txt"
        className="drop-input"
        onChange={e => onFile(e.target.files?.[0] ?? null)}
      />
    </label>
  );
}

function AnalysisPage({ onProgress, jobListKey }: { onProgress: (jobId: string) => void; jobListKey: number }) {
  const [tab, setTab] = useState<"new" | "jobs">("new");
  const [selectedType, setSelectedType] = useState<AnalysisType | null>(null);

  return (
    <main className="page narrow">
      <div className="tab-bar">
        <button className={tab === "new" ? "primary" : "secondary"} type="button" onClick={() => setTab("new")}>New Analysis</button>
        <button className={tab === "new" ? "secondary" : "primary"} type="button" onClick={() => setTab("jobs")}>My Jobs</button>
      </div>
      {tab === "new" && !selectedType && (
        <WelcomeCards onSelect={setSelectedType} />
      )}
      {tab === "new" && selectedType && (
        <AnalysisForm
          initialType={selectedType}
          onProgress={onProgress}
          onBack={() => setSelectedType(null)}
        />
      )}
      {tab === "jobs" && <JobList key={jobListKey} onProgress={onProgress} />}
    </main>
  );
}

function WelcomeCards({ onSelect }: { onSelect: (t: AnalysisType) => void }) {
  return (
    <section className="welcome">
      <h1 className="welcome-title">OmicsPrism</h1>
      <p className="welcome-desc">
        Upload your omics data and run statistical analyses with publication-ready visualizations.
        No account required: your data stays in this browser session.
      </p>
      <div className="welcome-cards">
        <button className="welcome-card" type="button" onClick={() => onSelect("differential")}>
          <span className="welcome-card-icon">DEG</span>
          <strong>Differential Expression</strong>
          <p>Compare gene expression between groups using DESeq2. Upload raw counts and sample metadata to identify differentially expressed genes with volcano plots and MA plots.</p>
          <span className="welcome-card-cta">Start &rarr;</span>
        </button>
        <button className="welcome-card" type="button" onClick={() => onSelect("correlation")}>
          <span className="welcome-card-icon">GMA</span>
          <strong>Gene-Metabolite Association</strong>
          <p>Discover relationships between transcriptome and metabolome. Three-way screening, ElasticNet + XGBoost modeling, RRA aggregation, WGCNA-style module detection, and interactive network visualizations.</p>
          <span className="welcome-card-cta">Start &rarr;</span>
        </button>
        <button className="welcome-card" type="button" onClick={() => onSelect("dem")}>
          <span className="welcome-card-icon">DEM</span>
          <strong>Differential Metabolite Analysis</strong>
          <p>Identify differentially abundant metabolites between groups using OPLS-DA with VIP scoring. Upload metabolite abundance matrix and sample metadata for volcano plots, VIP bar plots, and multi-contrast comparison.</p>
          <span className="welcome-card-cta">Start &rarr;</span>
        </button>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Analysis Form                                                       */
/* ------------------------------------------------------------------ */

type FormState = {
  analysisType: AnalysisType;
  files: Record<string, File | null>;
  params: Record<string, string | number | boolean>;
  submitting: boolean;
  error: string | null;
  preflightResult: string | null;
};

const DEG_FIELDS: { name: string; label: string; required: boolean }[] = [
  { name: "counts", label: "Counts matrix (CSV)", required: true },
  { name: "metadata", label: "Metadata table (CSV)", required: true },
];

const CORR_FIELDS: { name: string; label: string; required: boolean }[] = [
  { name: "transcriptome", label: "Transcriptome matrix (CSV)", required: true },
  { name: "metabolome", label: "Metabolome matrix (CSV)", required: true },
  { name: "group", label: "Group table (CSV)", required: true },
];

const DEM_FIELDS: { name: string; label: string; required: boolean }[] = [
  { name: "metabs", label: "Metabolite abundance matrix (CSV)", required: true },
  { name: "metadata", label: "Metadata table (CSV)", required: true },
];

function AnalysisForm({ initialType, onProgress, onBack }: {
  initialType: AnalysisType; onProgress: (jobId: string) => void; onBack: () => void;
}) {
  const [state, setState] = useState<FormState>({
    analysisType: initialType,
    files: {},
    params: {},
    submitting: false,
    error: null,
    preflightResult: null,
  });

  function setFile(name: string, file: File | null) {
    setState(s => ({ ...s, files: { ...s.files, [name]: file }, error: null, preflightResult: null }));
  }

  function setParam(name: string, value: string | number | boolean) {
    setState(s => ({ ...s, params: { ...s.params, [name]: value }, error: null }));
  }

  function setType(t: AnalysisType) {
    setState(s => ({ ...s, analysisType: t, files: {}, params: {}, error: null, preflightResult: null }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setState(s => ({ ...s, submitting: true, error: null }));

    const formData = new FormData();
    formData.set("analysis_type", state.analysisType);
    const fields =
      state.analysisType === "differential" ? DEG_FIELDS :
      state.analysisType === "dem" ? DEM_FIELDS :
      CORR_FIELDS;
    for (const f of fields) {
      const file = state.files[f.name];
      if (f.required && !file) {
        setState(s => ({ ...s, submitting: false, error: `${f.label} is required.` }));
        return;
      }
      if (file) formData.set(f.name, file);
    }
    if (state.analysisType === "differential") {
      const compareField = String(state.params.compare_field || "");
      const testedLevels = String(state.params.tested_levels || "");
      const referenceLevel = String(state.params.reference_level || "");
      if (!compareField || !testedLevels || !referenceLevel) {
        setState(s => ({ ...s, submitting: false, error: "compare_field, tested_levels, and reference_level are required." }));
        return;
      }
      formData.set("compare_field", compareField);
      formData.set("tested_levels", testedLevels);
      formData.set("reference_level", referenceLevel);
      formData.set("same_fields", String(state.params.same_fields || ""));
      formData.set("padj_cutoff", String(state.params.padj_cutoff ?? 0.05));
      formData.set("log2fc_cutoff", String(state.params.log2fc_cutoff ?? 1.0));
      formData.set("min_total_count", String(state.params.min_total_count ?? 10));
      formData.set("min_replicates", String(state.params.min_replicates ?? 2));
    } else if (state.analysisType === "dem") {
      const compareField = String(state.params.compare_field || "");
      const testedLevels = String(state.params.tested_levels || "");
      const referenceLevel = String(state.params.reference_level || "");
      if (!compareField || !testedLevels || !referenceLevel) {
        setState(s => ({ ...s, submitting: false, error: "compare_field, tested_levels, and reference_level are required." }));
        return;
      }
      formData.set("compare_field", compareField);
      formData.set("tested_levels", testedLevels);
      formData.set("reference_level", referenceLevel);
      formData.set("same_fields", String(state.params.same_fields || ""));
      formData.set("padj_cutoff", String(state.params.padj_cutoff ?? 0.05));
      formData.set("log2fc_cutoff", String(state.params.log2fc_cutoff ?? 1.0));
      formData.set("vip_cutoff", String(state.params.vip_cutoff ?? 1.0));
      formData.set("max_missing_fraction", String(state.params.max_missing_fraction ?? 0.5));
      formData.set("impute_method", String(state.params.impute_method || "half-min"));
      formData.set("normalize", String(state.params.normalize ?? true));
      formData.set("log_transform", String(state.params.log_transform ?? true));
      formData.set("min_replicates", String(state.params.min_replicates ?? 2));
      formData.set("n_orthogonal_components", String(state.params.n_orthogonal_components ?? 1));
    } else {
    }

    try {
      const data = await apiFetchJson("/api/jobs", { method: "POST", body: formData });
      onProgress(data.id);
    } catch (err) {
      const msg = err instanceof ApiRequestError ? err.message : "Submission failed.";
      setState(s => ({ ...s, submitting: false, error: msg }));
    }
  }

  const fields =
    state.analysisType === "differential" ? DEG_FIELDS :
    state.analysisType === "dem" ? DEM_FIELDS :
    CORR_FIELDS;
  const isDEG = state.analysisType === "differential";
  const isDEM = state.analysisType === "dem";

  return (
    <section className="panel">
      <div className="panel-head">
        <h1>{
          state.analysisType === "differential" ? "Differential Expression" :
          state.analysisType === "dem" ? "Differential Metabolite Analysis" :
          "Gene-Metabolite Association"
        }</h1>
        <button className="secondary" type="button" onClick={onBack}>Back</button>
      </div>
      <form className="analysis-form" onSubmit={handleSubmit}>
        <div className="field-group">
          <h3>Upload files</h3>
          {fields.map(f => (
            <DropZone
              key={f.name}
              label={f.label}
              required={f.required}
              file={state.files[f.name] ?? null}
              onFile={file => setFile(f.name, file)}
            />
          ))}
        </div>

        {isDEG && (
          <div className="field-group">
            <h3>Differential expression parameters</h3>
            <label className="field">
              <span>Compare field *</span>
              <input
                type="text"
                placeholder="e.g. group1"
                value={String(state.params.compare_field || "")}
                onChange={e => setParam("compare_field", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Tested levels (comma-separated) *</span>
              <input
                type="text"
                placeholder="e.g. Treatment_A,Treatment_B"
                value={String(state.params.tested_levels || "")}
                onChange={e => setParam("tested_levels", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Reference level *</span>
              <input
                type="text"
                placeholder="e.g. Control"
                value={String(state.params.reference_level || "")}
                onChange={e => setParam("reference_level", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Same fields (comma-separated, optional)</span>
              <input
                type="text"
                placeholder="e.g. batch,time"
                value={String(state.params.same_fields || "")}
                onChange={e => setParam("same_fields", e.target.value)}
              />
            </label>
            <div className="field-row">
              <label className="field">
                <span>Adjusted p-value cutoff</span>
                <input type="number" step="0.01" min="0" max="1" value={String(state.params.padj_cutoff ?? 0.05)}
                  onChange={e => setParam("padj_cutoff", parseFloat(e.target.value) || 0.05)} />
              </label>
              <label className="field">
                <span>|log2FC| cutoff</span>
                <input type="number" step="0.1" min="0" value={String(state.params.log2fc_cutoff ?? 1.0)}
                  onChange={e => setParam("log2fc_cutoff", parseFloat(e.target.value) || 1.0)} />
              </label>
            </div>
            <div className="field-row">
              <label className="field">
                <span>Min total count</span>
                <input type="number" min="1" value={String(state.params.min_total_count ?? 10)}
                  onChange={e => setParam("min_total_count", parseInt(e.target.value) || 10)} />
              </label>
              <label className="field">
                <span>Min replicates</span>
                <input type="number" min="2" value={String(state.params.min_replicates ?? 2)}
                  onChange={e => setParam("min_replicates", parseInt(e.target.value) || 2)} />
              </label>
            </div>
          </div>
        )}

        {isDEM && (
          <div className="field-group">
            <h3>DEM parameters</h3>
            <label className="field">
              <span>Compare field *</span>
              <input type="text" placeholder="e.g. group1"
                value={String(state.params.compare_field || "")}
                onChange={e => setParam("compare_field", e.target.value)} />
            </label>
            <label className="field">
              <span>Tested levels (comma-separated) *</span>
              <input type="text" placeholder="e.g. Treatment_A,Treatment_B"
                value={String(state.params.tested_levels || "")}
                onChange={e => setParam("tested_levels", e.target.value)} />
            </label>
            <label className="field">
              <span>Reference level *</span>
              <input type="text" placeholder="e.g. Control"
                value={String(state.params.reference_level || "")}
                onChange={e => setParam("reference_level", e.target.value)} />
            </label>
            <label className="field">
              <span>Same fields (comma-separated, optional)</span>
              <input type="text" placeholder="e.g. batch,time"
                value={String(state.params.same_fields || "")}
                onChange={e => setParam("same_fields", e.target.value)} />
            </label>
            <div className="field-row">
              <label className="field">
                <span>Adjusted p-value cutoff</span>
                <input type="number" step="0.01" min="0" max="1"
                  value={String(state.params.padj_cutoff ?? 0.05)}
                  onChange={e => setParam("padj_cutoff", parseFloat(e.target.value) || 0.05)} />
              </label>
              <label className="field">
                <span>|log2FC| cutoff</span>
                <input type="number" step="0.1" min="0"
                  value={String(state.params.log2fc_cutoff ?? 1.0)}
                  onChange={e => setParam("log2fc_cutoff", parseFloat(e.target.value) || 1.0)} />
              </label>
            </div>
            <div className="field-row">
              <label className="field">
                <span>VIP cutoff</span>
                <input type="number" step="0.1" min="0"
                  value={String(state.params.vip_cutoff ?? 1.0)}
                  onChange={e => setParam("vip_cutoff", parseFloat(e.target.value) || 1.0)} />
              </label>
              <label className="field">
                <span>Min replicates</span>
                <input type="number" min="2"
                  value={String(state.params.min_replicates ?? 2)}
                  onChange={e => setParam("min_replicates", parseInt(e.target.value) || 2)} />
              </label>
            </div>
            <div className="field-row">
              <label className="field">
                <span>Max missing fraction</span>
                <input type="number" step="0.05" min="0" max="1"
                  value={String(state.params.max_missing_fraction ?? 0.5)}
                  onChange={e => setParam("max_missing_fraction", parseFloat(e.target.value) || 0.5)} />
              </label>
              <label className="field">
                <span>Orthogonal components</span>
                <input type="number" min="0" max="5"
                  value={String(state.params.n_orthogonal_components ?? 1)}
                  onChange={e => setParam("n_orthogonal_components", parseInt(e.target.value) || 1)} />
              </label>
            </div>
            <label className="field">
              <span>Impute method</span>
              <select
                value={String(state.params.impute_method || "half-min")}
                onChange={e => setParam("impute_method", e.target.value)}
              >
                <option value="half-min">Half-minimum</option>
                <option value="median">Median</option>
              </select>
            </label>
            <div className="field-row">
              <label className="field checkbox-field">
                <input type="checkbox"
                  checked={Boolean(state.params.normalize ?? true)}
                  onChange={e => setParam("normalize", e.target.checked)} />
                <span>Median normalize</span>
              </label>
              <label className="field checkbox-field">
                <input type="checkbox"
                  checked={Boolean(state.params.log_transform ?? true)}
                  onChange={e => setParam("log_transform", e.target.checked)} />
                <span>Log2 transform</span>
              </label>
            </div>
          </div>
        )}

        {!isDEG && !isDEM && (
          <p className="panel-note">Analysis runs with default parameters (FDR 0.05, Spearman correlation, WGCNA module detection).</p>
        )}

        {state.error && <p className="failure-text">{state.error}</p>}
        {state.preflightResult && <pre className="log-box">{state.preflightResult}</pre>}

        <button className="primary" type="submit" disabled={state.submitting}>
          {state.submitting ? "Submitting..." : "Start analysis"}
        </button>
      </form>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Job List                                                            */
/* ------------------------------------------------------------------ */

function JobList({ onProgress }: { onProgress: (jobId: string) => void }) {
  const [jobs, setJobs] = useState<JobResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      setLoading(true);
      try {
        const data = await apiFetchJson("/api/jobs");
        if (alive) { setJobs(data.jobs ?? []); setError(null); }
      } catch (err) {
        if (alive) setError(err instanceof ApiRequestError ? err.message : "Failed to load jobs.");
      } finally {
        if (alive) setLoading(false);
      }
    }
    void load();
    return () => { alive = false; };
  }, []);

  async function deleteJob(jobId: string) {
    try {
      await fetch(`/api/jobs/${jobId}`, { method: "DELETE", credentials: "include" });
      setJobs(prev => prev.filter(j => j.id !== jobId));
    } catch { /* ignore */ }
  }

  if (loading) return <section className="panel"><p>Loading jobs...</p></section>;
  if (error) return <section className="panel"><p className="failure-text">{error}</p></section>;
  if (jobs.length === 0) {
    return (
      <section className="panel">
        <p className="panel-note">No jobs yet. Create your first analysis above.</p>
      </section>
    );
  }

  return (
    <section className="panel">
      <h1>My Jobs</h1>
      <div className="job-table-wrap">
        <table className="job-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Status</th>
              <th>Created</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map(job => (
              <tr key={job.id}>
                <td>{ANALYSIS_LABELS[job.analysis_type]}</td>
                <td><span className={`status-pill ${job.status}`}>{STATUS_LABELS[job.status]}</span></td>
                <td>{formatDateTime(job.created_at)}</td>
                <td className="row-actions compact-actions">
                  {job.status === "succeeded" || job.status === "failed" ? (
                    <button className="secondary" type="button" onClick={() => onProgress(job.id)}>View</button>
                  ) : (
                    <button className="secondary" type="button" onClick={() => onProgress(job.id)}>Progress</button>
                  )}
                  <button className="secondary danger-action" type="button" onClick={() => deleteJob(job.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Progress Page                                                       */
/* ------------------------------------------------------------------ */

const STAGES = [
  { key: "load", label: "Load" },
  { key: "preprocess", label: "Preprocess" },
  { key: "analyze", label: "Analyze" },
  { key: "output", label: "Output" },
];

function stageIndex(step: string): number {
  const s = step.toLowerCase();
  if (s.includes("prepar") || s.includes("load") || s.includes("queued")) return 0;
  if (s.includes("preprocess")) return 1;
  if (s.includes("run") || s.includes("anal")) return 2;
  if (s.includes("generat") || s.includes("complet") || s.includes("visual")) return 3;
  return 0;
}

function StageIndicator({ step, status }: { step: string; status: string }) {
  const active = status === "succeeded" ? 4 : status === "failed" ? -1 : stageIndex(step);

  return (
    <div className="stage-bar">
      {STAGES.map((s, i) => (
        <div key={s.key} className={`stage-step${i < active ? " stage-done" : ""}${i === active ? " stage-current" : ""}`}>
          <span className="stage-dot" />
          <span className="stage-label">{s.label}</span>
        </div>
      ))}
    </div>
  );
}

function ProgressPage({ jobId, onResults, onBack }: { jobId: string; onResults: () => void; onBack: () => void }) {
  const [job, setJob] = useState<JobResponse | null>(null);
  const [showCompletionPrompt, setShowCompletionPrompt] = useState(false);
  const [wasSucceeded, setWasSucceeded] = useState(false);
  const { progress, error, mode, connectionState, reconnectAttempts } = useJobProgressSubscription(jobId);

  useEffect(() => {
    let alive = true;
    async function loadJob() {
      try {
        const data = await apiFetchJson(`/api/jobs/${jobId}`);
        if (alive) setJob(data);
      } catch { /* ignore */ }
    }
    void loadJob();
    return () => { alive = false; };
  }, [jobId]);

  useEffect(() => {
    if (progress?.status === "succeeded" && !wasSucceeded) {
      setWasSucceeded(true);
      setShowCompletionPrompt(true);
      const timer = window.setTimeout(onResults, 2000);
      return () => window.clearTimeout(timer);
    }
    setShowCompletionPrompt(false);
    return undefined;
  }, [onResults, progress?.status, wasSucceeded]);

  const status: JobStatus = progress?.status ?? job?.status ?? "queued";
  const pct = progress?.progress ?? job?.progress ?? 0;
  const step = progress?.progress_step ?? job?.progress_step ?? "Waiting";
  const remaining = progress?.estimated_remaining_seconds ?? job?.estimated_remaining_seconds ?? null;
  const elapsed = progress?.elapsed_seconds ?? job?.elapsed_seconds ?? null;
  const renderedFailure = (progress?.status === "failed" || job?.status === "failed")
    ? (progress?.error ?? job?.error ?? "Analysis failed")
    : null;
  const connectionLabel = connectionState === "open" ? "Live"
    : mode === "polling" ? "Polling"
    : connectionState === "recovering" ? `Reconnecting${reconnectAttempts ? ` (${reconnectAttempts})` : ""}`
    : "Connecting";

  async function cancelJob() {
    await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST", credentials: "include" });
  }

  return (
    <main className="page narrow">
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Job progress</p>
            <h1>{job?.project_name ?? jobId}</h1>
          </div>
          <div>
            <span className={`status-pill ${status}`}>{STATUS_LABELS[status]}</span>
            <small> {connectionLabel}</small>
          </div>
        </div>
        <div className={`progress-shell ${status}`}>
          <div className="progress-track"><div className="progress-fill" style={{ width: `${Math.max(3, pct)}%` }} /></div>
          <div className="progress-meta"><span>{pct}%</span><span>{step}</span></div>
        </div>
        <StageIndicator step={step} status={status} />
        <div className="metric-grid">
          <div className="metric"><span>Status</span><strong>{STATUS_LABELS[status]}</strong></div>
          <div className="metric"><span>Remaining</span><strong>{formatSeconds(remaining)}</strong></div>
          <div className="metric"><span>Elapsed</span><strong>{formatSeconds(elapsed)}</strong></div>
        </div>
        {showCompletionPrompt && (
          <div className="completion-box">
            <strong>Analysis finished</strong>
            <button className="primary" type="button" onClick={onResults}>Open results</button>
          </div>
        )}
        {renderedFailure && (
          <div className="failure-box">
            <h2>Analysis failed</h2>
            <pre className="log-box">{renderedFailure}</pre>
          </div>
        )}
        {(error || progress?.error) && !renderedFailure && <pre className="log-box">{progress?.error ?? error}</pre>}
        <section>
          <h2>Recent log</h2>
          <pre className="log-box">{progress?.recent_log_excerpt || "Waiting for worker..."}</pre>
        </section>
        <div className="row-actions">
          <button className="secondary" type="button" onClick={onBack}>Back</button>
          {status === "queued" || status === "running" ? (
            <button className="secondary danger-action" type="button" onClick={cancelJob}>Cancel</button>
          ) : null}
          <button className="primary" type="button" disabled={status !== "succeeded"} onClick={onResults}>View results</button>
        </div>
      </section>
    </main>
  );
}

/* ------------------------------------------------------------------ */
/* Results Page                                                        */
/* ------------------------------------------------------------------ */

function figureTypeLabel(filename: string): string {
  const n = filename.toLowerCase();
  if (n.includes("volcano")) return "Volcano";
  if (n.includes("pca")) return "PCA";
  if (n.includes("heatmap") || n.includes("eigengene")) return "Heatmap";
  if (n.includes("circos")) return "Circos";
  if (n.includes("network") || n.includes("cnet")) return "Network";
  if (n.includes("upset")) return "UpSet";
  if (n.includes("scatter") || n.includes("regression") || n.includes("pairs")) return "Scatter";
  if (n.includes("dendrogram") || n.includes("cluster")) return "Dendrogram";
  if (n.includes("ma_plot") || n.includes("ma.")) return "MA Plot";
  if (n.includes("vip") && !n.includes("vip_log2fc") && !n.includes("padj_log2fc")) return "VIP Bar";
  if (n.includes("oplsda") || n.includes("score")) return "OPLS-DA Scores";
  if (n.includes("sankey")) return "Sankey";
  if (n.includes("bar") || n.includes("count")) return "Bar";
  return "Figure";
}

function ResultsPage({ jobId, onBack, onProgress }: { jobId: string; onBack: () => void; onProgress: () => void }) {
  const [job, setJob] = useState<JobResponse | null>(null);
  const [files, setFiles] = useState<JobFilesResponse | null>(null);
  const [images, setImages] = useState<ImageInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedImage, setSelectedImage] = useState<ImageInfo | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const [jobData, filesData] = await Promise.all([
          apiFetchJson(`/api/jobs/${jobId}`),
          apiFetchJson(`/api/jobs/${jobId}/files`),
        ]);
        setJob(jobData);
        setFiles(filesData);
      } catch (err) {
        setError(err instanceof ApiRequestError ? err.message : "Failed to load results.");
      }
      try {
        const imgData: ImageInfo[] = await apiFetchJson(`/api/jobs/${jobId}/images`);
        setImages((imgData ?? []).filter(img => img.name.endsWith(".svg")));
      } catch { /* images optional */ }
    }
    void load();
  }, [jobId]);

  const archiveUrl = files?.result_files.find(f => f.path.endsWith("OmicsPrism_results.zip"))?.download_url ?? null;
  const tableFiles = (files?.result_files ?? []).filter(f =>
    f.name.endsWith(".csv") || f.name.endsWith(".zip")
  );

  if (error) {
    return (
      <main className="page narrow">
        <section className="panel">
          <div className="failure-box">
            <h2>Results unavailable</h2>
            <p>{error}</p>
            <div className="row-actions">
              <button className="primary" type="button" onClick={onProgress}>View log</button>
              <button className="secondary" type="button" onClick={onBack}>Back</button>
            </div>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="page">
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Results</p>
            <h1>{job?.project_name ?? jobId}</h1>
          </div>
          <div className="row-actions compact-actions">
            {archiveUrl && <a className="primary" href={archiveUrl}>Download ZIP</a>}
            <button className="secondary" type="button" onClick={onProgress}>Log</button>
            <button className="secondary" type="button" onClick={onBack}>Back</button>
          </div>
        </div>

        <div className="row-actions">
          {files?.report_links.summary && <a className="primary" href={files.report_links.summary}>Summary report</a>}
          {files?.report_links.interactive && <a className="secondary" href={files.report_links.interactive}>Interactive report</a>}
        </div>

        {images.length > 0 && (
          <section>
            <h2>Visualizations</h2>
            <div className="result-figure-grid">
              {images.map(img => (
                <button key={img.path} className="result-figure-card" type="button" onClick={() => setSelectedImage(img)}>
                  <div className="result-figure-thumb">
                    <img src={img.thumbnail_url} alt={img.name} loading="lazy" />
                  </div>
                  <div className="result-figure-body">
                    <span className="figure-type-tag">{figureTypeLabel(img.name)}</span>
                    <strong>{img.name}</strong>
                  </div>
                </button>
              ))}
            </div>
          </section>
        )}

        <section>
          <h2>Result tables</h2>
          <div className="file-list">
            {tableFiles.map(f => (
              <a className="file-row" key={f.path} href={f.download_url}>
                <span>{f.name}</span><em>{Math.ceil(f.size_bytes / 1024)} KB</em>
              </a>
            ))}
          </div>
        </section>

        {selectedImage && <FigureViewer image={selectedImage} onClose={() => setSelectedImage(null)} />}
      </section>
    </main>
  );
}
