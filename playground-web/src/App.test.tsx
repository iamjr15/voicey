import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

vi.mock("./PipecatSession", () => ({
  default: ({ onEnd, onError }: { onEnd: () => void; onError: () => void }) => (
    <>
      <button type="button" onClick={onEnd}>
        End session
      </button>
      <button type="button" onClick={onError}>
        Simulate media failure
      </button>
    </>
  ),
}));

vi.mock("./LiveKitSession", () => ({
  default: ({ onEnd }: { onEnd: () => void }) => (
    <button type="button" onClick={onEnd}>
      End LiveKit session
    </button>
  ),
}));

const bootstrap = {
  agent: "support-desk",
  runtime: "pipecat",
  public_origin: "http://127.0.0.1:7861",
  models: {
    stt: "deepgram/nova-3",
    llm: "anthropic/claude-sonnet-5",
    tts: "cartesia/sonic-3.5",
    fallbacks: {},
  },
  reload: { revision: 0, state: "ready", message: null },
};

const session = {
  session_id: "web_test",
  token: "header.payload.signature",
  expires_at: 4_102_444_800,
  runtime: "pipecat",
  webrtc_url: "http://127.0.0.1:7861/api/offer",
  poll_url: "/api/playground/sessions/web_test",
};

const snapshot = {
  session: {
    session_id: "web_test",
    expires_at: 4_102_444_800,
    call_id: "call_test",
    pc_id: "pc_test",
    active: true,
  },
  call: {
    call_id: "call_test",
    status: "active",
    started_at: "2026-07-27T12:00:00Z",
    ended_at: null,
    terminal_reason: null,
    timeline: [
      {
        event_type: "call.connected",
        occurred_at: "2026-07-27T12:00:01Z",
        details: {},
      },
    ],
    transcript: [
      {
        turn_id: "turn_1",
        role: "user",
        text: "I need to move my appointment.",
        t_ms: 1600,
      },
      {
        turn_id: "turn_2",
        role: "assistant",
        text: "I can help with that.",
        t_ms: 2200,
      },
    ],
    tool_calls: [
      {
        invocation_id: "tool_1",
        tool_name: "find_appointment",
        arguments: { account: "north" },
        result: { found: true },
        duration_ms: 42,
        status: "succeeded",
        occurred_at: "2026-07-27T12:00:02Z",
      },
    ],
    latency: [
      {
        turn_id: "turn_2",
        turn_index: 1,
        metric: "e2e",
        duration_ms: 740,
        observed_at: "2026-07-27T12:00:03Z",
      },
      {
        turn_id: "turn_2",
        turn_index: 1,
        metric: "llm_ttft",
        duration_ms: 310,
        observed_at: "2026-07-27T12:00:03Z",
      },
    ],
  },
  data: {
    outcome: null,
    data: { requested_action: "reschedule" },
    interruptions: 1,
  },
  recording: null,
  terminal_event: null,
  reload: { revision: 2, state: "ready", message: "configuration loaded" },
};

function jsonResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
    const path = String(input);
    if (path.endsWith("/bootstrap")) return jsonResponse(bootstrap);
    if (path.endsWith("/sessions")) return jsonResponse(session);
    if (path === session.poll_url) return jsonResponse(snapshot);
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("App", () => {
  it("renders a usable idle console without accessibility violations", async () => {
    const { container } = render(<App />);
    expect(await screen.findByText("support-desk")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start talking" })).toBeEnabled();
    expect(screen.getByText("Provider keys never enter this page.")).toBeInTheDocument();

    const result = await axe.run(container, {
      rules: {
        region: { enabled: false },
      },
    });
    expect(result.violations).toEqual([]);
  });

  it("shows durable transcript, latency, tools, data, and reload revision", async () => {
    render(<App />);
    const start = await screen.findByRole("button", { name: "Start talking" });
    fireEvent.click(start);

    expect(await screen.findByRole("button", { name: "End session" })).toBeInTheDocument();
    expect(await screen.findByText("I need to move my appointment.")).toBeInTheDocument();
    expect(screen.getByText("I can help with that.")).toBeInTheDocument();
    expect(screen.getAllByText("740 ms")).toHaveLength(2);
    expect(screen.getByText("find_appointment")).toBeInTheDocument();
    expect(screen.getByText("1 interruptions")).toBeInTheDocument();
    expect(screen.getByText("revision 2 loaded")).toBeInTheDocument();
    expect(screen.getByText(/requested_action/)).toBeInTheDocument();
  });

  it("selects the LiveKit media adapter behind the identical console", async () => {
    const livekitBootstrap = { ...bootstrap, runtime: "livekit" };
    const livekitSession = {
      session_id: "web_livekit",
      token: "header.payload.signature",
      expires_at: 4_102_444_800,
      runtime: "livekit",
      token_url: "http://127.0.0.1:7861/api/livekit/token",
      poll_url: "/api/playground/sessions/web_livekit",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/bootstrap")) return jsonResponse(livekitBootstrap);
        if (path.endsWith("/sessions")) return jsonResponse(livekitSession);
        if (path === livekitSession.poll_url) {
          return jsonResponse({
            ...snapshot,
            session: { ...snapshot.session, session_id: "web_livekit" },
          });
        }
        return new Response("not found", { status: 404 });
      }),
    );

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start talking" }));
    expect(
      await screen.findByRole("button", { name: "End LiveKit session" }),
    ).toBeInTheDocument();
    expect(screen.getByText("livekit")).toBeInTheDocument();
    expect(screen.getByText("Provider keys never enter this page.")).toBeInTheDocument();
  });

  it("ends a session and offers a fresh token", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start talking" }));
    const end = await screen.findByRole("button", { name: "End session" });
    await act(async () => fireEvent.click(end));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "New session" })).toBeEnabled();
    });
  });

  it("renders catalog errors with their exact next step", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/bootstrap")) return jsonResponse(bootstrap);
      return jsonResponse(
        {
          error: {
            code: "VY-WEB-003",
            detail: "retry after 8s.",
            fix: "Wait and try again.",
          },
        },
        429,
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start talking" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("VY-WEB-003");
    expect(alert).toHaveTextContent("retry after 8s.");
    expect(alert).toHaveTextContent("Wait and try again.");
  });

  it("unmounts failed media and offers a fresh authorized session", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start talking" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Simulate media failure" }),
    );

    expect(
      await screen.findByRole("button", { name: "Retry session" }),
    ).toBeEnabled();
    expect(screen.queryByRole("button", { name: "End session" })).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("VY-WEB-005");
  });
});
