import { useState, useCallback, useRef, useEffect } from "react";
import type { ImageInfo } from "../api-types";
import { apiUrl, assetUrl } from "../api";
import { ControlPanel } from "./ControlPanel";
import "./FigureViewer.css";

interface Props {
  image: ImageInfo;
  onClose: () => void;
}

type DownloadFormat = "png" | "svg" | "pdf";

interface InteractiveTarget {
  pageId: string;
  params?: Record<string, string>;
}

/** Map static figure filenames to interactive page targets. */
function figureToInteractiveTarget(filename: string): InteractiveTarget | null {
  const n = filename.replace(/\.\w+$/, "").toLowerCase();
  // F01
  if (n.includes("f01") || n.includes("dendrogram") || n.includes("clustering")) return { pageId: "dendrogram" };
  // F02-F09 all open the unified PCA explorer with figure-specific initial state.
  if (n.includes("f02") || n.includes("f04")) {
    return { pageId: "pca", params: { source: "transcriptome", color_by: "group1", x_pc: "1", y_pc: "2" } };
  }
  if (n.includes("f03") || n.includes("f05")) {
    return { pageId: "pca", params: { source: "transcriptome", color_by: "group2", x_pc: "1", y_pc: "2" } };
  }
  if (n.includes("f06") || n.includes("f08")) {
    return { pageId: "pca", params: { source: "metabolome", color_by: "group1", x_pc: "1", y_pc: "2" } };
  }
  if (n.includes("f07") || n.includes("f09")) {
    return { pageId: "pca", params: { source: "metabolome", color_by: "group2", x_pc: "1", y_pc: "2" } };
  }
  // F10
  if (n.includes("f10") || n.includes("upset") || n.includes("evidence")) return { pageId: "upset" };
  // F11, F24
  if (n.includes("f11") || (n.includes("bubble") && !n.includes("module"))) return { pageId: "bubble-heatmap", params: { level: "gene" } };
  if (n.includes("f24") || (n.includes("bubble") && n.includes("module"))) return { pageId: "bubble-heatmap", params: { level: "module" } };
  // F12 / F23 correlation heatmaps share the corr-heatmap page with view param
  if (n.includes("f12") || (n.includes("correlation") && n.includes("heatmap"))) return { pageId: "corr-heatmap", params: { view: "gene-metabolite" } };
  if (n.includes("f23") || (n.includes("module") && n.includes("metabolite") && n.includes("association") && n.includes("heatmap"))) return { pageId: "corr-heatmap", params: { view: "module-metabolite" } };
  // F13, F25
  if (n.includes("f13") || (n.includes("pairs") && n.includes("gene"))) return { pageId: "scatter-panels", params: { panel_type: "gene-metabolite" } };
  if (n.includes("f25") || (n.includes("regression") && n.includes("module"))) return { pageId: "scatter-panels", params: { panel_type: "module-metabolite" } };
  // F14, F21, F22
  if (n.includes("f14") || (n.includes("violin") && n.includes("metabolite"))) return { pageId: "violin-box", params: { view: "metabolite" } };
  if (n.includes("f21") || (n.includes("violin") && n.includes("eigengene"))) return { pageId: "violin-box", params: { view: "module" } };
  if (n.includes("f22") || n.includes("kme")) return null;
  // F15, F16
  if (n.includes("f15") || (n.includes("eigengene") && n.includes("heatmap") && !n.includes("group2"))) return null;
  if (n.includes("f16") || (n.includes("eigengene") && n.includes("heatmap") && n.includes("group2"))) return null;
  // F17, F18, F26
  if (n.includes("f17") || (n.includes("zscore") && n.includes("line") && !n.includes("gene"))) return null;
  if (n.includes("f18") || (n.includes("gene") && n.includes("zscore") && n.includes("line"))) return null;
  if (n.includes("f26") || (n.includes("trend") && n.includes("panel"))) return { pageId: "line-panels" };
  // F19, F20
  if (n.includes("f19")) return null;
  if (n.includes("f20") || n.includes("ridge")) return { pageId: "ridge" };
  // F27, F28
  if (n.includes("f27") || n.includes("direction")) return null;
  if (n.includes("f28") || (n.includes("edgeweight") && n.includes("distribution"))) return null;
  // F29, F30
  if (n.includes("f29") || (n.includes("circos") && !n.includes("cnet"))) return { pageId: "circos", params: { layout: "circos" } };
  if (n.includes("f30") || n.includes("cnet")) return { pageId: "circos", params: { layout: "cnet" } };
  // Fallback: detect by chart characteristics
  if (n.includes("volcano")) return { pageId: "volcano" };
  if (n.includes("pca") || n.includes("oplsda")) return { pageId: "pca", params: { source: "transcriptome", color_by: "group1", x_pc: "1", y_pc: "2" } };
  if (n.includes("heatmap")) return null;
  if (n.includes("scatter") || n.includes("regression")) return { pageId: "scatter-panels", params: { panel_type: "gene-metabolite" } };
  if (n.includes("violin") || n.includes("boxplot")) return { pageId: "violin-box", params: { view: "metabolite" } };
  if (n.includes("ridge")) return { pageId: "ridge" };
  if (n.includes("bar") || n.includes("count")) return null;
  return null;
}

function buildInteractiveUrl(jobId: string, target: InteractiveTarget | null): string | null {
  if (!jobId || !target) return null;
  const params = new URLSearchParams(target.params || {});
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return `/interactive/${jobId}/${target.pageId}${suffix}`;
}

export default function FigureViewer({ image, onClose }: Props) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [imageError, setImageError] = useState(false);

  const canvasRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const minZoom = 0.25;
  const maxZoom = 5;
  const zoomStep = 0.25;

  const clampZoom = useCallback((z: number) => Math.min(maxZoom, Math.max(minZoom, z)), []);

  const handleWheel = useCallback(
    (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const delta = e.deltaY > 0 ? -zoomStep : zoomStep;
      setZoom((prev) => clampZoom(prev + delta));
    },
    [clampZoom]
  );

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      setIsDragging(true);
      setDragStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
    },
    [pan]
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!isDragging) return;
      setPan({ x: e.clientX - dragStart.x, y: e.clientY - dragStart.y });
    },
    [isDragging, dragStart]
  );

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  const resetView = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  const zoomIn = useCallback(() => setZoom((prev) => clampZoom(prev + zoomStep)), [clampZoom]);
  const zoomOut = useCallback(() => setZoom((prev) => clampZoom(prev - zoomStep)), [clampZoom]);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      containerRef.current.requestFullscreen();
    }
  }, []);

  const downloadImage = useCallback(
    (format: DownloadFormat) => {
      const base = assetUrl(image.full_url).replace(/\.\w+$/, "");
      const url = `${base}.${format}`;
      const link = document.createElement("a");
      link.href = url;
      link.download = `${image.name.replace(/\.\w+$/, "")}.${format}`;
      link.click();
    },
    [image]
  );

  const jobId = extractJobId(image);
  const interactiveTarget = figureToInteractiveTarget(image.name);
  const interactiveUrl = buildInteractiveUrl(jobId, interactiveTarget);

  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "+" || e.key === "=") zoomIn();
      if (e.key === "-") zoomOut();
      if (e.key === "0") resetView();
      if (e.key === "f" || e.key === "F") toggleFullscreen();
    },
    [onClose, zoomIn, zoomOut, resetView, toggleFullscreen]
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKey);
    document.body.style.overflow = "hidden";
    const canvas = canvasRef.current;
    if (canvas) {
      canvas.addEventListener("wheel", handleWheel, { passive: false });
    }
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = "";
      if (canvas) {
        canvas.removeEventListener("wheel", handleWheel);
      }
    };
  }, [handleKey, handleWheel]);

  return (
    <div className="fv-overlay" onClick={onClose}>
      <div className="fv-container" ref={containerRef} onClick={(e) => e.stopPropagation()}>
        {/* Toolbar */}
        <div className="fv-toolbar">
          <div className="fv-toolbar-left">
            <h2 className="fv-title">{image.name}</h2>
          </div>
          <div className="fv-toolbar-center">
            <button className="fv-tool-btn" title="Zoom out (-)" onClick={zoomOut} disabled={zoom <= minZoom}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="2" y="7" width="12" height="2" rx="1" fill="currentColor"/></svg>
            </button>
            <span className="fv-zoom-label">{Math.round(zoom * 100)}%</span>
            <button className="fv-tool-btn" title="Zoom in (+)" onClick={zoomIn} disabled={zoom >= maxZoom}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="2" y="7" width="12" height="2" rx="1" fill="currentColor"/><rect x="7" y="2" width="2" height="12" rx="1" fill="currentColor"/></svg>
            </button>
            <button className="fv-tool-btn" title="Reset (0)" onClick={resetView}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 2v5h5M14 14v-5H9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M13.5 6.5A5.5 5.5 0 0 0 3.4 4.9L2 7M2.5 9.5a5.5 5.5 0 0 0 10.1 1.6L14 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
            <button className="fv-tool-btn" title="Fullscreen (F)" onClick={toggleFullscreen}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 5V2h3M11 2h3v3M14 11v3h-3M5 14H2v-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
          </div>
          <div className="fv-toolbar-right">
            <button className="fv-close-btn" title="Close (Esc)" onClick={onClose}>
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="fv-body">
          <div
            className={`fv-canvas${isDragging ? " fv-dragging" : ""}`}
            ref={canvasRef}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
            onDoubleClick={resetView}
          >
            {imageError ? (
              <div className="fv-empty">
                <p>Failed to load image.</p>
                <a href={assetUrl(image.full_url)} download className="fv-download-link">Download instead</a>
              </div>
            ) : (
              <img
                src={assetUrl(image.full_url)}
                alt={image.name}
                className="fv-image"
                style={{ transform: `scale(${zoom}) translate(${pan.x / zoom}px, ${pan.y / zoom}px)` }}
                onError={() => setImageError(true)}
                draggable={false}
              />
            )}
          </div>

          {/* Right-side action panel */}
          <div className="fv-action-panel">
            <div className="fv-action-section">
              <h3 className="fv-action-title">Download</h3>
              <button className="fv-action-btn fv-dl-png" onClick={() => downloadImage("png")} title="Download PNG image">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                <span>PNG<span className="fv-action-sub">Raster image</span></span>
              </button>
              <button className="fv-action-btn fv-dl-svg" onClick={() => downloadImage("svg")} title="Download SVG image">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                <span>SVG<span className="fv-action-sub">Vector image</span></span>
              </button>
              <button className="fv-action-btn fv-dl-pdf" onClick={() => downloadImage("pdf")} title="Download PDF image">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                <span>PDF<span className="fv-action-sub">Print-ready</span></span>
              </button>
            </div>

            <div className="fv-action-section">
              <h3 className="fv-action-title">Explore</h3>
              {interactiveUrl ? (
                <a
                  className="fv-action-btn fv-interactive-btn"
                  href={interactiveUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title="Open interactive chart in new window"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                  <span>Interactive<span className="fv-action-sub">Explore &amp; analyze</span></span>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="fv-external-icon"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>
                </a>
              ) : (
                <p className="fv-action-disabled">Interactive view not available for this figure type.</p>
              )}
            </div>

            <ControlPanel image={image} />
          </div>
        </div>
      </div>
    </div>
  );
}

function extractJobId(image: ImageInfo): string {
  const match = image.full_url?.match(/\/api\/jobs\/([^/]+)\//);
  return match ? match[1] : "";
}
