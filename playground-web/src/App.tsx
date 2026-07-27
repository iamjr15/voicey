import type {
  BotOutputData,
  RTVIEventCallbacks,
  TranscriptData,
  TransportState,
} from "@pipecat-ai/client-js";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  getBootstrap,
  getSessionSnapshot,
  issueSession,
  PlaygroundApiError,
  type Bootstrap,
  type CallRecord,
  type IssuedSession,
  type LatencySample,
  type SessionSnapshot,
  type TranscriptTurn,
} from "./api";

type LocalEvent = {
  id: number;
  label: string;
  detail: string;
  at: Date;
  tone: "neutral" | "positive" | "warning";
};

type Phase = "idle" | "requesting" | "connecting" | "listening" | "ended" | "error";

const POLL_INTERVAL_MS = 450;
const PipecatSession = lazy(() => import("./PipecatSession"));
const LiveKitSession = lazy(() => import("./LiveKitSession"));

function displayModel(model: string): string {
  return model.includes("/") ? model.split("/").slice(1).join("/") : model;
}

function displayMetric(metric: LatencySample["metric"]): string {
  return {
    stt_partial: "STT partial",
    stt_final: "STT final",
    llm_ttft: "LLM first token",
    tts_ttfb: "TTS first byte",
    e2e: "mouth to ear",
  }[metric];
}

function formatLatency(durationMs: number): string {
  return durationMs < 1000 ? `${Math.round(durationMs)} ms` : `${(durationMs / 1000).toFixed(2)} s`;
}

function formatOffset(tMs: number): string {
  const seconds = Math.floor(tMs / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function catalogError(error: unknown): { code: string; message: string; fix: string | null } {
  if (error instanceof PlaygroundApiError) {
    return { code: error.code, message: error.message, fix: error.fix };
  }
  if (error instanceof Error) {
    return { code: "VK-WEB-005", message: error.message, fix: "Run voicekit doctor." };
  }
  return {
    code: "VK-WEB-005",
    message: "The playground could not complete the request.",
    fix: "Run voicekit doctor.",
  };
}

function useBootstrap(): {
  bootstrap: Bootstrap | null;
  error: ReturnType<typeof catalogError> | null;
} {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [error, setError] = useState<ReturnType<typeof catalogError> | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getBootstrap()
      .then(setBootstrap)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(catalogError(reason));
      });
    return () => controller.abort();
  }, []);

  return { bootstrap, error };
}

function useSessionPoll(session: IssuedSession | null): SessionSnapshot | null {
  const [snapshot, setSnapshot] = useState<SessionSnapshot | null>(null);

  useEffect(() => {
    if (session === null) {
      setSnapshot(null);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      try {
        const value = await getSessionSnapshot(session.poll_url, controller.signal);
        setSnapshot(value);
        if (
          value.terminal_event === null &&
          Date.now() / 1000 < session.expires_at &&
          !controller.signal.aborted
        ) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (!controller.signal.aborted && Date.now() / 1000 < session.expires_at) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS * 2);
        }
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [session]);

  return snapshot;
}

function StatusMark({
  current,
  label,
  state,
}: {
  current: Phase;
  label: string;
  state: Phase;
}) {
  const order: Phase[] = ["idle", "requesting", "connecting", "listening", "ended"];
  const currentIndex = order.indexOf(current);
  const stateIndex = order.indexOf(state);
  const status =
    current === "error"
      ? "waiting"
      : current === state
        ? "current"
        : currentIndex > stateIndex
          ? "complete"
          : "waiting";
  return (
    <li className={`state-step state-step--${status}`}>
      <span className="state-dot" aria-hidden="true" />
      <span>{label}</span>
    </li>
  );
}

function LatencyBadge({
  turn,
  latency,
}: {
  turn: TranscriptTurn;
  latency: LatencySample[];
}) {
  const samples = latency.filter((sample) => sample.turn_id === turn.turn_id);
  const e2e = samples.find((sample) => sample.metric === "e2e");
  if (e2e === undefined) return null;
  const tone =
    e2e.duration_ms <= 800 ? "fast" : e2e.duration_ms <= 1500 ? "moderate" : "slow";
  const detail = samples
    .map((sample) => `${displayMetric(sample.metric)} ${formatLatency(sample.duration_ms)}`)
    .join(", ");
  return (
    <span className={`latency-badge latency-badge--${tone}`} title={detail}>
      {formatLatency(e2e.duration_ms)}
    </span>
  );
}

function Transcript({
  call,
  liveTurns,
}: {
  call: CallRecord | null;
  liveTurns: TranscriptTurn[];
}) {
  const turns = call?.transcript.length ? call.transcript : liveTurns;
  const latency = call?.latency ?? [];
  return (
    <section className="panel transcript-panel" aria-labelledby="transcript-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Live record</p>
          <h2 id="transcript-title">Transcript</h2>
        </div>
        <span className="count-label">{turns.length} turns</span>
      </div>
      <div className="transcript-scroll" aria-live="polite" aria-relevant="additions text">
        {turns.length === 0 ? (
          <div className="blank-state">
            <span className="blank-wave" aria-hidden="true">
              <i />
              <i />
              <i />
              <i />
              <i />
            </span>
            <p>Start a session, then speak naturally.</p>
            <span>Final turns and measured latency appear here.</span>
          </div>
        ) : (
          turns.map((turn) => (
            <article className={`turn turn--${turn.role}`} key={turn.turn_id}>
              <div className="turn-meta">
                <span>{turn.role === "assistant" ? "Agent" : turn.role}</span>
                <time>{formatOffset(turn.t_ms)}</time>
                <LatencyBadge turn={turn} latency={latency} />
              </div>
              <p>{turn.text}</p>
            </article>
          ))
        )}
      </div>
    </section>
  );
}

function Signals({
  snapshot,
  events,
}: {
  snapshot: SessionSnapshot | null;
  events: LocalEvent[];
}) {
  const call = snapshot?.call;
  const latency = call?.latency ?? [];
  const timeline = call?.timeline ?? [];
  const toolCalls = call?.tool_calls ?? [];
  return (
    <aside className="signal-column" aria-label="Session signals">
      <section className="panel compact-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Measured</p>
            <h2>Latency</h2>
          </div>
        </div>
        {latency.length === 0 ? (
          <p className="quiet-copy">No completed turn yet.</p>
        ) : (
          <div className="metric-list">
            {latency.slice(-8).map((sample, index) => (
              <div className="metric-row" key={`${sample.turn_id}-${sample.metric}-${index}`}>
                <span>{displayMetric(sample.metric)}</span>
                <strong>{formatLatency(sample.duration_ms)}</strong>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="panel compact-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Sequence</p>
            <h2>Events</h2>
          </div>
          <span className="count-label">
            {snapshot?.data?.interruptions ?? 0} interruptions
          </span>
        </div>
        <ol className="event-feed">
          {events.length === 0 && timeline.length === 0 ? (
            <li className="quiet-copy">Connection events will stream here.</li>
          ) : (
            <>
              {events.slice(-5).map((event) => (
                <li key={event.id}>
                  <span className={`event-pip event-pip--${event.tone}`} aria-hidden="true" />
                  <div>
                    <strong>{event.label}</strong>
                    <span>{event.detail}</span>
                  </div>
                  <time>{event.at.toLocaleTimeString([], { minute: "2-digit", second: "2-digit" })}</time>
                </li>
              ))}
              {timeline.slice(-5).map((event, index) => (
                <li key={`${event.event_type}-${event.occurred_at}-${index}`}>
                  <span className="event-pip" aria-hidden="true" />
                  <div>
                    <strong>{event.event_type.replaceAll(".", " ")}</strong>
                    <span>{Object.keys(event.details).length ? JSON.stringify(event.details) : "runtime"}</span>
                  </div>
                </li>
              ))}
            </>
          )}
        </ol>
      </section>

      <section className="panel compact-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Execution</p>
            <h2>Tools</h2>
          </div>
          <span className="count-label">{toolCalls.length} calls</span>
        </div>
        {toolCalls.length === 0 ? (
          <p className="quiet-copy">Typed tool calls will appear with arguments and results.</p>
        ) : (
          <div className="tool-list">
            {toolCalls.slice(-4).map((toolCall) => (
              <details key={toolCall.invocation_id}>
                <summary>
                  <span>{toolCall.tool_name}</span>
                  <span className={`tool-status tool-status--${toolCall.status}`}>
                    {toolCall.status}
                  </span>
                </summary>
                <pre>{JSON.stringify({
                  arguments: toolCall.arguments,
                  result: toolCall.result,
                  duration_ms: toolCall.duration_ms,
                }, null, 2)}</pre>
              </details>
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}

function OutputPanel({ snapshot }: { snapshot: SessionSnapshot | null }) {
  const payload = snapshot?.terminal_event;
  const data = snapshot?.data;
  return (
    <section className="panel output-panel" aria-labelledby="output-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Durable output</p>
          <h2 id="output-title">{payload === null || payload === undefined ? "Live data" : "Terminal event"}</h2>
        </div>
        <span className={`integrity-label ${payload ? "integrity-label--ready" : ""}`}>
          <span aria-hidden="true">◆</span>
          {payload ? "exact webhook bytes" : "waiting for terminal commit"}
        </span>
      </div>
      <pre className="json-preview">
        {JSON.stringify(payload ?? data?.data ?? {}, null, 2)}
      </pre>
    </section>
  );
}

export function App() {
  const { bootstrap, error: bootstrapError } = useBootstrap();
  const [session, setSession] = useState<IssuedSession | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [sessionError, setSessionError] = useState<ReturnType<typeof catalogError> | null>(null);
  const [events, setEvents] = useState<LocalEvent[]>([]);
  const [liveTurns, setLiveTurns] = useState<TranscriptTurn[]>([]);
  const nextEventId = useRef(0);
  const snapshot = useSessionPoll(session);

  const addEvent = useCallback(
    (label: string, detail: string, tone: LocalEvent["tone"] = "neutral") => {
      nextEventId.current += 1;
      setEvents((current) => [
        ...current,
        { id: nextEventId.current, label, detail, at: new Date(), tone },
      ]);
    },
    [],
  );

  const callbacks = useMemo<RTVIEventCallbacks>(
    () => ({
      onTransportStateChanged: (state: TransportState) => {
        if (state === "connecting" || state === "authenticating") setPhase("connecting");
        if (state === "ready" || state === "connected") setPhase("listening");
        if (state === "error") {
          setPhase("error");
          setSessionError({
            code: "VK-WEB-005",
            message: "The browser media transport entered an error state.",
            fix: "Run voicekit doctor, then retry the session.",
          });
        }
        addEvent("Transport", state);
      },
      onConnected: () => addEvent("Media connected", "encrypted WebRTC path", "positive"),
      onBotReady: () => {
        setPhase("listening");
        addEvent("Agent ready", "microphone is live", "positive");
      },
      onDisconnected: () => {
        setPhase("ended");
        addEvent("Session ended", "media transport closed", "warning");
      },
      onUserStartedSpeaking: () => addEvent("Caller speaking", "input activity"),
      onUserStoppedSpeaking: () => addEvent("Caller stopped", "turn boundary"),
      onBotStartedSpeaking: () => addEvent("Agent speaking", "audio output"),
      onBotStoppedSpeaking: () => addEvent("Agent stopped", "output complete"),
      onUserTranscript: (data: TranscriptData) => {
        if (!data.final) return;
        setLiveTurns((current) => [
          ...current,
          {
            turn_id: `local-user-${data.timestamp}-${current.length}`,
            role: "user",
            text: data.text,
            t_ms: 0,
          },
        ]);
      },
      onBotOutput: (data: BotOutputData) => {
        if (!data.text.trim() || data.spoken_status === "in-progress") return;
        setLiveTurns((current) => [
          ...current,
          {
            turn_id: `local-agent-${data.segment_id ?? current.length}`,
            role: "assistant",
            text: data.text,
            t_ms: 0,
          },
        ]);
      },
      onError: (message) => {
        setPhase("error");
        setSessionError(catalogError(new Error(JSON.stringify(message))));
      },
    }),
    [addEvent],
  );

  const begin = async () => {
    setPhase("requesting");
    setSession(null);
    setSessionError(null);
    setEvents([]);
    setLiveTurns([]);
    try {
      const issued = await issueSession();
      if (bootstrap !== null && issued.runtime !== bootstrap.runtime) {
        throw new Error("The issued browser session does not match the configured runtime.");
      }
      setSession(issued);
      addEvent("Session authorized", "short-lived browser scope", "positive");
      setPhase("connecting");
    } catch (reason) {
      setPhase("error");
      setSessionError(catalogError(reason));
    }
  };
  const endSession = useCallback(() => setPhase("ended"), []);
  const mediaError = useCallback((error?: unknown) => {
    setPhase("error");
    setSessionError(
      error === undefined
        ? {
            code: "VK-WEB-005",
            message: "The browser media connection could not start.",
            fix: "Run voicekit doctor, then retry the session.",
          }
        : catalogError(error),
    );
  }, []);
  const liveKitState = useCallback(
    (state: string) => {
      if (state === "connecting" || state === "reconnecting") setPhase("connecting");
      if (state === "connected") setPhase("listening");
      if (state === "disconnected") setPhase("ended");
      addEvent("Transport", state);
    },
    [addEvent],
  );
  const liveKitTranscript = useCallback((turn: TranscriptTurn) => {
    setLiveTurns((current) =>
      current.some((item) => item.turn_id === turn.turn_id) ? current : [...current, turn],
    );
  }, []);

  const visibleError = bootstrapError ?? sessionError;
  const reload = snapshot?.reload ?? bootstrap?.reload;
  const call = snapshot?.call ?? null;
  const expired = session !== null && Date.now() / 1000 >= session.expires_at;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="wordmark" href="/" aria-label="voicekit playground home">
          <span className="wordmark-glyph" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>voicekit</span>
          <em>playground</em>
        </a>
        <div className="runtime-meta" aria-label="Runtime configuration">
          <span className="runtime-chip">
            <span className={`status-led status-led--${phase}`} aria-hidden="true" />
            {phase === "idle" ? "local" : phase}
          </span>
          <span>{bootstrap?.runtime ?? "loading runtime"}</span>
          <span className="meta-divider" aria-hidden="true" />
          <span>{bootstrap?.agent ?? "reading agent"}</span>
        </div>
      </header>

      <div className="workspace">
        <aside className="session-rail" aria-labelledby="session-title">
          <div>
            <p className="eyebrow">Current run</p>
            <h1 id="session-title">Voice session</h1>
            <p className="rail-copy">
              A protected browser call through the same engine path as production.
            </p>
          </div>

          <ol className="state-list" aria-label="Session progress">
            <StatusMark current={phase} state="idle" label="Ready" />
            <StatusMark current={phase} state="requesting" label="Authorize" />
            <StatusMark current={phase} state="connecting" label="Connect media" />
            <StatusMark current={phase} state="listening" label="Conversation" />
            <StatusMark current={phase} state="ended" label="Terminal result" />
          </ol>

          <div className="session-action">
            {session === null ||
            phase === "idle" ||
            phase === "ended" ||
            phase === "error" ||
            expired ? (
              <button
                className="session-button"
                type="button"
                onClick={() => void begin()}
                disabled={bootstrap === null || phase === "requesting"}
              >
                <span className="button-icon button-icon--mic" aria-hidden="true">●</span>
                {phase === "requesting"
                  ? "Authorizing…"
                  : phase === "ended"
                    ? "New session"
                    : phase === "error"
                      ? "Retry session"
                      : "Start talking"}
              </button>
            ) : (
              <Suspense
                fallback={
                  <button className="session-button" type="button" disabled>
                    Loading media…
                  </button>
                }
              >
                {session.runtime === "pipecat" ? (
                  <PipecatSession
                    key={session.session_id}
                    session={session}
                    callbacks={callbacks}
                    onEnd={endSession}
                    onError={mediaError}
                  />
                ) : (
                  <LiveKitSession
                    key={session.session_id}
                    session={session}
                    onState={liveKitState}
                    onEvent={addEvent}
                    onTranscript={liveKitTranscript}
                    onEnd={endSession}
                    onError={mediaError}
                  />
                )}
              </Suspense>
            )}
            <p className="privacy-note">
              <span aria-hidden="true">⌁</span>
              Provider keys never enter this page.
            </p>
          </div>

          <div className="model-stack" aria-label="Active models">
            <p className="eyebrow">Signal chain</p>
            {(["stt", "llm", "tts"] as const).map((axis) => (
              <div key={axis}>
                <span>{axis}</span>
                <strong>{bootstrap ? displayModel(bootstrap.models[axis]) : "—"}</strong>
              </div>
            ))}
          </div>
        </aside>

        <div className="main-stage">
          {visibleError && (
            <div className="error-banner" role="alert">
              <strong>{visibleError.code}</strong>
              <span>{visibleError.message}</span>
              {visibleError.fix && <small>{visibleError.fix}</small>}
            </div>
          )}
          {reload && reload.state !== "ready" && (
            <div className={`reload-banner reload-banner--${reload.state}`} role="status">
              <span aria-hidden="true">↻</span>
              <strong>{reload.state.replace("_", " ")}</strong>
              <span>{reload.message ?? "The next safe revision is loading."}</span>
            </div>
          )}
          {reload && reload.revision > 0 && reload.state === "ready" && (
            <p className="revision-marker" role="status">
              <span aria-hidden="true">✓</span> revision {reload.revision} loaded
            </p>
          )}

          <div className="conversation-grid">
            <Transcript call={call} liveTurns={liveTurns} />
            <Signals snapshot={snapshot} events={events} />
          </div>
          <OutputPanel snapshot={snapshot} />
        </div>
      </div>
    </main>
  );
}
