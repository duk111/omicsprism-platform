import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }
export function InteractiveRidge({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Ridge Distribution">
      {(data, controls) => {
        const rd = data.ridge_data as Record<string, unknown> | undefined;
        const ridges = (rd?.ridges || []) as Record<string, unknown>[];
        const xGrid = (rd?.x_grid || []) as number[];
        const available = data.available_states || {};
        const state = controls.state;

        const traces: Plotly.Data[] = [];
        ridges.forEach((r, idx) => {
          const yBase = ridges.length - idx - 1;
          const ridgeH = 0.7;
          const density = (r.density as number[] | null);
          const groups = (r.groups as Record<string, unknown>[] | undefined);
          const color = String(r.color || "#9ca3af");
          const moduleName = String(r.module || "");

          if (groups) {
            groups.forEach((g) => {
              const gDensity = (g.density as number[] | null);
              const gColor = String(g.color || color);
              if (gDensity) {
                const yVals = gDensity.map((d: number) => yBase + d * ridgeH);
                traces.push({
                  type: "scatter", mode: "lines", x: xGrid, y: yVals,
                  name: `${moduleName} - ${g.group}`,
                  fill: "tonexty", fillcolor: gColor.replace(")", ",0.16)").replace("rgb", "rgba"),
                  line: { color: gColor, width: 1 }, showlegend: false,
                } as Plotly.Data);
              }
            });
          } else if (density) {
            const yVals = density.map((d: number) => yBase + d * ridgeH);
            traces.push({
              type: "scatter", mode: "lines", x: xGrid, y: yVals, name: moduleName,
              fill: "tonexty", fillcolor: color.replace(")", ",0.35)").replace("rgb", "rgba"),
              line: { color, width: 1.2 }, showlegend: false,
            } as Plotly.Data);
          }
        });

        return (
          <>
            <div className="ip-chart">
              <div className="ip-chart-area">
                <Plot data={traces}
                  layout={{ autosize: true, hovermode: "closest" as const, showlegend: false,
                    yaxis: { tickvals: ridges.map((_, i) => ridges.length - i - 1),
                      ticktext: ridges.map(r => String(r.module || "")) },
                    xaxis: { title: { text: "z-score" } } }}
                  config={{ displayModeBar: true, displaylogo: false }}
                  useResizeHandler style={{ width: "100%", height: "100%" }} />
              </div>
            </div>
            <div className="ip-controls">
              {available.grouped && (
                <div className="ip-control-group">
                  <label className="ip-control-label">Split by group</label>
                  <select className="ip-control-select" value={String(state.grouped)}
                    onChange={e => controls.setState("grouped", e.target.value === "true")}>
                    <option value="false">No</option><option value="true">Yes</option>
                  </select>
                </div>
              )}
            </div>
          </>
        );
      }}
    </InteractivePageShell>
  );
}
