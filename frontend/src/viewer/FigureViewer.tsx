import { useState, useCallback, useRef, useEffect } from "react";
import type { ImageInfo } from "../api-types";
import { ControlPanel } from "./ControlPanel";
import "./FigureViewer.css";

interface Props {
  image: ImageInfo;
  onClose: () => void;
}

type DownloadFormat = "png" | "svg" | "pdf";

export default function FigureViewer({ image, onClose }: Props) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [showDownloadMenu, setShowDownloadMenu] = useState(false);
  const [activeFormat, setActiveFormat] = useState<DownloadFormat>("png");
  const [imageError, setImageError] = useState(false);

  const canvasRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const minZoom = 0.25;
  const maxZoom = 5;
  const zoomStep = 0.25;

  const clampZoom = useCallback((z: number) => Math.min(maxZoom, Math.max(minZoom, z)), []);

  const handleWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
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
      const url =
        format === "png"
          ? image.full_url
          : image.full_url.replace(/\.\w+$/, `.${format}`);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${image.name.replace(/\.\w+$/, "")}.${format}`;
      link.click();
      setShowDownloadMenu(false);
    },
    [image]
  );

  const dataTableUrl = image.path
    ? `/api/jobs/${extractJobId(image)}/download/${guessTablePath(image)}`
    : null;

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
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = "";
    };
  }, [handleKey]);

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
          </div>
          <div className="fv-toolbar-right">
            {dataTableUrl && (
              <a className="fv-tool-btn fv-data-link" href={dataTableUrl} title="Download source data (CSV)" download>
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="1" y="1" width="6" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/><rect x="9" y="1" width="6" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/><rect x="1" y="9" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.2"/><rect x="9" y="10" width="6" height="5" rx="1" stroke="currentColor" strokeWidth="1.2"/></svg>
                Data
              </a>
            )}
            {image.interactive_url && (
              <a className="fv-tool-btn fv-data-link" href={image.interactive_url} title="Open interactive figure" target="_blank" rel="noopener noreferrer">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 2.5h10A1.5 1.5 0 0 1 14.5 4v8a1.5 1.5 0 0 1-1.5 1.5H3A1.5 1.5 0 0 1 1.5 12V4A1.5 1.5 0 0 1 3 2.5Z" stroke="currentColor" strokeWidth="1.3"/><path d="M4.5 10.5 7 8 4.5 5.5M8 10.5h3.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/></svg>
                Interactive
              </a>
            )}
            <div className="fv-download-wrap">
              <button className="fv-tool-btn" title="Download" onClick={() => setShowDownloadMenu(!showDownloadMenu)}>
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 1v10M4 7l4 4 4-4M2 13v1a1 1 0 001 1h10a1 1 0 001-1v-1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </button>
              {showDownloadMenu && (
                <div className="fv-download-menu">
                  {(["png", "svg", "pdf"] as DownloadFormat[]).map((fmt) => (
                    <button key={fmt} className="fv-download-item" onClick={() => downloadImage(fmt)}>
                      {fmt.toUpperCase()}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button className="fv-tool-btn" title="Fullscreen (F)" onClick={toggleFullscreen}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 5V2h3M11 2h3v3M14 11v3h-3M5 14H2v-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
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
            onWheel={handleWheel}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
            onDoubleClick={resetView}
          >
            {imageError ? (
              <div className="fv-empty">
                <p>Failed to load image.</p>
                <a href={image.full_url} download className="fv-download-link">Download instead</a>
              </div>
            ) : (
              <img
                src={image.full_url}
                alt={image.name}
                className="fv-image"
                style={{ transform: `scale(${zoom}) translate(${pan.x / zoom}px, ${pan.y / zoom}px)` }}
                onError={() => setImageError(true)}
                draggable={false}
              />
            )}
          </div>

          <ControlPanel image={image} />
        </div>
      </div>
    </div>
  );
}

function extractJobId(image: ImageInfo): string {
  const match = image.full_url?.match(/\/api\/jobs\/([^/]+)\//);
  return match ? match[1] : "";
}

function guessTablePath(image: ImageInfo): string {
  const stem = image.name.replace(/\.\w+$/, "").toLowerCase();
  if (stem.includes("volcano") || stem.includes("deg")) return "outputs/deg_results.csv";
  if (stem.includes("module")) return "outputs/T09_Module_Metabolite_Association.csv";
  if (stem.includes("network") || stem.includes("circos")) return "outputs/T03_High_Confidence_Network.csv";
  if (stem.includes("association") || stem.includes("regression")) return "outputs/T01_Metabolite_Gene_Scoring_Table.csv";
  if (stem.includes("dem")) return "outputs/differential_metabolite_counts.csv";
  if (stem.includes("union")) return "outputs/union_significant_metabolites.csv";
  return "outputs/OmicsPrism_results.zip";
}
