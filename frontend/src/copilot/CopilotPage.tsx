import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Bot, ChevronDown, FilePlus2, Menu, MessageSquarePlus, Paperclip, Send, Square, Trash2, Wifi, WifiOff, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import type { AgentMessageResponse, AgentRunResponse, AgentStreamEvent, AgentThreadResponse, AgentTurnResponse, GraphInterrupt, GraphPendingInterrupt, GraphTurnResult } from "../api-types";
import { ApiRequestError } from "../api";
import { createClientId } from "../clientId";
import { agentApi, isGraphTurnResult } from "./agentApi";
import type { GraphResumeRequest } from "./agentApi";
import { GraphInterruptPanel } from "./GraphInterruptPanel";
import { MessageBlocks } from "./MessageBlocks";
import "./copilot.css";

const INPUT_FIELDS = ["counts", "metadata", "metabs", "transcriptome", "metabolome", "group"];
type Attachment = { id: string; file: File; field: string };
type PendingGraph = GraphPendingInterrupt;

export default function CopilotPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const focusJobId = searchParams.get("job_id");
  const [threads, setThreads] = useState<AgentThreadResponse[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRunResponse | null>(null);
  const [messages, setMessages] = useState<AgentMessageResponse[]>([]);
  const [turns, setTurns] = useState<AgentTurnResponse[]>([]);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [pendingGraph, setPendingGraph] = useState<PendingGraph | null>(null);
  const [graphBusy, setGraphBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [connection, setConnection] = useState<"live" | "recovering" | "offline">("recovering");
  const [railOpen, setRailOpen] = useState(false);
  const initialized = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const messageEnd = useRef<HTMLDivElement>(null);

  const recover = useCallback(async (threadId: string) => {
    const [detail, messageList, turnList, interrupt] = await Promise.all([
      agentApi.getThread(threadId),
      agentApi.listMessages(threadId),
      agentApi.listTurns(threadId),
      agentApi.getPendingInterrupt(threadId),
    ]);
    setRun(detail.run); setMessages(messageList.messages); setTurns(turnList.turns);
    setPendingGraph(interrupt);
  }, []);

  const createThread = useCallback(async (focusIds: string[] = []) => {
    const created = await agentApi.createThread(focusIds);
    setThreads(current => [created, ...current.filter(item => item.thread_id !== created.thread_id)]);
    setPendingGraph(null); setGraphBusy(false);
    setActiveId(created.thread_id); setRailOpen(false); setNotice(null);
    if (focusIds.length) setSearchParams({}, { replace: true });
    return created.thread_id;
  }, [setSearchParams]);

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
      const payload = JSON.parse(event.data) as AgentStreamEvent;
      setConnection("live");
      if (payload.event_type === "message.created") setMessages(current => upsert(current, payload.data as AgentMessageResponse, "message_id"));
      else if (payload.event_type === "turn.updated") {
        const turn = payload.data as AgentTurnResponse;
        setTurns(current => upsert(current, turn, "turn_id"));
        if (turn.status === "queued" || turn.status === "completed" || turn.status === "failed" || turn.status === "cancelled") {
          setPendingGraph(current => current?.checkpoint_turn_id === turn.turn_id ? null : current);
        }
      } else {
        setPendingGraph(payload.data as GraphPendingInterrupt | null);
      }
    };
    source.addEventListener("message.created", accept);
    source.addEventListener("turn.updated", accept);
    source.addEventListener("interrupt.updated", accept);
    source.onopen = () => setConnection("live");
    source.onerror = () => {
      setConnection(navigator.onLine ? "recovering" : "offline");
      if (!fallback) fallback = window.setInterval(() => recover(activeId).catch(() => setConnection("offline")), 3000);
    };
    return () => { source.close(); if (fallback) window.clearInterval(fallback); };
  }, [activeId, recover]);

  useEffect(() => { messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages, pendingGraph]);

  const pendingTurn = turns.some(turn => turn.status === "queued" || turn.status === "running");
  const pendingTurnRecord = turns.find(turn => turn.status === "queued" || turn.status === "running");
  const focusIds = run?.focus.in_scope_job_ids ?? [];
  const checkpointLabel = run ? `Version ${run.version}` : "Ready";

  async function send() {
    const text = draft.trim();
    if (!text || sending || pendingTurn) return;
    setSending(true); setNotice(null);
    try {
      const threadId = activeId || await createThread(focusJobId ? [focusJobId] : []);
      const bundle = attachments.length ? await agentApi.uploadBundle(threadId, attachments) : null;
      const result = await agentApi.createTurn(threadId, text, bundle?.bundle_id ?? null, focusIds);
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

  async function deleteConversation(threadId: string) {
    if (!window.confirm("Delete this conversation and its stored history?")) return;
    try {
      await agentApi.deleteThread(threadId);
      const remaining = threads.filter(item => item.thread_id !== threadId);
      setThreads(remaining);
      if (activeId === threadId) {
        setActiveId(remaining[0]?.thread_id ?? null);
        if (!remaining.length) await createThread();
      }
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

  return <main className="copilot-page">
    <button className="copilot-rail-toggle" type="button" aria-label="Open conversations" onClick={() => setRailOpen(true)}><Menu size={20} /></button>
    <aside className={`copilot-rail${railOpen ? " rail-open" : ""}`} aria-label="Conversations">
      <div className="rail-heading"><div><span>Workspace</span><h1>Copilot</h1></div><button type="button" title="Close conversations" aria-label="Close conversations" onClick={() => setRailOpen(false)}><X size={19} /></button></div>
      <button type="button" className="new-thread" onClick={() => void createThread()}><MessageSquarePlus size={17} />New conversation</button>
      <div className="thread-list">{threads.map(thread => <div className={`thread-item${thread.thread_id === activeId ? " active" : ""}`} key={thread.thread_id}><button type="button" className="thread-select" onClick={() => { setPendingGraph(null); setGraphBusy(false); setActiveId(thread.thread_id); setRailOpen(false); }}><strong>{thread.title || "Untitled conversation"}</strong><time>{relativeTime(thread.updated_at)}</time></button><button type="button" className="thread-delete" title="Delete conversation" aria-label={`Delete ${thread.title || "conversation"}`} onClick={() => void deleteConversation(thread.thread_id)}><Trash2 size={15} /></button></div>)}</div>
    </aside>

    <section className="conversation-panel">
      <header className="conversation-header"><div><span className="conversation-kicker">OmicsPrism Copilot</span><h2>{activeThread?.title || "New conversation"}</h2></div><div className={`connection-state ${connection}`} title="Connection status">{connection === "live" ? <Wifi size={15} /> : <WifiOff size={15} />}{statusLabel}</div></header>
      <div className="message-scroll" aria-live="polite">
        {loading ? <EmptyState loading /> : messages.length === 0 ? <EmptyState /> : messages.map(message => <article className={`copilot-message ${message.role}`} key={message.message_id}><div className="message-author">{message.role === "assistant" ? <Bot size={16} /> : null}{message.role === "assistant" ? "Copilot" : "You"}</div><div className="message-body"><MessageBlocks message={message} onRetry={() => setDraft(latestUserText(messages))} /></div></article>)}
        {pendingGraph && <GraphInterruptPanel interrupt={pendingGraph.interrupt} busy={graphBusy} onResume={request => void resumeGraph(request)} />}
        {pendingTurn && !pendingGraph && <div className="working-state"><span /><span /><span /><em>Working on your request</em><button type="button" className="stop-request" disabled={canceling || !pendingTurnRecord} onClick={() => void cancelPendingTurn()} title="Stop request"><Square size={14} />Stop</button></div>}
        {notice && <div className="copilot-notice" role="alert"><AlertCircle size={17} /><span>{notice}</span><button type="button" aria-label="Dismiss" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        <div ref={messageEnd} />
      </div>
      <div className="composer-zone">
        {attachments.length > 0 && <div className="attachment-tray">{attachments.map(item => <div className="attachment-item" key={item.id}><FilePlus2 size={16} /><span title={item.file.name}>{item.file.name}</span><label>Role<select value={item.field} onChange={event => setAttachments(current => current.map(entry => entry.id === item.id ? { ...entry, field: event.target.value } : entry))}>{INPUT_FIELDS.map(field => <option key={field}>{field}</option>)}</select><ChevronDown size={14} /></label><button type="button" aria-label={`Remove ${item.file.name}`} onClick={() => setAttachments(current => current.filter(entry => entry.id !== item.id))}><X size={15} /></button></div>)}</div>}
        <div className="composer"><input ref={fileInput} hidden type="file" multiple accept=".csv,text/csv" onChange={event => { addFiles(event.target.files); event.target.value = ""; }} /><button type="button" className="icon-button" title="Attach CSV files" aria-label="Attach CSV files" onClick={() => fileInput.current?.click()}><Paperclip size={19} /></button><textarea value={draft} rows={1} placeholder="Ask about an analysis or result" aria-label="Message Copilot" onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} /><button type="button" className="send-button" aria-label="Send message" title="Send message" disabled={!draft.trim() || sending || pendingTurn} onClick={() => void send()}><Send size={18} /></button></div>
      </div>
    </section>

    <aside className="copilot-context" aria-label="Conversation context"><section><span className="context-label">Checkpoint</span><strong>{checkpointLabel}</strong><p>Graph checkpoint</p></section><section><span className="context-label">Focused jobs</span>{focusIds.length ? <ul>{focusIds.map(id => <li key={id}><a href={`/jobs/${encodeURIComponent(id)}`}>{id.slice(0, 8)}...</a></li>)}</ul> : <p>No job selected</p>}</section><section><span className="context-label">Input roles</span><p>Assign each CSV its role before sending. Copilot validates the files before confirmation.</p></section></aside>
    {railOpen && <button className="rail-backdrop" type="button" aria-label="Close conversations" onClick={() => setRailOpen(false)} />}
  </main>;
}

function EmptyState({ loading = false }: { loading?: boolean }) {
  return <div className="copilot-empty"><div className="empty-mark"><Bot size={25} /></div><h2>{loading ? "Opening your workspace" : "What are you investigating?"}</h2><p>{loading ? "Restoring conversations and current state." : "Describe your data or attach CSV inputs. You will confirm validated parameters before a job is created."}</p></div>;
}

function upsert<T extends Record<K, string>, K extends keyof T>(items: T[], incoming: T, key: K): T[] { const found = items.findIndex(item => item[key] === incoming[key]); return found < 0 ? [...items, incoming] : items.map((item, index) => index === found ? incoming : item); }
function errorMessage(error: unknown) { if (error instanceof ApiRequestError) return error.status === 404 ? "This conversation is unavailable." : error.message; return navigator.onLine ? "Copilot is temporarily unavailable. Try again." : "You are offline. Reconnect to continue."; }
function latestUserText(messages: AgentMessageResponse[]) { for (const message of [...messages].reverse()) { if (message.role !== "user") continue; const block = message.blocks.find(item => item.type === "text"); if (block?.type === "text") return block.text; } return ""; }
function guessField(name: string) { const value = name.toLowerCase(); return INPUT_FIELDS.find(field => value.includes(field)) || (value.includes("count") ? "counts" : "metadata"); }
function relativeTime(value: string) { const date = new Date(value); if (Number.isNaN(date.valueOf())) return ""; const days = Math.floor((Date.now() - date.valueOf()) / 86400000); return days < 1 ? "Today" : days === 1 ? "Yesterday" : `${days}d ago`; }
