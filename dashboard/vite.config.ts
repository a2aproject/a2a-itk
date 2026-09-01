import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base: the site is published under /a2a-itk/dashboard/ on GitHub
// Pages, but `./` also keeps `vite preview` and file:// smoke tests working.
export default defineConfig({
  base: "./",
  plugins: [react()],
});
