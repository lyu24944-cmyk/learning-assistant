import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // 后端端口可以用环境变量或 frontend/.env.local 改，默认仍是 8010。
  const fromFile = loadEnv(mode, process.cwd(), "LEARNING_ASSISTANT_");
  const apiTarget =
    process.env.LEARNING_ASSISTANT_API ||
    fromFile.LEARNING_ASSISTANT_API ||
    "http://127.0.0.1:8010";
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      // 端口被占时直接报错，不要悄悄换到 5174：后端 Origin 白名单默认只认 5173。
      strictPort: true,
      proxy: {
        "/api": apiTarget,
      },
    },
  };
});
