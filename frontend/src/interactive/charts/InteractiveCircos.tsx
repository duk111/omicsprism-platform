import { useEffect, useMemo, useRef, useState } from "react";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";
import { downloadSvg, downloadPng } from "../svgExport";

interface Props { jobId: string; pageId: string; }

type LayoutMode = "circos" | "cnet";

interface NetworkEdge {
  source: string;
  target: string;
  weight: number;
  sign: string;
  model_support: number;
  color?: string;
}

interface CircosNode {
  id: string;
  name: string;
  type: "gene" | "metabolite";
  theta_start: number;
  theta_end: number;
  theta_mid: number;
  module: string;
  module_color: string;
  mean_zscore: number;
  weighted_degree: number;
  module_core: number;
  direction_bias: number;
  positive_edges?: number;
  negative_edges?: number;
  kme: number;
  track_values: number[];
}

interface CnetNode {
  id: string;
  name: string;
  type: "gene" | "metabolite";
  module: string;
  module_color: string;
  x: number;
  y: number;
  theta: number;
  ring_radius: number;
  node_radius: number;
  edge_count: number;
  mean_zscore: number;
  weighted_degree: number;
  direction_bias: number;
  kme: number;
}

interface CircosLayout {
  type: "circos";
  nodes: CircosNode[];
  edges: NetworkEdge[];
  gene_nodes?: string[];
  metabolite_nodes?: string[];
  radii: Record<string, number>;
  scales: Record<string, number>;
  group1_order: string[];
  group1_color_map: Record<string, string>;
  group_legend: Array<{ label: string; color: string }>;
  module_legend: Array<{ label: string; color: string }>;
  track_legend: Array<{ label: string; description: string }>;
}

interface CnetLayout {
  type: "cnet";
  nodes: CnetNode[];
  edges: NetworkEdge[];
  legend: Array<{ label: string; color: string }>;
}

interface CircosData {
  layouts?: {
    circos?: CircosLayout | null;
    cnet?: CnetLayout | null;
  };
  edge_palette?: Record<string, string>;
}

interface TooltipState {
  x: number;
  y: number;
  text: string;
}

interface SvgViewBox {
  minX: number;
  minY: number;
  width: number;
  height: number;
}

const VIEW_BOX = { minX: -1.62, minY: -1.22, width: 2.94, height: 2.44 };

export function InteractiveCircos({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Circos / CNet Network">
      {(data, controls) => <CircosChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function CircosChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const circosData = data.circos_data as CircosData | undefined;
  const layouts = circosData?.layouts || {};
  const availableLayouts = (data.available_states?.layout || ["circos", "cnet"])
    .map(String)
    .filter((layout): layout is LayoutMode => (layout === "circos" || layout === "cnet") && Boolean(layouts[layout]));
  const requestedLayout = String(controls.state.layout || data.default_state?.layout || "circos") as LayoutMode;
  const layoutMode = availableLayouts.includes(requestedLayout) ? requestedLayout : availableLayouts[0] || "circos";
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const chartAreaRef = useRef<HTMLDivElement>(null);

  const circosFilename = (data.title || data.figure_id || "circos").replace(/\s+/g, "_");
  const { setDownloadHandlers } = controls;
  useEffect(() => {
    setDownloadHandlers(
      () => {
        const svg = chartAreaRef.current?.querySelector("svg") as SVGSVGElement | null;
        if (svg) downloadPng(svg, 1, circosFilename);  // zoom=1: viewBox already encodes zoom/pan
      },
      () => {
        const svg = chartAreaRef.current?.querySelector("svg") as SVGSVGElement | null;
        if (svg) downloadSvg(svg, 1, circosFilename);
      },
    );
    return () => setDownloadHandlers(null, null);
  }, [setDownloadHandlers, circosFilename]);

  const stats = useMemo(() => {
    const layout = layouts[layoutMode];
    return {
      nodes: layout?.nodes?.length || 0,
      edges: layout?.edges?.length || 0,
    };
  }, [layouts, layoutMode]);

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area" ref={chartAreaRef} style={{ position: "relative", display: "grid", placeItems: "center", background: "#fff" }}>
          {layoutMode === "cnet" && layouts.cnet ? (
            <CnetSvg
              layout={layouts.cnet}
              selectedNodeId={selectedNodeId}
              onSelectNode={nodeId => setSelectedNodeId(current => current === nodeId ? "" : nodeId)}
              onClearSelection={() => setSelectedNodeId("")}
              onTooltip={setTooltip}
            />
          ) : layouts.circos ? (
            <CompressedCircosSvg
              layout={layouts.circos}
              edgePalette={circosData?.edge_palette || {}}
              selectedNodeId={selectedNodeId}
              onSelectNode={nodeId => setSelectedNodeId(current => current === nodeId ? "" : nodeId)}
              onClearSelection={() => setSelectedNodeId("")}
              onTooltip={setTooltip}
            />
          ) : (
            <div className="ip-empty-chart">Network data is not available.</div>
          )}
          {tooltip && (
            <div
              style={{
                position: "absolute",
                left: tooltip.x + 12,
                top: tooltip.y - 8,
                background: "rgba(17,24,39,0.90)",
                color: "#fff",
                padding: "6px 9px",
                borderRadius: 5,
                fontSize: "0.76rem",
                whiteSpace: "pre-line",
                pointerEvents: "none",
                zIndex: 10,
                maxWidth: 260,
              }}
            >
              {tooltip.text}
            </div>
          )}
        </div>
        <div className="ip-infobar">
          <span>Layout: {layoutMode === "cnet" ? "CNet" : "Circos"}</span>
          <span>Nodes: {stats.nodes}</span>
          <span>Edges: {stats.edges}</span>
          {selectedNodeId && <span>Selected: {selectedNodeId}</span>}
        </div>
      </div>
      <div className="ip-controls">
        <div className="ip-control-group">
          <label className="ip-control-label">Layout</label>
          <select
            className="ip-control-select"
            value={layoutMode}
            onChange={event => controls.setState("layout", event.target.value)}
          >
            {availableLayouts.map(option => (
              <option key={option} value={option}>{option === "cnet" ? "CNet" : "Circos"}</option>
            ))}
          </select>
        </div>
      </div>
    </>
  );
}

function CompressedCircosSvg({
  layout,
  edgePalette,
  selectedNodeId,
  onSelectNode,
  onClearSelection,
  onTooltip,
}: {
  layout: CircosLayout;
  edgePalette: Record<string, string>;
  selectedNodeId: string;
  onSelectNode: (nodeId: string) => void;
  onClearSelection: () => void;
  onTooltip: (tooltip: TooltipState | null) => void;
}) {
  const nodeById = useMemo(() => new Map(layout.nodes.map(node => [node.id, node])), [layout.nodes]);
  const connectedNodeIds = useMemo(() => connectedNodes(layout.edges, selectedNodeId), [layout.edges, selectedNodeId]);
  const supportValues = layout.edges.map(edge => Number(edge.model_support || 0));
  const supportMin = supportValues.length ? Math.min(...supportValues) : 0;
  const supportMax = supportValues.length ? Math.max(...supportValues) : 1;

  return (
    <ZoomableSvg baseViewBox={VIEW_BOX} onMouseLeave={() => onTooltip(null)} onCanvasClick={onClearSelection}>
      <rect x={VIEW_BOX.minX} y={VIEW_BOX.minY} width={VIEW_BOX.width} height={VIEW_BOX.height} fill="#ffffff" />
      <g>
        {layout.edges.map((edge, idx) => {
          const source = nodeById.get(edge.source);
          const target = nodeById.get(edge.target);
          if (!source || !target) return null;
          const highlighted = isHighlightedEdge(edge, selectedNodeId);
          const faded = Boolean(selectedNodeId) && !highlighted;
          const color = edge.sign === "positive"
            ? edgePalette.positive || "#dc2626"
            : edgePalette.negative || "#2563eb";
          const alpha = supportMax > supportMin
            ? 0.05 + 0.30 * (edge.model_support - supportMin) / (supportMax - supportMin)
            : (supportMax > 0 ? 0.22 : 0.08);
          const width = 0.0012 + 0.0105 * Math.sqrt(Math.min(1, Math.max(0, edge.weight || 0)));
          return (
            <path
              key={`${edge.source}-${edge.target}-${idx}`}
              d={linkPath(source.theta_mid, target.theta_mid, layout.radii.link_radius || 0.47)}
              fill="none"
              stroke={color}
              strokeWidth={highlighted ? width * 3.0 : width}
              strokeLinecap="round"
              opacity={highlighted ? 0.95 : faded ? 0.045 : clamp(alpha, 0.025, 0.62)}
            />
          );
        })}
      </g>

      {layout.nodes.map(node => {
        const nodeSelected = selectedNodeId === node.id;
        const nodeConnected = connectedNodeIds.has(node.id);
        const nodeFaded = Boolean(selectedNodeId) && !nodeSelected && !nodeConnected;
        return (
        <g
          key={node.id}
          opacity={nodeFaded ? 0.18 : 1}
          style={{ cursor: "pointer" }}
          onClick={event => {
            event.stopPropagation();
            onSelectNode(node.id);
          }}
          onMouseMove={event => onTooltip(relativeTooltip(event, nodeTooltip(node)))}
          onMouseLeave={() => onTooltip(null)}
        >
          <AnnularSegment
            thetaStart={node.theta_start}
            thetaEnd={node.theta_end}
            rInner={layout.radii.outer_strip_inner}
            rOuter={layout.radii.outer_strip_outer}
            fill={node.type === "metabolite" ? "#c9ad85" : node.module_color || "#9ca3af"}
            stroke="#ffffff"
            strokeWidth={0.0045}
          />
          <GroupMeanTrack node={node} layout={layout} />
          <AnnularSegment
            thetaStart={node.theta_start}
            thetaEnd={node.theta_end}
            rInner={layout.radii.track_meanheat_inner}
            rOuter={layout.radii.track_meanheat_outer}
            fill={divergingColor(node.mean_zscore, node.type === "gene" ? layout.scales.gene_mean : layout.scales.metabolite_mean)}
            stroke="#ffffff"
            strokeWidth={0.0022}
          />
          <AnnularSegment
            thetaStart={node.theta_start}
            thetaEnd={node.theta_end}
            rInner={layout.radii.track_degree_inner}
            rOuter={scaledOuter(layout.radii.track_degree_inner, layout.radii.track_degree_outer, node.weighted_degree, node.type === "gene" ? layout.scales.gene_degree : layout.scales.metabolite_degree)}
            fill="#4b5563"
            stroke="none"
            opacity={0.92}
          />
          <AnnularSegment
            thetaStart={node.theta_start}
            thetaEnd={node.theta_end}
            rInner={layout.radii.track_core_inner}
            rOuter={scaledOuter(layout.radii.track_core_inner, layout.radii.track_core_outer, node.module_core, node.type === "gene" ? layout.scales.gene_core : layout.scales.metabolite_core)}
            fill={node.type === "gene" ? node.module_color || "#9ca3af" : "#8c6d46"}
            stroke="none"
            opacity={0.92}
          />
          <AnnularSegment
            thetaStart={node.theta_start}
            thetaEnd={node.theta_end}
            rInner={layout.radii.track_bias_inner}
            rOuter={layout.radii.track_bias_outer}
            fill={divergingColor(node.direction_bias, 1)}
            stroke="#ffffff"
            strokeWidth={0.0022}
          />
          {nodeSelected && (
            <AnnularSegment
              thetaStart={node.theta_start}
              thetaEnd={node.theta_end}
              rInner={layout.radii.outer_strip_inner - 0.006}
              rOuter={layout.radii.outer_strip_outer + 0.012}
              fill="none"
              stroke="#f59e0b"
              strokeWidth={0.008}
            />
          )}
        </g>
        );
      })}

      <TrackNumberLabels radii={layout.radii} labelTheta={outerGapTheta(layout)} />
      <TrackLegend items={layout.track_legend} />
      <DiscreteLegend title="Track 2 group colors" items={layout.group_legend} x={-1.48} y={0.08} shape="circle" />
      <DiscreteLegend title="Modules" items={layout.module_legend} x={-1.48} y={0.46} shape="rect" />
    </ZoomableSvg>
  );
}

function CnetSvg({
  layout,
  selectedNodeId,
  onSelectNode,
  onClearSelection,
  onTooltip,
}: {
  layout: CnetLayout;
  selectedNodeId: string;
  onSelectNode: (nodeId: string) => void;
  onClearSelection: () => void;
  onTooltip: (tooltip: TooltipState | null) => void;
}) {
  const nodeById = useMemo(() => new Map(layout.nodes.map(node => [node.id, node])), [layout.nodes]);
  const connectedNodeIds = useMemo(() => connectedNodes(layout.edges, selectedNodeId), [layout.edges, selectedNodeId]);
  const extent = Math.max(1.26, ...layout.nodes.map(node => Math.max(Math.abs(node.x), Math.abs(node.y)) + node.node_radius + 0.08));
  const baseViewBox = useMemo(() => ({ minX: -extent, minY: -extent, width: 2 * extent, height: 2 * extent }), [extent]);

  return (
    <ZoomableSvg baseViewBox={baseViewBox} onMouseLeave={() => onTooltip(null)} onCanvasClick={onClearSelection}>
      <rect x={-extent} y={-extent} width={2 * extent} height={2 * extent} fill="#ffffff" />
      {layout.edges.map((edge, idx) => {
        const source = nodeById.get(edge.source);
        const target = nodeById.get(edge.target);
        if (!source || !target) return null;
        const highlighted = isHighlightedEdge(edge, selectedNodeId);
        const faded = Boolean(selectedNodeId) && !highlighted;
        const radius = Math.max(0.70, Math.min(source.ring_radius, target.ring_radius) - 0.05);
        return (
          <path
            key={`${edge.source}-${edge.target}-${idx}`}
            d={linkPath(source.theta, target.theta, radius)}
            fill="none"
            stroke={edge.color || "#9ca3af"}
            strokeWidth={highlighted ? 0.011 : 0.003}
            strokeLinecap="round"
            opacity={highlighted ? 0.95 : faded ? 0.08 : 0.80}
          />
        );
      })}
      {layout.nodes.map(node => {
        const nodeSelected = selectedNodeId === node.id;
        const nodeConnected = connectedNodeIds.has(node.id);
        const nodeFaded = Boolean(selectedNodeId) && !nodeSelected && !nodeConnected;
        const radius = node.node_radius * 1.55;
        return (
        <circle
          key={node.id}
          cx={node.x}
          cy={-node.y}
          r={radius}
          fill={node.module_color || (node.type === "metabolite" ? "#c9ad85" : "#9ca3af")}
          stroke={nodeSelected ? "#f59e0b" : "#ffffff"}
          strokeWidth={nodeSelected ? 0.014 : 0.009}
          opacity={nodeFaded ? 0.22 : 1}
          style={{ cursor: "pointer" }}
          onClick={event => {
            event.stopPropagation();
            onSelectNode(node.id);
          }}
          onMouseMove={event => onTooltip(relativeTooltip(event, cnetTooltip(node)))}
          onMouseLeave={() => onTooltip(null)}
        />
        );
      })}
      <CnetLegend items={layout.legend} x={extent - 0.54} y={-extent + 0.12} />
    </ZoomableSvg>
  );
}

function AnnularSegment({
  thetaStart,
  thetaEnd,
  rInner,
  rOuter,
  fill,
  stroke = "#ffffff",
  strokeWidth = 0.0035,
  opacity = 1,
}: {
  thetaStart: number;
  thetaEnd: number;
  rInner: number;
  rOuter: number;
  fill: string;
  stroke?: string;
  strokeWidth?: number;
  opacity?: number;
}) {
  if (rOuter <= rInner) return null;
  return <path d={annularPath(thetaStart, thetaEnd, rInner, rOuter)} fill={fill} stroke={stroke} strokeWidth={strokeWidth} opacity={opacity} />;
}

function ZoomableSvg({
  baseViewBox,
  children,
  onMouseLeave,
  onCanvasClick,
}: {
  baseViewBox: SvgViewBox;
  children: React.ReactNode;
  onMouseLeave?: () => void;
  onCanvasClick?: () => void;
}) {
  const [viewBox, setViewBox] = useState(baseViewBox);
  const [dragStart, setDragStart] = useState<{ x: number; y: number; viewBox: SvgViewBox } | null>(null);
  const [dragged, setDragged] = useState(false);

  useEffect(() => {
    setViewBox(baseViewBox);
    setDragStart(null);
  }, [baseViewBox.minX, baseViewBox.minY, baseViewBox.width, baseViewBox.height]);

  function pointFromEvent(event: React.MouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    return {
      x: viewBox.minX + ((event.clientX - rect.left) / Math.max(rect.width, 1)) * viewBox.width,
      y: viewBox.minY + ((event.clientY - rect.top) / Math.max(rect.height, 1)) * viewBox.height,
    };
  }

  return (
    <svg
      width="100%"
      height="100%"
      viewBox={`${viewBox.minX} ${viewBox.minY} ${viewBox.width} ${viewBox.height}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ maxWidth: "100%", maxHeight: "100%", cursor: dragStart ? "grabbing" : "grab", userSelect: "none" }}
      onWheel={event => {
        event.preventDefault();
        const pointer = pointFromEvent(event);
        const factor = event.deltaY > 0 ? 1.12 : 0.88;
        const nextWidth = clamp(viewBox.width * factor, baseViewBox.width * 0.25, baseViewBox.width * 4);
        const nextHeight = clamp(viewBox.height * factor, baseViewBox.height * 0.25, baseViewBox.height * 4);
        const rx = (pointer.x - viewBox.minX) / viewBox.width;
        const ry = (pointer.y - viewBox.minY) / viewBox.height;
        setViewBox({
          minX: pointer.x - rx * nextWidth,
          minY: pointer.y - ry * nextHeight,
          width: nextWidth,
          height: nextHeight,
        });
      }}
      onMouseDown={event => {
        if (event.button !== 0) return;
        setDragStart({ x: event.clientX, y: event.clientY, viewBox });
        setDragged(false);
      }}
      onMouseMove={event => {
        if (!dragStart) return;
        if (Math.abs(event.clientX - dragStart.x) > 2 || Math.abs(event.clientY - dragStart.y) > 2) {
          setDragged(true);
        }
        const rect = event.currentTarget.getBoundingClientRect();
        const dx = ((event.clientX - dragStart.x) / Math.max(rect.width, 1)) * dragStart.viewBox.width;
        const dy = ((event.clientY - dragStart.y) / Math.max(rect.height, 1)) * dragStart.viewBox.height;
        setViewBox({
          ...dragStart.viewBox,
          minX: dragStart.viewBox.minX - dx,
          minY: dragStart.viewBox.minY - dy,
        });
      }}
      onMouseUp={() => setDragStart(null)}
      onMouseLeave={() => {
        setDragStart(null);
        onMouseLeave?.();
      }}
      onClick={() => {
        if (!dragged) onCanvasClick?.();
        setDragged(false);
      }}
    >
      {children}
    </svg>
  );
}

function GroupMeanTrack({ node, layout }: { node: CircosNode; layout: CircosLayout }) {
  const rInner = layout.radii.track_meanbar_inner;
  const rOuter = layout.radii.track_meanbar_outer;
  const rMid = 0.5 * (rInner + rOuter);
  const scale = Math.max(layout.scales.track_abs || 1, 1e-6);
  const thetaMid = 0.5 * (node.theta_start + node.theta_end);
  const thetaWidth = node.theta_end - node.theta_start;
  const values = (node.track_values || []).filter(value => Number.isFinite(value));
  const groupOrder = layout.group1_order || [];
  const colors = layout.group1_color_map || {};

  return (
    <>
      <AnnularSegment thetaStart={node.theta_start} thetaEnd={node.theta_end} rInner={rInner} rOuter={rOuter} fill="#fbfbfb" stroke="#eef2f7" strokeWidth={0.0014} />
      <path d={arcPath(node.theta_start, node.theta_end, rMid)} fill="none" stroke="#d1d5db" strokeWidth={0.0018} opacity={0.9} />
      {values.map((value, idx) => {
        const offset = values.length > 1 ? (idx - (values.length - 1) / 2) * thetaWidth * 0.025 : 0;
        const clipped = clamp(value, -scale, scale);
        const radius = rMid + (clipped / scale) * 0.42 * (rOuter - rInner);
        const point = polar(thetaMid + offset, radius);
        const groupName = groupOrder[idx] || "";
        return (
          <circle
            key={`${node.id}-${idx}`}
            cx={point.x}
            cy={point.y}
            r={0.005}
            fill={colors[groupName] || "#6b7280"}
            opacity={0.92}
          />
        );
      })}
    </>
  );
}

function TrackNumberLabels({ radii, labelTheta }: { radii: Record<string, number>; labelTheta: number }) {
  const trackRadii = [
    0.5 * (radii.outer_strip_inner + radii.outer_strip_outer),
    0.5 * (radii.track_meanbar_inner + radii.track_meanbar_outer),
    0.5 * (radii.track_meanheat_inner + radii.track_meanheat_outer),
    0.5 * (radii.track_degree_inner + radii.track_degree_outer),
    0.5 * (radii.track_core_inner + radii.track_core_outer),
    0.5 * (radii.track_bias_inner + radii.track_bias_outer),
  ];
  return (
    <>
      {trackRadii.map((radius, idx) => {
        const point = polar(labelTheta, radius);
        return (
          <text
            key={idx}
            x={point.x}
            y={point.y}
            fontSize={0.032}
            fontWeight={700}
            fill="#374151"
            textAnchor="middle"
            dominantBaseline="middle"
          >
            {idx + 1}
          </text>
        );
      })}
    </>
  );
}

function TrackLegend({ items }: { items: Array<{ label: string; description: string }> }) {
  return (
    <g transform="translate(-1.48,-0.98)">
      <text fontSize={0.035} fontWeight={700} fill="#111827">Track annotations</text>
      {items.map((item, idx) => (
        <g key={item.label} transform={`translate(0,${(idx + 1) * 0.072})`}>
          <text fontSize={0.031} fontWeight={700} fill="#374151">{item.label}</text>
          <text x={0.18} fontSize={0.031} fill="#6b7280">{item.description}</text>
        </g>
      ))}
    </g>
  );
}

function DiscreteLegend({
  title,
  items,
  x,
  y,
  shape,
}: {
  title: string;
  items: Array<{ label: string; color: string }>;
  x: number;
  y: number;
  shape: "circle" | "rect";
}) {
  if (!items.length) return null;
  return (
    <g transform={`translate(${x},${y})`}>
      <text fontSize={0.035} fontWeight={700} fill="#111827">{title}</text>
      {items.slice(0, 12).map((item, idx) => (
        <g key={`${title}-${item.label}`} transform={`translate(0,${(idx + 1) * 0.072})`}>
          {shape === "circle"
            ? <circle cx={0.013} cy={0} r={0.013} fill={item.color} stroke="#9ca3af" strokeWidth={0.003} />
            : <rect x={0} y={-0.013} width={0.11} height={0.026} fill={item.color} stroke="#9ca3af" strokeWidth={0.003} />}
          <text x={shape === "circle" ? 0.056 : 0.14} y={0.006} fontSize={0.031} fill="#374151">{item.label}</text>
        </g>
      ))}
    </g>
  );
}

function CnetLegend({ items, x, y }: { items: Array<{ label: string; color: string }>; x: number; y: number }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x={-0.04} y={-0.05} width={0.48} height={0.24} fill="#ffffff" stroke="none" opacity={0.88} />
      {items.map((item, idx) => (
        <g key={item.label} transform={`translate(0,${idx * 0.07})`}>
          {idx < 2
            ? <circle cx={0.02} cy={0} r={0.022} fill={item.color} stroke="#ffffff" strokeWidth={0.006} />
            : <line x1={0} x2={0.05} y1={0} y2={0} stroke={item.color} strokeWidth={0.007} />}
          <text x={0.07} y={0.008} fontSize={0.035} fill="#374151">{item.label}</text>
        </g>
      ))}
    </g>
  );
}

function annularPath(thetaStart: number, thetaEnd: number, rInner: number, rOuter: number) {
  const largeArc = Math.abs(thetaEnd - thetaStart) > Math.PI ? 1 : 0;
  const p1 = polar(thetaStart, rInner);
  const p2 = polar(thetaStart, rOuter);
  const p3 = polar(thetaEnd, rOuter);
  const p4 = polar(thetaEnd, rInner);
  return [
    `M ${p1.x} ${p1.y}`,
    `L ${p2.x} ${p2.y}`,
    `A ${rOuter} ${rOuter} 0 ${largeArc} 0 ${p3.x} ${p3.y}`,
    `L ${p4.x} ${p4.y}`,
    `A ${rInner} ${rInner} 0 ${largeArc} 1 ${p1.x} ${p1.y}`,
    "Z",
  ].join(" ");
}

function arcPath(thetaStart: number, thetaEnd: number, radius: number) {
  const largeArc = Math.abs(thetaEnd - thetaStart) > Math.PI ? 1 : 0;
  const p1 = polar(thetaStart, radius);
  const p2 = polar(thetaEnd, radius);
  return `M ${p1.x} ${p1.y} A ${radius} ${radius} 0 ${largeArc} 0 ${p2.x} ${p2.y}`;
}

function linkPath(thetaStart: number, thetaEnd: number, radius: number) {
  const p1 = polar(thetaStart, radius);
  const p2 = polar(thetaEnd, radius);
  return `M ${p1.x} ${p1.y} C ${p1.x * 0.18} ${p1.y * 0.18}, ${p2.x * 0.18} ${p2.y * 0.18}, ${p2.x} ${p2.y}`;
}

function polar(theta: number, radius: number) {
  return { x: radius * Math.cos(theta), y: -radius * Math.sin(theta) };
}

function scaledOuter(inner: number, outer: number, value: number, scale: number) {
  return inner + (outer - inner) * Math.min(1, Math.max(0, value / Math.max(scale, 1e-6)));
}

function divergingColor(value: number, scale: number) {
  const t = clamp((value / Math.max(scale, 1e-6) + 1) / 2, 0, 1);
  if (t < 0.5) {
    const k = t / 0.5;
    return mixColor([33, 102, 172], [247, 247, 247], k);
  }
  return mixColor([247, 247, 247], [178, 24, 43], (t - 0.5) / 0.5);
}

function mixColor(a: [number, number, number], b: [number, number, number], t: number) {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function isHighlightedEdge(edge: NetworkEdge, selectedNodeId: string) {
  return Boolean(selectedNodeId) && (edge.source === selectedNodeId || edge.target === selectedNodeId);
}

function connectedNodes(edges: NetworkEdge[], selectedNodeId: string) {
  const ids = new Set<string>();
  if (!selectedNodeId) return ids;
  ids.add(selectedNodeId);
  edges.forEach(edge => {
    if (edge.source === selectedNodeId) ids.add(edge.target);
    if (edge.target === selectedNodeId) ids.add(edge.source);
  });
  return ids;
}

function outerGapTheta(layout: CircosLayout) {
  const nodeById = new Map(layout.nodes.map(node => [node.id, node]));
  const firstGeneId = layout.gene_nodes?.[0] || layout.nodes.find(node => node.type === "gene")?.id || "";
  const metaboliteNodes = layout.metabolite_nodes || layout.nodes.filter(node => node.type === "metabolite").map(node => node.id);
  const lastMetaboliteId = metaboliteNodes[metaboliteNodes.length - 1] || "";
  const firstGene = nodeById.get(firstGeneId);
  const lastMetabolite = nodeById.get(lastMetaboliteId);
  if (!firstGene || !lastMetabolite) return Math.PI / 2;

  const gapStart = lastMetabolite.theta_end;
  const gapEnd = firstGene.theta_start + 2 * Math.PI;
  return ((0.5 * (gapStart + gapEnd)) % (2 * Math.PI));
}

function relativeTooltip(event: React.MouseEvent, text: string): TooltipState {
  const target = event.currentTarget as SVGElement;
  const rect = target.ownerSVGElement?.getBoundingClientRect() || target.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top, text };
}

function nodeTooltip(node: CircosNode) {
  const positiveEdges = Number(node.positive_edges || 0);
  const negativeEdges = Number(node.negative_edges || 0);
  return [
    `${node.type === "gene" ? "Gene" : "Metabolite"}: ${node.name}`,
    `Degree: ${positiveEdges + negativeEdges || "NA"}`,
    `Weighted degree: ${formatNumber(node.weighted_degree)}`,
    `Positive edges: ${positiveEdges}`,
    `Negative edges: ${negativeEdges}`,
    node.type === "gene" ? `Module: ${node.module || "NA"}` : "Module: metabolite",
    node.type === "gene" ? `kME: ${formatNumber(node.kme)}` : `Core strength: ${formatNumber(node.module_core)}`,
  ].join("\n");
}

function cnetTooltip(node: CnetNode) {
  return [
    `${node.name} (${node.type})`,
    node.type === "gene" ? `Module: ${node.module || "NA"}` : "Module: metabolite",
    `Edges: ${node.edge_count}`,
    `Weighted degree: ${formatNumber(node.weighted_degree)}`,
    node.type === "gene" ? `kME: ${formatNumber(node.kme)}` : "",
  ].filter(Boolean).join("\n");
}

function formatNumber(value: number | null | undefined) {
  return Number.isFinite(value ?? NaN) ? Number(value).toFixed(3) : "NA";
}
