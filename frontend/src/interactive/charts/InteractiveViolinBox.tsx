import { useMemo, useRef, useEffect } from "react";
import Plot from "react-plotly.js";
import PlotlyLib from "plotly.js-dist-min";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

type ViolinView = "metabolite" | "module";
type PlotType = "violin" | "box" | "violin+box";

interface FeatureGroup {
  group: string;
  values: number[];
  sample_ids?: string[];
}

interface FeaturePayload {
  id: string;
  rank: number;
  type: ViolinView | "module-eigengene";
  label?: string;
  feature?: string;
  groups: FeatureGroup[];
}

type ViolinVariant = {
  plotly_spec: Record<string, unknown>;
  default_state: Record<string, unknown>;
};

const PANEL_KEYS = ["panel_1", "panel_2", "panel_3", "panel_4"] as const;
const PANEL_LABELS = ["Left top", "Right top", "Left bottom", "Right bottom"];

export function InteractiveViolinBox({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Violin & Box Plot">
      {(data, controls) => <ViolinChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function ViolinChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const state = controls.state;
  const chartRef = useRef<HTMLDivElement>(null);
  const filename = (data.title || data.figure_id || "violin").replace(/\s+/g, "_");

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

  const availableViews = useMemo(() => {
    const values = variants
      .map(variant => variantView(variant))
      .filter((value): value is ViolinView => value === "metabolite" || value === "module");
    return Array.from(new Set(values));
  }, [variants]);

  const requestedView = String(state.view || data.default_state?.view || "metabolite") as ViolinView;
  const view = availableViews.includes(requestedView) ? requestedView : availableViews[0] || requestedView;
  const activeVariant = useMemo(() => (
    variants.find(variant => variantView(variant) === view) || variants[0]
  ), [variants, view]);

  const spec = activeVariant?.plotly_spec || {};
  const defaultState = activeVariant?.default_state || {};
  const features = ((spec.features || []) as FeaturePayload[]).filter(Boolean);
  const featureById = useMemo(() => new Map(features.map(feature => [feature.id, feature])), [features]);
  const featureOptions = ((spec.feature_options || features.map(feature => feature.id)) as string[]).filter(Boolean);
  const defaultFeatureIds = ((spec.default_feature_ids || []) as string[]).filter(Boolean);
  const groupOrder = ((spec.group_order || []) as string[]).filter(Boolean);
  const groupColors = ((spec.group_colors || []) as string[]).filter(Boolean);
  const yLabel = String(spec.y_label || defaultYLabel(view));
  const plotType = normalizePlotType(String(state.plot_type || state.chart_style || defaultState.plot_type || "violin+box"));

  const selectedFeatures = PANEL_KEYS.map((key, idx) => {
    const stateFeatureId = String(state[`${key}_feature_id`] || "");
    const defaultFeatureId = String(defaultState[`${key}_feature_id`] || defaultFeatureIds[idx] || featureOptions[idx] || "");
    const featureId = featureById.has(stateFeatureId) ? stateFeatureId : defaultFeatureId;
    return featureById.get(featureId) || null;
  });

  const traces = selectedFeatures.flatMap((feature, idx) => (
    feature ? featureToTraces(feature, idx, groupOrder, groupColors, plotType) : []
  ));
  const annotations = selectedFeatures.flatMap((feature, idx) => (
    feature ? panelTitleAnnotation(feature, idx) : emptyPanelAnnotation(idx)
  ));

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area" ref={chartRef}>
          {traces.length > 0 ? (
            <Plot
              data={traces}
              layout={{
                autosize: true,
                grid: { rows: 2, columns: 2, pattern: "independent" },
                hovermode: "closest",
                violinmode: "group",
                boxmode: "group",
                violingap: 0.03,
                violingroupgap: 0.04,
                boxgap: 0.12,
                boxgroupgap: 0.14,
                legend: { x: 1.02, y: 1, tracegroupgap: 4 },
                margin: { l: 58, r: 120, t: 58, b: 72 },
                annotations,
                ...axisLayouts(selectedFeatures, groupOrder, yLabel),
              } as unknown as Plotly.Layout}
              config={{
                displayModeBar: true,
                displaylogo: false,
                modeBarButtonsToRemove: ["lasso2d", "select2d"],
              }}
              useResizeHandler
              style={{ width: "100%", height: "100%" }}
            />
          ) : (
            <div className="ip-empty-chart">Violin/box data is not available.</div>
          )}
        </div>
        <div className="ip-infobar">
          <span>View: {formatView(view)}</span>
          <span>Plot type: {formatPlotType(plotType)}</span>
          <span>Showing: {selectedFeatures.filter(Boolean).length} panels</span>
        </div>
      </div>

      <div className="ip-controls ip-violin-controls">
        <div className="ip-control-group">
          <label className="ip-control-label">View</label>
          <select
            className="ip-control-select"
            value={view}
            onChange={e => setView(controls, variants, e.target.value as ViolinView)}
          >
            {availableViews.map(option => (
              <option key={option} value={option}>{formatView(option)}</option>
            ))}
          </select>
        </div>

        <div className="ip-control-group">
          <label className="ip-control-label">Plot type</label>
          <select
            className="ip-control-select"
            value={plotType}
            onChange={e => controls.setState("plot_type", e.target.value)}
          >
            <option value="violin">Violin only</option>
            <option value="box">Box only</option>
            <option value="violin+box">Violin + Box</option>
          </select>
        </div>

        {PANEL_KEYS.map((key, idx) => {
          const selected = selectedFeatures[idx];
          const selectedId = selected?.id || String(state[`${key}_feature_id`] || featureOptions[idx] || "");
          return (
            <div className="ip-panel-control" key={key}>
              <h3>{PANEL_LABELS[idx]}</h3>
              <label className="ip-control-label">{view === "module" ? "Module" : "Metabolite"}</label>
              <select
                className="ip-control-select"
                value={selectedId}
                onChange={e => controls.setState(`${key}_feature_id`, e.target.value)}
              >
                {featureOptions.map(option => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </div>
          );
        })}
      </div>
    </>
  );
}

function variantView(variant: ViolinVariant): ViolinView | "" {
  const fromState = String(variant.default_state?.view || "");
  if (fromState) return fromState as ViolinView;
  const fromSpec = String(variant.plotly_spec?.view || "");
  return fromSpec as ViolinView | "";
}

function setView(controls: ControlsAPI, variants: ViolinVariant[], view: ViolinView) {
  const variant = variants.find(item => variantView(item) === view);
  const spec = variant?.plotly_spec || {};
  const defaultState = variant?.default_state || {};
  const defaultFeatureIds = ((spec.default_feature_ids || []) as string[]).filter(Boolean);

  controls.setState("view", view);
  PANEL_KEYS.forEach((key, idx) => {
    controls.setState(`${key}_feature_id`, String(defaultState[`${key}_feature_id`] || defaultFeatureIds[idx] || ""));
  });
}

function featureToTraces(
  feature: FeaturePayload,
  idx: number,
  groupOrder: string[],
  groupColors: string[],
  plotType: PlotType,
): Plotly.Data[] {
  const axis = axisSuffix(idx);
  const orderedGroups = groupOrder.length
    ? groupOrder.map(group => feature.groups.find(item => item.group === group)).filter((item): item is FeatureGroup => Boolean(item))
    : feature.groups;

  return orderedGroups.flatMap(group => {
    const values = (group.values || []).filter(value => Number.isFinite(Number(value))).map(Number);
    if (!values.length) return [];
    const colorIdx = groupOrder.indexOf(group.group);
    const color = groupColors[colorIdx] || "#9ca3af";
    const sampleIds = group.sample_ids || [];
    const isCombined = plotType === "violin+box";
    const common = {
      name: group.group,
      legendgroup: group.group,
      scalegroup: `${feature.id}-${idx}`,
      marker: { color: isCombined ? "#111827" : color, opacity: isCombined ? 0.42 : 0.58, size: 3 },
      line: { color: "#111827", width: 0.8 },
      x: Array(values.length).fill(group.group),
      y: values,
      customdata: sampleIds,
      showlegend: idx === 0,
      xaxis: `x${axis}`,
      yaxis: `y${axis}`,
      hovertemplate: [
        "Sample ID: %{customdata}",
        `Feature: ${feature.id}`,
        "Group: %{x}",
        "Value: %{y:.3f}",
        "<extra></extra>",
      ].join("<br>"),
    };

    if (plotType === "box") {
      return [{
        ...common,
        type: "box",
        boxpoints: "all",
        jitter: 0.18,
        pointpos: 0,
        width: 0.34,
        fillcolor: withAlpha(color, 0.42),
        marker: { color, opacity: 0.62, size: 3 },
      } as Plotly.Data];
    }

    return [{
      ...common,
      type: "violin",
      box: {
        visible: isCombined,
        width: 0.26,
        fillcolor: "rgba(255,255,255,0.82)",
        line: { color: "#111827", width: 0.85 },
      },
      meanline: { visible: false },
      points: "all",
      jitter: 0.12,
      pointpos: 0,
      width: 0.82,
      fillcolor: withAlpha(color, 0.78),
      spanmode: "hard",
      scalemode: "width",
    } as Plotly.Data];
  });
}

function axisLayouts(
  selectedFeatures: Array<FeaturePayload | null>,
  groupOrder: string[],
  yLabel: string,
): Partial<Plotly.Layout> {
  const layout: Partial<Plotly.Layout> = {};
  selectedFeatures.forEach((feature, idx) => {
    const axis = axisSuffix(idx);
    const xKey = `xaxis${axis === "" ? "" : axis}` as keyof Plotly.Layout;
    const yKey = `yaxis${axis === "" ? "" : axis}` as keyof Plotly.Layout;
    const domain = subplotDomain(idx);
    layout[xKey] = {
      domain: domain.x,
      categoryorder: "array",
      categoryarray: groupOrder,
      tickangle: -35,
      title: { text: "" },
      automargin: true,
    } as never;
    layout[yKey] = {
      domain: domain.y,
      title: { text: feature ? yLabel : "" },
      zeroline: false,
      gridcolor: "#e5e7eb",
      automargin: true,
    } as never;
  });
  return layout;
}

function panelTitleAnnotation(feature: FeaturePayload, idx: number): Partial<Plotly.Annotations>[] {
  const domain = subplotDomain(idx);
  return [{
    text: feature.label || feature.id,
    xref: "paper",
    yref: "paper",
    x: (domain.x[0] + domain.x[1]) / 2,
    y: domain.y[1] + 0.04,
    showarrow: false,
    xanchor: "center",
    yanchor: "bottom",
    font: { size: 12, color: "#111827" },
  }];
}

function emptyPanelAnnotation(idx: number): Partial<Plotly.Annotations>[] {
  const domain = subplotDomain(idx);
  return [{
    text: "No data available",
    xref: "paper",
    yref: "paper",
    x: (domain.x[0] + domain.x[1]) / 2,
    y: (domain.y[0] + domain.y[1]) / 2,
    showarrow: false,
    font: { size: 12, color: "#6b7280" },
  }];
}

function subplotDomain(idx: number) {
  const col = idx % 2;
  const row = Math.floor(idx / 2);
  const xDomains: [number, number][] = [[0.04, 0.42], [0.58, 0.96]];
  const yDomains: [number, number][] = [[0.58, 1.0], [0.0, 0.42]];
  return { x: xDomains[col], y: yDomains[row] };
}

function axisSuffix(idx: number) {
  return idx === 0 ? "" : String(idx + 1);
}

function normalizePlotType(value: string): PlotType {
  if (value === "violin" || value === "box" || value === "violin+box") return value;
  if (value === "violin+box+strip") return "violin+box";
  return "violin+box";
}

function formatView(view: ViolinView) {
  if (view === "module") return "Module";
  return "Metabolite";
}

function formatPlotType(plotType: PlotType) {
  if (plotType === "violin") return "Violin only";
  if (plotType === "box") return "Box only";
  return "Violin + Box";
}

function defaultYLabel(view: ViolinView) {
  if (view === "module") return "Module eigengene z-score";
  return "Metabolite abundance z-score";
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
