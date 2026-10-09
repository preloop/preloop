import { defineConfig } from "@trigger.dev/sdk";

export default defineConfig({
  // Replace with your project ref from the trigger.dev dashboard
  // (or your self-hosted instance). Type-checking does not need it.
  project: process.env.TRIGGER_PROJECT_REF ?? "proj_replace_me",
  dirs: ["./src/trigger"],
  // The refund approval blocks the run while a human decides. Keep this above
  // the timeout of the Preloop approval workflow (300 s unless you changed it).
  maxDuration: 900,
});
