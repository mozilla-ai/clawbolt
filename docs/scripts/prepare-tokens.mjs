#!/usr/bin/env node
/*
 * Stage brand-tokens.css from frontend/src/styles/ into docs/src/styles/ so
 * custom.css can @import "./brand-tokens.css" using a local path. The file
 * is gitignored in docs/ -- frontend/ is the single source of truth.
 *
 * Runs automatically via the predev/prestart/prebuild hooks in
 * docs/package.json. Mirrors the equivalent script in
 * clawbolt-premium/docs/user-guide/scripts/prepare-tokens.mjs so the two
 * docs sites follow the same staging pattern.
 */
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(
  here,
  "..",
  "..",
  "frontend",
  "src",
  "styles",
  "brand-tokens.css",
);
const dest = resolve(here, "..", "src", "styles", "brand-tokens.css");

if (!existsSync(source)) {
  console.error(
    `[prepare-tokens] ERROR: ${source} not found.\n` +
      `\n` +
      `The docs site consumes brand-tokens.css from the sibling frontend/\n` +
      `directory. If you cloned docs/ standalone, check out the full clawbolt\n` +
      `repo so frontend/ sits next to docs/. If frontend/ exists but the file\n` +
      `is missing, this branch predates the shared-tokens refactor; rebase on\n` +
      `main or manually copy frontend/src/styles/brand-tokens.css into place.\n` +
      `\n` +
      `Note: the staged copy is not HMR-aware. If you edit brand-tokens.css\n` +
      `while astro dev is running, restart dev to pick up the change.`,
  );
  process.exit(1);
}

// Guard: the staged copy is gitignored, but a force-add or a bad rebase can
// still track it. If that happens, the .gitignore silently stops mattering
// and the staged file becomes an inconsistent duplicate source of truth.
// Fail loudly when git is available; skip silently when it is not (e.g.,
// inside a Docker stage that copies in an already-staged tree).
try {
  const repoRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], {
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
  const relDest = relative(repoRoot, dest);
  const tracked = execFileSync("git", ["ls-files", "--", relDest], {
    encoding: "utf-8",
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
  if (tracked) {
    console.error(
      `[prepare-tokens] ERROR: ${relDest} is tracked by git.\n` +
        `\n` +
        `The staged copy must stay untracked -- frontend/src/styles/\n` +
        `brand-tokens.css is the single source of truth. Run:\n` +
        `\n` +
        `  git rm --cached ${relDest}\n` +
        `\n` +
        `then commit. The docs/.gitignore will keep it untracked going forward.`,
    );
    process.exit(1);
  }
} catch {
  // git not available (e.g., Docker stage with no git binary) -- skip the
  // guard. The Dockerfile uses COPY instead of this script anyway.
}

mkdirSync(dirname(dest), { recursive: true });
copyFileSync(source, dest);
console.log(`[prepare-tokens] copied ${source} -> ${dest}`);
