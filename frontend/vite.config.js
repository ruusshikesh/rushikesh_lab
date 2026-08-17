import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite is the tool that bundles your React code and runs the dev server.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173, // the backend's CORS already allows this port
  },
});
