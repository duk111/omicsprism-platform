import { useRef, useEffect, useCallback } from "react";
import { InteractivePageShell, type FigureData, type ControlsAPI } from "../InteractivePage";

interface Props { jobId: string; pageId: string; }
export function InteractiveCircos({ jobId, pageId }: Props) {
  return (
    <InteractivePageShell jobId={jobId} pageId={pageId} pageTitle="Circos Network">
      {(data, controls) => <CircosChart data={data} controls={controls} />}
    </InteractivePageShell>
  );
}

interface CircosNode {
  id: string; name: string; type: "gene" | "metabolite";
  theta_start: number; theta_end: number; theta_mid: number;
  module: string; module_color: string;
  mean_zscore: number; weighted_degree: number; kme: number; direction_bias: number;
}
interface CircosEdge { source: string; target: string; weight: number; sign: string; }

function CircosChart({ data, controls }: { data: FigureData; controls: ControlsAPI }) {
  const cd = data.circos_data as Record<string, unknown> | undefined;
  const nodes = (cd?.nodes || []) as CircosNode[];
  const edges = (cd?.edges || []) as CircosEdge[];
  const available = data.available_states || {};
  const state = controls.state;
  const svgRef = useRef<SVGSVGElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  const draw = useCallback(() => {
    if (!svgRef.current || !nodes.length) return;
    const svg = svgRef.current;
    const W = svg.clientWidth || 800;
    const H = svg.clientHeight || 800;
    const cx = W / 2, cy = H / 2;
    const R = Math.min(cx, cy) * 0.85;
    const nodeR = R * 0.12;
    const minW = Number(state.min_edge_weight || 0);
    const signF = String(state.sign_filter || "all");

    const filteredEdges = edges.filter(e =>
      e.weight >= minW && (signF === "all" || e.sign === signF)
    );

    let html = `<circle cx="${cx}" cy="${cy}" r="${R * 0.55}" fill="none" stroke="#f3f4f6" stroke-width="1"/>\n`;

    filteredEdges.forEach(e => {
      const src = nodes.find(n => n.id === e.source);
      const tgt = nodes.find(n => n.id === e.target);
      if (!src || !tgt) return;
      const x1 = cx + R * 0.55 * Math.cos(src.theta_mid);
      const y1 = cy + R * 0.55 * Math.sin(src.theta_mid);
      const x2 = cx + R * 0.55 * Math.cos(tgt.theta_mid);
      const y2 = cy + R * 0.55 * Math.sin(tgt.theta_mid);
      const color = e.sign === "positive" ? "#dc2626" : "#2563eb";
      const alpha = Math.min(0.6, 0.08 + e.weight * 0.5);
      html += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${color}" stroke-width="${(0.2 + e.weight * 1.5).toFixed(2)}" opacity="${alpha}"/>\n`;
    });

    nodes.forEach(n => {
      const innerR = R - nodeR, outerR = R;
      const x1 = cx + innerR * Math.cos(n.theta_start), y1 = cy + innerR * Math.sin(n.theta_start);
      const x2 = cx + outerR * Math.cos(n.theta_start), y2 = cy + outerR * Math.sin(n.theta_start);
      const x3 = cx + outerR * Math.cos(n.theta_end), y3 = cy + outerR * Math.sin(n.theta_end);
      const x4 = cx + innerR * Math.cos(n.theta_end), y4 = cy + innerR * Math.sin(n.theta_end);
      const color = n.module_color || (n.type === "gene" ? "#7db8ab" : "#c9ad85");
      html += `<path d="M${x1.toFixed(1)},${y1.toFixed(1)} L${x2.toFixed(1)},${y2.toFixed(1)} A${outerR},${outerR} 0 0,1 ${x3.toFixed(1)},${y3.toFixed(1)} L${x4.toFixed(1)},${y4.toFixed(1)} A${innerR},${innerR} 0 0,0 ${x1.toFixed(1)},${y1.toFixed(1)} Z" fill="${color}" stroke="#fff" stroke-width="0.5" data-id="${n.id}" class="cn" style="cursor:pointer"/>\n`;
    });

    svg.innerHTML = html;

    svg.querySelectorAll<SVGElement>(".cn").forEach(el => {
      el.addEventListener("mouseenter", ev => {
        const node = nodes.find(n => n.id === el.dataset.id);
        if (!node || !tooltipRef.current) return;
        const rect = svg.getBoundingClientRect();
        const t = tooltipRef.current;
        t.style.display = "block";
        t.style.left = `${(ev as MouseEvent).clientX - rect.left + 12}px`;
        t.style.top = `${(ev as MouseEvent).clientY - rect.top - 8}px`;
        t.textContent = `${node.name} (${node.type})\nModule: ${node.module}\n${node.type === "gene" ? `kME: ${node.kme?.toFixed(3)}` : `Degree: ${node.weighted_degree?.toFixed(2)}`}`;
      });
      el.addEventListener("mouseleave", () => {
        if (tooltipRef.current) tooltipRef.current.style.display = "none";
      });
    });
  }, [nodes, edges, state.min_edge_weight, state.sign_filter]);

  useEffect(() => { const id = requestAnimationFrame(draw); return () => cancelAnimationFrame(id); }, [draw]);
  useEffect(() => {
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [draw]);

  return (
    <>
      <div className="ip-chart">
        <div className="ip-chart-area" style={{ position: "relative" }}>
          <svg ref={svgRef} width="100%" height="100%" style={{ background: "#fff" }} />
          <div ref={tooltipRef} style={{
            display: "none", position: "absolute", background: "rgba(0,0,0,0.82)",
            color: "#fff", padding: "6px 10px", borderRadius: 6, fontSize: "0.76rem",
            whiteSpace: "pre-line", pointerEvents: "none", zIndex: 10, maxWidth: 200,
          }} />
        </div>
        <div className="ip-infobar">
          <span>Nodes: {nodes.length}</span>
          <span>Edges (total): {edges.length}</span>
        </div>
      </div>
      <div className="ip-controls">
        {available.layout && (
          <div className="ip-control-group">
            <label className="ip-control-label">Layout</label>
            <select className="ip-control-select" value={String(state.layout || "compressed")}
              onChange={e => controls.setState("layout", e.target.value)}>
              {(available.layout as string[]).map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
        {available.sign_filter && (
          <div className="ip-control-group">
            <label className="ip-control-label">Sign filter</label>
            <select className="ip-control-select" value={String(state.sign_filter || "all")}
              onChange={e => controls.setState("sign_filter", e.target.value)}>
              {(available.sign_filter as string[]).map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
        <div className="ip-control-group">
          <label className="ip-control-label">Min edge weight: {Number(state.min_edge_weight || 0).toFixed(2)}</label>
          <div className="ip-control-range">
            <input type="range" min="0" max="1" step="0.05" value={Number(state.min_edge_weight || 0)}
              onChange={e => controls.setState("min_edge_weight", parseFloat(e.target.value))} />
          </div>
        </div>
      </div>
    </>
  );
}
