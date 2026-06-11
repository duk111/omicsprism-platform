import { useEffect, useState, useCallback, useMemo, type ReactNode } from "react";
import { apiFetchJson, ApiRequestError } from "../api";

export interface FigureData {
  figure_id: string;
  title: string;
  chart_type: string;
  interactive_page_id: string | null;
  static_files: Record<string, string | null>;
  plotly_spec: Record<string, unknown>;
  default_state: Record<string, unknown>;
  available_states: Record<string, unknown[]>;
  style: Record<string, unknown>;
  // Alternative data for merged multi-view pages (e.g. bubble-heatmap):
  alt_data?: Record<string, { plotly_spec?: Record<string, unknown>; default_state?: Record<string, unknown> }>;
  // Alternative data keys for non-Plotly charts:
  tree_data?: Record<string, unknown>;
  upset_data?: Record<string, unknown>;
  ridge_data?: Record<string, unknown>;
  circos_data?: Record<string, unknown>;
}

interface Props {
  jobId: string;
  pageId: string;
  pageTitle: string;
  children: (data: FigureData, controls: ControlsAPI) => ReactNode;
}

export interface ControlsAPI {
  state: Record<string, unknown>;
  setState: (key: string, value: unknown) => void;
  available: Record<string, unknown[]>;
  downloadPNG: () => void;
  downloadSVG: () => void;
  downloadPDF: () => void;
}

export function InteractivePageShell({ jobId, pageId, pageTitle, children }: Props) {
  const [data, setData] = useState<FigureData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [controls, setControls] = useState<Record<string, unknown>>({});

  useEffect(() => {
    let alive = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const result = await apiFetchJson(`/api/jobs/${jobId}/figure-data/${pageId}`);
        if (alive) {
          setData(result as FigureData);
          setControls((result as FigureData).default_state || {});
        }
      } catch (err) {
        if (alive) {
          setError(err instanceof ApiRequestError ? err.message : "Failed to load figure data");
        }
      } finally {
        if (alive) setLoading(false);
      }
    }
    void load();
    return () => { alive = false; };
  }, [jobId, pageId]);

  // Merge URL search params into initial state after data loads
  useEffect(() => {
    if (!data) return;
    const params = new URLSearchParams(window.location.search);
    if (params.toString()) {
      const overrides: Record<string, unknown> = {};
      params.forEach((v, k) => { overrides[k] = v === "true" ? true : v === "false" ? false : v; });
      setControls(prev => ({ ...prev, ...overrides }));
    }
  }, [data]);

  const setControl = useCallback((key: string, value: unknown) => {
    setControls(prev => ({ ...prev, [key]: value }));
  }, []);

  const downloadPNG = useCallback(() => {
    if (!data?.static_files?.png) return;
    const link = document.createElement("a");
    link.href = `/api/jobs/${jobId}/download/${data.static_files.png}`;
    link.download = `${pageId}.png`;
    link.click();
  }, [data, jobId, pageId]);

  const downloadSVG = useCallback(() => {
    if (!data?.static_files?.svg) return;
    const link = document.createElement("a");
    link.href = `/api/jobs/${jobId}/download/${data.static_files.svg}`;
    link.download = `${pageId}.svg`;
    link.click();
  }, [data, jobId, pageId]);

  const downloadPDF = useCallback(() => {
    if (!data?.static_files?.pdf) return;
    const link = document.createElement("a");
    link.href = `/api/jobs/${jobId}/download/${data.static_files.pdf}`;
    link.download = `${pageId}.pdf`;
    link.click();
  }, [data, jobId, pageId]);

  const controlsAPI: ControlsAPI = useMemo(() => ({
    state: controls,
    setState: setControl,
    available: data?.available_states || {},
    downloadPNG,
    downloadSVG,
    downloadPDF,
  }), [controls, setControl, data, downloadPNG, downloadSVG, downloadPDF]);

  if (loading) {
    return (
      <div className="ip-shell">
        <header className="ip-toolbar">
          <a className="ip-back" href="javascript:window.close()">&larr; Back</a>
          <h1 className="ip-title">{pageTitle}</h1>
        </header>
        <div className="ip-loading">Loading figure data...</div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="ip-shell">
        <header className="ip-toolbar">
          <a className="ip-back" href="javascript:window.close()">&larr; Back</a>
          <h1 className="ip-title">{pageTitle}</h1>
        </header>
        <div className="ip-error">
          <p>{error || "Figure data not available for this job."}</p>
          <p>This may happen if the analysis did not produce this figure type.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="ip-shell">
      <header className="ip-toolbar">
        <a className="ip-back" href="javascript:window.close()">&larr; Back</a>
        <h1 className="ip-title">{data.title || pageTitle}</h1>
        <div className="ip-toolbar-actions">
          <button className="ip-dl-btn" onClick={downloadPNG} title="Download PNG">PNG</button>
          <button className="ip-dl-btn" onClick={downloadSVG} title="Download SVG">SVG</button>
          <button className="ip-dl-btn" onClick={downloadPDF} title="Download PDF">PDF</button>
        </div>
      </header>
      <div className="ip-body">
        {children(data, controlsAPI)}
      </div>
    </div>
  );
}
