import assert from "node:assert/strict";
import fs from "node:fs";
import {test} from "node:test";
import {employeeClientOptionsFor} from "../dist/employee.js";

const config = {codex_gateway_api_key:"scoped-example", codex_gateway_base_url:"https://preloop.example.com/openai/v1", codex_employee_mcp_enabled:true};
test("employee isolates broad config and scopes provider, MCP and native hooks", () => {
 const options = employeeClientOptionsFor(config, () => "--require-flow-credential");
 try {
  assert.equal(options.config.model_provider,"preloop_employee");
  assert.equal(options.config.model_providers.preloop_employee.env_key,"PRELOOP_FLOW_TOKEN");
  assert.equal(options.config.mcp_servers.preloop.bearer_token_env_var,"PRELOOP_FLOW_TOKEN");
  assert.equal(options.env.PRELOOP_FLOW_TOKEN,"scoped-example");
  assert.equal(options.env.PRELOOP_FLOW_API_URL,"https://preloop.example.com");
  assert.equal(options.env.PRELOOP_FLOW_CREDENTIAL_REQUIRED,"1");
  assert.equal(fs.existsSync(options.env.CODEX_HOME + "/config.toml"),false);
  assert.equal(fs.existsSync(options.env.CODEX_HOME + "/auth.json"),false);
  const hooks = fs.readFileSync(options.env.CODEX_HOME + "/hooks.json","utf8");
  assert.equal(hooks.includes("--require-flow-credential"),true);
  assert.equal(hooks.includes("scoped-example"),false);
  assert.equal(JSON.stringify(options.config).includes("scoped-example"),false);
 } finally {fs.rmSync(options.env.CODEX_HOME,{recursive:true,force:true});}
});
test("old CLI and incomplete scoped provider fail before SDK execution", () => {
 assert.throws(() => employeeClientOptionsFor(config, () => "legacy CLI"), /Update Preloop CLI/);
 assert.throws(() => employeeClientOptionsFor({...config,codex_gateway_api_key:undefined}, () => "--require-flow-credential"), /execution credential/);
});

test("employee preserves curated role/native policy without broad model configuration", () => {
 const native = fs.mkdtempSync("/tmp/preloop-native-codex-");
 const previous = process.env.CODEX_HOME;
 process.env.CODEX_HOME = native;
 fs.writeFileSync(native + "/AGENTS.md", "Example role index");
 fs.writeFileSync(native + "/config.toml", 'approval_policy = "untrusted"\nmodel_provider = "broad"\n[model_providers.broad]\nexperimental_bearer_token = "broad-secret"\n');
 fs.writeFileSync(native + "/hooks.json", JSON.stringify({hooks:{PreToolUse:[{matcher:"*",hooks:[{type:"command",command:"native-policy"},{type:"command",command:"old-preloop agents permission-hook"}]}]}}));
 let options;
 try {
  options = employeeClientOptionsFor(config, () => "--require-flow-credential");
  assert.equal(options.config.approval_policy,"untrusted");
  assert.equal(options.config.model_provider,"preloop_employee");
  assert.equal(fs.readFileSync(options.env.CODEX_HOME + "/AGENTS.md","utf8"),"Example role index");
  const hooks = fs.readFileSync(options.env.CODEX_HOME + "/hooks.json","utf8");
  assert.equal(hooks.includes("native-policy"),true);
  assert.equal(hooks.includes("old-preloop"),false);
  assert.equal(JSON.stringify(options).includes("broad-secret"),false);
 } finally {
  if (previous === undefined) delete process.env.CODEX_HOME; else process.env.CODEX_HOME = previous;
  if (options) fs.rmSync(options.env.CODEX_HOME,{recursive:true,force:true});
  fs.rmSync(native,{recursive:true,force:true});
 }
});

test("split model gateway retains enrolled API origin and refuses a foreign API", () => {
 const split = {...config, control_ws_url:"wss://preloop.example.com/api/v1/agents/control/ws", codex_gateway_base_url:"https://gateway.example.com/openai/v1", codex_employee_api_url:"https://preloop.example.com"};
 const options = employeeClientOptionsFor(split, () => "--require-flow-credential");
 try {
  assert.equal(options.env.PRELOOP_FLOW_API_URL,"https://preloop.example.com");
  assert.equal(options.config.mcp_servers.preloop.url,"https://preloop.example.com/mcp/v1");
  assert.equal(options.baseUrl,"https://gateway.example.com/openai/v1");
 } finally {fs.rmSync(options.env.CODEX_HOME,{recursive:true,force:true});}
 assert.throws(() => employeeClientOptionsFor({...split,codex_employee_api_url:"https://foreign.example.com"}, () => "--require-flow-credential"), /enrolled control origin/);
});
