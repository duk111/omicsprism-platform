import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { publicRelativePath } from "./api";
import { InteractiveRouter } from "./interactive/InteractiveRouter";
import "./index.css";

function Root() {
  const path = publicRelativePath().toLowerCase();
  if (path.startsWith("/interactive/")) {
    return <InteractiveRouter />;
  }
  return <App />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
