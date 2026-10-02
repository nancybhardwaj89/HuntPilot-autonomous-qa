import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Requests to /api and /reports go to the Python backend on port 8001
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8001",
      "/reports": "http://localhost:8001",
    },
  },
});
