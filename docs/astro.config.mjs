import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

export default defineConfig({
  site: process.env.CI
    ? "https://clawbolt.ai"
    : "http://localhost:4321",
  base: "/",
  integrations: [
    starlight({
      title: "Clawbolt",
      logo: {
        light: "./src/assets/clawbolt_text.png",
        dark: "./src/assets/clawbolt_text.png",
        replacesTitle: true,
      },
      favicon: "/clawbolt.png",
      head: [
        {
          tag: "link",
          attrs: {
            rel: "preconnect",
            href: "https://fonts.googleapis.com",
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "preconnect",
            href: "https://fonts.gstatic.com",
            crossorigin: true,
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "stylesheet",
            href: "https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap",
          },
        },
      ],
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
            { label: "Memory", slug: "features/memory" },
            { label: "Photos", slug: "features/photos" },
            { label: "File Cataloging", slug: "features/file-cataloging" },
            { label: "Heartbeat", slug: "features/heartbeat" },
            { label: "Google Calendar", slug: "features/calendar" },
            { label: "QuickBooks Online", slug: "features/quickbooks" },
          ],
        },
        {
          label: "Deployment",
          items: [
            { label: "Docker", slug: "deployment/docker" },
            { label: "BlueBubbles Setup (iMessage)", slug: "deployment/bluebubbles-setup" },
            { label: "Linq Setup (Texting)", slug: "deployment/linq-setup" },
            { label: "Telegram Setup", slug: "deployment/telegram-setup" },
            { label: "Storage Providers", slug: "deployment/storage" },
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
