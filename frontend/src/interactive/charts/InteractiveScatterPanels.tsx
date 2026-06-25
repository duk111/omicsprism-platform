import { useMemo, useRef, useEffect } from "react";
import Plot from "react-plotly.js";
import PlotlyLib from "plotly.js-dist-min";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

type PanelType = "gene-metabolite" | "module-metabolite";

interface RegressionPayload {
  x: number[];
  y: number[];
  ci?: { x: number[]; lower: number[]; upper: number[] };
}

interface ScatterPanel {
  id: string;
  rank: number;
  type: PanelType;
  entity_id: string;
  entity_label?: string;
  metabolite_id: string;
  title: string;
  x: number[];
  y: number[];
  sample_ids: string[];
  x_label: string;
  y_label: string;
  color: string;
  metric_label: "rho" | "r";
  metric_value: number | null;
  regression?: RegressionPayload | null;
  edge_weight?: number | null;
  rra_rank?: number | null;
  fdr?: number | null;
  p_value?: number | null;
  module?: string;
}

const PANEL_KEYS = ["panel_1", "panel_2", "panel_3", "panel_4"] as const;
const PANEL_LABELS = ["Left top", "Right top", "Left bottom", "Right bottom"];

type ScatterVariant = {
  plotly_spec: Record<string, unknown>;
  default_state: Record<string, unknown>;
};

export function InteractiveScatterPanels({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Regression Panels">
      {(data, controls) => <ScatterPanelsChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function ScatterPanelsChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const state = controls.state;
  const chartRef = useRef<HTMLDivElement>(null);
  const filename = (data.title || data.figure_id || "scatter").replace(/\s+/g, "_");

  const { setDownloadHandlers } = controls;
  useEffect(() => {
    setDownloadHandlers(
      () => {
        const el = chartRef.current?.querySelector(".js-plotly-plot") as HTMLElement | null;
        if (el) PlotlyLib.downloadImage(el, { format: "png", filename });
      },
      () => {
        const el = chartRef.current?.querySelector(".js-plotly-plot") as HTMLElement | null;
        if (el) PlotlyLib.downloadImage(el, { format: "svg", filename });
      },
    );
    return () => setDownloadHandlers(null, null);
  }, [setDownloadHandlers, filename]);

  const variants = useMemo(() => {
    const primary = [{ plotly_spec: data.plotly_spec || {}, default_state: data.default_state || {} }];
    const alts = Object.values(data.alt_data || {}).map(alt => ({
      plotly_spec: alt.plotly_spec || {},
      default_state: alt.default_state || {},
    }));
    return [...primary, ...alts];
  }, [data]);

  const availablePanelTypes = useMemo(() => {
    const values = variants
      .map(variant => variantPanelType(variant))
      .filter((value): value is PanelType => value === "gene-metabolite" || value === "module-metabolite");
    return Array.from(new Set(values));
  }, [variants]);

  const requestedPanelType = String(state.panel_type || data.default_state?.panel_type || "gene-metabolite") as PanelType;
  const panelType = availablePanelTypes.includes(requestedPanelType)
    ? requestedPanelType
    : availablePanelTypes[0] || requestedPanelType;

  const activeVariant = useMemo(() => (
    variants.find(variant => variantPanelType(variant) === panelType) || variants[0]
  ), [variants, panelType]);

  const spec = activeVariant?.plotly_spec || {};
  const defaultState = activeVariant?.default_state || {};
  const panels = ((spec.panels || []) as ScatterPanel[]).filter(Boolean);
  const panelById = useMemo(() => new Map(panels.map(panel => [panel.id, panel])), [panels]);
  const showSampleId = Boolean(state.show_sample_id);
  const showRegressionLine = Boolean(state.show_regression_line ?? true);

  const entityOptions = ((spec.entity_options || []) as string[]).filter(Boolean);
  const metaboliteOptions = ((spec.metabolite_options || []) as string[]).filter(Boolean);
  const pairOptions = ((spec.pair_options || panels.map(panel => panel.id)) as string[]).filter(Boolean);
  const topPairPanels = pairOptions.map(id => panelById.get(id)).filter((panel): panel is ScatterPanel => Boolean(panel));

  const selectedPanels = PANEL_KEYS.map((key, idx) => {
    const statePairId = String(state[`${key}_pair_id`] || "");
    const defaultPairId = String(defaultState[`${key}_pair_id`] || pairOptions[idx] || "");
    const pairId = panelById.has(statePairId) ? statePairId : defaultPairId;
    const entity = String(state[`${key}_entity_id`] || "");
    const metabolite = String(state[`${key}_metabolite_id`] || "");
    return resolvePanel({ pairId, entity, metabolite, panels, panelById }) || null;
  });

  const plotData = selectedPanels.flatMap((panel, idx) => panel ? panelToTraces(panel, idx, showSampleId, showRegressionLine) : []);
  const annotations = selectedPanels.flatMap((panel, idx) => panel ? panelAnnotations(panel, idx) : emptyPanelAnnotation(idx));

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area" ref={chartRef}>
          {plotData.length > 0 ? (
            <Plot
              data={plotData}
              layout={{
                autosize: true,
                grid: { rows: 2, columns: 2, pattern: "independent" },
                showlegend: false,
                hovermode: "closest",
                dragmode: "pan",
                margin: { l: 58, r: 62, t: 56, b: 54 },
                annotations,
                ...axisLayouts(selectedPanels),
              }}
              config={{
                displayModeBar: true,
                displaylogo: false,
                scrollZoom: true,
                modeBarButtonsToRemove: ["lasso2d", "select2d"],
              }}
              useResizeHandler
              style={{ width: "100%", height: "100%" }}
            />
          ) : (
            <div className="ip-empty-chart">Scatter panel data is not available.</div>
          )}
        </div>
        <div className="ip-infobar">
          <span>View: {panelType}</span>
          <span>Showing: {selectedPanels.filter(Boolean).length} panels</span>
          <span>Pair ranking: abs(Spearman rho)</span>
        </div>
      </div>
      <div className="ip-controls ip-scatter-controls">
        <div className="ip-control-group">
          <label className="ip-control-label">View</label>
          <select
            className="ip-control-select"
            value={panelType}
            onChange={e => setPanelType(controls, variants, e.target.value as PanelType)}
          >
            {availablePanelTypes.map(option => (
              <option key={option} value={option}>{formatPanelType(option)}</option>
            ))}
          </select>
        </div>

        <div className="ip-control-group">
          <label className="ip-control-label">Display</label>
          <label className="ip-control-toggle">
            <input
              type="checkbox"
              checked={showSampleId}
              onChange={e => controls.setState("show_sample_id", e.target.checked)}
            />
            Show sample ID
          </label>
          <label className="ip-control-toggle">
            <input
              type="checkbox"
              checked={showRegressionLine}
              onChange={e => controls.setState("show_regression_line", e.target.checked)}
            />
            Show regression line
          </label>
        </div>

        {PANEL_KEYS.map((key, idx) => {
          const selected = selectedPanels[idx];
          const selectedPairId = selected?.id || String(state[`${key}_pair_id`] || pairOptions[idx] || "");
          return (
            <div className="ip-panel-control" key={key}>
              <h3>{PANEL_LABELS[idx]}</h3>
              <label className="ip-control-label">Pair</label>
              <select
                className="ip-control-select"
                value={selectedPairId}
                onChange={e => setPanelPair(controls, key, panelById.get(e.target.value))}
              >
                {topPairPanels.map(panel => (
                  <option key={panel.id} value={panel.id}>{formatPairOption(panel)}</option>
                ))}
              </select>

              <label className="ip-control-label">{panelType === "module-metabolite" ? "Module" : "Gene"}</label>
              <select
                className="ip-control-select"
                value={selected?.entity_id || ""}
                onChange={e => setPanelEntityMetabolite(controls, key, panels, e.target.value, selected?.metabolite_id || "")}
              >
                <option value="">Select...</option>
                {entityOptions.map(option => <option key={option} value={option}>{option}</option>)}
              </select>

              <label className="ip-control-label">Metabolite</label>
              <select
                className="ip-control-select"
                value={selected?.metabolite_id || ""}
                onChange={e => setPanelEntityMetabolite(controls, key, panels, selected?.entity_id || "", e.target.value)}
              >
                <option value="">Select...</option>
                {metaboliteOptions.map(option => <option key={option} value={option}>{option}</option>)}
              </select>
            </div>
          );
        })}
      </div>
    </>
  );
}

function resolvePanel({
  pairId,
  entity,
  metabolite,
  panels,
  panelById,
}: {
  pairId: string;
  entity: string;
  metabolite: string;
  panels: ScatterPanel[];
  panelById: Map<string, ScatterPanel>;
}) {
  if (entity && metabolite) {
    const matched = panels.find(panel => panel.entity_id === entity && panel.metabolite_id === metabolite);
    if (matched) return matched;
  }
  return panelById.get(pairId);
}

function variantPanelType(variant: ScatterVariant): PanelType | "" {
  const fromState = String(variant.default_state?.panel_type || "");
  if (fromState) return fromState as PanelType;
  const panels = ((variant.plotly_spec?.panels || []) as ScatterPanel[]).filter(Boolean);
  return (panels[0]?.type || "") as PanelType | "";
}

function setPanelType(controls: ControlsAPI, variants: ScatterVariant[], panelType: PanelType) {
  const variant = variants.find(item => variantPanelType(item) === panelType);
  const spec = variant?.plotly_spec || {};
  const defaultState = variant?.default_state || {};
  const defaultPairIds = ((spec.default_pair_ids || []) as string[]).filter(Boolean);

  controls.setState("panel_type", panelType);
  PANEL_KEYS.forEach((key, idx) => {
    controls.setState(`${key}_pair_id`, String(defaultState[`${key}_pair_id`] || defaultPairIds[idx] || ""));
    controls.setState(`${key}_entity_id`, "");
    controls.setState(`${key}_metabolite_id`, "");
  });
}

function setPanelPair(controls: ControlsAPI, key: typeof PANEL_KEYS[number], panel?: ScatterPanel) {
  controls.setState(`${key}_pair_id`, panel?.id || "");
  controls.setState(`${key}_entity_id`, panel?.entity_id || "");
  controls.setState(`${key}_metabolite_id`, panel?.metabolite_id || "");
}

function setPanelEntityMetabolite(
  controls: ControlsAPI,
  key: typeof PANEL_KEYS[number],
  panels: ScatterPanel[],
  entity: string,
  metabolite: string,
) {
  controls.setState(`${key}_entity_id`, entity);
  controls.setState(`${key}_metabolite_id`, metabolite);
  const matched = panels.find(panel => panel.entity_id === entity && panel.metabolite_id === metabolite);
  if (matched) {
    controls.setState(`${key}_pair_id`, matched.id);
  }
}

function panelToTraces(panel: ScatterPanel, idx: number, showSampleId: boolean, showRegressionLine: boolean): Plotly.Data[] {
  const axis = axisSuffix(idx);
  const traces: Plotly.Data[] = [];
  if (showRegressionLine && panel.regression?.ci) {
    const ci = panel.regression.ci;
    traces.push({
      type: "scatter",
      mode: "lines",
      x: [...ci.x, ...ci.x.slice().reverse()],
      y: [...ci.upper, ...ci.lower.slice().reverse()],
      fill: "toself",
      fillcolor: withAlpha(panel.color, 0.16),
      line: { color: "rgba(0,0,0,0)", width: 0 },
      hoverinfo: "skip",
      showlegend: false,
      xaxis: `x${axis}`,
      yaxis: `y${axis}`,
    } as Plotly.Data);
  }
  if (showRegressionLine && panel.regression) {
    traces.push({
      type: "scatter",
      mode: "lines",
      x: panel.regression.x,
      y: panel.regression.y,
      line: { color: "#111111", width: 1.4 },
      hoverinfo: "skip",
      showlegend: false,
      xaxis: `x${axis}`,
      yaxis: `y${axis}`,
    } as Plotly.Data);
  }
  traces.push({
    type: "scatter",
    mode: showSampleId ? "markers+text" : "markers",
    x: panel.x,
    y: panel.y,
    text: showSampleId ? panel.sample_ids : undefined,
    textposition: "top center",
    textfont: { size: 9, color: "#374151" },
    customdata: panel.sample_ids,
    marker: { size: 6.5, color: panel.color, opacity: 0.86, line: { color: "white", width: 0.5 } },
    xaxis: `x${axis}`,
    yaxis: `y${axis}`,
    showlegend: false,
    hovertemplate: hoverTemplate(panel),
  } as Plotly.Data);
  return traces;
}

function panelAnnotations(panel: ScatterPanel, idx: number): Partial<Plotly.Annotations>[] {
  const domain = subplotDomain(idx);
  const metric = formatMetric(panel);
  return [
    {
      text: metric,
      xref: "paper",
      yref: "paper",
      x: domain.x[1] - 0.018,
      y: domain.y[1] - 0.04,
      showarrow: false,
      xanchor: "right",
      yanchor: "middle",
      font: { size: 11, color: "#111827" },
      bgcolor: "rgba(255,255,255,0.82)",
      borderpad: 2,
    },
  ];
}

function emptyPanelAnnotation(idx: number): Partial<Plotly.Annotations>[] {
  const domain = subplotDomain(idx);
  return [{
    text: "No paired data available",
    xref: "paper",
    yref: "paper",
    x: (domain.x[0] + domain.x[1]) / 2,
    y: (domain.y[0] + domain.y[1]) / 2,
    showarrow: false,
    font: { size: 12, color: "#6b7280" },
  }];
}

function axisLayouts(selectedPanels: Array<ScatterPanel | null>): Partial<Plotly.Layout> {
  const layout: Partial<Plotly.Layout> = {};
  selectedPanels.forEach((panel, idx) => {
    const axis = axisSuffix(idx);
    const xKey = `xaxis${axis === "" ? "" : axis}` as keyof Plotly.Layout;
    const yKey = `yaxis${axis === "" ? "" : axis}` as keyof Plotly.Layout;
    const domain = subplotDomain(idx);
    layout[xKey] = {
      title: { text: panel?.x_label || "" },
      domain: domain.x,
      zeroline: false,
      gridcolor: "#e5e7eb",
    } as never;
    layout[yKey] = {
      title: { text: panel?.y_label || "" },
      domain: domain.y,
      zeroline: false,
      gridcolor: "#e5e7eb",
    } as never;
  });
  return layout;
}

function subplotDomain(idx: number) {
  const col = idx % 2;
  const row = Math.floor(idx / 2);
  const xDomains: [number, number][] = [[0.04, 0.42], [0.62, 1.0]];
  const yDomains: [number, number][] = [[0.58, 1.0], [0.0, 0.42]];
  return { x: xDomains[col], y: yDomains[row] };
}

function axisSuffix(idx: number) {
  return idx === 0 ? "" : String(idx + 1);
}

function formatMetric(panel: ScatterPanel) {
  const label = panel.metric_label === "r" ? "r" : "rho";
  return `${label} = ${Number.isFinite(panel.metric_value ?? NaN) ? Number(panel.metric_value).toFixed(2) : "NA"}`;
}

function formatPairOption(panel: ScatterPanel) {
  const metric = Number.isFinite(panel.metric_value ?? NaN) ? Number(panel.metric_value).toFixed(2) : "NA";
  return `${panel.entity_id} vs ${panel.metabolite_id} | ${panel.metric_label} ${metric}`;
}

function formatPanelType(panelType: PanelType) {
  return panelType === "module-metabolite" ? "Module-Metabolite" : "Gene-Metabolite";
}

function hoverTemplate(panel: ScatterPanel) {
  const metric = formatMetric(panel);
  const extra = panel.type === "gene-metabolite"
    ? `<br>EdgeWeight: ${formatOptional(panel.edge_weight)}`
    : `<br>FDR: ${formatOptional(panel.fdr)}`;
  return [
    "Sample ID: %{customdata}",
    `${panel.x_label}: %{x:.3f}`,
    `${panel.y_label}: %{y:.3f}`,
    `Pair: ${panel.entity_id} vs ${panel.metabolite_id}`,
    metric + extra,
    "<extra></extra>",
  ].join("<br>");
}

function formatOptional(value: number | null | undefined) {
  return Number.isFinite(value ?? NaN) ? Number(value).toPrecision(3) : "NA";
}

function withAlpha(hex: string, alpha: number) {
  const normalized = hex.trim();
  if (!normalized.startsWith("#") || (normalized.length !== 7 && normalized.length !== 4)) {
    return `rgba(156, 163, 175, ${alpha})`;
  }
  const full = normalized.length === 4
    ? `#${normalized[1]}${normalized[1]}${normalized[2]}${normalized[2]}${normalized[3]}${normalized[3]}`
    : normalized;
  const r = parseInt(full.slice(1, 3), 16);
  const g = parseInt(full.slice(3, 5), 16);
  const b = parseInt(full.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
