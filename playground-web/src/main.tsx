import "@fontsource/instrument-sans/400.css";
import "@fontsource/instrument-sans/500.css";
import "@fontsource/instrument-sans/600.css";
import "@fontsource/fragment-mono/400.css";
import "@pipecat-ai/voice-ui-kit/styles.scoped.css";
import "./styles.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("voicey playground root is missing");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
