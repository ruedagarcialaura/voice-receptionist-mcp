import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, forward /api to the EHR service. In production the EHR service serves the built files itself.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
