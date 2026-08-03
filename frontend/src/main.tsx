import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { publicRelativePath } from "./api";
import "./index.css";

const InteractiveRouter = lazy(() => import("./interactive/InteractiveRouter").then(module => ({ default: module.InteractiveRouter })));

function Root() {
  const path = publicRelativePath().toLowerCase();
  if (path.startsWith("/interactive/")) {
    return <Suspense fallback={null}><InteractiveRouter /></Suspense>;
  }
  return <App />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
