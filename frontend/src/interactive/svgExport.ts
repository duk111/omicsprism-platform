/**
 * SVG export utilities for non-Plotly interactive charts.
 *
 * All four custom-SVG charts (Dendrogram, UpSet, Bubble, Circos) use inline
 * SVG attributes (fill, stroke, fontSize, etc.) for visual styling — no CSS
 * class dependency.  This means we can deep-clone the SVG DOM, adjust
 * dimensions to bake in the current zoom level, serialize with XMLSerializer,
 * and get a faithful reproduction of what the user sees.
 */

/**
 * Return the SVG's effective pixel dimensions.
 * When width/height are absolute pixel values (e.g. Dendrogram, UpSet, Bubble)
 * we parse them directly.  When they are relative (e.g. Circos uses
 * `width="100%"`) we fall back to the element's actual rendered bounding box.
 */
function getSvgPixelSize(svgEl: SVGSVGElement): { w: number; h: number } {
  const rawW = svgEl.getAttribute("width") || "";
  const rawH = svgEl.getAttribute("height") || "";
  const parsedW = parseFloat(rawW);
  const parsedH = parseFloat(rawH);

  // If attributes are missing, non-numeric, or relative (%, em, vw, etc.) use
  // the browser-computed bounding box.
  const isAbsolute =
    Number.isFinite(parsedW) &&
    Number.isFinite(parsedH) &&
    !rawW.includes("%") &&
    !rawH.includes("%") &&
    parsedW > 0 &&
    parsedH > 0;

  if (isAbsolute) return { w: parsedW, h: parsedH };

  const rect = svgEl.getBoundingClientRect();
  return {
    w: Math.round(rect.width || 800),
    h: Math.round(rect.height || 600),
  };
}

/** Serialize an SVG element, baking in the given zoom multiplier. */
export function serializeSvg(svgEl: SVGSVGElement, zoom: number): string {
  const clone = svgEl.cloneNode(true) as SVGSVGElement;

  const { w, h } = getSvgPixelSize(svgEl);

  // Bake zoom into explicit absolute pixel dimensions so the exported image
  // matches the user's current view regardless of how zoom is implemented
  // (CSS transform, wrapper-div sizing, or viewBox manipulation).
  clone.setAttribute("width", String(Math.round(w * zoom)));
  clone.setAttribute("height", String(Math.round(h * zoom)));

  // Remove CSS-based transforms so they don't double-apply (Dendrogram).
  clone.style.transform = "";
  clone.style.transformOrigin = "";

  // Ensure opaque white background for PNG rasterisation.
  clone.style.backgroundColor = "#fff";

  return new XMLSerializer().serializeToString(clone);
}

/** Download the SVG element's current visual state as an .svg file. */
export function downloadSvg(svgEl: SVGSVGElement, zoom: number, filename: string): void {
  const svgString = serializeSvg(svgEl, zoom);
  const blob = new Blob([svgString], { type: "image/svg+xml;charset=utf-8" });
  triggerDownload(blob, `${filename}.svg`);
}

/** Rasterise the SVG element's current visual state and download as .png. */
export function downloadPng(svgEl: SVGSVGElement, zoom: number, filename: string): void {
  const svgString = serializeSvg(svgEl, zoom);
  const { w, h } = getSvgPixelSize(svgEl);
  const canvasW = Math.round(w * zoom);
  const canvasH = Math.round(h * zoom);

  const blob = new Blob([svgString], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);

  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = canvasW;
    canvas.height = canvasH;
    const ctx = canvas.getContext("2d")!;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvasW, canvasH);
    ctx.drawImage(img, 0, 0, canvasW, canvasH);
    canvas.toBlob((pngBlob) => {
      if (!pngBlob) return;
      triggerDownload(pngBlob, `${filename}.png`);
    }, "image/png");
    URL.revokeObjectURL(url);
  };
  img.onerror = () => URL.revokeObjectURL(url);
  img.src = url;
}

function triggerDownload(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}
