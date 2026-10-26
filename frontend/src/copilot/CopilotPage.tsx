import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Bot, Check, CheckCircle2, ChevronDown, CircleAlert, CircleHelp, Clock3, FilePlus2, LoaderCircle, Menu, MessageSquarePlus, MoreHorizontal, Paperclip, Pencil, Pin, PinOff, Send, Square, Trash2, Wifi, WifiOff, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import type { AgentJobWaitResponse, AgentMessageResponse, AgentRunResponse, AgentStreamEvent, AgentThreadResponse, AgentTraceEventResponse, AgentTurnResponse, GraphInterrupt, GraphPendingInterrupt, GraphTurnResult } from "../api-types";
import { ApiRequestError } from "../api";
import { createClientId } from "../clientId";
import { agentApi, isGraphTurnResult } from "./agentApi";
import type { GraphResumeRequest } from "./agentApi";
import { GraphInterruptPanel } from "./GraphInterruptPanel";
import { MessageBlocks } from "./MessageBlocks";
import { TracePanel } from "./TracePanel";
import "./copilot.css";
import type { AnalysisType } from "../api-types";

export type ResultCopilotContext = {
  jobId: string;
  analysisType: AnalysisType;
  resultType: "overview" | "figure" | "table";
  title: string;
  artifact?: string;
};
type CopilotProps = { context?: ResultCopilotContext; embedded?: boolean; onClose?: () => void };
const RESULT_SUGGESTIONS: Record<AnalysisType, string[]> = {
  deg: ["哪些基因差异最显著？", "当前结果中上调最明显的基因有哪些？"],
  dem: ["哪些代谢物具有显著差异？", "当前结果中变化幅度最大的代谢物有哪些？"],
  gma: ["当前结果中有哪些重要的基因—代谢物关联？", "当前结果中哪些网络模块值得关注？"],
};

const INPUT_FIELDS = ["counts", "metadata", "transcriptome", "metabolome", "group"] as const;
type InputField = typeof INPUT_FIELDS[number];
const INPUT_FIELD_LABELS: Record<InputField, string> = {
  counts: "Counts matrix",
  metadata: "Sample metadata table",
  transcriptome: "Transcriptome matrix",
  metabolome: "Metabolome matrix",
  group: "Sample group table (sample_id/group1/group2)",
};
const INPUT_FIELD_DESCRIPTIONS: Record<InputField, string> = {
  counts: "Raw gene counts with genes in rows and sample IDs in columns. Use integer counts for DEG.",
  metadata: "Sample metadata mapping each sample ID to comparison groups and optional covariates.",
  transcriptome: "Gene expression matrix with genes in rows and sample IDs in columns for GMA.",
  metabolome: "Metabolite abundance matrix with metabolites in rows and sample IDs in columns.",
  group: "Sample group table containing sample_id, group1, and group2 columns for GMA.",
};
const PINNED_THREADS_STORAGE_KEY = "omicsprism.copilot.pinned_threads";

function loadPinnedThreadIds(): string[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(PINNED_THREADS_STORAGE_KEY) || "[]");
    return Array.isArray(value) ? value.filter(item => typeof item === "string") : [];
  } catch {
    return [];
  }
}

type Attachment = { id: string; file: File; field: InputField };
type PendingGraph = GraphPendingInterrupt;

export default function CopilotPage(props: CopilotProps) {
  const [searchParams] = useSearchParams();
  const focusJobId = props.context?.jobId ?? searchParams.get("job_id");
  // Job changes dispose the old transport and all of its asynchronous state.
  return <CopilotConversation key={focusJobId ?? "general"} {...props} focusJobId={focusJobId} />;
}

function CopilotConversation({ context, embedded = false, onClose, focusJobId }: CopilotProps & { focusJobId: string | null }) {
  const [threads, setThreads] = useState<AgentThreadResponse[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRunResponse | null>(null);
  const [messages, setMessages] = useState<AgentMessageResponse[]>([]);
  const [turns, setTurns] = useState<AgentTurnResponse[]>([]);
  const [jobWaits, setJobWaits] = useState<AgentJobWaitResponse[]>([]);
  const [traceEvents, setTraceEvents] = useState<AgentTraceEventResponse[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [cancelingWaitId, setCancelingWaitId] = useState<string | null>(null);
  const [pendingGraph, setPendingGraph] = useState<PendingGraph | null>(null);
  const [graphBusy, setGraphBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [menuThreadId, setMenuThreadId] = useState<string | null>(null);
  const [editingThreadId, setEditingThreadId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [savingThreadId, setSavingThreadId] = useState<string | null>(null);
  const [pinnedThreadIds, setPinnedThreadIds] = useState<string[]>(loadPinnedThreadIds);
  const [connection, setConnection] = useState<"live" | "recovering" | "offline">("recovering");
  const [railOpen, setRailOpen] = useState(false);
  const initialized = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const messageEnd = useRef<HTMLDivElement>(null);
  const activeThreadRef = useRef(activeId);
  activeThreadRef.current = activeId;

  const refreshTrace = useCallback(async (threadId: string) => {
    setTraceLoading(true);
    try {
      const result = await agentApi.listTrace(threadId);
      setTraceEvents(result.events);
    } catch {
      // Trace is an operator aid; a transient read failure must not interrupt chat recovery.
    } finally {
      setTraceLoading(false);
    }
  }, []);

  const recover = useCallback(async (threadId: string) => {
    const [detail, messageList, turnList, interrupt, waitList, traceList] = await Promise.all([
      agentApi.getThread(threadId),
      agentApi.listMessages(threadId),
      agentApi.listTurns(threadId),
      agentApi.getPendingInterrupt(threadId),
      agentApi.listJobWaits(threadId),
      agentApi.listTrace(threadId),
    ]);
    if (activeThreadRef.current !== threadId) return;
    setRun(detail.run); setMessages(messageList.messages); setTurns(turnList.turns);
    setPendingGraph(interrupt); setJobWaits(waitList.waits);
    setTraceEvents(traceList.events);
  }, []);

  const createThread = useCallback(async (focusIds: string[] = []) => {
    const created = await agentApi.createThread(focusIds);
    setThreads(current => [created, ...current.filter(item => item.thread_id !== created.thread_id)]);
    setPendingGraph(null); setGraphBusy(false); setJobWaits([]); setTraceEvents([]);
    setActiveId(created.thread_id); setRailOpen(false); setNotice(null);
    return created.thread_id;
  }, []);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    void (async () => {
      try {
        const listed = await agentApi.listThreads();
        setThreads(listed.threads);
        if (focusJobId) await createThread([focusJobId]);
        else if (listed.threads[0]) setActiveId(listed.threads[0].thread_id);
        else await createThread();
      } catch (error) { setNotice(errorMessage(error)); }
      finally { setLoading(false); }
    })();
  }, [createThread, focusJobId]);

  useEffect(() => {
    if (!activeId) return;
    setPendingGraph(null); setGraphBusy(false); setLoading(true); setNotice(null);
    recover(activeId).catch(error => setNotice(errorMessage(error))).finally(() => setLoading(false));
  }, [activeId, recover]);

  useEffect(() => {
    if (!activeId) return;
    let fallback: number | undefined;
    const source = new EventSource(agentApi.streamUrl(activeId), { withCredentials: true });
    const accept = (event: MessageEvent) => {
      if (activeThreadRef.current !== activeId) return;
      let payload: AgentStreamEvent;
      try {
        payload = JSON.parse(event.data) as AgentStreamEvent;
        if (!payload || !["message.created", "job.updated", "turn.updated", "interrupt.updated"].includes(payload.event_type)) throw new Error("Unknown Copilot event");
      }
      catch { setNotice("Could not read a Copilot update. Reconnecting..."); source.onerror?.(new Event("error")); return; }
      setConnection("live");
      if (payload.event_type === "message.created") setMessages(current => upsert(current, payload.data as AgentMessageResponse, "message_id"));
      else if (payload.event_type === "job.updated") setJobWaits(current => upsert(current, payload.data as AgentJobWaitResponse, "wait_id"));
      else if (payload.event_type === "turn.updated") {
        const turn = payload.data as AgentTurnResponse;
        setTurns(current => upsert(current, turn, "turn_id"));
        if (turn.status === "queued" || turn.status === "completed" || turn.status === "failed" || turn.status === "cancelled") {
          setPendingGraph(current => current?.checkpoint_turn_id === turn.turn_id ? null : current);
        }
        void refreshTrace(activeId);
      } else {
        setPendingGraph(payload.data as GraphPendingInterrupt | null);
      }
    };
    source.addEventListener("message.created", accept);
    source.addEventListener("job.updated", accept);
    source.addEventListener("turn.updated", accept);
    source.addEventListener("interrupt.updated", accept);
    source.onopen = () => { setConnection("live"); if (fallback) { window.clearInterval(fallback); fallback = undefined; } };
    source.onerror = () => {
      setConnection(navigator.onLine ? "recovering" : "offline");
      if (!fallback) fallback = window.setInterval(() => recover(activeId).catch(() => setConnection("offline")), 3000);
    };
    return () => { source.close(); if (fallback) window.clearInterval(fallback); };
  }, [activeId, recover, refreshTrace]);

  useEffect(() => { messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages, pendingGraph, jobWaits]);

  const pendingTurn = turns.some(turn => turn.status === "queued" || turn.status === "running");
  const pendingTurnRecord = turns.find(turn => turn.status === "queued" || turn.status === "running");
  const activeJobWaits = jobWaits.filter(wait => wait.wait_status === "waiting" || wait.wait_status === "resume_queued");
  const focusIds = context ? [context.jobId] : run?.focus.in_scope_job_ids ?? (focusJobId ? [focusJobId] : []);
  const checkpointLabel = run ? `Version ${run.version}` : "Ready";
  const visibleThreads = useMemo(() => [...threads].sort((left, right) => {
    const leftPinned = pinnedThreadIds.includes(left.thread_id) ? 1 : 0;
    const rightPinned = pinnedThreadIds.includes(right.thread_id) ? 1 : 0;
    if (leftPinned !== rightPinned) return rightPinned - leftPinned;
    return new Date(right.updated_at).valueOf() - new Date(left.updated_at).valueOf();
  }), [pinnedThreadIds, threads]);

  async function send() {
    const text = draft.trim();
    if (!text || sending || pendingTurn || loading) return;
    setSending(true); setNotice(null);
    try {
      const threadId = activeId || await createThread(focusJobId ? [focusJobId] : []);
      const bundle = attachments.length ? await agentApi.uploadBundle(threadId, attachments) : null;
      const result = await agentApi.createTurn(threadId, text, bundle?.bundle_id ?? null, focusIds, context ? {
        job_id: context.jobId,
        analysis_type: context.analysisType.toUpperCase() as "DEG" | "DEM" | "GMA",
        result_type: context.resultType,
        artifact: context.artifact ?? null,
      } : undefined);
      setDraft(""); setAttachments([]);
      if (isGraphTurnResult(result)) applyGraphResult(result);
      else setTurns(current => upsert(current, result, "turn_id"));
      await recover(threadId);
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setSending(false); }
  }

  async function resumeGraph(request: GraphResumeRequest) {
    if (!activeId || !pendingGraph || graphBusy) return;
    setGraphBusy(true); setNotice(null);
    try {
      const result = await agentApi.resumeTurn(activeId, pendingGraph.checkpoint_turn_id, request);
      applyGraphResult(result);
      await recover(activeId);
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setGraphBusy(false); }
  }

  async function cancelPendingTurn() {
    if (!activeId || !pendingTurnRecord || canceling) return;
    setCanceling(true); setNotice(null);
    try {
      const cancelled = await agentApi.cancelTurn(activeId, pendingTurnRecord.turn_id);
      setTurns(current => upsert(current, cancelled, "turn_id"));
      await recover(activeId);
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setCanceling(false); }
  }

  async function cancelJobWait(wait: AgentJobWaitResponse) {
    if (!activeId || cancelingWaitId) return;
    setCancelingWaitId(wait.wait_id); setNotice(null);
    try {
      const cancelled = await agentApi.cancelJobWait(activeId, wait.wait_id);
      setJobWaits(current => upsert(current, cancelled, "wait_id"));
      setNotice(`Stopped waiting for job ${wait.job_id.slice(0, 12)}.`);
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setCancelingWaitId(null); }
  }

  function togglePinned(threadId: string) {
    setPinnedThreadIds(current => {
      const next = current.includes(threadId)
        ? current.filter(id => id !== threadId)
        : [threadId, ...current];
      window.localStorage.setItem(PINNED_THREADS_STORAGE_KEY, JSON.stringify(next));
      return next;
    });
    setMenuThreadId(null);
  }

  function startRename(thread: AgentThreadResponse) {
    setEditingThreadId(thread.thread_id);
    setEditingTitle(thread.title || "New Copilot thread");
    setMenuThreadId(null);
  }

  function cancelRename() {
    setEditingThreadId(null);
    setEditingTitle("");
  }

  async function saveRename(threadId: string) {
    const title = editingTitle.trim();
    if (!title) {
      setNotice("Conversation name cannot be blank.");
      return;
    }
    setSavingThreadId(threadId);
    setNotice(null);
    try {
      const updated = await agentApi.renameThread(threadId, title);
      setThreads(current => current.map(thread => thread.thread_id === threadId ? updated : thread));
      cancelRename();
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setSavingThreadId(null);
    }
  }

  async function deleteConversation(threadId: string) {
    if (!window.confirm("Delete this conversation and its stored history?")) return;
    try {
      await agentApi.deleteThread(threadId);
      const remaining = threads.filter(item => item.thread_id !== threadId);
      const nextPinned = pinnedThreadIds.filter(id => id !== threadId);
      setPinnedThreadIds(nextPinned);
      window.localStorage.setItem(PINNED_THREADS_STORAGE_KEY, JSON.stringify(nextPinned));
      setThreads(remaining);
      if (activeId === threadId) {
        setActiveId(visibleThreads.find(item => item.thread_id !== threadId)?.thread_id ?? remaining[0]?.thread_id ?? null);
        if (!remaining.length) await createThread();
      }
      setMenuThreadId(null);
    } catch (error) { setNotice(errorMessage(error)); }
  }

  function applyGraphResult(result: GraphTurnResult) {
    setTurns(current => upsert(current, result.turn, "turn_id"));
    if (result.message) setMessages(current => upsert(current, result.message!, "message_id"));
    setPendingGraph(result.interrupt ? { checkpoint_turn_id: result.checkpoint_turn_id, interrupt: result.interrupt } : null);
  }

  function addFiles(list: FileList | null) {
    if (!list) return;
    const next = Array.from(list).filter(file => file.name.toLowerCase().endsWith(".csv")).slice(0, 6 - attachments.length).map(file => ({ id: createClientId(), file, field: guessField(file.name) }));
    if (next.length !== list.length) setNotice("Only CSV files are accepted, with at most six files per bundle.");
    setAttachments(current => [...current, ...next]);
  }

  const activeThread = threads.find(item => item.thread_id === activeId);
  const statusLabel = useMemo(() => connection === "live" ? "Live" : connection === "offline" ? "Offline" : "Reconnecting", [connection]);

  return <div className={`copilot-page${embedded ? " copilot-embedded" : ""}`}>
    {!embedded && <>
    <button className="copilot-rail-toggle" type="button" aria-label="Open conversations" onClick={() => setRailOpen(true)}><Menu size={20} /></button>
    <aside className={`copilot-rail${railOpen ? " rail-open" : ""}`} aria-label="Conversations">
      <div className="rail-heading"><div><span>Workspace</span><h1>Copilot</h1></div><button type="button" title="Close conversations" aria-label="Close conversations" onClick={() => setRailOpen(false)}><X size={19} /></button></div>
      <button type="button" className="new-thread" onClick={() => void createThread()}><MessageSquarePlus size={17} />New conversation</button>
      <div className="thread-list">{visibleThreads.map(thread => {
        const isPinned = pinnedThreadIds.includes(thread.thread_id);
        const isEditing = editingThreadId === thread.thread_id;
        return <div className={`thread-item${thread.thread_id === activeId ? " active" : ""}${isPinned ? " pinned" : ""}${menuThreadId === thread.thread_id ? " menu-open" : ""}`} key={thread.thread_id}>
          {isEditing ? (
            <form className="thread-rename" onSubmit={event => { event.preventDefault(); if (!savingThreadId) void saveRename(thread.thread_id); }}>
              <input autoFocus value={editingTitle} maxLength={200} aria-label="Conversation name" onChange={event => setEditingTitle(event.target.value)} />
              <button type="submit" aria-label="Save conversation name" title="Save" disabled={savingThreadId === thread.thread_id}><Check size={15} /></button>
              <button type="button" aria-label="Cancel rename" title="Cancel" onClick={cancelRename}><X size={15} /></button>
            </form>
          ) : (
            <>
              <button type="button" className="thread-select" onClick={() => { setMenuThreadId(null); setPendingGraph(null); setGraphBusy(false); setActiveId(thread.thread_id); setRailOpen(false); }}>
                <strong>{isPinned && <Pin size={13} aria-label="Pinned" />}{thread.title || "Untitled conversation"}</strong>
                <time>{relativeTime(thread.updated_at)}</time>
              </button>
              <button type="button" className="thread-menu-toggle" title="Conversation actions" aria-label={`Actions for ${thread.title || "conversation"}`} aria-expanded={menuThreadId === thread.thread_id} onClick={event => { event.stopPropagation(); setMenuThreadId(current => current === thread.thread_id ? null : thread.thread_id); }}><MoreHorizontal size={18} /></button>
              {menuThreadId === thread.thread_id && <div className="thread-menu" role="menu">
                <button type="button" role="menuitem" onClick={() => startRename(thread)}><Pencil size={15} />Rename</button>
                <button type="button" role="menuitem" onClick={() => togglePinned(thread.thread_id)}>{isPinned ? <PinOff size={15} /> : <Pin size={15} />}{isPinned ? "Unpin" : "Pin to top"}</button>
                <button type="button" className="thread-menu-danger" role="menuitem" onClick={() => void deleteConversation(thread.thread_id)}><Trash2 size={15} />Delete</button>
              </div>}
            </>
          )}
        </div>;
      })}</div>
    </aside>

    </>}
    <section className="conversation-panel">
      <header className="conversation-header"><div><span className="conversation-kicker">OmicsPrism Copilot</span><h2>{embedded ? "询问 Copilot" : activeThread?.title || "New conversation"}</h2></div><div className={`connection-state ${connection}`} title="Connection status">{connection === "live" ? <Wifi size={15} /> : <WifiOff size={15} />}{statusLabel}</div>{onClose && <button type="button" className="icon-button" aria-label="关闭 Copilot" onClick={onClose}><X size={19} /></button>}</header>
      {context && <div className="result-copilot-context" aria-label="当前结果上下文">
        <strong>当前分析：{context.analysisType.toUpperCase()}</strong>
        <span>当前任务：{context.jobId}</span>
        <span>当前结果：{context.title}</span>
        {context.artifact && <code>{context.artifact}</code>}
        <small>关闭后保留对话；刷新或离开页面后重新打开将新建对话。历史对话可在 Copilot 页面查看。</small>
      </div>}
      <div className="message-scroll" aria-live="polite">
        {loading ? <EmptyState loading /> : messages.length === 0 ? <EmptyState /> : messages.map(message => <article className={`copilot-message ${message.role}`} key={message.message_id}><div className="message-author">{message.role === "assistant" ? <Bot size={16} /> : null}{message.role === "assistant" ? "Copilot" : "You"}</div><div className="message-body"><MessageBlocks message={message} onRetry={() => setDraft(latestUserText(messages))} /></div></article>)}
        {pendingGraph && <GraphInterruptPanel interrupt={pendingGraph.interrupt} busy={graphBusy} onResume={request => void resumeGraph(request)} />}
        {activeJobWaits.map(wait => <JobWaitingState key={wait.wait_id} wait={wait} canceling={cancelingWaitId === wait.wait_id} onCancel={() => void cancelJobWait(wait)} />)}
        {pendingTurn && !pendingGraph && <div className="working-state"><span /><span /><span /><em>Working on your request</em></div>}
        {notice && <div className="copilot-notice" role="alert"><AlertCircle size={17} /><span>{notice}</span><button type="button" aria-label="Dismiss" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        <div ref={messageEnd} />
      </div>
      <div className="composer-zone">
        {context && <div className="context-suggestions" aria-label="建议问题">{RESULT_SUGGESTIONS[context.analysisType].map(question => <button key={question} type="button" onClick={() => setDraft(question)}>{question}</button>)}</div>}
        {attachments.length > 0 && <div className="attachment-tray">{attachments.map(item => <div className="attachment-item" key={item.id}><FilePlus2 size={16} /><span title={item.file.name}>{item.file.name}</span><label>Role<select value={item.field} title={INPUT_FIELD_DESCRIPTIONS[item.field]} aria-label={`Role for ${item.file.name}`} onChange={event => setAttachments(current => current.map(entry => entry.id === item.id ? { ...entry, field: event.target.value as InputField } : entry))}>{INPUT_FIELDS.map(field => <option key={field} value={field}>{INPUT_FIELD_LABELS[field]}</option>)}</select><span className="data-type-help attachment-help" tabIndex={0} aria-label={`About ${INPUT_FIELD_LABELS[item.field]}`}><CircleHelp size={14} aria-hidden="true" /><span className="data-type-tooltip" role="tooltip">{INPUT_FIELD_DESCRIPTIONS[item.field]}</span></span><ChevronDown size={14} /></label><button type="button" aria-label={`Remove ${item.file.name}`} onClick={() => setAttachments(current => current.filter(entry => entry.id !== item.id))}><X size={15} /></button></div>)}</div>}
        <div className="composer"><input ref={fileInput} hidden type="file" multiple accept=".csv,text/csv" onChange={event => { addFiles(event.target.files); event.target.value = ""; }} /><button type="button" className="icon-button" title="Attach CSV files" aria-label="Attach CSV files" onClick={() => fileInput.current?.click()}><Paperclip size={19} /></button><textarea value={draft} rows={1} placeholder="Ask about an analysis or result" aria-label="Message Copilot" onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />{pendingTurn ? <button type="button" className="stop-composer-button" aria-label="Stop request" title="Stop request" disabled={canceling || !pendingTurnRecord} onClick={() => void cancelPendingTurn()}><Square size={17} fill="currentColor" /></button> : <button type="button" className="send-button" aria-label="Send message" title="Send message" disabled={!draft.trim() || sending || loading} onClick={() => void send()}><Send size={18} /></button>}</div>
      </div>
    </section>

    {!embedded && <aside className="copilot-context" aria-label="Conversation context"><section><span className="context-label">Checkpoint</span><strong>{checkpointLabel}</strong><p>Graph checkpoint</p></section><section><span className="context-label">Focused jobs</span>{focusIds.length ? <ul>{focusIds.map(id => <li key={id}><a href={`/jobs/${encodeURIComponent(id)}`}>{id.slice(0, 8)}...</a></li>)}</ul> : <p>No job selected</p>}</section><section><span className="context-label">Input roles</span><p>Assign each CSV its role before sending. Copilot validates the files before confirmation.</p></section><TracePanel events={traceEvents} loading={traceLoading} onRefresh={() => { if (activeId) void refreshTrace(activeId); }} /></aside>}
    {!embedded && railOpen && <button className="rail-backdrop" type="button" aria-label="Close conversations" onClick={() => setRailOpen(false)} />}
  </div>;
}

function JobWaitingState({ wait, canceling, onCancel }: { wait: AgentJobWaitResponse; canceling: boolean; onCancel: () => void }) {
  const queued = wait.job_status === "queued";
  const running = wait.job_status === "running";
  const failed = wait.job_status === "failed" || wait.job_status === "cancelled";
  const title = wait.wait_status === "resume_queued"
    ? "Preparing the analysis result"
    : failed
      ? "Analysis finished with an issue"
      : queued
        ? "Waiting for the analysis worker"
        : running
          ? "Analysis is running"
          : "Waiting for the analysis job";
  const Icon = failed ? CircleAlert : wait.wait_status === "resume_queued" ? LoaderCircle : running ? Clock3 : queued ? Clock3 : CheckCircle2;
  const progress = wait.progress == null ? null : Math.max(0, Math.min(100, wait.progress));
  return <section className={`job-waiting${failed ? " failed" : ""}`} aria-live="polite">
    <div className="job-waiting-heading"><Icon size={17} className={wait.wait_status === "resume_queued" || running ? "spinning" : ""} /><div><strong>{title}</strong><small>Job {wait.job_id.slice(0, 12)}...</small></div>{progress != null && <b>{progress}%</b>}</div>
    {progress != null && <div className="job-progress" aria-label={`Analysis progress ${progress}%`}><span style={{ width: `${progress}%` }} /></div>}
    <p>{wait.progress_step || (failed ? wait.error || "The analysis job did not complete successfully." : "The Copilot will continue when the analysis is complete.")}</p>
    {!failed && <button type="button" className="stop-request" disabled={canceling} onClick={onCancel} title="Stop waiting for this job"><Square size={14} />{canceling ? "Stopping..." : "Stop waiting"}</button>}
  </section>;
}

function EmptyState({ loading = false }: { loading?: boolean }) {
  return <div className="copilot-empty"><div className="empty-mark"><Bot size={25} /></div><h2>{loading ? "Opening your workspace" : "What are you investigating?"}</h2><p>{loading ? "Restoring conversations and current state." : "Describe your data or attach CSV inputs. You will confirm validated parameters before a job is created."}</p></div>;
}

function upsert<T extends Record<K, string>, K extends keyof T>(items: T[], incoming: T, key: K): T[] { const found = items.findIndex(item => item[key] === incoming[key]); return found < 0 ? [...items, incoming] : items.map((item, index) => index === found ? incoming : item); }
function errorMessage(error: unknown) { if (error instanceof ApiRequestError) return error.status === 404 ? "This conversation is unavailable." : error.message; return navigator.onLine ? "Copilot is temporarily unavailable. Try again." : "You are offline. Reconnect to continue."; }
function latestUserText(messages: AgentMessageResponse[]) { for (const message of [...messages].reverse()) { if (message.role !== "user") continue; const block = message.blocks.find(item => item.type === "text"); if (block?.type === "text") return block.text; } return ""; }
function guessField(name: string): InputField {
  const value = name.toLowerCase();
  return INPUT_FIELDS.find(field => value.includes(field))
    || (value.includes("count") ? "counts"
      : value.includes("metab") ? "metabolome"
      : value.includes("deat") || value.includes("transcript") || value.includes("gene_expression")
        ? "transcriptome"
        : value.includes("group") ? "group" : "metadata");
}
function relativeTime(value: string) { const date = new Date(value); if (Number.isNaN(date.valueOf())) return ""; const days = Math.floor((Date.now() - date.valueOf()) / 86400000); return days < 1 ? "Today" : days === 1 ? "Yesterday" : `${days}d ago`; }
