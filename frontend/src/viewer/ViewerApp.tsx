import { useEffect, useState } from "react";
import type { ImageInfo } from "../api-types";
import ImageModal from "./ImageModal";
import "./ViewerApp.css";

export default function ViewerApp() {
  const params = new URLSearchParams(window.location.search);
  const jobId = params.get("jobId");

  const [images, setImages] = useState<ImageInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedImage, setSelectedImage] = useState<ImageInfo | null>(null);

  useEffect(() => {
    if (!jobId) {
      setError("缺少任务ID参数");
      setLoading(false);
      return;
    }
    fetch(`/api/jobs/${jobId}/images`)
      .then((res) => {
        if (!res.ok) throw new Error("加载失败");
        return res.json();
      })
      .then((data) => {
        setImages(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "加载失败");
        setLoading(false);
      });
  }, [jobId]);

  if (loading) {
    return (
      <div className="viewer-shell">
        <div className="viewer-loading">正在加载可视化结果...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="viewer-shell">
        <div className="viewer-error">{error}</div>
      </div>
    );
  }

  return (
    <div className="viewer-shell">
      <header className="viewer-header">
        <h1>可视化结果</h1>
        <p className="viewer-job-id">任务: {jobId}</p>
      </header>

      {images.length === 0 ? (
        <p className="viewer-empty">暂无可视化结果图片。</p>
      ) : (
        <div className="gallery-grid">
          {images.map((img) => (
            <button
              key={img.path}
              className="gallery-item"
              type="button"
              onClick={() => setSelectedImage(img)}
            >
              <img
                src={img.thumbnail_url}
                alt={img.name}
                loading="lazy"
                className="gallery-thumb"
              />
              <span className="gallery-caption">{img.name}</span>
            </button>
          ))}
        </div>
      )}

      {selectedImage && (
        <ImageModal
          image={selectedImage}
          onClose={() => setSelectedImage(null)}
        />
      )}
    </div>
  );
}
