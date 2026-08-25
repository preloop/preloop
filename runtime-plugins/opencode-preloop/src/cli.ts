#!/usr/bin/env node
import { PreloopOpenCodePlugin } from "./index.js";

function parseArgs(): { command: string; configPath?: string } {
  const [, , command = "verify", ...rest] = process.argv;
  const configIndex = rest.indexOf("--config");
  return {
    command,
    configPath: configIndex >= 0 ? rest[configIndex + 1] : undefined,
  };
}

const args = parseArgs();
const instance = new PreloopOpenCodePlugin(args.configPath);
if (args.command === "verify") {
  instance.verify();
  console.log("@preloop-ai/opencode-plugin verified");
} else if (args.command === "run") {
  void instance.start();
} else {
  throw new Error(`Unknown command: ${args.command}`);
}
