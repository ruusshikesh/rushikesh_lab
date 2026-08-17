import React from "react";
import ReactDOM from "react-dom/client";
import RushAlgo from "./RushAlgo.jsx";

// This finds the <div id="root"> in index.html and renders your app into it.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <RushAlgo />
  </React.StrictMode>
);
