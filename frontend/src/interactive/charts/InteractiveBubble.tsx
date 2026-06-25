import { useMemo, useRef, useState, useCallback, useEffect } from "react";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";
import { downloadSvg, downloadPng } from "../svgExport";

interface Props { jobId: string; pageId: string; }

interface BubbleRow {
  gene: string;
  metabolite: string;
  spearman_rho: number;
  edge_weight: number;
  module: string;
  module_color: string;
}

interface TooltipState {
  x: number;
  y: number;
  content: string;
  visible: boolean;
}

export function InteractiveBubble({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Bubble Heatmap">
      {(data, controls) => <BubbleChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function BubbleChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [zoom, setZoom] = useState(1);
  const [tooltip, setTooltip] = useState<TooltipState>({ x: 0, y: 0, content: "", visible: false });
  const [isPanning, setIsPanning] = useState(false);
  const panOrigin = useRef({ x: 0, y: 0, scrollLeft: 0, scrollTop: 0 });

  const minZoom = 0.5;
  const maxZoom = 3;
  const zoomStep = 0.15;

  const available = data.available_states || {};
  const state = controls.state;
  const level = String(state.level || data.default_state?.level || "gene");
  const minRho = Number(state.min_abs_rho ?? 0);

  const isGeneLevel = level === "gene";

  // Resolve effective plotly_spec from alt_data when level differs from primary
  const effectiveSpec = useMemo(() => {
    const primary = data.plotly_spec || {};
    const primaryLevel = String(data.default_state?.level || "");
    if (level === primaryLevel || !data.alt_data) return primary;
    for (const alt of Object.values(data.alt_data)) {
      const altState = (alt as Record<string, unknown>)?.default_state as Record<string, unknown>;
      if (String(altState?.level || "") === level) {
        return {
          ...primary,
          ...((alt as Record<string, unknown>)?.plotly_spec || {}),
        };
      }
    }
    return primary;
  }, [data, level]);

  const allRows = (effectiveSpec.data || []) as BubbleRow[];
  const xOrder = (effectiveSpec.x_order || []) as string[];
  const yOrder = (effectiveSpec.y_order || []) as string[];
  const yModules = (effectiveSpec.y_modules || []) as string[];
  const yColors = (effectiveSpec.y_colors || []) as string[];
  const yLabel = String(effectiveSpec.y_label || "Gene");
  const sizeLabel = String(effectiveSpec.size_label || "EdgeWeight");

  const referenceModuleOrder = useMemo(() => {
    const ordered: string[] = [];
    const add = (moduleName: unknown) => {
      const label = String(moduleName || "").trim();
      if (label && !ordered.includes(label)) ordered.push(label);
    };
    yModules.forEach(add);
    if (ordered.length === 0 && data.alt_data) {
      for (const alt of Object.values(data.alt_data)) {
        const altState = (alt as Record<string, unknown>)?.default_state as Record<string, unknown>;
        if (String(altState?.level || "") !== "module") continue;
        const altSpec = ((alt as Record<string, unknown>)?.plotly_spec || {}) as Record<string, unknown>;
        ((altSpec.y_order || []) as unknown[]).forEach(add);
      }
    }
    allRows.forEach(row => add(row.module));
    return ordered;
  }, [yModules, data.alt_data, allRows]);

  // Filter by rho threshold
  const rows = useMemo(() => {
    return minRho > 0
      ? allRows.filter(r => Math.abs(Number(r.spearman_rho) || 0) >= minRho)
      : allRows;
  }, [allRows, minRho]);

  const staticXOrder = useMemo(() => {
    const stats = new Map<string, { bestEdge: number; edgeCount: Set<string>; maxAbsRho: number; originalRank: number }>();
    xOrder.forEach((metabolite, i) => {
      stats.set(metabolite, { bestEdge: Number.NEGATIVE_INFINITY, edgeCount: new Set(), maxAbsRho: 0, originalRank: i });
    });
    allRows.forEach((row, i) => {
      const metabolite = row.metabolite;
      if (!metabolite) return;
      const current = stats.get(metabolite) || {
        bestEdge: Number.NEGATIVE_INFINITY,
        edgeCount: new Set<string>(),
        maxAbsRho: 0,
        originalRank: xOrder.length + i,
      };
      current.bestEdge = Math.max(current.bestEdge, Number(row.edge_weight) || 0);
      current.edgeCount.add(row.gene);
      current.maxAbsRho = Math.max(current.maxAbsRho, Math.abs(Number(row.spearman_rho) || 0));
      stats.set(metabolite, current);
    });
    return Array.from(stats.entries())
      .sort((a, b) => {
        const sa = a[1];
        const sb = b[1];
        if (isGeneLevel) {
          const edgeCountDiff = sb.edgeCount.size - sa.edgeCount.size;
          if (edgeCountDiff !== 0) return edgeCountDiff;
          const bestDiff = sb.bestEdge - sa.bestEdge;
          if (bestDiff !== 0) return bestDiff;
        } else {
          const rhoDiff = sb.maxAbsRho - sa.maxAbsRho;
          if (rhoDiff !== 0) return rhoDiff;
        }
        return sa.originalRank - sb.originalRank;
      })
      .map(([metabolite]) => metabolite);
  }, [allRows, xOrder, isGeneLevel]);

  // Build ordered axis labels
  const xLabels = useMemo(() => {
    const present = new Set(rows.map(r => r.metabolite));
    return staticXOrder.filter(m => present.has(m));
  }, [rows, staticXOrder]);

  const rowMeta = useMemo(() => {
    const moduleByLabel = new Map<string, string>();
    const colorByLabel = new Map<string, string>();
    yOrder.forEach((label, i) => {
      if (yModules[i]) moduleByLabel.set(label, yModules[i]);
      if (yColors[i]) colorByLabel.set(label, yColors[i]);
    });
    for (const row of allRows) {
      if (!moduleByLabel.has(row.gene)) moduleByLabel.set(row.gene, row.module || "Unassigned");
      if (!colorByLabel.has(row.gene)) colorByLabel.set(row.gene, row.module_color || "#d1d5db");
    }
    return { moduleByLabel, colorByLabel };
  }, [yOrder, yModules, yColors, allRows]);

  const yLabels = useMemo(() => {
    const present = new Set(rows.map(r => r.gene));
    const base = yOrder.filter(g => present.has(g));
    if (!isGeneLevel) return base;

    const originalRank = new Map<string, number>();
    base.forEach((label, i) => originalRank.set(label, i));
    const moduleRank = new Map<string, number>();
    referenceModuleOrder.forEach((moduleName, i) => moduleRank.set(moduleName, i));

    return [...base].sort((a, b) => {
      const moduleA = rowMeta.moduleByLabel.get(a) || "Unassigned";
      const moduleB = rowMeta.moduleByLabel.get(b) || "Unassigned";
      const rankA = moduleRank.get(moduleA) ?? referenceModuleOrder.length;
      const rankB = moduleRank.get(moduleB) ?? referenceModuleOrder.length;
      if (rankA !== rankB) return rankA - rankB;
      if (moduleA !== moduleB) return moduleA.localeCompare(moduleB);
      return (originalRank.get(a) ?? 0) - (originalRank.get(b) ?? 0);
    });
  }, [rows, yOrder, isGeneLevel, referenceModuleOrder, rowMeta]);

  const xPos = useMemo(() => {
    const map = new Map<string, number>();
    xLabels.forEach((m, i) => map.set(m, i));
    return map;
  }, [xLabels]);

  const yPos = useMemo(() => {
    const map = new Map<string, number>();
    yLabels.forEach((g, i) => map.set(g, i));
    return map;
  }, [yLabels]);

  // Compute contiguous module spans for gene-level view.
  const moduleSpans = useMemo(() => {
    if (!isGeneLevel || yLabels.length === 0) {
      return [] as Array<{ start: number; end: number; module: string; color: string }>;
    }
    const spans: Array<{ start: number; end: number; module: string; color: string }> = [];
    let start = 0;
    let prevModule = "";
    yLabels.forEach((gene, i) => {
      const mod = rowMeta.moduleByLabel.get(gene) || "Unassigned";
      if (i > 0 && mod !== prevModule) {
        const prevGene = yLabels[i - 1];
        spans.push({
          start,
          end: i,
          module: prevModule,
          color: rowMeta.colorByLabel.get(prevGene) || "#d1d5db",
        });
        start = i;
      }
      prevModule = mod;
    });
    const lastGene = yLabels[yLabels.length - 1];
    spans.push({
      start,
      end: yLabels.length,
      module: prevModule || "Unassigned",
      color: rowMeta.colorByLabel.get(lastGene) || "#d1d5db",
    });
    return spans;
  }, [isGeneLevel, yLabels, rowMeta]);

  const moduleBoundaries = useMemo(() => moduleSpans.slice(1).map(span => span.start), [moduleSpans]);

  // Color scale: RdBu_r (reversed Red-Blue)
  const colorForRho = useCallback((rho: number) => {
    const t = Math.max(-1, Math.min(1, rho));
    if (t < -0.5) {
      const k = (t + 1) / 0.5;
      return interpolateColor([33, 102, 172], [92, 151, 201], k);
    } else if (t < 0) {
      const k = (t + 0.5) / 0.5;
      return interpolateColor([92, 151, 201], [247, 247, 247], k);
    } else if (t < 0.5) {
      const k = t / 0.5;
      return interpolateColor([247, 247, 247], [239, 138, 98], k);
    } else {
      const k = (t - 0.5) / 0.5;
      return interpolateColor([239, 138, 98], [178, 24, 43], k);
    }
  }, []);

  // Size mapping
  const ewValues = useMemo(() => rows.map(r => r.edge_weight).filter(v => Number.isFinite(v)), [rows]);
  const minEW = useMemo(() => (ewValues.length ? Math.min(...ewValues) : 0), [ewValues]);
  const maxEW = useMemo(() => (ewValues.length ? Math.max(...ewValues) : 1), [ewValues]);

  const radiusFor = useCallback((ew: number) => {
    if (maxEW <= minEW) return 6;
    const minR = 4.5;
    const maxR = isGeneLevel ? 10 : 9;
    return minR + ((ew - minEW) / (maxEW - minEW)) * (maxR - minR);
  }, [minEW, maxEW, isGeneLevel]);

  // Layout
  const cellW = 28;
  const cellH = 22;
  const colorStripW = isGeneLevel ? 16 : 0;
  const labelW = 130;
  const colorbarW = 56;
  const legendW = 70;
  const tickFontSize = isGeneLevel ? 12 : 10;
  const labelGap = 4;
  const bottomPad = isGeneLevel ? 250 : 250;
  const plotAxisX = labelW + colorStripW + labelGap + 18;
  const pad = { top: 50, right: colorbarW + legendW + 16, bottom: bottomPad, left: plotAxisX + cellW / 2 };
  const xCount = Math.max(1, xLabels.length);
  const yCount = Math.max(1, yLabels.length);
  const plotLeft = pad.left - cellW / 2;
  const plotRight = pad.left + (xCount - 0.5) * cellW;
  const plotTop = pad.top - cellH / 2;
  const plotBottom = pad.top + (yCount - 0.5) * cellH;
  const plotW = xCount * cellW;
  const plotH = yCount * cellH;
  const colorbarH = Math.max(48, plotH * 0.8);
  const xAxisTitleY = plotBottom + (isGeneLevel ? 220 : 220);
  const viewW = plotRight + pad.right;
  const viewH = Math.max(plotBottom + pad.bottom, xAxisTitleY + 26);
  const stripX = plotLeft - colorStripW;
  const yLabelX = isGeneLevel ? stripX - labelGap : plotLeft - 10;
  const xTickLabelY = plotBottom + (isGeneLevel ? 18 : 18);

  // Colorbar gradient stops
  const cbStops = useMemo(() => {
    const stops: { offset: string; color: string }[] = [];
    for (let i = 0; i <= 20; i++) {
      const rho = -1 + (i / 20) * 2;
      stops.push({ offset: `${(i / 20) * 100}%`, color: colorForRho(rho) });
    }
    return stops;
  }, [colorForRho]);

  // Legend sizes (3 reference circles) — fixed proportions 0.35, 0.65, 0.95
  const legendValues = useMemo(() => {
    if (maxEW <= minEW) return [{ v: minEW, r: 4.5 }];
    const vals: number[] = [];
    for (const p of [0.35, 0.65, 0.95]) {
      vals.push(minEW + (maxEW - minEW) * p);
    }
    return vals.map(v => ({ v, r: radiusFor(v) }));
  }, [minEW, maxEW, radiusFor]);

  // Zoom / Pan handlers
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
    panOrigin.current = { x: e.clientX, y: e.clientY, scrollLeft: el.scrollLeft, scrollTop: el.scrollTop };
  }, []);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning) {
      // Tooltip hit-test
      const svg = svgRef.current;
      if (!svg) return;
      const pt = svg.createSVGPoint();
      pt.x = e.clientX;
      pt.y = e.clientY;
      const cursorPt = pt.matrixTransform(svg.getScreenCTM()?.inverse());
      if (!cursorPt) return;

      let best: { row: BubbleRow; dist: number; r: number } | null = null;
      for (const row of rows) {
        const xi = xPos.get(row.metabolite);
        const yi = yPos.get(row.gene);
        if (xi == null || yi == null) continue;
        const cx = pad.left + xi * cellW;
        const cy = pad.top + yi * cellH;
        const r = radiusFor(row.edge_weight) / zoom;
        const dx = cursorPt.x - cx;
        const dy = cursorPt.y - cy;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist <= r + 2 / zoom) {
          if (!best || dist < best.dist) {
            best = { row, dist, r };
          }
        }
      }

      if (best) {
        const r = best.row;
        const rect = containerRef.current?.getBoundingClientRect();
        if (rect) {
          setTooltip({
            x: e.clientX - rect.left + 14,
            y: e.clientY - rect.top - 10,
            content:
              `${yLabel}: ${r.gene}\n` +
              `Metabolite: ${r.metabolite}\n` +
              `Spearman ρ: ${r.spearman_rho.toFixed(3)}\n` +
              `${sizeLabel}: ${r.edge_weight.toFixed(3)}` +
              (isGeneLevel ? `\nModule: ${r.module}` : ""),
            visible: true,
          });
        }
      } else {
        setTooltip(prev => ({ ...prev, visible: false }));
      }
      return;
    }
    const el = containerRef.current;
    if (!el) return;
    const dx = e.clientX - panOrigin.current.x;
    const dy = e.clientY - panOrigin.current.y;
    el.scrollLeft = panOrigin.current.scrollLeft - dx;
    el.scrollTop = panOrigin.current.scrollTop - dy;
  }, [isPanning, rows, xPos, yPos, pad.left, pad.top, cellW, cellH, radiusFor, zoom, yLabel, sizeLabel, isGeneLevel]);

  const handleMouseUp = useCallback(() => setIsPanning(false), []);
  const handleMouseLeave = useCallback(() => {
    setIsPanning(false);
    setTooltip(prev => ({ ...prev, visible: false }));
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const preventScroll = (e: WheelEvent) => e.preventDefault();
    el.addEventListener("wheel", preventScroll, { passive: false });
    return () => el.removeEventListener("wheel", preventScroll);
  }, []);

  const bubbleFilename = (data.title || data.figure_id || "bubble").replace(/\s+/g, "_");
  const { setDownloadHandlers } = controls;
  useEffect(() => {
    setDownloadHandlers(
      () => { if (svgRef.current) downloadPng(svgRef.current, zoom, bubbleFilename); },
      () => { if (svgRef.current) downloadSvg(svgRef.current, zoom, bubbleFilename); },
    );
    return () => setDownloadHandlers(null, null);
  }, [setDownloadHandlers, bubbleFilename, zoom]);

  return (
    <>
      <div className="ip-chart" style={{ minHeight: "520px" }}>
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
          onMouseLeave={handleMouseLeave}
        >
          <div
            ref={contentRef}
            style={{
              width: viewW * zoom,
              height: viewH * zoom,
              position: "relative",
            }}
          >
            <svg
              ref={svgRef}
              width={viewW}
              height={viewH}
              viewBox={`0 0 ${viewW} ${viewH}`}
              style={{
                display: "block",
                width: "100%",
                height: "100%",
              }}
            >
              <defs>
                <linearGradient id="rhoGradient" x1="0" y1="1" x2="0" y2="0">
                  {cbStops.map((s, i) => (
                    <stop key={i} offset={s.offset} stopColor={s.color} />
                  ))}
                </linearGradient>
              </defs>

              {/* Title */}
              <text x={(plotLeft + plotRight) / 2} y={28} textAnchor="middle" fontSize="14" fontWeight="600" fill="#111827">
                {isGeneLevel ? "High-Confidence Gene-Metabolite Correlation Bubble Heatmap" : "Module-Metabolite Association Bubble Plot"}
              </text>

              {/* Grid lines */}
              <g>
                {xLabels.map((_, i) => (
                  <line
                    key={`vg-${i}`}
                    x1={pad.left + i * cellW}
                    y1={plotTop}
                    x2={pad.left + i * cellW}
                    y2={plotBottom}
                    stroke="#e5e7eb"
                    strokeWidth={0.42}
                  />
                ))}
                {yLabels.map((_, i) => (
                  <line
                    key={`hg-${i}`}
                    x1={plotLeft}
                    y1={pad.top + i * cellH}
                    x2={plotRight}
                    y2={pad.top + i * cellH}
                    stroke="#e5e7eb"
                    strokeWidth={0.42}
                  />
                ))}
              </g>

              {/* Static-style axes */}
              <line x1={plotLeft} y1={plotTop} x2={plotLeft} y2={plotBottom} stroke="#111827" strokeWidth={1.2} />
              <line x1={plotLeft} y1={plotBottom} x2={plotRight} y2={plotBottom} stroke="#111827" strokeWidth={1.2} />

              {/* Module boundaries (gene level only) */}
              {isGeneLevel && moduleBoundaries.map((b, i) => (
                <line
                  key={`mod-line-${i}`}
                  x1={plotLeft}
                  y1={pad.top + (b - 0.5) * cellH}
                  x2={plotRight}
                  y2={pad.top + (b - 0.5) * cellH}
                  stroke="#9ca3af"
                  strokeWidth={0.55}
                  opacity={0.75}
                />
              ))}

              {/* Color strip (gene level only) — continuous, no gaps */}
              {isGeneLevel && moduleSpans.map((span, i) => (
                <rect
                  key={`strip-${i}-${span.module}`}
                  x={stripX}
                  y={pad.top + (span.start - 0.5) * cellH}
                  width={colorStripW}
                  height={(span.end - span.start) * cellH}
                  fill={span.color}
                />
              ))}

              {/* Y labels */}
              {yLabels.map((gene, i) => (
                <text
                  key={`ylabel-${i}`}
                  x={yLabelX}
                  y={pad.top + i * cellH}
                  textAnchor="end"
                  dominantBaseline="middle"
                  fontSize={tickFontSize}
                  fill="#111827"
                >
                  {gene}
                </text>
              ))}

              {/* Y axis title */}
              <text
                x={20}
                y={(plotTop + plotBottom) / 2}
                textAnchor="middle"
                fontSize="12"
                fill="#374151"
                fontWeight="500"
                transform={`rotate(-90, 20, ${(plotTop + plotBottom) / 2})`}
              >
                {isGeneLevel ? "High-confidence gene" : "Module"}
              </text>

              {/* X labels — full metabolite names, smaller font when many */}
              {xLabels.map((met, i) => {
                return (
                  <g
                    key={`xlabel-${i}`}
                    transform={`translate(${pad.left + i * cellW}, ${xTickLabelY}) rotate(45)`}
                  >
                    <text
                      textAnchor="start"
                      fontSize={tickFontSize}
                      fill="#111827"
                    >
                      {met}
                    </text>
                  </g>
                );
              })}

              {/* X axis title */}
              <text x={(plotLeft + plotRight) / 2} y={xAxisTitleY} textAnchor="middle" fontSize="12" fill="#374151" fontWeight="500">
                Metabolite
              </text>

              {/* Bubbles — placed on grid intersections (matches matplotlib scatter) */}
              {rows.map((r, i) => {
                const xi = xPos.get(r.metabolite);
                const yi = yPos.get(r.gene);
                if (xi == null || yi == null) return null;
                const cx = pad.left + xi * cellW;
                const cy = pad.top + yi * cellH;
                const radius = radiusFor(r.edge_weight);
                return (
                  <circle
                    key={`bubble-${i}`}
                    cx={cx}
                    cy={cy}
                    r={radius}
                    fill={colorForRho(r.spearman_rho)}
                    stroke="#111827"
                    strokeWidth={0.35}
                    opacity={0.88}
                    style={{ pointerEvents: "none" }}
                  />
                );
              })}

              {/* Colorbar — shorter height, centered vertically */}
              <g transform={`translate(${plotRight + 16}, ${plotTop + (plotH - colorbarH) / 2})`}>
                <rect x={0} y={0} width={12} height={colorbarH} fill="url(#rhoGradient)" stroke="#d1d5db" strokeWidth={0.5} />
                <text x={20} y={0} textAnchor="start" fontSize="9" fill="#6b7280">1.0</text>
                <text x={20} y={colorbarH / 2} textAnchor="start" fontSize="9" fill="#6b7280">0</text>
                <text x={20} y={colorbarH} textAnchor="start" fontSize="9" fill="#6b7280">-1.0</text>
                <text x={6} y={-10} textAnchor="middle" fontSize="10" fill="#374151">Spearman ρ</text>
              </g>

              {/* Size legend */}
              <g transform={`translate(${plotRight + colorbarW + 30}, ${Math.max(plotTop + 70, plotBottom - 110)})`}>
                <text x={0} y={-8} textAnchor="start" fontSize="10" fill="#374151" fontWeight="500">
                  {sizeLabel}
                </text>
                {legendValues.map((lv, i) => (
                  <g key={`leg-${i}`} transform={`translate(${lv.r + 2}, ${i * 34 + 16})`}>
                    <circle cx={0} cy={0} r={lv.r} fill="#9ca3af" stroke="#111827" strokeWidth={0.35} />
                    <text x={lv.r + 6} y={4} fontSize="9" fill="#6b7280">{lv.v.toFixed(2)}</text>
                  </g>
                ))}
              </g>
            </svg>
          </div>

          {/* Tooltip */}
          {tooltip.visible && (
            <div
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
                maxWidth: "260px",
                pointerEvents: "none",
                whiteSpace: "pre-line",
                lineHeight: 1.5,
              }}
            >
              {tooltip.content}
            </div>
          )}
        </div>

        <div className="ip-infobar">
          <span>Level: {isGeneLevel ? "Gene" : "Module"}</span>
          <span>Showing: {rows.length} associations</span>
          <span>Zoom: {Math.round(zoom * 100)}%</span>
          <button
            className="secondary"
            style={{ padding: "2px 8px", fontSize: "11px", cursor: "pointer" }}
            onClick={() => setZoom(1)}
            type="button"
          >
            Reset zoom
          </button>
        </div>
      </div>

      <div className="ip-controls">
        {available.level && (
          <div className="ip-control-group">
            <label className="ip-control-label">Level</label>
            <select className="ip-control-select" value={level}
              onChange={e => controls.setState("level", e.target.value)}>
              {(available.level as string[]).map(s => (
                <option key={s} value={s}>{s === "gene" ? "Gene" : "Module"}</option>
              ))}
            </select>
          </div>
        )}
        <div className="ip-control-group">
          <label className="ip-control-label">Min |ρ|: {minRho.toFixed(2)}</label>
          <div className="ip-control-range">
            <input type="range" min="0" max="1" step="0.05" value={minRho}
              onChange={e => controls.setState("min_abs_rho", parseFloat(e.target.value))} />
            <span>{minRho.toFixed(2)}</span>
          </div>
        </div>
      </div>
    </>
  );
}

function interpolateColor(a: number[], b: number[], t: number): string {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

function int(v: number): number {
  return Math.floor(v);
}
