import { useMemo } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }
export function InteractiveBar({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Bar / Box Chart">
      {(data, controls) => <BarChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function BarChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const barData = (data.bar_data || []) as Record<string, unknown>[];
  const available = data.available_states || {};
  const state = controls.state;
  const viewType = String(state.view_type || data.default_state?.view_type || "direction");
  const isEdgeweight = viewType === "edgeweight";

  const traces = useMemo<Plotly.Data[]>(() => {
    const modules = barData.map(d => String(d.module));
    if (isEdgeweight) {
      const posVals = barData.map(d => (d.positive as number[]) || []);
      const negVals = barData.map(d => (d.negative as number[]) || []);
      return [
        { type: "box", name: "Positive", marker: { color: "#e8a29a" },
          y: posVals.flat(), x: posVals.flatMap((v, i) => Array(v.length).fill(modules[i])) },
        { type: "box", name: "Negative", marker: { color: "#8fb7df" },
          y: negVals.flat(), x: negVals.flatMap((v, i) => Array(v.length).fill(modules[i])) },
      ];
    }
    return [
      { type: "bar", name: "Positive", marker: { color: "#e8a29a" },
        x: modules, y: barData.map(d => Number(d.positive) || 0) },
      { type: "bar", name: "Negative", marker: { color: "#8fb7df" },
        x: modules, y: barData.map(d => Number(d.negative) || 0) },
    ];
  }, [barData, isEdgeweight]);

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          <Plot data={traces}
            layout={{
              autosize: true,
              barmode: (isEdgeweight ? "group" : "stack") as Plotly.Layout["barmode"],
              boxmode: "group",
              hovermode: "closest",
              xaxis: { tickangle: 35 },
              yaxis: { title: { text: isEdgeweight ? "Edge Weight" : "Count" } },
              legend: { x: 1.01, y: 1 },
            }}
            config={{ displayModeBar: true, displaylogo: false }}
            useResizeHandler style={{ width: "100%", height: "100%" }} />
        </div>
      </div>
      <div className="ip-controls">
        {available.view_type && (
          <div className="ip-control-group">
            <label className="ip-control-label">View</label>
            <select className="ip-control-select" value={viewType}
              onChange={e => controls.setState("view_type", e.target.value)}>
              {(available.view_type as string[]).map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
      </div>
    </>
  );
}
