import { useMemo, useRef, useState, useCallback, useEffect } from "react";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";
import { downloadSvg, downloadPng } from "../svgExport";

interface Props { jobId: string; pageId: string; }

interface UpSetData {
  sets: Array<{ name: string; size: number; color: string }>;
  intersections: Array<Record<string, unknown>>;
  n_edges: number;
}

interface TooltipState {
  x: number;
  y: number;
  content: string;
  visible: boolean;
}

const EVIDENCE_KEYS = ["In_PCC", "In_Spearman", "In_MI", "ElasticNetSelected", "XGBoostSelected"];

export function InteractiveUpSet({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Association Evidence Overlap">
      {(data, controls) => <UpSetChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

function UpSetChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [zoom, setZoom] = useState(1);
  const [tooltip, setTooltip] = useState<TooltipState>({ x: 0, y: 0, content: "", visible: false });
  const [isPanning, setIsPanning] = useState(false);
  const panOrigin = useRef({ x: 0, y: 0, scrollLeft: 0, scrollTop: 0 });

  const minZoom = 0.5;
  const maxZoom = 3;
  const zoomStep = 0.15;

  const ud = data.upset_data as UpSetData | undefined;
  const sets = ud?.sets || [];
  const intersections = ud?.intersections || [];
  const nEdges = ud?.n_edges ?? 0;

  const available = data.available_states || {};
  const state = controls.state;
  const sortBy = String(state.sort_by || data.default_state?.sort_by || "size");
  const maxIntersections = Number(state.max_intersections || data.default_state?.max_intersections || 30);

  // Sort intersections according to user selection
  const sortedIntersections = useMemo(() => {
    const arr = [...intersections];
    if (sortBy === "size") {
      arr.sort((a, b) => (Number(b.count) || 0) - (Number(a.count) || 0));
    } else if (sortBy === "degree") {
      arr.sort((a, b) => (Number(b.support) || 0) - (Number(a.support) || 0));
    } else if (sortBy === "combination") {
      arr.sort((a, b) => {
        const pa = EVIDENCE_KEYS.map(k => (a[k] ? "1" : "0")).join("");
        const pb = EVIDENCE_KEYS.map(k => (b[k] ? "1" : "0")).join("");
        return pa.localeCompare(pb);
      });
    }
    return arr.slice(0, Math.max(1, maxIntersections));
  }, [intersections, sortBy, maxIntersections]);

  const nSets = sets.length;
  const nInts = sortedIntersections.length;

  // Layout constants — generous spacing to avoid text overlap at any data size
  const margin = { top: 16, right: 24, bottom: 16, left: 16 };
  const gapX = 28;
  const gapY = 28;
  const titleH = 26;
  const leftW = 190;
  const rightW = Math.max(460, 28 * nInts);
  const topInnerH = 220;
  const bottomInnerH = Math.max(150, 32 * nSets);
  const totalW = margin.left + leftW + gapX + rightW + margin.right;
  const totalH = margin.top + titleH + topInnerH + gapY + bottomInnerH + margin.bottom;

  // Bar chart metrics (inside top-right inner box)
  const barPad = { top: 12, right: 16, bottom: 8, left: 52 };
  const barPlotW = rightW - barPad.left - barPad.right;
  const barPlotH = topInnerH - barPad.top - barPad.bottom;
  const barColW = barPlotW / Math.max(1, nInts);
  const counts = sortedIntersections.map(d => Number(d.count) || 0);
  const maxCount = Math.max(1, ...counts);

  // Set size bar metrics (inside bottom-left inner box)
  const setPad = { top: 24, right: 24, bottom: 8, left: 10 };
  const setPlotW = leftW - setPad.left - setPad.right;
  const setPlotH = bottomInnerH - setPad.top - setPad.bottom;
  const setRowH = setPlotH / Math.max(1, nSets);
  const maxSetSize = Math.max(1, ...sets.map(s => s.size));

  // Matrix metrics (inside bottom-right inner box)
  const matPad = { top: 24, right: 16, bottom: 8, left: 56 };
  const matPlotW = rightW - matPad.left - matPad.right;
  const matPlotH = bottomInnerH - matPad.top - matPad.bottom;
  const matColW = matPlotW / Math.max(1, nInts);
  const matRowH = matPlotH / Math.max(1, nSets);

  // Coordinate origins for the 4 quadrants
  const summaryX = margin.left;
  const summaryY = margin.top + titleH;
  const barX = margin.left + leftW + gapX;
  const barY = margin.top + titleH;
  const setX = margin.left;
  const setY = margin.top + titleH + topInnerH + gapY;
  const matX = margin.left + leftW + gapX;
  const matY = margin.top + titleH + topInnerH + gapY;

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
    if (!isPanning) return;
    const el = containerRef.current;
    if (!el) return;
    const dx = e.clientX - panOrigin.current.x;
    const dy = e.clientY - panOrigin.current.y;
    el.scrollLeft = panOrigin.current.scrollLeft - dx;
    el.scrollTop = panOrigin.current.scrollTop - dy;
  }, [isPanning]);

  const handleMouseUp = useCallback(() => setIsPanning(false), []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const preventScroll = (e: WheelEvent) => e.preventDefault();
    el.addEventListener("wheel", preventScroll, { passive: false });
    return () => el.removeEventListener("wheel", preventScroll);
  }, []);

  const showTooltip = useCallback((e: React.MouseEvent, content: string) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setTooltip({
      x: e.clientX - rect.left + 12,
      y: e.clientY - rect.top - 10,
      content,
      visible: true,
    });
  }, []);

  const hideTooltip = useCallback(() => {
    setTooltip(prev => ({ ...prev, visible: false }));
  }, []);

  const upsetFilename = (data.title || data.figure_id || "upset").replace(/\s+/g, "_");
  const { setDownloadHandlers } = controls;
  useEffect(() => {
    setDownloadHandlers(
      () => { if (svgRef.current) downloadPng(svgRef.current, zoom, upsetFilename); },
      () => { if (svgRef.current) downloadSvg(svgRef.current, zoom, upsetFilename); },
    );
    return () => setDownloadHandlers(null, null);
  }, [setDownloadHandlers, upsetFilename, zoom]);

  return (
    <>
      <div className="ip-chart" style={{ minHeight: `${Math.min(640, totalH + 40)}px` }}>
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
          <div style={{ width: totalW * zoom, height: totalH * zoom, position: "relative" }}>
            <svg
              ref={svgRef}
              width={totalW}
              height={totalH}
              viewBox={`0 0 ${totalW} ${totalH}`}
              style={{
                display: "block",
                width: "100%",
                height: "100%",
              }}
            >
            {/* Top title */}
            <text
              x={barX + rightW / 2}
              y={margin.top + 18}
              textAnchor="middle"
              fontSize="14"
              fontWeight="600"
              fill="#111827"
            >
              Association Evidence Overlap
            </text>

            {/* Background stripes for matrix + set-size rows */}
            {Array.from({ length: nSets }, (_, ri) => (
              <g key={`stripe-${ri}`}>
                <rect
                  x={matX + matPad.left}
                  y={matY + matPad.top + ri * matRowH}
                  width={matPlotW}
                  height={matRowH}
                  fill={ri % 2 === 0 ? "#f8fafc" : "transparent"}
                />
                <rect
                  x={setX + setPad.left}
                  y={setY + setPad.top + ri * setRowH}
                  width={setPlotW}
                  height={setRowH}
                  fill={ri % 2 === 0 ? "#f8fafc" : "transparent"}
                />
              </g>
            ))}

            {/* Bar Chart (top-right) */}
            <g transform={`translate(${barX + barPad.left}, ${barY + barPad.top})`}>
              <text
                x={18}
                y={barPlotH / 2}
                textAnchor="middle"
                fontSize="11"
                fill="#4b5563"
                transform={`rotate(-90, 18, ${barPlotH / 2})`}
              >
                Candidate edges
              </text>
              {counts.map((count, ci) => {
                const barH = (count / maxCount) * barPlotH;
                const x = ci * barColW + barColW * 0.14;
                const w = barColW * 0.72;
                const labelY = barPlotH - barH - 5;
                const hasRoom = labelY > 14;
                return (
                  <g key={`bar-${ci}`}>
                    <rect
                      x={x} y={barPlotH - barH} width={w} height={barH}
                      fill="#374151"
                      style={{ cursor: "pointer" }}
                      onMouseEnter={(e) => {
                        const pattern = EVIDENCE_KEYS.filter(k => sortedIntersections[ci][k]).map((k, i) => sets[i]?.name || k).join(" ∩ ");
                        showTooltip(e, `Count: ${count}\nPattern: ${pattern || "∅"}`);
                      }}
                      onMouseMove={(e) => {
                        const pattern = EVIDENCE_KEYS.filter(k => sortedIntersections[ci][k]).map((k, i) => sets[i]?.name || k).join(" ∩ ");
                        showTooltip(e, `Count: ${count}\nPattern: ${pattern || "∅"}`);
                      }}
                      onMouseLeave={hideTooltip}
                    />
                    {hasRoom && (nInts <= 40 || ci % 2 === 0) && (
                      <text x={x + w / 2} y={labelY} textAnchor="middle" fontSize="8" fill="#374151">
                        {count}
                      </text>
                    )}
                  </g>
                );
              })}
              {/* Y grid lines + ticks */}
              {Array.from({ length: 5 }, (_, i) => {
                const y = barPlotH * (1 - i / 4);
                return (
                  <g key={`grid-${i}`}>
                    <line x1={0} y1={y} x2={barPlotW} y2={y} stroke="#e5e7eb" strokeWidth={0.8} />
                    <text x={-4} y={y + 3} textAnchor="end" fontSize="9" fill="#6b7280">
                      {Math.round(maxCount * (i / 4))}
                    </text>
                  </g>
                );
              })}
            </g>

            {/* Set Size Bar (bottom-left) */}
            <g transform={`translate(${setX + setPad.left}, ${setY + setPad.top})`}>
              <text x={setPlotW / 2} y={-8} textAnchor="middle" fontSize="11" fontWeight="500" fill="#374151">
                Set size
              </text>
              {sets.map((s, ri) => {
                const barW = (s.size / maxSetSize) * setPlotW;
                const y = ri * setRowH + setRowH * 0.19;
                const h = setRowH * 0.62;
                return (
                  <g key={`set-${ri}`}>
                    <rect
                      x={0} y={y} width={barW} height={h}
                      fill={s.color}
                      style={{ cursor: "pointer" }}
                      onMouseEnter={(e) => showTooltip(e, `${s.name}: ${s.size.toLocaleString()} edges`)}
                      onMouseMove={(e) => showTooltip(e, `${s.name}: ${s.size.toLocaleString()} edges`)}
                      onMouseLeave={hideTooltip}
                    />
                    <text x={barW + 6} y={y + h * 0.72} fontSize="9" fill="#374151">
                      {s.size.toLocaleString()}
                    </text>
                    <text x={-4} y={y + h * 0.72} textAnchor="end" fontSize="10" fill="#111827">
                      {s.name}
                    </text>
                  </g>
                );
              })}
            </g>

            {/* Matrix (bottom-right) */}
            <g transform={`translate(${matX + matPad.left}, ${matY + matPad.top})`}>
              <text x={matPlotW / 2} y={-8} textAnchor="middle" fontSize="11" fontWeight="500" fill="#374151">
                Top {nInts} evidence intersections
              </text>
              {/* Connecting lines */}
              {sortedIntersections.map((row, ci) => {
                const activeRows = EVIDENCE_KEYS.map((k, ri) => row[k] ? ri : -1).filter(ri => ri >= 0);
                if (activeRows.length < 2) return null;
                const x = ci * matColW + matColW / 2;
                const y1 = activeRows[0] * matRowH + matRowH / 2;
                const y2 = activeRows[activeRows.length - 1] * matRowH + matRowH / 2;
                return (
                  <line
                    key={`line-${ci}`}
                    x1={x} y1={y1} x2={x} y2={y2}
                    stroke="#111827"
                    strokeWidth={1.1}
                    strokeLinecap="round"
                  />
                );
              })}
              {/* Dots */}
              {sortedIntersections.map((row, ci) =>
                EVIDENCE_KEYS.map((k, ri) => {
                  const active = !!row[k];
                  const x = ci * matColW + matColW / 2;
                  const y = ri * matRowH + matRowH / 2;
                  return (
                    <circle
                      key={`dot-${ci}-${ri}`}
                      cx={x} cy={y} r={active ? 5.5 : 4}
                      fill={active ? sets[ri]?.color || "#4c78a8" : "#d1d5db"}
                      stroke={active ? "#111827" : "none"}
                      strokeWidth={active ? 0.35 : 0}
                      style={{ cursor: "pointer", transition: "r 0.12s" }}
                      onMouseEnter={(e) => {
                        const count = Number(row.count) || 0;
                        const name = sets[ri]?.name || k;
                        showTooltip(e, `${name}: ${count} edges`);
                      }}
                      onMouseMove={(e) => {
                        const count = Number(row.count) || 0;
                        const name = sets[ri]?.name || k;
                        showTooltip(e, `${name}: ${count} edges`);
                      }}
                      onMouseLeave={hideTooltip}
                    />
                  );
                })
              )}
              {/* Y tick labels for matrix — placed inside matrix left padding */}
              {sets.map((s, ri) => (
                <text
                  key={`mat-label-${ri}`}
                  x={-10}
                  y={ri * matRowH + matRowH / 2 + 4}
                  textAnchor="end"
                  fontSize="10"
                  fill="#111827"
                >
                  {s.name}
                </text>
              ))}
            </g>

            {/* Summary (top-left) */}
            <g transform={`translate(${summaryX + 16}, ${summaryY + 20})`}>
              <text x={0} y={0} fontSize="11" fontWeight="600" fill="#111827">Evidence-positive edges: {nEdges.toLocaleString()}</text>
              <text x={0} y={20} fontSize="11" fill="#4b5563">Displayed intersections: {nInts}</text>
              <text x={0} y={40} fontSize="11" fill="#4b5563">Unit: metabolite-gene edge</text>
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
          <span>Intersections: {nInts}</span>
          <span>Sets: {nSets}</span>
          <span>Edges: {nEdges.toLocaleString()}</span>
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
        <div className="ip-control-group">
          <label className="ip-control-label">Sort by</label>
          <select className="ip-control-select" value={sortBy}
            onChange={e => controls.setState("sort_by", e.target.value)}>
            <option value="size">Size (count desc)</option>
            <option value="degree">Degree (support desc)</option>
            <option value="combination">Combination (pattern asc)</option>
          </select>
        </div>
        <div className="ip-control-group">
          <label className="ip-control-label">Max intersections</label>
          <select className="ip-control-select" value={String(maxIntersections)}
            onChange={e => controls.setState("max_intersections", Number(e.target.value))}>
            {[10, 20, 30, 40, 50].map(v => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
      </div>
    </>
  );
}
