import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#20263d",
        panel: "#f5f6fc",
        line: "#e5e8f2",
        accent: "#5855db",
        warn: "#b45309"
      }
    }
  },
  plugins: []
};

export default config;
