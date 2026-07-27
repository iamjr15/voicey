export type ReloadStatus = {
  revision: number;
  state: "ready" | "reloading" | "restart_pending" | "error";
  message: string | null;
};

export type Bootstrap = {
  agent: string;
  runtime: "pipecat" | "livekit";
  public_origin: string;
  models: {
    stt: string;
    llm: string;
    tts: string;
    fallbacks: Record<string, string>;
  };
  reload: ReloadStatus;
};

export type IssuedSession = {
  session_id: string;
  token: string;
  expires_at: number;
  webrtc_url: string;
  poll_url: string;
};

export type TimelineEvent = {
  event_type: string;
  occurred_at: string;
  details: Record<string, unknown>;
};

export type TranscriptTurn = {
  turn_id: string;
  role: "user" | "assistant" | "system" | "tool";
  text: string;
  t_ms: number;
};

export type ToolCall = {
  invocation_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  result: unknown;
  duration_ms: number;
  status: "succeeded" | "failed" | "timed_out";
  occurred_at: string;
};

export type LatencySample = {
  turn_id: string;
  turn_index: number;
  metric: "stt_partial" | "stt_final" | "llm_ttft" | "tts_ttfb" | "e2e";
  duration_ms: number;
  observed_at: string;
};

export type CallRecord = {
  call_id: string;
  status: "active" | "completed" | "failed";
  started_at: string;
  ended_at: string | null;
  terminal_reason: string | null;
  timeline: TimelineEvent[];
  transcript: TranscriptTurn[];
  tool_calls: ToolCall[];
  latency: LatencySample[];
};

export type SessionSnapshot = {
  session: {
    session_id: string;
    expires_at: number;
    call_id: string | null;
    pc_id: string | null;
    active: boolean;
  };
  call: CallRecord | null;
  data: {
    outcome: string | null;
    data: Record<string, unknown>;
    interruptions: number;
  } | null;
  recording: {
    status: "pending" | "ready" | "failed";
    access_url: string | null;
  } | null;
  terminal_event: Record<string, unknown> | null;
  reload: ReloadStatus;
};

type ApiErrorBody = {
  error?: {
    code?: string;
    detail?: string;
    fix?: string;
  };
};

export class PlaygroundApiError extends Error {
  readonly code: string;
  readonly fix: string | null;

  constructor(code: string, message: string, fix: string | null = null) {
    super(message);
    this.name = "PlaygroundApiError";
    this.code = code;
    this.fix = fix;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      accept: "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // The status and catalog fallback remain actionable for non-JSON proxy errors.
    }
    const code = body.error?.code ?? "VK-WEB-005";
    const detail = body.error?.detail ?? `request failed with HTTP ${response.status}`;
    throw new PlaygroundApiError(code, detail, body.error?.fix ?? null);
  }
  return (await response.json()) as T;
}

export function getBootstrap(): Promise<Bootstrap> {
  return requestJson<Bootstrap>("/api/playground/bootstrap");
}

export function issueSession(signal?: AbortSignal): Promise<IssuedSession> {
  return requestJson<IssuedSession>("/api/playground/sessions", {
    method: "POST",
    signal,
  });
}

export function getSessionSnapshot(
  pollUrl: string,
  signal?: AbortSignal,
): Promise<SessionSnapshot> {
  return requestJson<SessionSnapshot>(pollUrl, { signal });
}
