import { useMemo, useRef, useState, useCallback, useEffect } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3.0;
const ZOOM_STEP = 0.15;
const MIN_ASPECT_RATIO = 0.5;
const MAX_ASPECT_RATIO = 2.0;
const ASPECT_RATIO_STEP = 0.05;
const PX_PER_INCH = 96;

export function InteractiveHeatmap({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Correlation Heatmap">
      {(data, controls) => <HeatmapChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(max, Math.max(min, value));
}

function shouldUseLightCellText(value: number | null | undefined): boolean {
  if (value == null || !Number.isFinite(Number(value))) return false;
  return Math.abs(Number(value)) >= 0.68;
}

function HeatmapChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [isPanning, setIsPanning] = useState(false);
  const panOrigin = useRef({ x: 0, y: 0, scrollLeft: 0, scrollTop: 0 });

  const available = data.available_states || {};
  const state = controls.state;
  const view = String(state.view || state.view_type || data.default_state?.view || "");
  const showSignificance = Boolean(state.show_significance ?? true);
  const showValues = Boolean(state.show_values);
  const aspectRatio = clampNumber(Number(state.aspect_ratio ?? 1), MIN_ASPECT_RATIO, MAX_ASPECT_RATIO);

  // Resolve effective plotly_spec: if current view differs from primary view,
  // try to load matching alt_data (backend merges multi-view pages into alt_data).
  const effectiveSpec = useMemo(() => {
    const primary = data.plotly_spec || {};
    const primaryView = String(data.default_state?.view || "");
    if (view === primaryView || !data.alt_data) return primary;
    const alts = Object.values(data.alt_data);
    for (const alt of alts) {
      const altState = (alt as Record<string, unknown>)?.default_state as Record<string, unknown>;
      if (String(altState?.view || "") === view) {
        return {
          ...primary,
          ...((alt as Record<string, unknown>)?.plotly_spec || {}),
        };
      }
    }
    return {};
  }, [data, view]);

  const allTraces = (effectiveSpec.data || []) as Plotly.Data[];
  const layout = (effectiveSpec.layout || {}) as Partial<Plotly.Layout>;
  const rawAnnotations = (effectiveSpec.annotations || []) as Array<Record<string, unknown>>;
  const yColors = (effectiveSpec.y_colors || []) as string[] | undefined;

  // Filter traces by view metadata if present
  const hasMeta = allTraces.some(t => (t as Record<string, unknown>).meta);
  const baseTraces = hasMeta
    ? allTraces.filter(t => {
        const m = ((t as Record<string, unknown>).meta || {}) as Record<string, unknown>;
        return !view || !m.view || m.view === view;
      })
    : allTraces;

  const viewUnavailable = !baseTraces.length;

  const firstTrace = baseTraces[0] as Record<string, unknown>;
  const xLabels = (firstTrace?.x as string[]) || [];
  const yLabels = (firstTrace?.y as string[]) || [];
  const zMatrix = (firstTrace?.z as Array<Array<number | null>>) || [];
  const nCols = xLabels.length;
  const nRows = yLabels.length;

  // Build significance text matrix from rawAnnotations
  const sigTextMatrix = useMemo(() => {
    const matrix = Array.from({ length: nRows }, () => Array(nCols).fill(""));
    for (const ann of rawAnnotations) {
      const row = ann.row as number;
      const col = ann.col as number;
      if (row >= 0 && row < nRows && col >= 0 && col < nCols) {
        matrix[row][col] = String(ann.text || "");
      }
    }
    return matrix;
  }, [rawAnnotations, nRows, nCols]);

  // Keep the trace text-free; visible cell labels are layout annotations so
  // values and stars can be centered consistently.
  const traces = useMemo(() => baseTraces.map(t => {
    const trace = { ...t } as Record<string, unknown>;
    if (trace.type === "heatmap") {
      delete trace.text;
      delete trace.texttemplate;
      trace.colorbar = {
        ...((trace.colorbar as Record<string, unknown>) || {}),
        title: { text: "Spearman rho", side: "right" },
        len: 0.72,
        y: 0.43,
        yanchor: "middle",
        x: 1.02,
      };
    }
    return trace as Plotly.Data;
  }), [baseTraces]);

  const cellAnnotations = useMemo<Partial<Plotly.Annotations>[]>(() => {
    if (!showValues && !showSignificance) return [];
    const annotations: Partial<Plotly.Annotations>[] = [];
    for (let row = 0; row < nRows; row += 1) {
      for (let col = 0; col < nCols; col += 1) {
        const value = zMatrix[row]?.[col];
        const hasValue = value != null && Number.isFinite(Number(value));
        const valueText = showValues && hasValue ? Number(value).toFixed(2) : "";
        const starText = showSignificance ? sigTextMatrix[row]?.[col] || "" : "";
        const text = valueText && starText ? `${valueText}<br>${starText}` : valueText || starText;
        if (!text) continue;

        annotations.push({
          x: xLabels[col],
          y: yLabels[row],
          xref: "x",
          yref: "y",
          text,
          showarrow: false,
          align: "center",
          xanchor: "center",
          yanchor: "middle",
          font: {
            size: valueText && starText ? 9 : 10,
            color: shouldUseLightCellText(value) ? "#ffffff" : "#111827",
          },
        });
      }
    }
    return annotations;
  }, [showValues, showSignificance, nRows, nCols, zMatrix, sigTextMatrix, xLabels, yLabels]);

  const legendAnnotations = useMemo<Partial<Plotly.Annotations>[]>(() => ([
    {
      xref: "paper",
      yref: "paper",
      x: 1.082,
      y: 0.82,
      text: "FDR<br>* &lt; 0.05<br>** &lt; 0.01<br>*** &lt; 0.001",
      showarrow: false,
      align: "center",
      xanchor: "center",
      yanchor: "bottom",
      font: { size: 12, color: "#111827" },
    },
  ]), []);

  // Build y-axis color strip shapes for gene-metabolite view (F12)
  const shapes = useMemo<Partial<Plotly.Shape>[]>(() => {
    if (!yColors || view !== "gene-metabolite") return [];
    if (!yLabels.length) return [];
    return yLabels.map((_, i) => ({
      type: "rect",
      x0: -0.9,
      x1: -0.35,
      y0: i - 0.5,
      y1: i + 0.5,
      fillcolor: yColors[i] || "#d1d5db",
      line: { width: 0 },
      xref: "x",
      yref: "y",
    } as Partial<Plotly.Shape>));
  }, [yColors, view, yLabels]);

  // Compute base pixel dimensions from static plot formulas (96 DPI)
  const baseSize = useMemo(() => {
    if (view === "gene-metabolite") {
      const width = Math.max(8.5, Math.min(24.0, 0.58 * Math.max(1, nCols) + 4.6)) * PX_PER_INCH;
      const height = Math.max(5.6, Math.min(20.0, 0.33 * Math.max(1, nRows) + 2.2)) * PX_PER_INCH;
      return { width: Math.round(width), height: Math.round(height) };
    } else {
      const width = Math.max(9.0, Math.min(28.0, 0.42 * Math.max(1, nCols) + 4.5)) * PX_PER_INCH;
      const height = Math.max(4.5, Math.min(18.0, 0.50 * Math.max(1, nRows) + 2.8)) * PX_PER_INCH;
      return { width: Math.round(width), height: Math.round(height) };
    }
  }, [view, nCols, nRows]);

  const wrapperWidth = Math.round(baseSize.width * zoom * aspectRatio);
  const wrapperHeight = Math.round(baseSize.height * zoom / aspectRatio);
  const isGeneView = view === "gene-metabolite";

  // Zoom / Pan handlers
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -ZOOM_STEP : ZOOM_STEP;
    setZoom(prev => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev + delta)));
  }, []);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement | null)?.closest(".modebar")) return;
    const el = containerRef.current;
    if (!el) return;
    e.preventDefault();
    e.stopPropagation();
    setIsPanning(true);
    panOrigin.current = { x: e.clientX, y: e.clientY, scrollLeft: el.scrollLeft, scrollTop: el.scrollTop };
  }, []);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning) return;
    e.preventDefault();
    e.stopPropagation();
    const el = containerRef.current;
    if (!el) return;
    const dx = e.clientX - panOrigin.current.x;
    const dy = e.clientY - panOrigin.current.y;
    el.scrollLeft = panOrigin.current.scrollLeft - dx;
    el.scrollTop = panOrigin.current.scrollTop - dy;
  }, [isPanning]);

  const handleMouseUp = useCallback((e?: React.MouseEvent) => {
    e?.preventDefault();
    e?.stopPropagation();
    setIsPanning(false);
  }, []);
  const handleMouseLeave = useCallback(() => setIsPanning(false), []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const preventScroll = (e: WheelEvent) => e.preventDefault();
    el.addEventListener("wheel", preventScroll, { passive: false });
    return () => el.removeEventListener("wheel", preventScroll);
  }, []);

  if (viewUnavailable) {
    return (
      <>
        <div className="ip-chart">
          <div className="ip-chart-area" style={{ display: "grid", placeItems: "center", minHeight: 420 }}>
            <div style={{ color: "#6b7280", fontSize: 13 }}>
              Heatmap data is not available for view: {view || "unknown"}.
            </div>
          </div>
        </div>
        <div className="ip-controls">
          {(available.view || available.view_type) && (
            <div className="ip-control-group">
              <label className="ip-control-label">View</label>
              <select className="ip-control-select" value={view}
                onChange={e => controls.setState("view", e.target.value)}>
                {((available.view || available.view_type) as string[]).map(s => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      <div className="ip-chart">
        <div
          className={`ip-chart-area ip-heatmap-area${isPanning ? " is-panning" : ""}`}
          ref={containerRef}
          style={{
            overflow: "auto",
            cursor: isPanning ? "grabbing" : "grab",
            userSelect: "none",
            position: "relative",
          }}
          onWheel={handleWheel}
          onMouseDownCapture={handleMouseDown}
          onMouseMoveCapture={handleMouseMove}
          onMouseUpCapture={handleMouseUp}
          onMouseLeave={handleMouseLeave}
        >
          <div style={{ width: wrapperWidth, height: wrapperHeight }}>
            <Plot
              data={traces}
              layout={{
                ...(layout as Record<string, unknown>),
                autosize: true,
                hovermode: "closest",
                dragmode: false,
                margin: { l: isGeneView ? 90 : 70, r: 145, t: 55, b: 65 },
                xaxis: {
                  ...(layout.xaxis || {}),
                  ticks: "",
                  tickangle: 45,
                  automargin: true,
                },
                yaxis: {
                  ...(layout.yaxis || {}),
                  autorange: "reversed",
                },
                shapes,
                annotations: [...cellAnnotations, ...legendAnnotations],
              }}
              config={{
                displayModeBar: true,
                displaylogo: false,
                scrollZoom: false,
                modeBarButtonsToRemove: ["lasso2d", "select2d"],
              }}
              useResizeHandler
              style={{ width: "100%", height: "100%" }}
            />
          </div>
        </div>
        <div className="ip-infobar">
          <span>View: {view || "—"}</span>
          <span>Zoom: {Math.round(zoom * 100)}%</span>
          <span>Aspect: {aspectRatio.toFixed(2)}x</span>
          <button
            className="secondary"
            style={{ padding: "2px 8px", fontSize: "11px", cursor: "pointer" }}
            onClick={() => setZoom(1)}
            type="button"
          >
            Reset zoom
          </button>
          {isGeneView && yColors && <span>Modules: {yColors.length}</span>}
        </div>
      </div>
      <div className="ip-controls">
        {(available.view || available.view_type) && (
          <div className="ip-control-group">
            <label className="ip-control-label">View</label>
            <select className="ip-control-select" value={view}
              onChange={e => controls.setState("view", e.target.value)}>
              {((available.view || available.view_type) as string[]).map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        )}
        <div className="ip-control-group">
          <label className="ip-control-label">Zoom: {Math.round(zoom * 100)}%</label>
          <div className="ip-control-range">
            <input
              type="range"
              min={MIN_ZOOM}
              max={MAX_ZOOM}
              step={ZOOM_STEP}
              value={zoom}
              onChange={e => setZoom(parseFloat(e.target.value))}
            />
            <span>{Math.round(zoom * 100)}%</span>
          </div>
        </div>
        <div className="ip-control-group">
          <label className="ip-control-label">Aspect ratio: {aspectRatio.toFixed(2)}x</label>
          <div className="ip-control-range">
            <input
              type="range"
              min={MIN_ASPECT_RATIO}
              max={MAX_ASPECT_RATIO}
              step={ASPECT_RATIO_STEP}
              value={aspectRatio}
              onChange={e => controls.setState("aspect_ratio", parseFloat(e.target.value))}
            />
            <span>{aspectRatio.toFixed(2)}x</span>
          </div>
        </div>
        <div className="ip-control-group">
          <label className="ip-control-toggle">
            <input type="checkbox" checked={showSignificance}
              onChange={e => controls.setState("show_significance", e.target.checked)} />
            Show significance
          </label>
        </div>
        <div className="ip-control-group">
          <label className="ip-control-toggle">
            <input type="checkbox" checked={showValues}
              onChange={e => controls.setState("show_values", e.target.checked)} />
            Show values
          </label>
        </div>
      </div>
    </>
  );
}
