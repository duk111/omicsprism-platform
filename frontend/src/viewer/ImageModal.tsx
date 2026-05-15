import { useEffect, useCallback } from "react";
import type { ImageInfo } from "../api-types";
import "./ImageModal.css";

interface Props {
  image: ImageInfo;
  onClose: () => void;
}

export default function ImageModal({ image, onClose }: Props) {
  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    },
    [onClose]
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
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title">{image.name}</h2>
          <button className="modal-close" type="button" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="modal-image-area">
            <img
              src={image.full_url}
              alt={image.name}
              className="modal-full-image"
            />
          </div>

          <aside className="modal-tools">
            <div className="tool-placeholder">
              <span className="tool-icon">🔍</span>
              <span>缩放工具（即将推出）</span>
            </div>
            <div className="tool-placeholder">
              <span className="tool-icon">📐</span>
              <span>标注工具（即将推出）</span>
            </div>
            <div className="tool-placeholder">
              <span className="tool-icon">📊</span>
              <span>数据查看（即将推出）</span>
            </div>
            <div className="tool-placeholder">
              <span className="tool-icon">💾</span>
              <span>导出图片（即将推出）</span>
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}
