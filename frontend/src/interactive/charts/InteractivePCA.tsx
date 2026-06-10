import { useMemo } from "react";
import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

interface PcaGroupRecord {
  sample_id: string;
  group1?: string;
  group2?: string;
}

interface PcaDataset {
  source: string;
  title: string;
  samples: string[];
  coords: number[][];
  var_exp: number[];
  groups: PcaGroupRecord[];
  group_styles?: {
    group1?: GroupStyle;
    group2?: GroupStyle;
  };
}

interface GroupStyle {
  groups?: string[];
  colors?: Record<string, string>;
  markers?: Record<string, string>;
  shape_by?: string;
}

export function InteractivePCA({ jobId, pageId }: Props) {
  const figureDataId = pageId === "pca-scatter" || pageId === "pca-pairs" ? "pca" : pageId;
  return (
    <InteractivePageShell jobId={jobId} pageId={figureDataId} pageTitle="PCA Explorer">
      {(data, controls) => <PCAChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function PCAChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const spec = data.plotly_spec;
  const datasets = (spec.datasets || {}) as Record<string, PcaDataset>;
  const available = data.available_states || {};
  const state = controls.state;
  const source = String(state.source || data.default_state?.source || Object.keys(datasets)[0] || "");
  const colorBy = String(state.color_by || data.default_state?.color_by || "group1");
  const dataset = datasets[source];

  if (!dataset && Array.isArray(spec.data)) {
    return <LegacyPCAChart data={data} controls={controls} />;
  }

  const pcOptions = available.x_pc as number[] | undefined;
  const maxPc = Math.min(5, dataset?.var_exp?.length || pcOptions?.length || 2);
  const xPc = clampPc(Number(state.x_pc || data.default_state?.x_pc || 1), maxPc);
  const yPcRaw = clampPc(Number(state.y_pc || data.default_state?.y_pc || 2), maxPc);
  const yPc = yPcRaw === xPc ? clampPc(xPc === 1 ? 2 : 1, maxPc) : yPcRaw;

  const hasGroup2 = dataset?.group_styles?.group2?.groups?.some(g => g && g !== "Samples") ?? false;
  const effectiveColorBy = hasGroup2 ? colorBy : "group1";

  const { traces, shapes } = useMemo(() => {
    if (!dataset) return { traces: [] as Plotly.Data[], shapes: [] as Partial<Plotly.Shape>[] };
    return buildPcaTraces(dataset, xPc - 1, yPc - 1, effectiveColorBy);
  }, [dataset, xPc, yPc, effectiveColorBy]);

  const sourceLabel = source === "metabolome" ? "Metabolome" : "Transcriptome";
  const xTitle = `PC${xPc} (${Number(dataset?.var_exp?.[xPc - 1] || 0).toFixed(1)}%)`;
  const yTitle = `PC${yPc} (${Number(dataset?.var_exp?.[yPc - 1] || 0).toFixed(1)}%)`;

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          <Plot
            data={traces}
            layout={{
              title: { text: `${sourceLabel} PCA` },
              autosize: true,
              dragmode: "pan",
              hovermode: "closest",
              paper_bgcolor: "#ffffff",
              plot_bgcolor: "#ffffff",
              margin: { l: 70, r: 30, t: 55, b: 65 },
              showlegend: traces.length > 1,
              legend: { x: 1.02, y: 1, xanchor: "left", yanchor: "top", font: { size: 10 } },
              xaxis: {
                title: { text: xTitle },
                zeroline: true,
                zerolinecolor: "#cbd5e1",
                zerolinewidth: 1,
                gridcolor: "#e5e7eb",
              },
              yaxis: {
                title: { text: yTitle },
                zeroline: true,
                zerolinecolor: "#cbd5e1",
                zerolinewidth: 1,
                gridcolor: "#e5e7eb",
              },
              shapes,
            }}
            config={{ displayModeBar: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] }}
            useResizeHandler
            style={{ width: "100%", height: "100%" }}
          />
        </div>
        <div className="ip-infobar">
          <span>Source: {sourceLabel}</span>
          <span>Shape: group1</span>
          <span>Color: {effectiveColorBy}</span>
          <span>X: PC{xPc}</span>
          <span>Y: PC{yPc}</span>
          <span>Samples: {dataset?.samples?.length || 0}</span>
        </div>
      </div>
      <div className="ip-controls">
        <SelectControl
          label="Data source"
          value={source}
          options={(available.source as string[] | undefined) || Object.keys(datasets)}
          onChange={value => controls.setState("source", value)}
          labels={{ transcriptome: "Transcriptome", metabolome: "Metabolome" }}
        />
        {hasGroup2 && (
          <SelectControl
            label="Color by"
            value={effectiveColorBy}
            options={(available.color_by as string[] | undefined) || ["group1", "group2"]}
            onChange={value => controls.setState("color_by", value)}
          />
        )}
        <SelectControl
          label="X axis"
          value={String(xPc)}
          options={Array.from({ length: maxPc }, (_, i) => String(i + 1))}
          onChange={value => controls.setState("x_pc", Number(value))}
          labels={pcLabels(dataset)}
        />
        <SelectControl
          label="Y axis"
          value={String(yPc)}
          options={Array.from({ length: maxPc }, (_, i) => String(i + 1))}
          onChange={value => controls.setState("y_pc", Number(value))}
          labels={pcLabels(dataset)}
        />
      </div>
    </>
  );
}

function LegacyPCAChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const state = controls.state;
  const source = String(state.source || data.default_state?.source || "transcriptome");
  const colorBy = String(state.color_by || data.default_state?.color_by || "group1");
  const selected = selectLegacyPcaSpec(data, source, colorBy);
  const spec = selected.plotly_spec || data.plotly_spec;
  const allTraces = (spec.data || []) as Plotly.Data[];
  const layout = (spec.layout || {}) as Partial<Plotly.Layout>;
  const available = data.available_states || {};
  const titleSource = source === "metabolome" ? "Metabolome" : "Transcriptome";
  const traces = allTraces.map(trace => {
    const t = trace as Plotly.Data & { hovertemplate?: string };
    return {
      ...t,
      hovertemplate: t.hovertemplate || "<b>%{text}</b><br>x: %{x:.3f}<br>y: %{y:.3f}<extra></extra>",
    };
  });

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          <Plot
            data={traces}
            layout={{
              ...(layout as Record<string, unknown>),
              title: { text: `${titleSource} PCA` },
              autosize: true,
              dragmode: "pan",
              hovermode: "closest",
            }}
            config={{ displayModeBar: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] }}
            useResizeHandler
            style={{ width: "100%", height: "100%" }}
          />
        </div>
        <div className="ip-infobar">
          <span>Source: {titleSource}</span>
          <span>Color by: {colorBy}</span>
          <span>Legacy PCA data: PC1/PC2 only</span>
        </div>
      </div>
      <div className="ip-controls">
        <SelectControl
          label="Data source"
          value={source}
          options={(available.source as string[] | undefined) || ["transcriptome", "metabolome"]}
          onChange={value => controls.setState("source", value)}
          labels={{ transcriptome: "Transcriptome", metabolome: "Metabolome" }}
        />
        <SelectControl
          label="Color by"
          value={colorBy}
          options={(available.color_by as string[] | undefined) || ["group1", "group2"]}
          onChange={value => controls.setState("color_by", value)}
        />
      </div>
    </>
  );
}

function selectLegacyPcaSpec(data: FigureData, source: string, colorBy: string) {
  const base = {
    plotly_spec: data.plotly_spec,
    default_state: data.default_state || {},
  };
  const baseSource = String(base.default_state.source || "");
  const baseColorBy = String(base.default_state.color_by || "");
  if (baseSource === source && baseColorBy === colorBy) return base;

  const altData = (data as FigureData & {
    alt_data?: Record<string, { plotly_spec?: Record<string, unknown>; default_state?: Record<string, unknown> }>;
  }).alt_data || {};

  for (const entry of Object.values(altData)) {
    const entrySource = String(entry.default_state?.source || "");
    const entryColorBy = String(entry.default_state?.color_by || "");
    if (entrySource === source && entryColorBy === colorBy) {
      return {
        plotly_spec: entry.plotly_spec || data.plotly_spec,
        default_state: entry.default_state || {},
      };
    }
  }

  return base;
}

function buildPcaTraces(dataset: PcaDataset, xIdx: number, yIdx: number, colorBy: string) {
  const groups = dataset.groups || [];
  const group2Style = dataset.group_styles?.group2 || {};
  const group1Style = dataset.group_styles?.group1 || {};
  const group2Names = (group2Style.groups && group2Style.groups.length)
    ? group2Style.groups
    : unique(groups.map(row => String(row.group2 || ""))).map(value => value || "Missing");
  const group1Names = (group1Style.groups && group1Style.groups.length)
    ? group1Style.groups
    : unique(groups.map(row => String(row.group1 || "Samples")));
  const group2Colors = group2Style.colors || {};
  const group1Colors = group1Style.colors || {};
  const markerByGroup1 = dataset.group_styles?.group1?.markers || {};
  const traces: Plotly.Data[] = [];
  const shapes: Partial<Plotly.Shape>[] = [];

  const hasGroup2 = group2Names.length && group2Names.some(name => name !== "Samples");
  const useGroup2Color = colorBy === "group2" && hasGroup2;

  if (useGroup2Color) {
    // group2 颜色 + group1 形状（无置信椭圆）
    for (const groupName of group2Names) {
      const points = collectPoints(dataset, groups, row => String(row.group2 || "Missing") === groupName, xIdx, yIdx);
      if (!points.x.length) continue;
      traces.push(scatterTrace(
        groupName,
        points,
        group2Colors[groupName] || "#4c78a8",
        points.group1.map(g => markerByGroup1[g] || "circle"),
      ));
    }

    // 形状图例（group1）
    for (const groupName of group1Names) {
      traces.push(shapeLegendTrace(groupName, markerByGroup1[groupName] || "circle"));
    }
  } else {
    // group1 颜色 + group1 形状 + 置信椭圆
    for (const groupName of group1Names) {
      const points = collectPoints(dataset, groups, row => String(row.group1 || "Samples") === groupName, xIdx, yIdx);
      if (!points.x.length) continue;
      const color = group1Colors[groupName] || "#4c78a8";
      traces.push(scatterTrace(groupName, points, color, markerByGroup1[groupName] || "circle"));
      const ellipse = confidenceEllipse(points.x, points.y, color);
      if (ellipse) shapes.push(ellipse);
    }
  }

  if (!traces.length) {
    const points = collectPoints(dataset, groups, () => true, xIdx, yIdx);
    traces.push(scatterTrace("Samples", points, "#4c78a8", "circle"));
  }

  return { traces, shapes };
}

function shapeLegendTrace(name: string, symbol: string): Plotly.Data {
  return {
    type: "scatter",
    mode: "markers",
    name: `group1: ${name}`,
    x: [null],
    y: [null],
    hoverinfo: "skip",
    showlegend: true,
    marker: {
      size: 8,
      color: "#111827",
      symbol,
      opacity: 0.9,
      line: { color: "white", width: 0.8 },
    },
  } as Plotly.Data;
}

function collectPoints(
  dataset: PcaDataset,
  groups: PcaGroupRecord[],
  predicate: (row: PcaGroupRecord) => boolean,
  xIdx: number,
  yIdx: number,
) {
  const x: number[] = [];
  const y: number[] = [];
  const sampleIds: string[] = [];
  const group1: string[] = [];
  const group2: string[] = [];

  dataset.coords.forEach((coord, idx) => {
    const row = groups[idx] || { sample_id: dataset.samples[idx] };
    if (!predicate(row)) return;
    x.push(Number(coord[xIdx]));
    y.push(Number(coord[yIdx]));
    sampleIds.push(row.sample_id || dataset.samples[idx]);
    group1.push(row.group1 || "");
    group2.push(row.group2 || "");
  });

  return { x, y, sampleIds, group1, group2 };
}

function scatterTrace(
  name: string,
  points: ReturnType<typeof collectPoints>,
  color: string,
  symbol: string | string[],
): Plotly.Data {
  return {
    type: "scatter",
    mode: "markers",
    name,
    x: points.x,
    y: points.y,
    customdata: points.sampleIds.map((sample, idx) => [sample, points.group1[idx], points.group2[idx]]),
    hovertemplate: "<b>%{customdata[0]}</b><br>group1: %{customdata[1]}<br>group2: %{customdata[2]}<br>x: %{x:.3f}<br>y: %{y:.3f}<extra></extra>",
    marker: {
      size: 8,
      color,
      symbol,
      opacity: 0.9,
      line: { color: "white", width: 0.8 },
    },
  };
}

function confidenceEllipse(x: number[], y: number[], color: string): Partial<Plotly.Shape> | null {
  if (x.length < 3 || y.length < 3) return null;
  const meanX = mean(x);
  const meanY = mean(y);
  const covXX = mean(x.map(v => (v - meanX) ** 2));
  const covYY = mean(y.map(v => (v - meanY) ** 2));
  const covXY = mean(x.map((v, i) => (v - meanX) * (y[i] - meanY)));
  const trace = covXX + covYY;
  const det = covXX * covYY - covXY * covXY;
  const disc = Math.sqrt(Math.max(0, trace * trace / 4 - det));
  const lambda1 = trace / 2 + disc;
  const lambda2 = trace / 2 - disc;
  if (!Number.isFinite(lambda1) || !Number.isFinite(lambda2) || lambda1 <= 0 || lambda2 <= 0) return null;
  const angle = Math.atan2(lambda1 - covXX, covXY || 1e-12);
  const scale = Math.sqrt(5.991464547107979);
  const rx = scale * Math.sqrt(lambda1);
  const ry = scale * Math.sqrt(lambda2);
  const points = Array.from({ length: 80 }, (_, idx) => {
    const t = (idx / 79) * Math.PI * 2;
    const px = rx * Math.cos(t);
    const py = ry * Math.sin(t);
    return [
      meanX + px * Math.cos(angle) - py * Math.sin(angle),
      meanY + px * Math.sin(angle) + py * Math.cos(angle),
    ];
  });
  const path = points.map((p, idx) => `${idx === 0 ? "M" : "L"} ${p[0]},${p[1]}`).join(" ") + " Z";
  return {
    type: "path",
    path,
    fillcolor: hexToRgba(color, 0.18),
    line: { color, width: 1.4 },
    layer: "below",
  };
}

function mean(values: number[]) {
  return values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length);
}

function hexToRgba(color: string, alpha: number) {
  if (!/^#[0-9a-fA-F]{6}$/.test(color)) return color;
  const r = parseInt(color.slice(1, 3), 16);
  const g = parseInt(color.slice(3, 5), 16);
  const b = parseInt(color.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function clampPc(value: number, maxPc: number) {
  if (!Number.isFinite(value)) return 1;
  return Math.min(Math.max(1, Math.round(value)), Math.max(1, maxPc));
}

function unique(values: string[]) {
  return Array.from(new Set(values.filter(Boolean)));
}

function pcLabels(dataset?: PcaDataset) {
  const labels: Record<string, string> = {};
  (dataset?.var_exp || []).slice(0, 5).forEach((variance, idx) => {
    labels[String(idx + 1)] = `PC${idx + 1} (${Number(variance).toFixed(1)}%)`;
  });
  return labels;
}

function SelectControl({
  label,
  value,
  options,
  onChange,
  labels = {},
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  labels?: Record<string, string>;
}) {
  return (
    <div className="ip-control-group">
      <label className="ip-control-label">{label}</label>
      <select className="ip-control-select" value={value} onChange={e => onChange(e.target.value)}>
        {options.map(option => <option key={option} value={option}>{labels[option] || option}</option>)}
      </select>
    </div>
  );
}
