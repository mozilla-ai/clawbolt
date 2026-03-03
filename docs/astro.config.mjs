import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

export default defineConfig({
  site: process.env.CI
    ? "https://mozilla-ai.github.io"
    : "http://localhost:4321",
  base: process.env.CI ? "/clawbolt" : "/",
  integrations: [
    starlight({
      title: "Clawbolt",
      logo: {
        light: "./src/assets/clawbolt_text.png",
        dark: "./src/assets/clawbolt_text.png",
        replacesTitle: true,
      },
      favicon: "/clawbolt.png",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/mozilla-ai/clawbolt",
        },
      ],
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        {
          label: "Start Here",
          items: [
            { label: "Welcome", link: "/" },
            { label: "Getting Started", slug: "getting-started" },
            { label: "Configuration", slug: "configuration" },
            { label: "Architecture", slug: "architecture" },
          ],
        },
        {
          label: "Features",
          items: [
            { label: "Estimates", slug: "features/estimates" },
            { label: "Memory", slug: "features/memory" },
            { label: "Photos", slug: "features/photos" },
            { label: "Voice", slug: "features/voice" },
            { label: "File Cataloging", slug: "features/file-cataloging" },
            { label: "Heartbeat", slug: "features/heartbeat" },
          ],
        },
        {
          label: "Deployment",
          items: [
            { label: "Docker", slug: "deployment/docker" },
            { label: "Storage Providers", slug: "deployment/storage" },
            { label: "Telegram Setup", slug: "deployment/telegram-setup" },
          ],
        },
        {
          label: "Development",
          items: [
            { label: "Local Setup", slug: "development/local-setup" },
            { label: "Testing", slug: "development/testing" },
            { label: "Contributing", slug: "development/contributing" },
          ],
        },
      ],
    }),
  ],
});
