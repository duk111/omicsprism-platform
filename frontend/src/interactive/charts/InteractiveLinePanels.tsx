import { useMemo } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }
export function InteractiveLinePanels({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Line Panels">
      {(data, controls) => <LinePanelsChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function LinePanelsChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const spec = data.plotly_spec;
  const allModules = (spec.panels || []) as Record<string, unknown>[];
  const g2Order = (spec.group2_order || []) as string[];
  const available = data.available_states || {};
  const state = controls.state;
  const viewType = String(state.view_type || data.default_state?.view_type || "module-zscore");
  const showScatter = Boolean(state.show_scatter ?? true);
  const unifyYAxis = Boolean(state.unify_yaxis ?? false);

  // Filter modules if metadata view_type is present
  const modules = useMemo(() => allModules.filter(m => {
    const mv = (m.meta as Record<string, unknown>)?.view_type;
    return !mv || mv === viewType;
  }), [allModules, viewType]);

  const traces = useMemo<Plotly.Data[]>(() => modules.flatMap(mod => {
    const modName = String(mod.module || "");
    const panels = (mod.panels || []) as Record<string, unknown>[];
    return panels.map(panel => {
      const means = (panel.means || []) as Record<string, unknown>[];
      const yVals = means.map(m => Number(m.y) || null) as (number | null)[];
      return {
        type: "scatter",
        mode: showScatter ? "lines+markers" : "lines",
        x: g2Order,
        y: yVals,
        name: `${modName} / ${panel.group1 || ""}`,
        line: { width: 1.5 },
        marker: { size: 5 },
        hovertemplate: `${modName}<br>%{x}: %{y:.3f}<extra></extra>`,
      } as Plotly.Data;
    });
  }), [modules, g2Order, showScatter]);

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          <Plot data={traces}
            layout={{
              autosize: true,
              hovermode: "closest",
              yaxis: { title: { text: "z-score" }, zeroline: true },
              ...(unifyYAxis ? { yaxis: { matches: undefined } } : {}),
            }}
            config={{ displayModeBar: true, displaylogo: false }}
            useResizeHandler style={{ width: "100%", height: "100%" }} />
        </div>
        <div className="ip-infobar">
          <span>View: {viewType}</span>
          <span>Modules: {modules.length}</span>
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
        <div className="ip-control-group">
          <label className="ip-control-toggle">
            <input type="checkbox" checked={showScatter}
              onChange={e => controls.setState("show_scatter", e.target.checked)} />
            Show data points
          </label>
        </div>
        <div className="ip-control-group">
          <label className="ip-control-toggle">
            <input type="checkbox" checked={unifyYAxis}
              onChange={e => controls.setState("unify_yaxis", e.target.checked)} />
            Unify Y axis
          </label>
        </div>
      </div>
    </>
  );
}
