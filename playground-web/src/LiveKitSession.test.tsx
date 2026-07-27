import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LiveKitSession from "./LiveKitSession";

const sdk = vi.hoisted(() => ({
  handlers: new Map<string, (...args: unknown[]) => void>(),
  connect: vi.fn(async () => undefined),
  startAudio: vi.fn(async () => undefined),
  setMicrophoneEnabled: vi.fn(async () => undefined),
  disconnect: vi.fn(async () => undefined),
}));

vi.mock("livekit-client", () => ({
  RoomEvent: {
    ConnectionStateChanged: "connectionStateChanged",
    TrackSubscribed: "trackSubscribed",
    TrackUnsubscribed: "trackUnsubscribed",
    ActiveSpeakersChanged: "activeSpeakersChanged",
    TranscriptionReceived: "transcriptionReceived",
    Disconnected: "disconnected",
  },
  Track: { Kind: { Audio: "audio" } },
  Room: class {
    localParticipant = {
      identity: "caller-id",
      setMicrophoneEnabled: sdk.setMicrophoneEnabled,
    };

    on(event: string, handler: (...args: unknown[]) => void) {
      sdk.handlers.set(event, handler);
      return this;
    }

    off(event: string) {
      sdk.handlers.delete(event);
      return this;
    }

    connect = sdk.connect;
    startAudio = sdk.startAudio;
    disconnect = sdk.disconnect;
  },
}));

beforeEach(() => {
  sdk.handlers.clear();
  sdk.connect.mockClear();
  sdk.startAudio.mockClear();
  sdk.setMicrophoneEnabled.mockClear();
  sdk.disconnect.mockClear();
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          server_url: "wss://project.livekit.cloud",
          participant_token: "participant-token",
          room_name: "web-room",
          participant_identity: "caller-id",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LiveKitSession", () => {
  it("exchanges, connects, publishes the mic, streams final text, and disconnects", async () => {
    const onState = vi.fn();
    const onEvent = vi.fn();
    const onTranscript = vi.fn();
    const onEnd = vi.fn();
    const onError = vi.fn();
    render(
      <LiveKitSession
        session={{
          session_id: "web-1",
          runtime: "livekit",
          token: "voicekit-token",
          token_url: "http://127.0.0.1:7861/api/livekit/token",
          poll_url: "/api/playground/sessions/web-1",
          expires_at: 4_102_444_800,
        }}
        onState={onState}
        onEvent={onEvent}
        onTranscript={onTranscript}
        onEnd={onEnd}
        onError={onError}
      />,
    );

    await waitFor(() => {
      expect(sdk.connect).toHaveBeenCalledWith(
        "wss://project.livekit.cloud",
        "participant-token",
        { autoSubscribe: true },
      );
      expect(sdk.setMicrophoneEnabled).toHaveBeenCalledWith(
        true,
        expect.objectContaining({ echoCancellation: true }),
      );
    });
    await act(async () => {
      sdk.handlers.get("connectionStateChanged")?.("connected");
      sdk.handlers.get("transcriptionReceived")?.(
        [
          {
            id: "segment-1",
            text: "Hello from the agent.",
            final: true,
            startTime: 120,
          },
        ],
        { identity: "voicekit-agent" },
      );
    });
    expect(onState).toHaveBeenCalledWith("connected");
    expect(onTranscript).toHaveBeenCalledWith(
      expect.objectContaining({
        role: "assistant",
        text: "Hello from the agent.",
        t_ms: 120,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "End session" }));
    await waitFor(() => {
      expect(sdk.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
      expect(sdk.disconnect).toHaveBeenCalledWith(true);
      expect(onEnd).toHaveBeenCalledOnce();
      expect(onError).not.toHaveBeenCalled();
    });
  });
});
