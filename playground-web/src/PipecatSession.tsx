import { LogLevel, type RTVIEventCallbacks } from "@pipecat-ai/client-js";
import { WavMediaManager } from "@pipecat-ai/small-webrtc-transport";
import { PipecatAppBase, type PipecatBaseChildProps } from "@pipecat-ai/voice-ui-kit";
import { memo, useCallback, useEffect, useMemo } from "react";

import type { PipecatIssuedSession } from "./api";

function SessionConsole({
  connection,
  onEnd,
  onError,
}: {
  connection: PipecatBaseChildProps;
  onEnd: () => void;
  onError: () => void;
}) {
  useEffect(() => {
    if (connection.error) onError();
  }, [connection.error, onError]);

  const end = async () => {
    await connection.handleDisconnect?.();
    onEnd();
  };
  return (
    <button
      className="session-button session-button--stop"
      type="button"
      onClick={() => void end()}
    >
      <span className="button-icon" aria-hidden="true">
        ■
      </span>
      End session
    </button>
  );
}

function PipecatSession({
  session,
  callbacks,
  onEnd,
  onError,
}: {
  session: PipecatIssuedSession;
  callbacks: RTVIEventCallbacks;
  onEnd: () => void;
  onError: () => void;
}) {
  const connectParams = useMemo(
    () => ({
      webrtcRequestParams: {
        endpoint: session.webrtc_url,
        headers: new Headers({ authorization: `Bearer ${session.token}` }),
      },
    }),
    [session.token, session.webrtc_url],
  );
  const clientOptions = useMemo(() => ({ callbacks }), [callbacks]);
  const transportOptions = useMemo(
    () => ({ mediaManager: new WavMediaManager() }),
    [],
  );
  const configureClient = useCallback(
    (client: { setLogLevel: (level: LogLevel) => void }) => {
      client.setLogLevel(LogLevel.WARN);
    },
    [],
  );

  return (
    <PipecatAppBase
      transportType="smallwebrtc"
      connectParams={connectParams}
      clientOptions={clientOptions}
      transportOptions={transportOptions}
      initDevicesOnMount
      connectOnMount
      noThemeProvider
      onClient={configureClient}
    >
      {(connection) => (
        <SessionConsole connection={connection} onEnd={onEnd} onError={onError} />
      )}
    </PipecatAppBase>
  );
}

export default memo(PipecatSession);
