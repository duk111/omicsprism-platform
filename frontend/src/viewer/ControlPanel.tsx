import type { ImageInfo } from "../api-types";
import "./ControlPanel.css";

interface Props {
  image: ImageInfo;
}

type ChartType = "volcano" | "pca" | "scatter" | "heatmap" | "bar" | "network" | "circos" | "upset" | "dendrogram" | "static";

function inferChartType(name: string): ChartType {
  const n = name.toLowerCase();
  if (n.includes("volcano")) return "volcano";
  if (n.includes("pca") || n.includes("oplsda") || n.includes("umap") || n.includes("tsne")) return "pca";
  if (n.includes("upset") || n.includes("evidence")) return "upset";
  if (n.includes("cnet")) return "network";
  if (n.includes("circos")) return "circos";
  if (n.includes("network")) return "network";
  if (n.includes("dendrogram") || n.includes("clustering")) return "dendrogram";
  if (n.includes("heatmap")) return "heatmap";
  if (n.includes("vip") || n.endsWith(".bar") || (n.includes("count") && (n.includes("dem") || n.includes("metabolite")))) return "bar";
  if (n.includes("regression") || n.includes("scatter") || n.includes("association") || n.includes("pairs")) return "scatter";
  if (n.includes("score") && !n.includes("zscore")) return "pca";
  if (n.includes("sankey")) return "static";
  return "static";
}

const CHART_LABELS: Record<ChartType, string> = {
  volcano: "Volcano Plot",
  pca: "Scores / PCA",
  scatter: "Scatter / Regression",
  heatmap: "Heatmap",
  bar: "Bar Chart",
  network: "Network",
  circos: "Circos",
  upset: "UpSet Plot",
  dendrogram: "Dendrogram",
  static: "Static Image",
};

const CHART_TIPS: Record<ChartType, string> = {
  volcano: "Scroll to zoom, drag to pan. Thresholds show significance cutoffs.",
  pca: "Scroll to zoom, drag to pan. Points are colored by group.",
  scatter: "Scroll to zoom, drag to pan. Regression line shows trend.",
  heatmap: "Scroll to zoom, drag to pan. Colors show association strength.",
  bar: "Scroll to zoom, drag to pan. Bars show ranked values.",
  network: "Scroll to zoom, drag to pan. Edges show relationships.",
  circos: "Scroll to zoom, drag to pan. Ribbons show connections.",
  upset: "Scroll to zoom, drag to pan. Bars show intersection sizes.",
  dendrogram: "Scroll to zoom, drag to pan. Tree shows clustering.",
  static: "Scroll to zoom, drag to pan.",
};

export function ControlPanel({ image }: Props) {
  const chartType = inferChartType(image.name);

  return (
    <aside className="cp-panel">
      <div className="cp-section">
        <span className="cp-chart-type-tag">{CHART_LABELS[chartType]}</span>
        <p className="cp-tip">{CHART_TIPS[chartType]}</p>
      </div>

      <div className="cp-section">
        <h3 className="cp-section-title">Shortcuts</h3>
        <div className="cp-shortcut-list">
          <span className="cp-shortcut"><kbd>Scroll</kbd> Zoom</span>
          <span className="cp-shortcut"><kbd>Drag</kbd> Pan</span>
          <span className="cp-shortcut"><kbd>Double-click</kbd> Reset</span>
          <span className="cp-shortcut"><kbd>+</kbd><kbd>-</kbd> Zoom in/out</span>
          <span className="cp-shortcut"><kbd>0</kbd> Reset view</span>
          <span className="cp-shortcut"><kbd>F</kbd> Fullscreen</span>
          <span className="cp-shortcut"><kbd>Esc</kbd> Close</span>
        </div>
      </div>

      {chartType === "volcano" && (
        <div className="cp-section">
          <h3 className="cp-section-title">Volcano Info</h3>
          <ul className="cp-info-list">
            <li>X-axis: log2 fold change</li>
            <li>Y-axis: -log10 adjusted p-value</li>
            <li>Red: upregulated</li>
            <li>Blue: downregulated</li>
            <li>Gray: not significant</li>
          </ul>
        </div>
      )}

      {chartType === "pca" && (
        <div className="cp-section">
          <h3 className="cp-section-title">PCA Info</h3>
          <ul className="cp-info-list">
            <li>Points: individual samples</li>
            <li>Colors: group membership</li>
            <li>Closer points = more similar</li>
          </ul>
        </div>
      )}

      {chartType === "heatmap" && (
        <div className="cp-section">
          <h3 className="cp-section-title">Heatmap Info</h3>
          <ul className="cp-info-list">
            <li>Rows: genes or modules</li>
            <li>Columns: metabolites</li>
            <li>Red: positive association</li>
            <li>Blue: negative association</li>
          </ul>
        </div>
      )}
    </aside>
  );
}
