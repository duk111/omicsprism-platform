import { type AnalysisType } from "../api-types";
import "./AnalysisSelector.css";

interface Props {
  onSelect: (type: AnalysisType) => void;
}

const cards: { type: AnalysisType; title: string; desc: string; icon: string }[] = [
  {
    type: "correlation",
    title: "关联分析",
    desc: "整合转录组与代谢组数据，构建多组学关联网络",
    icon: "🔗"
  }
];

export default function AnalysisSelector({ onSelect }: Props) {
  return (
    <div className="selector-container">
      <h2 className="selector-heading">选择分析类型</h2>
      <p className="selector-sub">请选择您要执行的分析任务</p>
      <div className="selector-cards">
        {cards.map((card) => (
          <button
            key={card.type}
            className="selector-card"
            type="button"
            onClick={() => onSelect(card.type)}
          >
            <span className="selector-card-icon">{card.icon}</span>
            <span className="selector-card-title">{card.title}</span>
            <span className="selector-card-desc">{card.desc}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
