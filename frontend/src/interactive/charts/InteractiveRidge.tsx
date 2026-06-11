import Plot from "react-plotly.js";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

interface RidgeGroup {
  group: string;
  color: string;
  density: number[] | null;
  rug: number[];
  rug_offset?: number;
}

interface RidgeRow {
  module: string;
  y_base?: number;
  groups: RidgeGroup[];
}

export function InteractiveRidge({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Ridge Distribution">
      {(data, controls) => <RidgeChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function RidgeChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const rd = data.ridge_data || {};
  const ridges = ((rd.ridges || []) as RidgeRow[]).filter(Boolean);
  const xGrid = ((rd.x_grid || []) as number[]).map(Number);
  const groupOrder = ((rd.group1_order || []) as string[]).filter(Boolean);
  const groupColors = (rd.group1_colors || {}) as Record<string, string>;
  const ridgeHeight = Number(rd.ridge_height ?? 0.72);
  const rugHeight = Number(rd.rug_height ?? ridgeHeight * 0.10);
  const state = controls.state;
  const visibleGroups = normalizeVisibleGroups(state.visible_groups, groupOrder);
  const visibleSet = new Set(visibleGroups);

  const traces: Plotly.Data[] = [];
  ridges.forEach((ridge, idx) => {
    const yBase = Number.isFinite(ridge.y_base ?? NaN) ? Number(ridge.y_base) : ridges.length - idx - 1;
    ridge.groups
      .filter(group => visibleSet.has(group.group))
      .forEach(group => {
        const color = group.color || groupColors[group.group] || "#9ca3af";
        if (group.density?.length && xGrid.length) {
          const yTop = group.density.map(value => yBase + Number(value) * ridgeHeight);
          traces.push({
            type: "scatter",
            mode: "lines",
            x: [...xGrid, ...xGrid.slice().reverse()],
            y: [...yTop, ...Array(xGrid.length).fill(yBase).reverse()],
            fill: "toself",
            fillcolor: withAlpha(color, 0.16),
            line: { color: "rgba(0,0,0,0)", width: 0 },
            hoverinfo: "skip",
            showlegend: false,
          } as Plotly.Data);
          traces.push({
            type: "scatter",
            mode: "lines",
            x: xGrid,
            y: yTop,
            name: group.group,
            legendgroup: group.group,
            line: { color, width: 1.0 },
            showlegend: idx === 0,
            hovertemplate: [
              `Module: ${ridge.module}`,
              `Group: ${group.group}`,
              "z-score: %{x:.3f}",
              "<extra></extra>",
            ].join("<br>"),
          } as Plotly.Data);
        }

        if (group.rug?.length) {
          const offset = Number(group.rug_offset || 0);
          group.rug.forEach((value, rugIdx) => {
            traces.push({
              type: "scatter",
              mode: "lines",
              x: [value, value],
              y: [yBase + offset, yBase + offset + rugHeight],
              line: { color, width: 0.55 },
              opacity: 0.36,
              showlegend: false,
              hovertemplate: [
                `Module: ${ridge.module}`,
                `Group: ${group.group}`,
                `Sample point: ${rugIdx + 1}`,
                "z-score: %{x:.3f}",
                "<extra></extra>",
              ].join("<br>"),
            } as Plotly.Data);
          });
        }
      });
  });

  const baselineShapes = ridges.map((ridge, idx) => {
    const yBase = Number.isFinite(ridge.y_base ?? NaN) ? Number(ridge.y_base) : ridges.length - idx - 1;
    return {
      type: "line",
      xref: "x",
      yref: "y",
      x0: xGrid[0] ?? -3,
      x1: xGrid[xGrid.length - 1] ?? 3,
      y0: yBase,
      y1: yBase,
      line: { color: "#e5e7eb", width: 0.55 },
      layer: "below",
    } as Partial<Plotly.Shape>;
  });

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area">
          {traces.length > 0 ? (
            <Plot
              data={traces}
              layout={{
                autosize: true,
                hovermode: "closest",
                showlegend: true,
                margin: { l: 120, r: 120, t: 52, b: 62 },
                title: { text: "Module Eigengene Ridge Distribution by group1", font: { size: 16 } },
                xaxis: {
                  title: { text: "Module eigengene z-score" },
                  gridcolor: "#e5e7eb",
                  zeroline: false,
                },
                yaxis: {
                  title: { text: "Module" },
                  tickvals: ridges.map((ridge, idx) => (
                    Number.isFinite(ridge.y_base ?? NaN) ? Number(ridge.y_base) : ridges.length - idx - 1
                  )),
                  ticktext: ridges.map(ridge => ridge.module),
                  range: [-0.35, Math.max(0, ridges.length - 1) + ridgeHeight + 0.20],
                },
                legend: { x: 1.015, y: 1, xanchor: "left", yanchor: "top" },
                shapes: baselineShapes,
              }}
              config={{
                displayModeBar: true,
                displaylogo: false,
                modeBarButtonsToRemove: ["lasso2d", "select2d"],
              }}
              useResizeHandler
              style={{ width: "100%", height: "100%" }}
            />
          ) : (
            <div className="ip-empty-chart">Ridge data is not available for the selected groups.</div>
          )}
        </div>
        <div className="ip-infobar">
          <span>Groups: {visibleGroups.length}/{groupOrder.length}</span>
          <span>Modules: {ridges.length}</span>
        </div>
      </div>
      <div className="ip-controls">
        <div className="ip-control-group">
          <label className="ip-control-label">group1</label>
          {groupOrder.map(group => (
            <label className="ip-control-toggle" key={group}>
              <input
                type="checkbox"
                checked={visibleSet.has(group)}
                onChange={e => {
                  const next = e.target.checked
                    ? [...visibleGroups, group]
                    : visibleGroups.filter(item => item !== group);
                  controls.setState("visible_groups", groupOrder.filter(item => next.includes(item)));
                }}
              />
              <span style={{ color: groupColors[group] || "#374151" }}>{group}</span>
            </label>
          ))}
        </div>
      </div>
    </>
  );
}

function normalizeVisibleGroups(value: unknown, groupOrder: string[]) {
  if (Array.isArray(value)) {
    const selected = value.map(String).filter(group => groupOrder.includes(group));
    return selected.length ? selected : groupOrder;
  }
  if (typeof value === "string" && value.trim()) {
    const selected = value.split(",").map(item => item.trim()).filter(group => groupOrder.includes(group));
    return selected.length ? selected : groupOrder;
  }
  return groupOrder;
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
