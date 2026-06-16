import { useMemo } from "react";
import { InteractivePCA } from "./charts/InteractivePCA";
import { InteractiveHeatmap } from "./charts/InteractiveHeatmap";
import { InteractiveBubble } from "./charts/InteractiveBubble";
import { InteractiveScatterPanels } from "./charts/InteractiveScatterPanels";
import { InteractiveViolinBox } from "./charts/InteractiveViolinBox";
import { InteractiveRidge } from "./charts/InteractiveRidge";
import { InteractiveLinePanels } from "./charts/InteractiveLinePanels";
import { InteractiveUpSet } from "./charts/InteractiveUpSet";
import { InteractiveDendrogram } from "./charts/InteractiveDendrogram";
import { InteractiveCircos } from "./charts/InteractiveCircos";
import { InteractiveVolcano } from "./charts/InteractiveVolcano";
import { publicRelativePath, publicUrl } from "../api";
import "./InteractivePage.css";

type PageId = string;
type PageComponent = React.ComponentType<{ jobId: string; pageId: string }>;

const PAGE_MAP: Record<PageId, PageComponent> = {
  "pca": InteractivePCA,
  "pca-scatter": InteractivePCA,
  "pca-pairs": InteractivePCA,
  "dendrogram": InteractiveDendrogram,
  "upset": InteractiveUpSet,
  "bubble-heatmap": InteractiveBubble,
  "scatter-panels": InteractiveScatterPanels,
  "violin-box": InteractiveViolinBox,
  "corr-heatmap": InteractiveHeatmap,
  "line-panels": InteractiveLinePanels,
  "ridge": InteractiveRidge,
  "circos": InteractiveCircos,
  "volcano": InteractiveVolcano,
};

export function InteractiveRouter() {
  const { jobId, pageId } = useMemo(() => {
    const parts = publicRelativePath().replace(/^\/+/, "").split("/");
    // Expected pattern: /interactive/{jobId}/{pageId}
    return {
      jobId: parts[1] || "",
      pageId: (parts[2] || "").toLowerCase(),
    };
  }, []);

  const Component = PAGE_MAP[pageId];

  if (!Component) {
    return (
      <div className="ip-error-page">
        <h1>Page not found</h1>
        <p>Interactive page &quot;{pageId}&quot; is not available.</p>
        <p><a href={publicUrl("/")}>Return to OmicsPrism</a></p>
      </div>
    );
  }

  return <Component jobId={jobId} pageId={pageId} />;
}
