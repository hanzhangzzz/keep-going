#!/usr/bin/env node
"use strict";

const path = require("path");

const { copyRuntimeSource } = require("../lib/runtime");

function main(argv) {
  const packageRoot = path.resolve(__dirname, "..");
  const repoRoot = path.resolve(packageRoot, "..", "..");
  const options = parseArgs(argv);
  const out = path.resolve(options.out || path.join(packageRoot, "runtime"));
  copyRuntimeSource(repoRoot, out, {
    replace: true,
  });
  console.log(`prepared Keep Going runtime: ${out}`);
}

function parseArgs(argv) {
  const options = {};
  const args = [...argv];
  while (args.length > 0) {
    const flag = args.shift();
    switch (flag) {
      case "--out":
        if (args.length === 0 || args[0].startsWith("-")) {
          throw new Error("missing value for --out");
        }
        options.out = args.shift();
        break;
      default:
        throw new Error(`unknown option: ${flag}`);
    }
  }
  return options;
}

try {
  main(process.argv.slice(2));
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`prepare-runtime: ${message}`);
  process.exit(1);
}
