import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Bot, ChevronDown, FilePlus2, Menu, MessageSquarePlus, Paperclip, Plus, Send, Wifi, WifiOff, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import type { AgentMessageResponse, AgentRunResponse, AgentStreamEvent, AgentThreadResponse, AgentTurnResponse } from "../api-types";
import { ApiRequestError } from "../api";
import { createClientId } from "../clientId";
import { agentApi } from "./agentApi";
import { MessageBlocks } from "./MessageBlocks";
import "./copilot.css";

const INPUT_FIELDS = ["counts", "metadata", "metabs", "transcriptome", "metabolome", "group"];
type Attachment = { id: string; file: File; field: string };

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
  const [approvalBusy, setApprovalBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [connection, setConnection] = useState<"live" | "recovering" | "offline">("recovering");
  const [railOpen, setRailOpen] = useState(false);
  const initialized = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const messageEnd = useRef<HTMLDivElement>(null);

  const recover = useCallback(async (threadId: string) => {
    const [detail, messageList, turnList] = await Promise.all([agentApi.getThread(threadId), agentApi.listMessages(threadId), agentApi.listTurns(threadId)]);
    setRun(detail.run); setMessages(messageList.messages); setTurns(turnList.turns);
  }, []);

  const createThread = useCallback(async (focusIds: string[] = []) => {
    const created = await agentApi.createThread(focusIds);
    setThreads(current => [created, ...current.filter(item => item.thread_id !== created.thread_id)]);
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
    setLoading(true); setNotice(null);
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
      else setTurns(current => upsert(current, payload.data as AgentTurnResponse, "turn_id"));
    };
    source.addEventListener("message.created", accept);
    source.addEventListener("turn.updated", accept);
    source.onopen = () => setConnection("live");
    source.onerror = () => {
      setConnection(navigator.onLine ? "recovering" : "offline");
      if (!fallback) fallback = window.setInterval(() => recover(activeId).catch(() => setConnection("offline")), 3000);
    };
    return () => { source.close(); if (fallback) window.clearInterval(fallback); };
  }, [activeId, recover]);

  useEffect(() => { messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages]);

  const pendingTurn = turns.some(turn => turn.status === "queued" || turn.status === "running");
  const focusIds = run?.focus.in_scope_job_ids ?? [];

  async function send() {
    const text = draft.trim();
    if (!text || sending || pendingTurn) return;
    setSending(true); setNotice(null);
    try {
      const threadId = activeId || await createThread(focusJobId ? [focusJobId] : []);
      const bundle = attachments.length ? await agentApi.uploadBundle(threadId, attachments) : null;
      const turn = await agentApi.createTurn(threadId, text, bundle?.bundle_id ?? null, focusIds);
      setDraft(""); setAttachments([]); setTurns(current => upsert(current, turn, "turn_id"));
      await recover(threadId);
    } catch (error) { setNotice(errorMessage(error)); }
    finally { setSending(false); }
  }

  async function decide(approvalId: string, decision: "approve" | "reject", planHash: string) {
    if (!activeId || approvalBusy) return;
    setApprovalBusy(approvalId); setNotice(null);
    try { await agentApi.decideApproval(activeId, approvalId, decision, planHash); await recover(activeId); }
    catch (error) { setNotice(errorMessage(error)); }
    finally { setApprovalBusy(null); }
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
      <div className="thread-list">{threads.map(thread => <button type="button" key={thread.thread_id} className={thread.thread_id === activeId ? "active" : ""} onClick={() => { setActiveId(thread.thread_id); setRailOpen(false); }}><strong>{thread.title || "Untitled conversation"}</strong><time>{relativeTime(thread.updated_at)}</time></button>)}</div>
    </aside>

    <section className="conversation-panel">
      <header className="conversation-header"><div><span className="conversation-kicker">OmicsPrism Copilot</span><h2>{activeThread?.title || "New conversation"}</h2></div><div className={`connection-state ${connection}`} title="Connection status">{connection === "live" ? <Wifi size={15} /> : <WifiOff size={15} />}{statusLabel}</div></header>
      <div className="message-scroll" aria-live="polite">
        {loading ? <EmptyState loading /> : messages.length === 0 ? <EmptyState /> : messages.map(message => <article className={`copilot-message ${message.role}`} key={message.message_id}><div className="message-author">{message.role === "assistant" ? <Bot size={16} /> : null}{message.role === "assistant" ? "Copilot" : "You"}</div><div className="message-body"><MessageBlocks message={message} approvalBusy={approvalBusy} onApproval={decide} onRetry={() => setDraft("Please retry the last step.")} /></div></article>)}
        {pendingTurn && <div className="working-state"><span /><span /><span /><em>Working on your request</em></div>}
        {notice && <div className="copilot-notice" role="alert"><AlertCircle size={17} /><span>{notice}</span><button type="button" aria-label="Dismiss" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        <div ref={messageEnd} />
      </div>
      <div className="composer-zone">
        {attachments.length > 0 && <div className="attachment-tray">{attachments.map(item => <div className="attachment-item" key={item.id}><FilePlus2 size={16} /><span title={item.file.name}>{item.file.name}</span><label>Role<select value={item.field} onChange={event => setAttachments(current => current.map(entry => entry.id === item.id ? { ...entry, field: event.target.value } : entry))}>{INPUT_FIELDS.map(field => <option key={field}>{field}</option>)}</select><ChevronDown size={14} /></label><button type="button" aria-label={`Remove ${item.file.name}`} onClick={() => setAttachments(current => current.filter(entry => entry.id !== item.id))}><X size={15} /></button></div>)}</div>}
        <div className="composer"><input ref={fileInput} hidden type="file" multiple accept=".csv,text/csv" onChange={event => { addFiles(event.target.files); event.target.value = ""; }} /><button type="button" className="icon-button" title="Attach CSV files" aria-label="Attach CSV files" onClick={() => fileInput.current?.click()}><Paperclip size={19} /></button><textarea value={draft} rows={1} placeholder="Ask about an analysis or result" aria-label="Message Copilot" onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} /><button type="button" className="send-button" aria-label="Send message" title="Send message" disabled={!draft.trim() || sending || pendingTurn} onClick={() => void send()}><Send size={18} /></button></div>
      </div>
    </section>

    <aside className="copilot-context" aria-label="Conversation context"><section><span className="context-label">Profile</span><strong>{run?.active_profile === "interpretation" ? "Result interpretation" : "Analysis planning"}</strong><p>{run?.state ? run.state.replace(/_/g, " ").toLowerCase() : "Ready"}</p></section><section><span className="context-label">Focused jobs</span>{focusIds.length ? <ul>{focusIds.map(id => <li key={id}><a href={`/jobs/${encodeURIComponent(id)}`}>{id.slice(0, 8)}...</a></li>)}</ul> : <p>No job selected</p>}</section><section><span className="context-label">Input roles</span><p>Assign each CSV its role before sending. Copilot validates the files before proposing a plan.</p></section></aside>
    {railOpen && <button className="rail-backdrop" type="button" aria-label="Close conversations" onClick={() => setRailOpen(false)} />}
  </main>;
}

function EmptyState({ loading = false }: { loading?: boolean }) {
  return <div className="copilot-empty"><div className="empty-mark"><Bot size={25} /></div><h2>{loading ? "Opening your workspace" : "What are you investigating?"}</h2><p>{loading ? "Restoring conversations and current state." : "Describe your data or attach CSV inputs. You will review every analysis plan before a job is created."}</p></div>;
}

function upsert<T extends Record<K, string>, K extends keyof T>(items: T[], incoming: T, key: K): T[] { const found = items.findIndex(item => item[key] === incoming[key]); return found < 0 ? [...items, incoming] : items.map((item, index) => index === found ? incoming : item); }
function errorMessage(error: unknown) { if (error instanceof ApiRequestError) return error.status === 404 ? "This conversation is unavailable." : error.message; return navigator.onLine ? "Copilot is temporarily unavailable. Try again." : "You are offline. Reconnect to continue."; }
function guessField(name: string) { const value = name.toLowerCase(); return INPUT_FIELDS.find(field => value.includes(field)) || (value.includes("count") ? "counts" : "metadata"); }
function relativeTime(value: string) { const date = new Date(value); if (Number.isNaN(date.valueOf())) return ""; const days = Math.floor((Date.now() - date.valueOf()) / 86400000); return days < 1 ? "Today" : days === 1 ? "Yesterday" : `${days}d ago`; }
