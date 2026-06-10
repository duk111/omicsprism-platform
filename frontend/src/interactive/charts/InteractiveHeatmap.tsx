import { useMemo } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

function resolveColorscale(
  scheme: string,
  backendColorscale: unknown,
): string | Array<[number, string]> | unknown {
  if (scheme === "vlag" && backendColorscale) return backendColorscale;
  return scheme;
}

export function InteractiveHeatmap({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Correlation Heatmap">
      {(data, controls) => <HeatmapChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function HeatmapChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const available = data.available_states || {};
  const state = controls.state;
  const view = String(state.view || state.view_type || data.default_state?.view || "");
  const colorScheme = String(state.color_scheme || data.default_state?.color_scheme || "vlag");
  const showSignificance = Boolean(state.show_significance ?? true);
  const showValues = Boolean(state.show_values);

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

  // Apply colorscale override and value display
  const traces = useMemo(() => baseTraces.map(t => {
    const trace = { ...t } as Record<string, unknown>;
    if (trace.type === "heatmap") {
      trace.colorscale = resolveColorscale(colorScheme, trace.colorscale);
      if (showValues) {
        trace.text = trace.z;
        trace.texttemplate = "%{text:.2f}";
      } else {
        delete trace.texttemplate;
      }
    }
    return trace as Plotly.Data;
  }), [baseTraces, colorScheme, showValues]);

  // Build y-axis color strip shapes for gene-metabolite view (F12)
  const shapes = useMemo<Partial<Plotly.Shape>[]>(() => {
    if (!yColors || view !== "gene-metabolite") return [];
    const firstTrace = baseTraces[0] as Record<string, unknown>;
    const yLabels = (firstTrace?.y as string[]) || [];
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
  }, [yColors, view, baseTraces]);

  // Convert row/col annotations to Plotly x/y annotations
  const annotations = useMemo<Partial<Plotly.Annotations>[]>(() => {
    if (!showSignificance || !rawAnnotations.length) return [];
    const firstTrace = baseTraces[0] as Record<string, unknown>;
    const xLabels = (firstTrace?.x as string[]) || [];
    const yLabels = (firstTrace?.y as string[]) || [];
    return rawAnnotations
      .map(ann => {
        const row = ann.row as number;
        const col = ann.col as number;
        if (row == null || col == null || row < 0 || col < 0) return null;
        return {
          x: xLabels[col] ?? col,
          y: yLabels[row] ?? row,
          text: String(ann.text || ""),
          showarrow: false,
          font: { size: 9, color: "#111827" },
          xref: "x",
          yref: "y",
        } as Partial<Plotly.Annotations>;
      })
      .filter((a): a is Partial<Plotly.Annotations> => a != null && !!a.text);
  }, [rawAnnotations, showSignificance, baseTraces]);

  const isGeneView = view === "gene-metabolite";

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
        <div className="ip-chart-area">
          <Plot
            data={traces}
            layout={{
              ...(layout as Record<string, unknown>),
              autosize: true,
              hovermode: "closest",
              dragmode: "pan",
              margin: { l: isGeneView ? 90 : 70, r: 30, t: 55, b: 65 },
              xaxis: {
                ...(layout.xaxis || {}),
                ticks: "",
                tickangle: 45,
              },
              yaxis: {
                ...(layout.yaxis || {}),
                autorange: "reversed",
              },
              shapes,
              annotations,
            }}
            config={{ displayModeBar: true, displaylogo: false, scrollZoom: true }}
            useResizeHandler
            style={{ width: "100%", height: "100%" }}
          />
        </div>
        <div className="ip-infobar">
          <span>View: {view || "—"}</span>
          <span>Colorscale: {colorScheme}</span>
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
          <label className="ip-control-label">Color scheme</label>
          <select className="ip-control-select" value={colorScheme}
            onChange={e => controls.setState("color_scheme", e.target.value)}>
            {["vlag", "RdBu_r", "RdBu", "RdYlBu_r", "coolwarm", "viridis"].map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
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
