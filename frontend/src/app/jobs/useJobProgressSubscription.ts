import { useEffect, useRef, useState } from "react";
import type { JobProgressResponse } from "../../api-types";
import {
  fetchJobProgress,
  isTerminalProgress,
  type ProgressConnectionMode,
  type ProgressConnectionState,
} from "./progress";

interface UseJobProgressSubscriptionResult {
  progress: JobProgressResponse | null;
  error: string | null;
  mode: ProgressConnectionMode;
  connectionState: ProgressConnectionState;
  reconnectAttempts: number;
}

const POLL_INTERVAL_MS = 3000;
const SSE_RECONNECT_BASE_MS = 1200;
const SSE_MAX_ATTEMPTS_BEFORE_FALLBACK = 3;

export function useJobProgressSubscription(jobId: string): UseJobProgressSubscriptionResult {
  const [progress, setProgress] = useState<JobProgressResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<ProgressConnectionMode>("sse");
  const [connectionState, setConnectionState] = useState<ProgressConnectionState>("connecting");
  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const progressRef = useRef<JobProgressResponse | null>(null);

  useEffect(() => {
    let active = true;
    let eventSource: EventSource | null = null;
    let reconnectTimer = 0;
    let pollTimer = 0;
    let abortController: AbortController | null = null;

    function updateProgress(next: JobProgressResponse) {
      progressRef.current = next;
      setProgress(next);
      setError(null);
    }

    function clearTimers() {
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(pollTimer);
      abortController?.abort();
      abortController = null;
    }

    function closeEventSource() {
      eventSource?.close();
      eventSource = null;
    }

    async function loadSnapshot() {
      abortController?.abort();
      abortController = new AbortController();
      try {
        const next = await fetchJobProgress(jobId, abortController.signal);
        if (!active) return;
        updateProgress(next);
      } catch (err) {
        if (!active) return;
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Failed to load job progress");
        }
      }
    }

    async function pollOnce() {
      await loadSnapshot();
      if (!active) return;
      setConnectionState(isTerminalProgress(progressRef.current) ? "closed" : "fallback");
      if (isTerminalProgress(progressRef.current)) return;
      if (active) pollTimer = window.setTimeout(pollOnce, POLL_INTERVAL_MS);
    }

    function startPolling() {
      closeEventSource();
      clearTimers();
      setMode("polling");
      setConnectionState("fallback");
      void pollOnce();
    }

    function scheduleSseReconnect(attempt: number) {
      if (!active) return;
      setReconnectAttempts(attempt);
      setConnectionState("recovering");
      if (attempt >= SSE_MAX_ATTEMPTS_BEFORE_FALLBACK) {
        startPolling();
        return;
      }
      const delay = Math.min(10_000, SSE_RECONNECT_BASE_MS * 2 ** Math.max(0, attempt - 1));
      reconnectTimer = window.setTimeout(() => startSse(attempt), delay);
    }

    function startSse(previousAttempts = 0) {
      closeEventSource();
      setMode("sse");
      setConnectionState(previousAttempts > 0 ? "recovering" : "connecting");

      try {
        eventSource = new EventSource(`/api/jobs/${jobId}/progress/events`, { withCredentials: true });
      } catch {
        startPolling();
        return;
      }

      eventSource.onopen = () => {
        if (!active) return;
        setConnectionState("open");
        setReconnectAttempts(0);
      };

      eventSource.addEventListener("progress", (event) => {
        if (!active) return;
        try {
          const next = JSON.parse((event as MessageEvent).data) as JobProgressResponse;
          updateProgress(next);
          if (isTerminalProgress(next)) {
            closeEventSource();
            setConnectionState("closed");
          }
        } catch {
          setError("Received malformed progress event");
        }
      });

      eventSource.addEventListener("complete", (event) => {
        if (!active) return;
        try {
          updateProgress(JSON.parse((event as MessageEvent).data) as JobProgressResponse);
        } catch {
          setError("Received malformed completion event");
        }
        closeEventSource();
        setConnectionState("closed");
      });

      eventSource.onerror = () => {
        if (!active) return;
        closeEventSource();
        if (isTerminalProgress(progressRef.current)) {
          setConnectionState("closed");
          return;
        }
        scheduleSseReconnect(previousAttempts + 1);
      };
    }

    void loadSnapshot();
    startSse();

    return () => {
      active = false;
      clearTimers();
      closeEventSource();
    };
  }, [jobId]);

  return { progress, error, mode, connectionState, reconnectAttempts };
}
