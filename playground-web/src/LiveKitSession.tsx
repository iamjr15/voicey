import {
  Room,
  RoomEvent,
  Track,
  type ConnectionState,
  type Participant,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
  type TranscriptionSegment,
} from "livekit-client";
import { memo, useEffect, useRef, useState } from "react";

import {
  exchangeLiveKitToken,
  type LiveKitIssuedSession,
  type TranscriptTurn,
} from "./api";

type SessionEventTone = "neutral" | "positive" | "warning";

function LiveKitSession({
  session,
  onState,
  onEvent,
  onTranscript,
  onEnd,
  onError,
}: {
  session: LiveKitIssuedSession;
  onState: (state: string) => void;
  onEvent: (label: string, detail: string, tone?: SessionEventTone) => void;
  onTranscript: (turn: TranscriptTurn) => void;
  onEnd: () => void;
  onError: (error: unknown) => void;
}) {
  const roomRef = useRef<Room | null>(null);
  const audioRootRef = useRef<HTMLDivElement | null>(null);
  const endingRef = useRef(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const room = new Room({
      adaptiveStream: true,
      dynacast: true,
      disconnectOnPageLeave: true,
      stopLocalTrackOnUnpublish: true,
    });
    roomRef.current = room;

    const connectionChanged = (state: ConnectionState) => {
      onState(state);
      if (state === "connected") {
        setReady(true);
        onEvent("Media connected", "LiveKit encrypted WebRTC room", "positive");
      } else if (state === "reconnecting") {
        onEvent("Media reconnecting", "LiveKit is recovering the room", "warning");
      }
    };
    const trackSubscribed = (
      track: RemoteTrack,
      _publication: RemoteTrackPublication,
      participant: RemoteParticipant,
    ) => {
      if (track.kind !== Track.Kind.Audio || audioRootRef.current === null) return;
      const element = track.attach();
      element.dataset.participant = participant.identity;
      audioRootRef.current.appendChild(element);
    };
    const trackUnsubscribed = (track: RemoteTrack) => {
      for (const element of track.detach()) element.remove();
    };
    const speakersChanged = (speakers: Participant[]) => {
      const localSpeaking = speakers.some(
        (participant) => participant.identity === room.localParticipant.identity,
      );
      const agentSpeaking = speakers.some(
        (participant) => participant.identity !== room.localParticipant.identity,
      );
      if (localSpeaking) onEvent("Caller speaking", "input activity");
      if (agentSpeaking) onEvent("Agent speaking", "audio output");
    };
    const transcriptionReceived = (
      segments: TranscriptionSegment[],
      participant?: Participant,
    ) => {
      const role =
        participant?.identity === room.localParticipant.identity ? "user" : "assistant";
      for (const segment of segments) {
        if (!segment.final || !segment.text.trim()) continue;
        onTranscript({
          turn_id: `livekit-${participant?.identity ?? role}-${segment.id}`,
          role,
          text: segment.text,
          t_ms: Math.max(0, Math.round(segment.startTime)),
        });
      }
    };
    const disconnected = () => {
      setReady(false);
      if (!endingRef.current) {
        onEvent("Session ended", "LiveKit room disconnected", "warning");
        onEnd();
      }
    };

    room
      .on(RoomEvent.ConnectionStateChanged, connectionChanged)
      .on(RoomEvent.TrackSubscribed, trackSubscribed)
      .on(RoomEvent.TrackUnsubscribed, trackUnsubscribed)
      .on(RoomEvent.ActiveSpeakersChanged, speakersChanged)
      .on(RoomEvent.TranscriptionReceived, transcriptionReceived)
      .on(RoomEvent.Disconnected, disconnected);

    const connect = async () => {
      try {
        const token = await exchangeLiveKitToken(session, controller.signal);
        if (controller.signal.aborted) return;
        onEvent("Room authorized", token.room_name, "positive");
        await room.connect(token.server_url, token.participant_token, {
          autoSubscribe: true,
        });
        await room.startAudio();
        await room.localParticipant.setMicrophoneEnabled(true, {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        });
        onEvent("Agent ready", "microphone is live", "positive");
      } catch (error) {
        if (!controller.signal.aborted) onError(error);
      }
    };
    void connect();

    return () => {
      controller.abort();
      endingRef.current = true;
      room
        .off(RoomEvent.ConnectionStateChanged, connectionChanged)
        .off(RoomEvent.TrackSubscribed, trackSubscribed)
        .off(RoomEvent.TrackUnsubscribed, trackUnsubscribed)
        .off(RoomEvent.ActiveSpeakersChanged, speakersChanged)
        .off(RoomEvent.TranscriptionReceived, transcriptionReceived)
        .off(RoomEvent.Disconnected, disconnected);
      void room.disconnect(true);
      roomRef.current = null;
    };
  }, [onEnd, onError, onEvent, onState, onTranscript, session]);

  const end = async () => {
    endingRef.current = true;
    const room = roomRef.current;
    if (room !== null) {
      await room.localParticipant.setMicrophoneEnabled(false);
      await room.disconnect(true);
    }
    onEnd();
  };

  return (
    <>
      <div className="remote-audio" ref={audioRootRef} aria-hidden="true" />
      <button
        className="session-button session-button--stop"
        type="button"
        onClick={() => void end()}
      >
        <span className="button-icon" aria-hidden="true">
          ■
        </span>
        {ready ? "End session" : "Connecting…"}
      </button>
    </>
  );
}

export default memo(LiveKitSession);
