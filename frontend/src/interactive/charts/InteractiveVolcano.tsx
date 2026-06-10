import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

export function InteractiveVolcano({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Volcano Plot">
      {(data, controls) => <VolcanoChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function VolcanoChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const spec = data.plotly_spec as Record<string, unknown>;
  const allTraces = spec.all_traces as Record<string, Plotly.Data[]> | undefined;
  const available = data.available_states || {};
  const state = controls.state;

  const contrast = String(state.contrast || data.default_state?.contrast || "");
  const traces: Plotly.Data[] = allTraces?.[contrast] ?? (spec.data as Plotly.Data[] ?? []);
  const layout = (spec.layout || {}) as Partial<Plotly.Layout>;

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          <Plot
            data={traces}
            layout={{ ...(layout as Record<string, unknown>), autosize: true, hovermode: "closest" }}
            config={{ displayModeBar: true, displaylogo: false, scrollZoom: true }}
            useResizeHandler
            style={{ width: "100%", height: "100%" }}
          />
        </div>
      </div>
      <div className="ip-controls">
        {((available.contrast as string[] | undefined)?.length ?? 0) > 1 && (
          <div className="ip-control-group">
            <label className="ip-control-label">Contrast</label>
            <select className="ip-control-select" value={contrast}
              onChange={e => controls.setState("contrast", e.target.value)}>
              {(available.contrast as string[]).map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
        )}
      </div>
    </>
  );
}
