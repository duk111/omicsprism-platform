import { useMemo } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }
export function InteractiveViolinBox({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Violin & Box Plot">
      {(data, controls) => <ViolinChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function ViolinChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const spec = data.plotly_spec;
  const allFeatures = (spec.features || []) as Record<string, unknown>[];
  const groupOrder = (spec.group_order || []) as string[];
  const groupColors = (spec.group_colors || []) as string[];
  const available = data.available_states || {};
  const state = controls.state;
  const featureType = String(state.feature_type || data.default_state?.feature_type || "metabolite");
  const chartMode = String(state.chart_mode || "violin");

  // Filter features by type if metadata present
  const features = useMemo(() => {
    const hasMeta = allFeatures.some(f => (f.meta as Record<string, unknown>)?.type);
    return hasMeta
      ? allFeatures.filter(f => {
          const m = (f.meta as Record<string, unknown>) || {};
          return !m.type || m.type === featureType;
        })
      : allFeatures;
  }, [allFeatures, featureType]);

  const traces = useMemo<Plotly.Data[]>(() => features.flatMap(feat => {
    const groups = (feat.groups || []) as Record<string, unknown>[];
    return groups.flatMap(g => {
      const vals = (g.values || []) as number[];
      if (!vals.length) return [];
      const colorIdx = groupOrder.indexOf(String(g.group));
      const color = groupColors[colorIdx] || "#9ca3af";
      const base = {
        name: String(g.group),
        legendgroup: String(g.group),
        scalegroup: String(feat.feature),
        marker: { color },
        x: Array(vals.length).fill(String(feat.feature)),
        y: vals,
        showlegend: features.indexOf(feat) === 0,
        hovertemplate: `${feat.feature}<br>${g.group}: %{y:.3f}<extra></extra>`,
      };
      if (chartMode === "box") return [{ ...base, type: "box", boxpoints: "all", jitter: 0.4, pointpos: 0 } as Plotly.Data];
      return [{ ...base, type: "violin", box: { visible: true }, meanline: { visible: true }, points: "all", jitter: 0.35, pointpos: 0 } as Plotly.Data];
    });
  }), [features, groupOrder, groupColors, chartMode]);

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          <Plot data={traces}
            layout={{
              autosize: true, hovermode: "closest",
              violingap: 0.05, violingroupgap: 0.1,
              violinmode: "group", boxmode: "group",
              legend: { x: 1.02, y: 1 },
              xaxis: { tickangle: 30 },
            } as unknown as Plotly.Layout}
            config={{ displayModeBar: true, displaylogo: false }}
            useResizeHandler style={{ width: "100%", height: "100%" }} />
        </div>
        <div className="ip-infobar">
          <span>Feature: {featureType}</span>
          <span>Showing: {features.length}</span>
        </div>
      </div>
      <div className="ip-controls">
        {available.feature_type && (
          <div className="ip-control-group">
            <label className="ip-control-label">Feature type</label>
            <select className="ip-control-select" value={featureType}
              onChange={e => controls.setState("feature_type", e.target.value)}>
              {(available.feature_type as string[]).map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
        <div className="ip-control-group">
          <label className="ip-control-label">Chart type</label>
          <select className="ip-control-select" value={chartMode}
            onChange={e => controls.setState("chart_mode", e.target.value)}>
            <option value="violin">Violin + Box</option>
            <option value="box">Box only</option>
          </select>
        </div>
      </div>
    </>
  );
}
