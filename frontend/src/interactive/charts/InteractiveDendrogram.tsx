import { useMemo, useRef, useState, useCallback } from "react";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }

interface DendrogramPayload {
  icoord: number[][];
  dcoord: number[][];
  ivl: string[];
  leaves: number[];
  color_list: string[];
  branch_samples?: string[][];
  color_threshold?: number;
}

interface TreeNode {
  id: number;
  name: string;
  is_leaf: boolean;
  height: number;
  left?: number;
  right?: number;
}

interface TooltipState {
  x: number;
  y: number;
  samples: string[];
  visible: boolean;
}

export function InteractiveDendrogram({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Sample Clustering Dendrogram">
      {(data, controls) => <DendrogramChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function DendrogramChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [tooltip, setTooltip] = useState<TooltipState>({ x: 0, y: 0, samples: [], visible: false });
  const [hoveredBranch, setHoveredBranch] = useState<number | null>(null);

  const td = data.tree_data as Record<string, unknown> | undefined;
  const state = controls.state;
  const baseThreshold = Number(td?.color_threshold || data.default_state?.color_threshold || 0);
  const userThreshold = Number(state.color_threshold || baseThreshold);
  const colorThreshold = userThreshold > 0 ? userThreshold : baseThreshold;

  const dendrogram = useMemo(() => normalizeDendrogram(td, colorThreshold), [td, colorThreshold]);
  const labels = ((dendrogram?.ivl || td?.labels || []) as string[]).map(String);
  const nLeaves = Math.max(1, Number(td?.n_leaves || labels.length || 0));

  // ── Layout calculations ──────────────────────────────────────────────
  const pad = { top: 50, right: 40, bottom: 140, left: 80 };
  const minLeafSpacing = 22;
  const maxLeafSpacing = 70;
  const labelFontSize = Math.max(7, Math.min(11, Math.floor(700 / nLeaves)));
  const estimatedLabelHeight = labelFontSize * 7;
  const leafSpacing = Math.max(minLeafSpacing, Math.min(maxLeafSpacing, estimatedLabelHeight * 0.55));
  const plotW = nLeaves * leafSpacing;
  const viewW = Math.max(900, pad.left + plotW + pad.right);
  const viewH = 620;
  const plotH = viewH - pad.top - pad.bottom;

  // Zoom state
  const [zoom, setZoom] = useState(1);
  const minZoom = 0.5;
  const maxZoom = 3;
  const zoomStep = 0.15;

  // Pan / drag-to-scroll state
  const [isPanning, setIsPanning] = useState(false);
  const panOrigin = useRef({ x: 0, scrollLeft: 0 });

  const geometry = useMemo(() => {
    if (!dendrogram?.icoord?.length || !dendrogram?.dcoord?.length) return null;
    const allY = dendrogram.dcoord.flat().map(Number);
    const maxY = Math.max(...allY, 1);
    const ticks = Array.from({ length: 6 }, (_, idx) => (maxY / 5) * idx);
    return { maxY, ticks };
  }, [dendrogram]);

  // Scipy leaf positions: 5, 15, 25, ... (step = 10)
  const scipyLeafStep = 10;
  const scipyMinX = 5;
  const scipyMaxX = 5 + (nLeaves - 1) * scipyLeafStep;

  // Map scipy x to SVG pixel positions, with edge padding so first/last leaves don't touch axes
  const edgePad = leafSpacing * 0.5;
  const effectivePlotW = Math.max(1, plotW - 2 * edgePad);

  const mapX = useCallback((scipyX: number) => {
    if (nLeaves <= 1) return pad.left + plotW / 2;
    const ratio = (scipyX - scipyMinX) / (scipyMaxX - scipyMinX);
    return pad.left + edgePad + ratio * effectivePlotW;
  }, [nLeaves, pad.left, plotW, edgePad, effectivePlotW, scipyMinX, scipyMaxX]);

  const mapY = useCallback((y: number) => {
    if (!geometry) return pad.top + plotH;
    return pad.top + plotH - (y / geometry.maxY) * plotH;
  }, [geometry, pad.top, plotH]);

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -zoomStep : zoomStep;
    setZoom(prev => Math.min(maxZoom, Math.max(minZoom, prev + delta)));
  }, []);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    const el = containerRef.current;
    if (!el) return;
    setIsPanning(true);
    panOrigin.current = { x: e.clientX, scrollLeft: el.scrollLeft };
  }, []);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning) return;
    const el = containerRef.current;
    if (!el) return;
    const dx = e.clientX - panOrigin.current.x;
    el.scrollLeft = panOrigin.current.scrollLeft - dx;
  }, [isPanning]);

  const handleMouseUp = useCallback(() => {
    setIsPanning(false);
  }, []);

  const handleBranchEnter = useCallback((event: React.MouseEvent, idx: number, samples: string[]) => {
    setHoveredBranch(idx);
    const rect = containerRef.current?.getBoundingClientRect();
    if (rect) {
      setTooltip({
        x: event.clientX - rect.left + 14,
        y: event.clientY - rect.top - 10,
        samples,
        visible: true,
      });
    }
  }, []);

  const handleBranchMove = useCallback((event: React.MouseEvent, samples: string[]) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (rect) {
      setTooltip(prev => ({
        ...prev,
        x: event.clientX - rect.left + 14,
        y: event.clientY - rect.top - 10,
        samples,
      }));
    }
  }, []);

  const handleBranchLeave = useCallback(() => {
    setHoveredBranch(null);
    setTooltip(prev => ({ ...prev, visible: false }));
  }, []);

  // Determine which leaves belong to each branch for hover highlighting
  const branchLeafSet = useMemo(() => {
    if (!dendrogram?.icoord?.length) return [] as Set<number>[];
    return dendrogram.icoord.map((xs) => {
      const set = new Set<number>();
      // Scipy icoord: [left_x, left_x, right_x, right_x]
      // Find leaves whose x position falls within this branch's x range
      const minBranchX = Math.min(...xs.map(Number));
      const maxBranchX = Math.max(...xs.map(Number));
      for (let i = 0; i < nLeaves; i++) {
        const leafX = scipyMinX + i * scipyLeafStep;
        if (leafX >= minBranchX - 0.1 && leafX <= maxBranchX + 0.1) {
          set.add(i);
        }
      }
      return set;
    });
  }, [dendrogram, nLeaves, scipyMinX, scipyLeafStep]);

  // Is a leaf highlighted by current hover?
  const isLeafHighlighted = useCallback((leafIdx: number) => {
    if (hoveredBranch === null) return false;
    return branchLeafSet[hoveredBranch]?.has(leafIdx) ?? false;
  }, [hoveredBranch, branchLeafSet]);

  if (!geometry || !dendrogram) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "400px", color: "#6b7280" }}>
        Dendrogram data is not available.
      </div>
    );
  }

  return (
    <>
      <div className="ip-chart" style={{ minHeight: `${viewH}px` }}>
        <div
          className="ip-chart-area"
          ref={containerRef}
          style={{
            position: "relative",
            background: "#fff",
            overflow: "auto",
            cursor: isPanning ? "grabbing" : "grab",
            userSelect: "none",
          }}
          onWheel={handleWheel}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
        >
          <svg
            ref={svgRef}
            width={viewW}
            height={viewH}
            viewBox={`0 0 ${viewW} ${viewH}`}
            style={{
              display: "block",
              minWidth: `${viewW}px`,
              transform: `scale(${zoom})`,
              transformOrigin: "top left",
            }}
            role="img"
            aria-label="Sample clustering dendrogram"
          >
            {/* Title */}
            <text x={pad.left + plotW / 2} y={28} textAnchor="middle" fontSize="14" fontWeight="600" fill="#111827">
              Sample Clustering Dendrogram
            </text>

            {/* Y-axis grid lines + ticks */}
            {geometry.ticks.map((tick) => {
              const y = mapY(tick);
              return (
                <g key={`tick-${tick}`}>
                  <line x1={pad.left - 4} y1={y} x2={pad.left} y2={y} stroke="#374151" strokeWidth={1} />
                  <line x1={pad.left} y1={y} x2={pad.left + plotW} y2={y} stroke="#e5e7eb" strokeWidth={0.6} />
                  <text x={pad.left - 8} y={y + 3.5} textAnchor="end" fontSize="10" fill="#4b5563">
                    {formatTick(tick)}
                  </text>
                </g>
              );
            })}

            {/* Threshold line */}
            {colorThreshold > 0 && (
              <g>
                <line
                  x1={pad.left} y1={mapY(colorThreshold)}
                  x2={pad.left + plotW} y2={mapY(colorThreshold)}
                  stroke="#9ca3af" strokeWidth={1} strokeDasharray="5,4"
                />
                <text
                  x={pad.left + plotW - 6}
                  y={mapY(colorThreshold) - 6}
                  textAnchor="end"
                  fontSize="10"
                  fill="#6b7280"
                >
                  threshold
                </text>
              </g>
            )}

            {/* Branches (polylines) */}
            {dendrogram.icoord.map((xs, idx) => {
              const ys = dendrogram.dcoord[idx] || [];
              if (xs.length < 4 || ys.length < 4) return null;
              const points = xs.map((x, pi) => `${mapX(Number(x)).toFixed(1)},${mapY(Number(ys[pi] || 0)).toFixed(1)}`).join(" ");
              const samples = dendrogram.branch_samples?.[idx] || [];
              const color = dendrogram.color_list?.[idx] || "#aaaaaa";
              const isHovered = hoveredBranch === idx;
              return (
                <polyline
                  key={`b-${idx}`}
                  points={points}
                  fill="none"
                  stroke={isHovered ? "#dc2626" : color}
                  strokeWidth={isHovered ? 2.5 : 1.5}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  style={{ cursor: "pointer", transition: "stroke 0.15s, stroke-width 0.15s" }}
                  onMouseEnter={(e) => handleBranchEnter(e, idx, samples)}
                  onMouseMove={(e) => handleBranchMove(e, samples)}
                  onMouseLeave={handleBranchLeave}
                />
              );
            })}

            {/* Axes */}
            <line x1={pad.left} y1={pad.top + plotH} x2={pad.left + plotW} y2={pad.top + plotH} stroke="#374151" strokeWidth={1.2} />
            <line x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + plotH} stroke="#374151" strokeWidth={1.2} />

            {/* Leaf labels */}
            {labels.map((label, idx) => {
              // Align label x with the corresponding scipy leaf position
              const x = mapX(scipyMinX + idx * scipyLeafStep);
              const highlighted = isLeafHighlighted(idx);
              return (
                <g key={`lbl-${idx}`}>
                  {/* Small tick mark */}
                  <line
                    x1={x} y1={pad.top + plotH}
                    x2={x} y2={pad.top + plotH + 5}
                    stroke={highlighted ? "#dc2626" : "#9ca3af"}
                    strokeWidth={highlighted ? 2 : 1}
                  />
                  {/* Rotated label */}
                  <g transform={`translate(${x}, ${pad.top + plotH + 10}) rotate(90)`}>
                    <text
                      textAnchor="start"
                      fontSize={labelFontSize}
                      fill={highlighted ? "#dc2626" : "#333"}
                      fontWeight={highlighted ? 700 : 400}
                      style={{ transition: "fill 0.15s, font-weight 0.15s" }}
                    >
                      {label}
                    </text>
                  </g>
                </g>
              );
            })}

            {/* Axis labels */}
            <text x={pad.left + plotW / 2} y={viewH - 10} textAnchor="middle" fontSize="12" fill="#374151" fontWeight={500}>
              Sample
            </text>
            <text
              x={20}
              y={pad.top + plotH / 2}
              textAnchor="middle"
              fontSize="12"
              fill="#374151"
              fontWeight={500}
              transform={`rotate(-90, 20, ${pad.top + plotH / 2})`}
            >
              Euclidean Distance
            </text>
          </svg>

          {/* Tooltip */}
          {tooltip.visible && tooltip.samples.length > 0 && (
            <div
              className="ip-tooltip"
              style={{
                position: "absolute",
                left: tooltip.x,
                top: tooltip.y,
                zIndex: 10,
                background: "rgba(255,255,255,0.97)",
                border: "1px solid #e5e7eb",
                borderRadius: "6px",
                padding: "8px 12px",
                boxShadow: "0 4px 12px rgba(0,0,0,0.12)",
                fontSize: "12px",
                maxWidth: "280px",
                pointerEvents: "none",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: "4px", color: "#111827" }}>
                {tooltip.samples.length === 1 ? tooltip.samples[0] : `${tooltip.samples.length} samples`}
              </div>
              {tooltip.samples.length > 1 && (
                <div style={{ color: "#4b5563", lineHeight: "1.5" }}>
                  {tooltip.samples.slice(0, 8).join(", ")}
                  {tooltip.samples.length > 8 && ` … +${tooltip.samples.length - 8} more`}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="ip-infobar">
          <span>Samples: {nLeaves}</span>
          <span>Method: average linkage</span>
          <span>Threshold: {colorThreshold.toFixed(2)}</span>
          <span>Zoom: {Math.round(zoom * 100)}%</span>
          <button
            className="secondary"
            style={{ padding: "2px 8px", fontSize: "11px" }}
            onClick={() => setZoom(1)}
            type="button"
          >
            Reset zoom
          </button>
        </div>
      </div>

      <div className="ip-controls">
        <div className="ip-control-group">
          <label className="ip-control-label">Color threshold</label>
          <div className="ip-control-range">
            <input
              type="range"
              min={0}
              max={baseThreshold > 0 ? Math.round(baseThreshold * 1.5 * 1000) / 1000 : 1}
              step={0.001}
              value={colorThreshold}
              onChange={(e) => controls.setState("color_threshold", parseFloat(e.target.value))}
            />
            <span>{colorThreshold.toFixed(3)}</span>
          </div>
        </div>
      </div>
    </>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────

function normalizeDendrogram(
  td?: Record<string, unknown>,
  overrideThreshold?: number,
): DendrogramPayload | undefined {
  const existing = td?.dendrogram as DendrogramPayload | undefined;
  const baseThreshold = Number(td?.color_threshold || 0);
  const threshold = overrideThreshold ?? baseThreshold;

  // Use scipy dendrogram output directly if available
  if (existing?.icoord?.length && existing?.dcoord?.length) {
    const dcoord = existing.dcoord || [];
    const colorList = existing.color_list || [];

    // If threshold changed, recompute colors; otherwise keep scipy's colors
    let newColorList = colorList;
    if (overrideThreshold !== undefined && dcoord.length) {
      const clusterColors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
      ];
      let clusterColorIndex = 0;
      newColorList = dcoord.map((ys) => {
        const yArr = ys as number[];
        // In scipy dendrogram dcoord, the branch height is the middle two values
        const branchHeight = yArr.length >= 2 ? Math.max(yArr[1], yArr[2] ?? yArr[1]) : Math.max(...yArr);
        return branchHeight > threshold ? "#aaaaaa" : clusterColors[clusterColorIndex++ % clusterColors.length];
      });
    }

    return {
      ...existing,
      color_threshold: threshold,
      color_list: newColorList,
    };
  }

  // Fallback: build from nodes/linkage tree structure
  const nodes = (td?.nodes || []) as TreeNode[];
  if (!nodes.length) return undefined;

  const nodeById = new Map<number, TreeNode>();
  nodes.forEach((node) => nodeById.set(Number(node.id), node));

  const internalNodes = nodes.filter((node) => !node.is_leaf);
  const root = internalNodes.length
    ? internalNodes.reduce((current, node) =>
        Number(node.id) > Number(current.id) ? node : current, internalNodes[0])
    : undefined;
  if (!root) return undefined;

  // Build leaf order by in-order traversal
  const leafOrder: string[] = [];
  function inorder(nodeId: number): void {
    const node = nodeById.get(nodeId);
    if (!node) return;
    if (node.is_leaf) {
      leafOrder.push(String(node.name));
      return;
    }
    inorder(Number(node.left));
    inorder(Number(node.right));
  }
  inorder(Number(root.id));

  const n = leafOrder.length;
  const leafPos = new Map<number, number>();
  nodes.filter((n) => n.is_leaf).forEach((node) => {
    const idx = leafOrder.indexOf(String(node.name));
    if (idx >= 0) leafPos.set(Number(node.id), 5 + idx * 10);
  });

  const icoord: number[][] = [];
  const dcoord: number[][] = [];
  const colorList: string[] = [];
  const branchSamples: string[][] = [];
  const xById = new Map<number, number>(leafPos);

  const maxHeight = Math.max(...internalNodes.map((n) => Number(n.height || 0)), 1);
  const treeThreshold = threshold > 0 ? threshold : 0.7 * maxHeight;

  const clusterColors = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
  ];
  let colorIdx = 0;

  function build(nodeId: number): { x: number; y: number; samples: string[] } {
    const node = nodeById.get(nodeId);
    if (!node) return { x: 0, y: 0, samples: [] };
    if (node.is_leaf) {
      const x = xById.get(nodeId) ?? 0;
      return { x, y: 0, samples: [String(node.name)] };
    }

    const left = build(Number(node.left));
    const right = build(Number(node.right));
    const x = (left.x + right.x) / 2;
    const y = Number(node.height || 0);
    const samples = [...left.samples, ...right.samples];
    xById.set(nodeId, x);

    // Inverted-U shape: (left_x, left_y) → (left_x, parent_y) → (right_x, parent_y) → (right_x, right_y)
    icoord.push([left.x, left.x, right.x, right.x]);
    dcoord.push([left.y, y, y, right.y]);
    colorList.push(y > treeThreshold ? "#aaaaaa" : clusterColors[colorIdx++ % clusterColors.length]);
    branchSamples.push(samples);

    return { x, y, samples };
  }

  build(Number(root.id));

  return {
    icoord,
    dcoord,
    ivl: leafOrder,
    leaves: [],
    color_list: colorList,
    branch_samples: branchSamples,
    color_threshold: treeThreshold,
  };
}

function formatTick(value: number): string {
  if (value === 0) return "0";
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}
