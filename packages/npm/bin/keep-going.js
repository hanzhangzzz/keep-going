#!/usr/bin/env node
"use strict";

const childProcess = require("child_process");
const path = require("path");

const { migratePrivateArtifacts, prepareRuntime, runtimePath, userHomeForRuntime } = require("../lib/runtime");

const PACKAGE_ROOT = path.resolve(__dirname, "..");
const PACKAGE = require("../package.json");

function main(argv) {
  const parsed = parseArgs(argv);
  if (parsed.command === "help") {
    printHelp();
    return 0;
  }
  if (parsed.command === "runtime-path") {
    console.log(runtimePath(parsed.options, PACKAGE.version));
    return 0;
  }
  const runtimeDir = prepareRuntime(PACKAGE_ROOT, PACKAGE.version, parsed.options, {
    replace:
      parsed.command === "upgrade" ||
      parsed.command === "sync-local" ||
      parsed.options.replaceRuntime,
  });
  if (!process.env.KEEP_GOING_USER_HOME) {
    process.env.KEEP_GOING_USER_HOME = userHomeForRuntime(runtimeDir);
  }
  console.log(`Keep Going runtime: ${runtimeDir}`);

  const sourceDir = parsed.options.source ? path.resolve(parsed.options.source) : null;
  if (sourceDir) {
    migratePrivateArtifacts(sourceDir, process.env.KEEP_GOING_USER_HOME);
  }
  if (sourceDir && parsed.command === "sync-local") {
    return syncLocal(sourceDir, parsed.options);
  }
  if (sourceDir && parsed.command === "start") {
    const syncCode = syncLocal(sourceDir, {
      ...parsed.options,
      noVerify: true,
      registerHosts: "none",
    });
    if (syncCode !== 0) {
      return syncCode;
    }
  }

  switch (parsed.command) {
    case "install":
      return install(runtimeDir, parsed.options, { upgrade: false });
    case "upgrade":
      return install(runtimeDir, parsed.options, { upgrade: true });
    case "start":
      return start(runtimeDir, parsed.options);
    case "onboard":
      return onboard(runtimeDir, parsed.options);
    case "sync-local":
      return syncLocal(runtimeDir, parsed.options);
    case "doctor":
      return doctor(runtimeDir, parsed.options);
    case "enable":
      return bridge(runtimeDir, "enable", parsed.options);
    case "disable":
      return bridge(runtimeDir, "disable", parsed.options);
    case "status":
      return bridge(runtimeDir, "status", parsed.options);
    default:
      throw new Error(`unknown command: ${parsed.command}`);
  }
}

function syncLocal(runtimeDir, options) {
  const args = ["sync-local"];
  appendOption(args, "--register-hosts", options.registerHosts);
  appendOption(args, "--runtime-home", options.runtimeHome);
  appendOption(args, "--runtime-version", options.runtimeVersion);
  appendHomeArgs(args, options);
  appendOption(args, "--agent", options.agent);
  appendOption(args, "--agents", options.agents);
  appendOption(args, "--render-mode", options.renderMode);
  if (options.noVerify) {
    args.push("--no-verify");
  }
  return runKeepGoing(runtimeDir, args);
}

function start(runtimeDir, options) {
  const args = ["start", "--project", options.project || process.cwd()];
  appendOption(args, "--host", options.host || "codex");
  appendOption(args, "--backend", options.backend);
  appendOption(args, "--command", options.command);
  appendOption(args, "--input-mode", options.inputMode);
  appendOption(args, "--force-skill", options.forceSkill);
  appendOption(args, "--shell-executable", options.shellExecutable);
  appendOption(args, "--state-home", options.stateHome);
  appendOption(args, "--register-hosts", options.registerHosts);
  appendHomeArgs(args, options);
  appendOption(args, "--agent", options.agent);
  appendOption(args, "--agents", options.agents);
  appendOption(args, "--render-mode", options.renderMode);
  if (options.shell) {
    args.push("--shell");
  }
  if (options.noVerify) {
    args.push("--no-verify");
  }
  if (options.jsonOutput) {
    args.push("--json-output");
  }
  return runKeepGoing(runtimeDir, args);
}

function onboard(runtimeDir, options) {
  const args = ["onboard", "--project", options.project || process.cwd()];
  appendOption(args, "--host", options.host || "auto");
  appendOption(args, "--max-sessions", options.maxSessions);
  appendOption(args, "--max-turns", options.maxTurns);
  appendOption(args, "--window-days", options.windowDays);
  appendOption(args, "--scope", options.scope);
  appendOption(args, "--register-hosts", options.registerHosts);
  appendOption(args, "--state-home", options.stateHome);
  appendHomeArgs(args, options);
  if (options.replace) {
    args.push("--replace");
  }
  if (options.noDeploy) {
    args.push("--no-deploy");
  }
  if (options.noVerify) {
    args.push("--no-verify");
  }
  if (options.jsonOutput) {
    args.push("--json-output");
  }
  return runKeepGoing(runtimeDir, args);
}

function install(runtimeDir, options, installOptions) {
  const args = ["install"];
  if (options.dryRun) {
    appendHomeArgs(args, options);
    return runKeepGoing(runtimeDir, args);
  }
  args.push("--execute");
  if (installOptions.upgrade || options.force) {
    args.push("--force");
  }
  appendOption(args, "--register-hosts", options.registerHosts);
  appendHomeArgs(args, options);
  const code = runKeepGoing(runtimeDir, args);
  if (code !== 0 || options.noVerify) {
    return code;
  }
  const verifyArgs = ["install", "--verify"];
  appendHomeArgs(verifyArgs, options);
  const verifyCode = runKeepGoing(runtimeDir, verifyArgs);
  if (verifyCode !== 0) {
    return verifyCode;
  }
  if (installOptions.upgrade) {
    console.log("Keep Going upgrade verified.");
  } else {
    console.log("Keep Going install verified.");
  }
  return 0;
}

function doctor(runtimeDir, options) {
  const auditCode = runKeepGoing(runtimeDir, ["audit", "--smoke", "--json-output"]);
  if (auditCode !== 0 || !options.verifyInstall) {
    return auditCode;
  }
  const verifyArgs = ["install", "--verify"];
  appendHomeArgs(verifyArgs, options);
  return runKeepGoing(runtimeDir, verifyArgs);
}

function bridge(runtimeDir, subcommand, options) {
  const args = ["bridge", subcommand];
  if (options.project) {
    args.push("--project", options.project);
  }
  if (options.stateHome) {
    args.push("--state-home", options.stateHome);
  }
  if (options.jsonOutput) {
    args.push("--json-output");
  }
  if (subcommand === "enable") {
    appendOption(args, "--host", options.host);
    appendOption(args, "--backend", options.backend);
    appendOption(args, "--command", options.command);
    appendOption(args, "--input-mode", options.inputMode);
    appendOption(args, "--force-skill", options.forceSkill);
    appendOption(args, "--shell-executable", options.shellExecutable);
    if (options.shell) {
      args.push("--shell");
    }
  }
  return runKeepGoing(runtimeDir, args);
}

function runKeepGoing(runtimeDir, args) {
  requireUv();
  const result = childProcess.spawnSync("uv", ["run", "keep-going", ...args], {
    cwd: runtimeDir,
    env: {
      ...process.env,
      KEEP_GOING_USER_HOME: userHomeForRuntime(runtimeDir),
    },
    stdio: "inherit",
  });
  if (result.error) {
    throw result.error;
  }
  if (result.signal) {
    throw new Error(`uv run keep-going terminated by signal ${result.signal}`);
  }
  return result.status || 0;
}

function requireUv() {
  const result = childProcess.spawnSync("uv", ["--version"], { stdio: "ignore" });
  if (result.error || result.status !== 0) {
    throw new Error("uv is required. Install uv first, then rerun the Keep Going npm installer.");
  }
}

function appendHomeArgs(args, options) {
  appendOption(args, "--codex-home", options.codexHome);
  appendOption(args, "--agents-home", options.agentsHome);
  appendOption(args, "--claude-home", options.claudeHome);
}

function appendOption(args, flag, value) {
  if (value !== undefined && value !== null && value !== "") {
    args.push(flag, String(value));
  }
}

function parseArgs(argv) {
  const args = [...argv];
  let command = "help";
  if (args.length > 0 && !args[0].startsWith("-")) {
    command = args.shift();
  }
  if (command === "--help" || command === "-h") {
    command = "help";
  }
  const options = {};
  while (args.length > 0) {
    const flag = args.shift();
    switch (flag) {
      case "--help":
      case "-h":
        command = "help";
        break;
      case "--source":
        options.source = readValue(flag, args);
        break;
      case "--runtime-home":
        options.runtimeHome = readValue(flag, args);
        break;
      case "--runtime-version":
        options.runtimeVersion = readValue(flag, args);
        break;
      case "--replace-runtime":
        options.replaceRuntime = true;
        break;
      case "--codex-home":
        options.codexHome = readValue(flag, args);
        break;
      case "--agents-home":
        options.agentsHome = readValue(flag, args);
        break;
      case "--claude-home":
        options.claudeHome = readValue(flag, args);
        break;
      case "--dry-run":
        options.dryRun = true;
        break;
      case "--no-verify":
        options.noVerify = true;
        break;
      case "--register-hosts":
        options.registerHosts = readValue(flag, args);
        break;
      case "--no-register-hosts":
        options.registerHosts = "none";
        break;
      case "--force":
        options.force = true;
        break;
      case "--verify-install":
        options.verifyInstall = true;
        break;
      case "--project":
        options.project = readValue(flag, args);
        break;
      case "--state-home":
        options.stateHome = readValue(flag, args);
        break;
      case "--host":
        options.host = readValue(flag, args);
        break;
      case "--backend":
        options.backend = readValue(flag, args);
        break;
      case "--command":
        options.command = readValue(flag, args);
        break;
      case "--shell":
        options.shell = true;
        break;
      case "--input-mode":
        options.inputMode = readValue(flag, args);
        break;
      case "--force-skill":
        options.forceSkill = readValue(flag, args);
        break;
      case "--shell-executable":
        options.shellExecutable = readValue(flag, args);
        break;
      case "--agent":
        options.agent = readValue(flag, args);
        break;
      case "--agents":
        options.agents = readValue(flag, args);
        break;
      case "--render-mode":
        options.renderMode = readValue(flag, args);
        if (!["advisory", "block"].includes(options.renderMode)) {
          throw new Error("--render-mode must be advisory or block");
        }
        break;
      case "--max-sessions":
        options.maxSessions = readValue(flag, args);
        break;
      case "--max-turns":
        options.maxTurns = readValue(flag, args);
        break;
      case "--window-days":
        options.windowDays = readValue(flag, args);
        break;
      case "--scope":
        options.scope = readValue(flag, args);
        break;
      case "--replace":
        options.replace = true;
        break;
      case "--no-deploy":
        options.noDeploy = true;
        break;
      case "--json-output":
        options.jsonOutput = true;
        break;
      default:
        throw new Error(`unknown option: ${flag}`);
    }
  }
  return { command, options };
}

function readValue(flag, args) {
  if (args.length === 0 || args[0].startsWith("-")) {
    throw new Error(`missing value for ${flag}`);
  }
  return args.shift();
}

function printHelp() {
  const text = `
Keep Going npm installer

Usage:
  npx keep-going onboard [--project <dir>] [--host auto|codex|claude-code]
    [--max-sessions 5] [--max-turns 40] [--window-days 90] [--scope recent|project]
    [--replace] [--no-deploy]
  npx keep-going sync-local [--source <repo>] [--register-hosts auto|all|claude-code|codex|none]
  npx keep-going start [--project <dir>] [--host codex|claude-code|generic] [--register-hosts auto|all|claude-code|codex|none]
  npx keep-going install [--dry-run] [--force] [--register-hosts auto|all|claude-code|codex|none] [--source <repo>]
  npx keep-going upgrade [--source <repo>] [--replace-runtime] [--register-hosts auto|all|claude-code|codex|none]
  npx keep-going doctor [--verify-install]
  npx keep-going enable --project <dir> [--backend direct|cli] [--command "c 0"] [--shell]
  npx keep-going disable --project <dir>
  npx keep-going status --project <dir>

Note:
  Claude Code plugin commands are available as slash commands.
  Codex CLI currently does not dispatch custom/plugin slash commands in the TUI; use $keep-going, MCP, or enable/status/disable.

Runtime options:
  --source <repo>          Use a local Keep Going source tree instead of bundled runtime.
  --runtime-home <dir>    Runtime install root. Default: ~/.keep-going/runtime.
  --runtime-version <v>   Runtime version directory. Default: npm package version.
  --replace-runtime       Recopy runtime even if the target version already exists.
  --register-hosts <mode> Register detected Claude Code/Codex plugins after install. Default: auto.
  --no-register-hosts     Skip host plugin registration.
`;
  console.log(text.trim());
}

try {
  const exitCode = main(process.argv.slice(2));
  process.exit(exitCode);
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`keep-going: ${message}`);
  process.exit(1);
}
