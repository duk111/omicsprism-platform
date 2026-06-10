import { useState } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

export function InteractiveScatterPanels({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Regression Panels">
      {(data, controls) => <ScatterPanelsChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function ScatterPanelsChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const spec = data.plotly_spec;
  const panels = (spec.panels || []) as Record<string, unknown>[];
  const available = data.available_states || {};
  const state = controls.state;
  const [page, setPage] = useState(0);
  const perPage = 4;
  const totalPages = Math.ceil(panels.length / perPage);
  const pagePanels = panels.slice(page * perPage, (page + 1) * perPage);

  const cols = 2;
  const rows = Math.ceil(pagePanels.length / cols);

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          {(pagePanels.length > 0) && (
            <Plot
              data={pagePanels.flatMap((panel, idx) => {
                const row = Math.floor(idx / cols) + 1;
                const col = (idx % cols) + 1;
                const x = (panel.x as number[]) || [];
                const y = (panel.y as number[]) || [];
                const color = String(panel.color || "#1f77b4");
                const name = String(panel.title || `Panel ${idx + 1}`);

                // Regression line
                const n = Math.min(x.length, y.length);
                let slope = 0, intercept = 0;
                if (n >= 2) {
                  const sumX = x.slice(0, n).reduce((a, b) => a + b, 0);
                  const sumY = y.slice(0, n).reduce((a, b) => a + b, 0);
                  const sumXY = x.slice(0, n).reduce((a, xi, i) => a + xi * y[i], 0);
                  const sumX2 = x.slice(0, n).reduce((a, xi) => a + xi * xi, 0);
                  const denom = n * sumX2 - sumX * sumX;
                  if (Math.abs(denom) > 1e-9) {
                    slope = (n * sumXY - sumX * sumY) / denom;
                    intercept = (sumY - slope * sumX) / n;
                  }
                }
                const xSorted = [...x].sort((a, b) => a - b);
                const regLine = xSorted.map(xi => slope * xi + intercept);

                return [
                  {
                    type: "scatter", mode: "markers", x: x, y: y, name: name,
                    marker: { size: 6, color: color, line: { color: "white", width: 0.5 } },
                    xaxis: `x${idx + 1}`, yaxis: `y${idx + 1}`, showlegend: false,
                    hovertemplate: `x: %{x:.3f}<br>y: %{y:.3f}<extra></extra>`,
                  },
                  {
                    type: "scatter", mode: "lines", x: xSorted, y: regLine, name: `${name} fit`,
                    line: { color: "#111111", width: 1.5 },
                    xaxis: `x${idx + 1}`, yaxis: `y${idx + 1}`, showlegend: false,
                    hoverinfo: "skip",
                  },
                ];
              })}
              layout={{
                autosize: true,
                grid: { rows, columns: cols, pattern: "independent" },
                showlegend: false,
                margin: { l: 50, r: 20, t: 50, b: 50 },
              }}
              config={{ displayModeBar: true, displaylogo: false }}
              useResizeHandler
              style={{ width: "100%", height: "100%" }}
            />
          )}
        </div>
        {totalPages > 1 && (
          <div className="ip-infobar" style={{ gap: 8, alignItems: "center" }}>
            <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
              style={{ padding: "2px 8px", cursor: "pointer" }}>&larr;</button>
            <span>Page {page + 1} / {totalPages}</span>
            <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}
              style={{ padding: "2px 8px", cursor: "pointer" }}>&rarr;</button>
          </div>
        )}
      </div>
      <div className="ip-controls">
        {available.panel_type && (
          <div className="ip-control-group">
            <label className="ip-control-label">Type</label>
            <select className="ip-control-select" value={String(state.panel_type || "gene-metabolite")}
              onChange={e => controls.setState("panel_type", e.target.value)}>
              {(available.panel_type as string[]).map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
        <div className="ip-control-group">
          <label className="ip-control-toggle">
            <input type="checkbox" checked={Boolean(state.show_ci ?? true)}
              onChange={e => controls.setState("show_ci", e.target.checked)} />
            Show CI
          </label>
        </div>
      </div>
    </>
  );
}
