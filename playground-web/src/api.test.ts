import { afterEach, describe, expect, it, vi } from "vitest";

import {
  exchangeLiveKitToken,
  getBootstrap,
  getSessionSnapshot,
  issueSession,
  PlaygroundApiError,
} from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("playground API", () => {
  it("uses same-origin admin routes and never puts the session token in a URL", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith("bootstrap")) {
        return new Response(
          JSON.stringify({
            agent: "support",
            runtime: "pipecat",
            public_origin: "http://127.0.0.1:7861",
            models: { stt: "a/b", llm: "c/d", tts: "e/f", fallbacks: {} },
            reload: { revision: 0, state: "ready", message: null },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (path.endsWith("sessions")) {
        expect(init?.method).toBe("POST");
        return new Response(
          JSON.stringify({
            session_id: "web_1",
            token: "header.payload.signature",
            expires_at: 1000,
            runtime: "pipecat",
            webrtc_url: "http://127.0.0.1:7861/api/offer",
            poll_url: "/api/playground/sessions/web_1",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      expect(path).toBe("/api/playground/sessions/web_1");
      expect(path).not.toContain("header.payload.signature");
      return new Response(
        JSON.stringify({
          session: {
            session_id: "web_1",
            expires_at: 1000,
            call_id: null,
            pc_id: null,
            active: false,
          },
          call: null,
          data: null,
          recording: null,
          terminal_event: null,
          reload: { revision: 0, state: "ready", message: null },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    expect((await getBootstrap()).agent).toBe("support");
    const session = await issueSession();
    expect(session.token).toBe("header.payload.signature");
    expect((await getSessionSnapshot(session.poll_url)).session.session_id).toBe("web_1");
  });

  it("exchanges a one-use Voicekit token in an authorization header", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      expect(String(input)).toBe("http://127.0.0.1:7861/api/livekit/token");
      expect(String(input)).not.toContain("header.payload.signature");
      expect(init?.method).toBe("POST");
      expect(init?.headers).toEqual(expect.objectContaining({
        authorization: "Bearer header.payload.signature",
      }));
      return new Response(
        JSON.stringify({
          server_url: "wss://project.livekit.cloud",
          participant_token: "lk-token",
          room_name: "web-room",
          participant_identity: "caller-id",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const token = await exchangeLiveKitToken({
      session_id: "web_1",
      token: "header.payload.signature",
      expires_at: 1000,
      runtime: "livekit",
      token_url: "http://127.0.0.1:7861/api/livekit/token",
      poll_url: "/api/playground/sessions/web_1",
    });
    expect(token.room_name).toBe("web-room");
  });

  it("surfaces catalog codes, details, and fixes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "VK-WEB-003",
              detail: "retry after 10s.",
              fix: "Wait before starting another session.",
            },
          }),
          { status: 429, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    await expect(issueSession()).rejects.toEqual(
      expect.objectContaining<Partial<PlaygroundApiError>>({
        code: "VK-WEB-003",
        message: "retry after 10s.",
        fix: "Wait before starting another session.",
      }),
    );
  });

  it("maps non-JSON proxy failures to the playground catalog", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(new Response("bad gateway", { status: 502 })),
    );

    await expect(getBootstrap()).rejects.toEqual(
      expect.objectContaining<Partial<PlaygroundApiError>>({
        code: "VK-WEB-005",
        message: "request failed with HTTP 502",
      }),
    );
  });
});
