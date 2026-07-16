"use strict";

const fs = require("fs");
const crypto = require("crypto");
const os = require("os");
const path = require("path");

const PUBLIC_RUNTIME_FILES = new Set([
  ".agents/plugins/marketplace.json",
  ".claude-plugin/marketplace.json",
  ".codex/agents/keep-going.toml",
  ".codex/skills/keep-going/SKILL.md",
  "README.md",
  "README.zh.md",
  "artifacts/decision-policy.template.yaml",
  "config.toml",
  "docs/assets/keep-going-concept.svg",
  "plugins/keep-going/.claude-plugin/plugin.json",
  "plugins/keep-going/.codex-plugin/plugin.json",
  "plugins/keep-going/.mcp.json",
  "plugins/keep-going/commands/self-test.md",
  "plugins/keep-going/commands/setup.md",
  "plugins/keep-going/commands/status.md",
  "plugins/keep-going/hooks.json",
  "plugins/keep-going/hooks/keep-going-stop-hook.sh",
  "plugins/keep-going/hooks/hooks.json",
  "plugins/keep-going/hooks/keep-going-decision-hook.sh",
  "plugins/keep-going/plugin.json",
  "plugins/keep-going/prompts/keep-going-self-test.md",
  "plugins/keep-going/prompts/keep-going-setup.md",
  "plugins/keep-going/prompts/keep-going-status.md",
  "plugins/keep-going/prompts/keep-going:self-test.md",
  "plugins/keep-going/prompts/keep-going:setup.md",
  "plugins/keep-going/prompts/keep-going:status.md",
  "plugins/keep-going/prompts/keep-going.md",
  "plugins/keep-going/scripts/bridge.sh",
  "plugins/keep-going/scripts/mcp.sh",
  "plugins/keep-going/scripts/reply.sh",
  "plugins/keep-going/skills/keep-going/SKILL.md",
  "pyproject.toml",
  "scripts/01-harvest.sh",
  "scripts/02-reason.sh",
  "scripts/03-reply.sh",
  "scripts/04-mcp.sh",
  "scripts/install-integration.sh",
  "src/keep_going/__init__.py",
  "src/keep_going/agents/__init__.py",
  "src/keep_going/agents/distill.py",
  "src/keep_going/agents/registry.py",
  "src/keep_going/audit.py",
  "src/keep_going/cli.py",
  "src/keep_going/config.py",
  "src/keep_going/corpus/__init__.py",
  "src/keep_going/corpus/adapters/__init__.py",
  "src/keep_going/corpus/adapters/claude_code.py",
  "src/keep_going/corpus/adapters/codex.py",
  "src/keep_going/corpus/classify.py",
  "src/keep_going/corpus/harvest.py",
  "src/keep_going/corpus/sample.py",
  "src/keep_going/corpus/schema.py",
  "src/keep_going/corpus/scrub.py",
  "src/keep_going/eval/__init__.py",
  "src/keep_going/eval/conformance.py",
  "src/keep_going/eval/loop_metrics.py",
  "src/keep_going/eval/overrides.py",
  "src/keep_going/eval/replay.py",
  "src/keep_going/integration/__init__.py",
  "src/keep_going/integration/bridge.py",
  "src/keep_going/integration/install.py",
  "src/keep_going/integration/package.py",
  "src/keep_going/integration/stop_context.py",
  "src/keep_going/mcp_stdio.py",
  "src/keep_going/patterns/__init__.py",
  "src/keep_going/patterns/distill.py",
  "src/keep_going/privacy.py",
  "src/keep_going/reasoning/__init__.py",
  "src/keep_going/reasoning/extract.py",
  "src/keep_going/reasoning/prompts.py",
  "src/keep_going/decision/__init__.py",
  "src/keep_going/decision/policy_prompt.py",
  "src/keep_going/decision/policy_runtime.py",
  "src/keep_going/decision/hook.py",
  "src/keep_going/decision/policy.py",
  "src/keep_going/decision/reply.py",
  "src/keep_going/decision/stop_decision.py",
  "src/keep_going/decision/stop_safety.py",
  "uv.lock",
]);

const ALWAYS_EXCLUDED_PARTS = new Set([
  ".DS_Store",
  ".mcp_runtime",
  "__pycache__",
]);

const SAFE_EMAIL_DOMAINS = new Set([
  "example.com",
  "example.net",
  "example.org",
  "users.noreply.github.com",
]);
const REVIEWED_MEDIA_SHA256 = new Map([
  ["docs/assets/keep-going-concept.svg", "2e76241a2e5221f3b7548b7d7e4bcb01d94efa6866a485c12fe409aeae40caa7"],
]);

function defaultRuntimeHome() {
  return process.env.KEEP_GOING_RUNTIME_HOME || path.join(os.homedir(), ".keep-going", "runtime");
}

function runtimePath(options, version) {
  const runtimeHome = path.resolve(options.runtimeHome || defaultRuntimeHome());
  const runtimeVersion = normalizeRuntimeVersion(options.runtimeVersion || version);
  return path.join(runtimeHome, runtimeVersion);
}

function findRuntimeSource(packageRoot, options) {
  const explicit = options.source || process.env.KEEP_GOING_SOURCE_DIR;
  if (explicit) {
    return path.resolve(explicit);
  }
  const bundled = path.join(packageRoot, "runtime");
  if (fs.existsSync(path.join(bundled, "pyproject.toml"))) {
    return bundled;
  }
  throw new Error(
    "Keep Going runtime source not found. Published npm packages must include runtime/, or set KEEP_GOING_SOURCE_DIR/--source."
  );
}

function prepareRuntime(packageRoot, version, options = {}, prepareOptions = {}) {
  const source = findRuntimeSource(packageRoot, options);
  assertRuntimeSource(source);
  const target = runtimePath(options, version);
  if (samePath(source, target)) {
    writeRuntimeRootMarker(target);
    return target;
  }
  if (fs.existsSync(path.join(target, "pyproject.toml")) && !prepareOptions.replace) {
    writeRuntimeRootMarker(target);
    return target;
  }
  copyRuntimeSource(source, target, { replace: true });
  writeRuntimeRootMarker(target);
  return target;
}

function copyRuntimeSource(source, target, options = {}) {
  const src = path.resolve(source);
  const dst = path.resolve(target);
  assertRuntimeSource(src, options);
  if (samePath(src, dst) || isInside(src, dst)) {
    throw new Error(`runtime target must not be the source directory or its parent: ${dst}`);
  }
  if (fs.existsSync(dst)) {
    if (!options.replace) {
      throw new Error(`runtime target already exists: ${dst}`);
    }
    fs.rmSync(dst, { recursive: true, force: true });
  }
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  const tmpParent = isInside(dst, src) ? fs.mkdtempSync(path.join(os.tmpdir(), "keep-going-runtime-")) : path.dirname(dst);
  const tmp = path.join(tmpParent, `${path.basename(dst)}.tmp-${process.pid}-${Date.now()}`);
  fs.cpSync(src, tmp, {
    recursive: true,
    filter: (item) => shouldCopy(src, item, options),
  });
  assertPublicRuntimeTree(tmp);
  try {
    fs.renameSync(tmp, dst);
  } catch (error) {
    if (!error || error.code !== "EXDEV") {
      throw error;
    }
    fs.cpSync(tmp, dst, { recursive: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  } finally {
    if (tmpParent !== path.dirname(dst)) {
      fs.rmSync(tmpParent, { recursive: true, force: true });
    }
  }
}

function writeRuntimeRootMarker(runtimeRoot) {
  const root = path.resolve(runtimeRoot);
  const pluginRoot = path.join(root, "plugins", "keep-going");
  if (!fs.existsSync(pluginRoot)) {
    return;
  }
  fs.writeFileSync(path.join(pluginRoot, "runtime-root"), `${root}\n`, "utf8");
  fs.writeFileSync(path.join(pluginRoot, ".repo-root"), `${root}\n`, "utf8");
}

function assertRuntimeSource(source) {
  const required = [...PUBLIC_RUNTIME_FILES];
  const missing = required.filter((rel) => !fs.existsSync(path.join(source, rel)));
  if (missing.length > 0) {
    throw new Error(`invalid Keep Going runtime source ${source}; missing ${missing.join(", ")}`);
  }
}

function shouldCopy(sourceRoot, item) {
  const rel = path.relative(sourceRoot, item);
  if (!rel) {
    return true;
  }
  const normalized = rel.split(path.sep).join("/");
  const parts = normalized.split("/");
  const base = path.basename(item);
  if (
    parts.some((part) => ALWAYS_EXCLUDED_PARTS.has(part)) ||
    base.endsWith(".pyc") ||
    base.endsWith(".pyo") ||
    base.endsWith(".egg-info")
  ) {
    return false;
  }
  const stat = fs.lstatSync(item);
  if (stat.isSymbolicLink()) {
    return false;
  }
  if (normalized === "plugins/keep-going/runtime-root" || normalized === "plugins/keep-going/.repo-root") {
    return false;
  }
  if (PUBLIC_RUNTIME_FILES.has(normalized)) {
    return true;
  }
  if (stat.isDirectory()) {
    return [...PUBLIC_RUNTIME_FILES].some((allowed) => allowed.startsWith(`${normalized}/`));
  }
  return false;
}

function assertPublicRuntimeTree(root) {
  for (const entry of walkFiles(root)) {
    if (entry.isSymlink) {
      throw new Error(`public runtime must not contain symlink: ${entry.relative}`);
    }
    const violations = contentViolations(fs.readFileSync(entry.path));
    const expectedHash = REVIEWED_MEDIA_SHA256.get(entry.relative);
    if (expectedHash) {
      const actualHash = crypto.createHash("sha256").update(fs.readFileSync(entry.path)).digest("hex");
      if (actualHash !== expectedHash) {
        violations.push("reviewed media hash mismatch");
      }
    }
    if (violations.length > 0) {
      throw new Error(`public runtime privacy violation: ${entry.relative}: ${violations.join(", ")}`);
    }
  }
}

function walkFiles(root) {
  const output = [];
  const pending = [path.resolve(root)];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const dirent of fs.readdirSync(current, { withFileTypes: true })) {
      const item = path.join(current, dirent.name);
      const relative = path.relative(root, item).split(path.sep).join("/");
      if (dirent.isSymbolicLink()) {
        output.push({ path: item, relative, isSymlink: true });
      } else if (dirent.isDirectory()) {
        pending.push(item);
      } else if (dirent.isFile()) {
        output.push({ path: item, relative, isSymlink: false });
      }
    }
  }
  return output;
}

function contentViolations(data) {
  const violations = [];
  if (data.includes(0)) {
    violations.push("binary content");
  }
  const magic = [
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    Buffer.from([0xff, 0xd8, 0xff]),
    Buffer.from("GIF87a"),
    Buffer.from("GIF89a"),
    Buffer.from("%PDF-"),
    Buffer.from([0x50, 0x4b, 0x03, 0x04]),
  ];
  if (magic.some((prefix) => data.subarray(0, prefix.length).equals(prefix))) {
    violations.push("binary/media content");
  }
  const text = data.toString("utf8");
  const homePattern = new RegExp("/" + "Users/(?!(?:USER|sample)(?:/|\\b)|<[^>]+>|\\{[^}]+\\})[^/\\s]+");
  if (homePattern.test(text)) {
    violations.push("absolute user-home path");
  }
  const emailPattern = /[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})/g;
  const domains = [...text.matchAll(emailPattern)].map((match) => match[1].toLowerCase());
  if (domains.some((domain) => !SAFE_EMAIL_DOMAINS.has(domain))) {
    violations.push("non-placeholder email address");
  }
  const secretPatterns = [
    new RegExp("-".repeat(5) + "BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY" + "-".repeat(5)),
    new RegExp("(?:s" + "k-(?:proj-)?[A-Za-z0-9_-]{16,}|gh" + "[po]_[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox" + "[baprs]-[A-Za-z0-9_-]{16,})"),
    new RegExp("Bearer[ \\t]+[A-Za-z0-9._~-]{20,}", "i"),
    new RegExp("^[ \\t]*(?:export[ \\t]+)?(?=[A-Z0-9_]*(?:PASS" + "WORD|PASSWD|API_KEY|SECRET|TOKEN|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*[ \\t]*=)[A-Z][A-Z0-9_]*[ \\t]*=[ \\t]*[\"']?[^\\s\"'#]{6,}", "m"),
  ];
  if (secretPatterns.some((pattern) => pattern.test(text))) {
    violations.push("secret or private-key pattern");
  }
  return violations;
}

function samePath(left, right) {
  return path.resolve(left) === path.resolve(right);
}

function isInside(child, parent) {
  const relative = path.relative(parent, child);
  return Boolean(relative) && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function normalizeRuntimeVersion(version) {
  const value = String(version || "").trim();
  if (!value || value === "." || value === ".." || path.isAbsolute(value) || value.includes("/") || value.includes("\\")) {
    throw new Error(`invalid runtime version directory: ${version}`);
  }
  return value;
}

module.exports = {
  assertPublicRuntimeTree,
  contentViolations,
  copyRuntimeSource,
  defaultRuntimeHome,
  findRuntimeSource,
  prepareRuntime,
  runtimePath,
  shouldCopy,
  writeRuntimeRootMarker,
};
