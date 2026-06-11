import { useMemo } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

interface TrendGroup {
  group1: string;
  color: string;
  module_values: Array<number | null>;
  metabolite_values: Array<number | null>;
  counts: number[];
}

interface TrendPair {
  id: string;
  static_rank: number | null;
  combo_rank: number;
  module: string;
  metabolite: string;
  spearman_rho: number | null;
  abs_rho: number | null;
  module_color: string;
  metabolite_color: string;
  groups: TrendGroup[];
}

interface PairOption {
  id: string;
  label: string;
  module: string;
  metabolite: string;
  spearman_rho: number | null;
  abs_rho: number | null;
  static_rank: number | null;
  combo_rank: number;
}

export function InteractiveLinePanels({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Module-Metabolite Trends">
      {(data, controls) => <LinePanelsChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function LinePanelsChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const spec = data.plotly_spec || {};
  const state = controls.state;
  const pairs = ((spec.pairs || []) as TrendPair[]).filter(pair => pair?.id);
  const pairById = useMemo(() => new Map(pairs.map(pair => [pair.id, pair])), [pairs]);
  const pairOptions = (((spec.pair_options || []) as PairOption[]).filter(option => option?.id).length > 0
    ? (spec.pair_options as PairOption[])
    : pairs.map(pair => ({
      id: pair.id,
      label: `${pair.module} - ${pair.metabolite}`,
      module: pair.module,
      metabolite: pair.metabolite,
      spearman_rho: pair.spearman_rho,
      abs_rho: pair.abs_rho,
      static_rank: pair.static_rank,
      combo_rank: pair.combo_rank,
    }))).filter(option => pairById.has(option.id));
  const moduleOptions = ((spec.module_options || []) as string[]).filter(Boolean);
  const metaboliteOptions = ((spec.metabolite_options || []) as string[]).filter(Boolean);
  const group1Order = ((spec.group1_order || []) as string[]).filter(Boolean);
  const group2Order = ((spec.group2_order || []) as string[]).filter(Boolean);

  const selectedPair = resolvePair({
    pairs,
    pairById,
    pairId: String(state.pair_id || data.default_state?.pair_id || ""),
    moduleId: String(state.module_id || data.default_state?.module_id || ""),
    metaboliteId: String(state.metabolite_id || data.default_state?.metabolite_id || ""),
  });
  const selectedPairId = selectedPair?.id || "";
  const selectedModule = selectedPair?.module || String(state.module_id || data.default_state?.module_id || "");
  const selectedMetabolite = selectedPair?.metabolite || String(state.metabolite_id || data.default_state?.metabolite_id || "");

  const selectedGroups = useMemo(() => {
    const fromState = Array.isArray(state.visible_groups) ? (state.visible_groups as string[]) : [];
    const fallback = Array.isArray(data.default_state?.visible_groups)
      ? (data.default_state.visible_groups as string[])
      : group1Order;
    const requested = (fromState.length > 0 ? fromState : fallback)
      .map(String)
      .filter(group => group1Order.includes(group));
    const unique = Array.from(new Set(requested));
    return unique.length > 0 ? unique : group1Order;
  }, [state.visible_groups, data.default_state, group1Order]);

  const visibleGroupSet = useMemo(() => new Set(selectedGroups), [selectedGroups]);
  const visibleGroups = selectedPair?.groups.filter(group => visibleGroupSet.has(group.group1)) || [];
  const plotData = selectedPair ? pairToTraces(selectedPair, visibleGroups, group2Order) : [];
  const annotations = selectedPair ? groupAnnotations(visibleGroups) : [];
  const layoutAxes = selectedPair ? axisLayouts(visibleGroups) : {};
  const grid = panelGrid(visibleGroups.length);
  const chartHeight = Math.max(560, grid.rows * 300);

  return (
    <>
      <div className="ip-chart" style={{ minHeight: `${chartHeight}px` }}>
        <div className="ip-chart-area">
          {plotData.length > 0 ? (
            <Plot
              data={plotData}
              layout={{
                autosize: true,
                hovermode: "closest",
                dragmode: "pan",
                showlegend: true,
                legend: { orientation: "h", x: 0.5, xanchor: "center", y: 1.08, yanchor: "bottom" },
                margin: { l: 68, r: 36, t: 82, b: 78 },
                annotations,
                ...layoutAxes,
              }}
              config={{
                displayModeBar: true,
                displaylogo: false,
                scrollZoom: true,
                modeBarButtonsToRemove: ["lasso2d", "select2d"],
                ...(spec.config || {}),
              }}
              useResizeHandler
              style={{ width: "100%", height: "100%" }}
            />
          ) : (
            <div className="ip-empty-chart">F26 trend data is not available. Please regenerate figure data with the latest OmicsPrism code.</div>
          )}
        </div>
        <div className="ip-infobar">
          <span>Pair: {selectedPair ? `${selectedPair.module} - ${selectedPair.metabolite}` : "NA"}</span>
          <span>Spearman rho: {formatRho(selectedPair?.spearman_rho)}</span>
          <span>group1 panels: {visibleGroups.length}/{group1Order.length}</span>
        </div>
      </div>
      <div className="ip-controls">
        <div className="ip-control-group">
          <label className="ip-control-label">Pair</label>
          <select
            className="ip-control-select"
            value={selectedPairId}
            onChange={event => setPair(controls, pairById.get(event.target.value))}
          >
            {pairOptions.map(option => (
              <option key={option.id} value={option.id}>{formatPairOption(option)}</option>
            ))}
          </select>
        </div>

        <div className="ip-control-group">
          <label className="ip-control-label">Module</label>
          <select
            className="ip-control-select"
            value={selectedModule}
            onChange={event => setModuleMetabolite(controls, pairs, event.target.value, selectedMetabolite)}
          >
            {moduleOptions.map(option => <option key={option} value={option}>{option}</option>)}
          </select>
        </div>

        <div className="ip-control-group">
          <label className="ip-control-label">Metabolite</label>
          <select
            className="ip-control-select"
            value={selectedMetabolite}
            onChange={event => setModuleMetabolite(controls, pairs, selectedModule, event.target.value)}
          >
            {metaboliteOptions.map(option => <option key={option} value={option}>{option}</option>)}
          </select>
        </div>

        <div className="ip-control-group">
          <label className="ip-control-label">group1</label>
          {group1Order.map(group => {
            const checked = visibleGroupSet.has(group);
            return (
              <label className="ip-control-toggle" key={group}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={event => setGroupVisibility(controls, selectedGroups, group, event.target.checked)}
                />
                {group}
              </label>
            );
          })}
        </div>
      </div>
    </>
  );
}

function resolvePair({
  pairs,
  pairById,
  pairId,
  moduleId,
  metaboliteId,
}: {
  pairs: TrendPair[];
  pairById: Map<string, TrendPair>;
  pairId: string;
  moduleId: string;
  metaboliteId: string;
}) {
  const exact = pairs.find(pair => pair.module === moduleId && pair.metabolite === metaboliteId);
  if (exact) return exact;
  return pairById.get(pairId) || pairs[0] || null;
}

function pairToTraces(pair: TrendPair, visibleGroups: TrendGroup[], group2Order: string[]): Plotly.Data[] {
  return visibleGroups.flatMap((group, idx) => {
    const axis = axisSuffix(idx);
    const common = {
      x: group2Order,
      xaxis: `x${axis}`,
      yaxis: `y${axis}`,
      customdata: group2Order.map((group2, pointIdx) => [
        pair.module,
        pair.metabolite,
        group.group1,
        group2,
        group.counts?.[pointIdx] ?? null,
      ]),
    };
    return [
      {
        ...common,
        type: "scatter",
        mode: "lines+markers",
        y: group.module_values,
        name: "Module eigengene",
        legendgroup: "module",
        showlegend: idx === 0,
        line: { color: pair.module_color || "#111827", width: 1.6 },
        marker: { symbol: "circle", size: 7, color: group.color, line: { color: "white", width: 0.6 } },
        hovertemplate: hoverTemplate("Module eigengene"),
      } as Plotly.Data,
      {
        ...common,
        type: "scatter",
        mode: "lines+markers",
        y: group.metabolite_values,
        name: "Metabolite",
        legendgroup: "metabolite",
        showlegend: idx === 0,
        line: { color: pair.metabolite_color || "#7c3aed", width: 1.6 },
        marker: { symbol: "square", size: 7, color: group.color, line: { color: "white", width: 0.6 } },
        hovertemplate: hoverTemplate("Metabolite"),
      } as Plotly.Data,
    ];
  });
}

function axisLayouts(visibleGroups: TrendGroup[]): Partial<Plotly.Layout> {
  const layout: Partial<Plotly.Layout> = {};
  const domains = panelDomains(visibleGroups.length);
  visibleGroups.forEach((_group, idx) => {
    const axis = axisSuffix(idx);
    const xKey = `xaxis${axis === "" ? "" : axis}` as keyof Plotly.Layout;
    const yKey = `yaxis${axis === "" ? "" : axis}` as keyof Plotly.Layout;
    layout[xKey] = {
      domain: domains[idx].x,
      anchor: `y${axis}`,
      tickangle: -45,
      tickfont: { size: 10 },
      title: { text: "group2", font: { size: 11 } },
      zeroline: false,
      showline: true,
      linecolor: "#111111",
      linewidth: 1,
      mirror: false,
      ticks: "outside",
      gridcolor: "#e5e7eb",
    } as never;
    layout[yKey] = {
      domain: domains[idx].y,
      anchor: `x${axis}`,
      title: { text: "Z-score mean", font: { size: 11 } },
      showticklabels: true,
      zeroline: true,
      zerolinecolor: "#9ca3af",
      zerolinewidth: 1,
      showline: true,
      linecolor: "#111111",
      linewidth: 1,
      mirror: false,
      ticks: "outside",
      gridcolor: "#e5e7eb",
    } as never;
  });
  return layout;
}

function panelDomains(count: number): Array<{ x: [number, number]; y: [number, number] }> {
  if (count <= 0) return [];
  const grid = panelGrid(count);
  const xGap = grid.cols === 1 ? 0 : 0.08;
  const yGap = grid.rows === 1 ? 0 : 0.12;
  const panelWidth = (1 - xGap * (grid.cols - 1)) / grid.cols;
  const panelHeight = (1 - yGap * (grid.rows - 1)) / grid.rows;
  return Array.from({ length: count }, (_, idx) => {
    const row = Math.floor(idx / grid.cols);
    const col = idx % grid.cols;
    const x0 = col * (panelWidth + xGap);
    const yTop = 1 - row * (panelHeight + yGap);
    const y0 = yTop - panelHeight;
    return {
      x: [x0, x0 + panelWidth] as [number, number],
      y: [y0, yTop] as [number, number],
    };
  });
}

function groupAnnotations(visibleGroups: TrendGroup[]): Partial<Plotly.Annotations>[] {
  const domains = panelDomains(visibleGroups.length);
  return visibleGroups.map((group, idx) => ({
    text: group.group1,
    xref: "paper",
    yref: "paper",
    x: (domains[idx].x[0] + domains[idx].x[1]) / 2,
    y: domains[idx].y[1] + 0.035,
    showarrow: false,
    xanchor: "center",
    yanchor: "bottom",
    font: { size: 12, color: group.color || "#111827" },
  }));
}

function panelGrid(count: number) {
  if (count <= 1) return { rows: 1, cols: 1 };
  const cols = Math.ceil(Math.sqrt(count));
  const rows = Math.ceil(count / cols);
  return { rows, cols };
}

function axisSuffix(idx: number) {
  return idx === 0 ? "" : String(idx + 1);
}

function setPair(controls: ControlsAPI, pair?: TrendPair) {
  if (!pair) return;
  controls.setState("pair_id", pair.id);
  controls.setState("module_id", pair.module);
  controls.setState("metabolite_id", pair.metabolite);
}

function setModuleMetabolite(
  controls: ControlsAPI,
  pairs: TrendPair[],
  moduleId: string,
  metaboliteId: string,
) {
  const exact = pairs.find(pair => pair.module === moduleId && pair.metabolite === metaboliteId);

  controls.setState("module_id", moduleId);
  controls.setState("metabolite_id", metaboliteId);
  if (exact) {
    controls.setState("pair_id", exact.id);
  }
}

function setGroupVisibility(controls: ControlsAPI, selectedGroups: string[], group: string, checked: boolean) {
  const next = checked
    ? Array.from(new Set([...selectedGroups, group]))
    : selectedGroups.filter(item => item !== group);
  controls.setState("visible_groups", next);
}

function hoverTemplate(seriesName: string) {
  return [
    `Series: ${seriesName}`,
    "Module: %{customdata[0]}",
    "Metabolite: %{customdata[1]}",
    "group1: %{customdata[2]}",
    "group2: %{customdata[3]}",
    "n: %{customdata[4]}",
    "Z-score mean: %{y:.3f}",
    "<extra></extra>",
  ].join("<br>");
}

function formatPairOption(option: PairOption) {
  return `${option.module} - ${option.metabolite} | rho ${formatRho(option.spearman_rho)}`;
}

function formatRho(value: number | null | undefined) {
  return Number.isFinite(value ?? NaN) ? Number(value).toFixed(2) : "NA";
}
