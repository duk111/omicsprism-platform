import { FormEvent, useState } from "react";
import type { AnalysisType } from "../api-types";
import "./AnalysisForm.css";

interface Props {
  type: AnalysisType;
  onBack: () => void;
  onSubmitStart: (jobId: string) => void;
}

interface FileFields {
  transcriptome: File | null;
  metabolome: File | null;
  group: File | null;
}

const fileFields: { key: keyof FileFields; label: string }[] = [
  { key: "transcriptome", label: "转录组数据文件 (CSV)" },
  { key: "metabolome", label: "代谢组数据文件 (CSV)" },
  { key: "group", label: "分组信息文件 (CSV)" }
];

export default function AnalysisForm({ type, onBack, onSubmitStart }: Props) {
  const [projectName, setProjectName] = useState("");
  const [files, setFiles] = useState<FileFields>({
    transcriptome: null,
    metabolome: null,
    group: null
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const title = "关联分析";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);

    if (!projectName.trim()) {
      setMessage("请输入项目名称");
      return;
    }

    for (const f of fileFields) {
      if (!files[f.key]) {
        setMessage(`请上传 ${f.label}`);
        return;
      }
    }

    const formData = new FormData();
    formData.append("project_name", projectName.trim());
    formData.append("analysis_type", type);
    for (const f of fileFields) {
      formData.append(f.key, files[f.key] as File);
    }

    setIsSubmitting(true);
    try {
      const response = await fetch("/api/jobs", { method: "POST", body: formData });
      if (!response.ok) {
        const err = await response.json().catch(() => null);
        throw new Error(err?.detail ?? "提交失败");
      }
      const job = await response.json();
      onSubmitStart(job.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "提交失败");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="form-container">
      <button className="back-button" type="button" onClick={onBack}>
        &larr; 返回选择
      </button>
      <h2 className="form-heading">{title}</h2>
      <form className="analysis-form" onSubmit={handleSubmit}>
        <label className="field">
          <span>项目名称</span>
          <input
            type="text"
            value={projectName}
            placeholder="例如：CRC队列研究"
            onChange={(e) => setProjectName(e.target.value)}
          />
        </label>

        {fileFields.map((f) => (
          <label className="field file-field" key={f.key}>
            <span>{f.label}</span>
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) =>
                setFiles((prev) => ({ ...prev, [f.key]: e.target.files?.[0] ?? null }))
              }
            />
            {files[f.key] && (
              <span className="file-name">{files[f.key]!.name}</span>
            )}
          </label>
        ))}

        <button className="submit-button" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "提交中..." : "开始分析"}
        </button>
        {message && <p className="message">{message}</p>}
      </form>
    </div>
  );
}
