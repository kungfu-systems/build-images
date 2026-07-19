#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import alphaContractWorldJson from "@kungfu-tech/buildchain/site/buildchain-contract.json" with { type: "json" };
import stableContractWorldJson from "@kungfu-tech/buildchain-stable/site/buildchain-contract.json" with { type: "json" };
import {
  evaluateBuildchainContractLock,
  readBuildchainContractLock,
  readBuildchainContractWorld,
} from "@kungfu-tech/buildchain/buildchain-contract";
import {
  checkKfdUpstreamFacts,
  collectKfdAggregate,
  collectKfdStatus,
  collectKfdUpstreamFacts,
  kfd3,
} from "@kungfu-tech/buildchain/kfd";
import {
  createKfd1ReleaseGateEvidence,
  resolveKfd1Metadata,
  sha256File,
  sha256Json,
  validateKfd2TrustTaxonomyEntry,
  validateKfd1ReleaseGateEvidence,
} from "@kungfu-tech/buildchain/kfd-gate";

const cwd = process.cwd();
const KFD3_REGISTRY_PATH = ".buildchain/kfd/kfd-3/surfaces.json";
const KFD3_RELEASE_ARTIFACT_NAME = "ghcr.io/kungfu-systems/build-images/base-linux";
const ALPHA_CONTRACT_LOCK_PATH = ".buildchain/alpha-contract-lock.json";
const STABLE_CONTRACT_LOCK_PATH = ".buildchain/contract-lock.json";
const STABLE_EVIDENCE_TIME = "1970-01-01T00:00:00.000Z";

process.env.BUILDCHAIN_KFD3_DETECTED_AT = process.env.BUILDCHAIN_KFD3_DETECTED_AT || STABLE_EVIDENCE_TIME;

function repoPath(relativePath) {
  return path.join(cwd, relativePath);
}

function readJson(relativePath) {
  return JSON.parse(fs.readFileSync(repoPath(relativePath), "utf8"));
}

function writeJson(relativePath, value) {
  const filePath = repoPath(relativePath);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function assertFile(relativePath) {
  const filePath = repoPath(relativePath);
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    throw new Error(`required file is missing: ${relativePath}`);
  }
}

function assertPassed(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function sha256RepoFile(relativePath) {
  assertFile(relativePath);
  return sha256File(repoPath(relativePath));
}

function stableGeneratedEvidence(value) {
  if (Array.isArray(value)) {
    return value.map((entry) => stableGeneratedEvidence(entry));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const next = {};
  for (const [key, entry] of Object.entries(value)) {
    next[key] = key === "cwd" ? "" : stableGeneratedEvidence(entry);
  }
  if (next.product && typeof next.product === "object" && !Array.isArray(next.product) && "version" in next.product) {
    next.product = { ...next.product, version: "" };
  }
  return next;
}

function contractSurface(relativePath, id = relativePath) {
  const digest = sha256RepoFile(relativePath);
  return {
    name: id,
    sourcePath: relativePath,
    sourceSha256: digest,
    artifactPath: relativePath,
    expectedSha256: digest,
    byteForByte: true,
  };
}

function checkLayout() {
  const status = collectKfdStatus({ cwd });
  assertPassed(status.layout.status === "current", `Buildchain layout is not current: ${status.layout.status}`);
  assertPassed(status.paths.config.path === ".buildchain/buildchain.toml", "Buildchain config must live under .buildchain/");
  assertPassed(status.paths.contractLock.exists, "Buildchain contract lock is missing");
  assertFile(ALPHA_CONTRACT_LOCK_PATH);
  assertPassed(status.paths.kfd3SurfaceRegistry.exists, "KFD-3 surface registry is missing");
  return status;
}

function refreshKfd3Registry() {
  const result = kfd3.registerSurfaces({
    cwd,
    registryPath: KFD3_REGISTRY_PATH,
    kinds: ["documentation"],
    product: { name: "Kungfu Build Images" },
  });
  assertPassed(result.registrySurfaceCount > 0, "KFD-3 registry has no surfaces");
  return result;
}

function checkContractLocks() {
  const tmpAlphaContractPath = repoPath(".buildchain/tmp/alpha-buildchain-contract.json");
  const tmpStableContractPath = repoPath(".buildchain/tmp/stable-buildchain-contract.json");
  fs.mkdirSync(path.dirname(tmpAlphaContractPath), { recursive: true });
  fs.writeFileSync(tmpAlphaContractPath, `${JSON.stringify(alphaContractWorldJson, null, 2)}\n`);
  fs.writeFileSync(tmpStableContractPath, `${JSON.stringify(stableContractWorldJson, null, 2)}\n`);
  try {
    const alphaCurrent = readBuildchainContractWorld(tmpAlphaContractPath);
    const stableCurrent = readBuildchainContractWorld(tmpStableContractPath);
    const stableLock = readBuildchainContractLock(repoPath(STABLE_CONTRACT_LOCK_PATH));
    const alphaLock = readBuildchainContractLock(repoPath(ALPHA_CONTRACT_LOCK_PATH));
    const stableEvaluation = evaluateBuildchainContractLock({
      lock: stableLock,
      current: stableCurrent,
      runtimeRef: stableLock?.buildchain?.ref || "v2",
      runtimeSha: stableLock?.buildchain?.resolvedSha || "",
      runtimeClass: "stable",
      compatibilityPolicy: stableLock?.buildchain?.compatibilityPolicy || "major-compatible",
    });
    assertPassed(stableEvaluation.ok, `Stable Buildchain contract lock failed: ${(stableEvaluation.reasons || []).join("; ")}`);
    const alphaEvaluation = evaluateBuildchainContractLock({
      lock: alphaLock,
      current: alphaCurrent,
      runtimeRef: alphaLock?.buildchain?.ref || "v2-alpha",
      runtimeSha: alphaLock?.buildchain?.resolvedSha || "",
      runtimeClass: "alpha",
      compatibilityPolicy: alphaLock?.buildchain?.compatibilityPolicy || "major-compatible",
    });
    assertPassed(alphaEvaluation.ok, `Alpha Buildchain contract lock failed: ${(alphaEvaluation.reasons || []).join("; ")}`);
    assertPassed(
      alphaLock.buildchain.contractDigest === alphaCurrent.contractDigest,
      "Alpha Buildchain contract lock must match the installed Buildchain contract exactly",
    );
    assertPassed(
      stableLock.buildchain.contractDigest === stableCurrent.contractDigest,
      "Stable Buildchain contract lock must match the installed stable Buildchain contract exactly",
    );
    assertPassed(stableLock.buildchain.ref === "v2", `Stable Buildchain ref must be v2, got ${stableLock.buildchain.ref}`);
    assertPassed(alphaLock.buildchain.ref === "v2-alpha", `Alpha Buildchain ref must be v2-alpha, got ${alphaLock.buildchain.ref}`);
    assertPassed(alphaLock.buildchain.majorLine === stableLock.buildchain.majorLine, "Alpha and stable Buildchain locks must use the same major line");
    assertPassed(stableLock.buildchain.compatibilityPolicy === "major-compatible", "Stable Buildchain lock must use major-compatible policy");
    assertPassed(alphaLock.buildchain.compatibilityPolicy === "major-compatible", "Alpha Buildchain lock must use major-compatible policy");
    return { stableEvaluation, alphaEvaluation, stableLock, alphaLock };
  } finally {
    fs.rmSync(repoPath(".buildchain/tmp"), { recursive: true, force: true });
  }
}

function writeKfd1Witness() {
  const metadata = resolveKfd1Metadata();
  const surfaces = [
    contractSurface("README.md", "readme"),
    contractSurface("docs/image-contract.md", "image-contract"),
    contractSurface("docs/release-and-tags.md", "release-and-tags"),
    contractSurface("docs/runner-boundary.md", "runner-boundary"),
    contractSurface("images.lock.json", "image-lock"),
    contractSurface("scripts/write-publish-evidence.py", "publish-evidence-writer"),
  ];
  const witness = {
    id: "build-images-release-contract-world",
    standard: metadata.key,
    source: {
      repo: "kungfu-systems/build-images",
      ref: "",
    },
    contractWorld: {
      schemaId: metadata.schemaIds.contractWorld,
      digest: `sha256:${sha256Json(surfaces)}`,
    },
    canonicalPolicy: {
      path: "docs/release-and-tags.md",
      sha256: sha256RepoFile("docs/release-and-tags.md"),
    },
    registry: {
      path: KFD3_REGISTRY_PATH,
      sha256: sha256RepoFile(KFD3_REGISTRY_PATH),
    },
    surfaces,
  };
  writeJson(".buildchain/kfd/kfd-1/build-images-contract-world.witness.json", witness);
  const gate = createKfd1ReleaseGateEvidence({
    cwd,
    artifactRoot: cwd,
    witnesses: [witness],
    verifiedAt: STABLE_EVIDENCE_TIME,
  });
  validateKfd1ReleaseGateEvidence(gate);
  assertPassed(gate.passportSection?.status === "passed", "KFD-1 release gate did not pass");
  writeJson(".buildchain/kfd/kfd-1/release-gate.json", gate.passportSection);
  return { witness, gate };
}

function checkUpstream() {
  const facts = stableGeneratedEvidence(collectKfdUpstreamFacts({ cwd }));
  const check = checkKfdUpstreamFacts(facts);
  assertPassed(check.ok, `KFD upstream check failed: ${JSON.stringify(check.issues || check, null, 2)}`);
  writeJson(".buildchain/kfd/kfd-2/kfd-upstream-aggregate.json", facts);
  const aggregate = stableGeneratedEvidence(collectKfdAggregate({ cwd }));
  assertPassed(aggregate.upstreamCheck?.status === "passed", "KFD aggregate upstream check did not pass");
  writeJson(".buildchain/kfd/kfd-2/kfd-aggregate.json", aggregate);
  return { facts, check, aggregate };
}

function writeKfd2Claim({ kfd1Witness, upstreamFacts }) {
  const sourceBindings = [
    { id: "readme", path: "README.md", sha256: sha256RepoFile("README.md") },
    { id: "image-contract", path: "docs/image-contract.md", sha256: sha256RepoFile("docs/image-contract.md") },
    { id: "release-and-tags", path: "docs/release-and-tags.md", sha256: sha256RepoFile("docs/release-and-tags.md") },
    { id: "image-lock", path: "images.lock.json", sha256: sha256RepoFile("images.lock.json") },
    { id: "buildchain-config", path: ".buildchain/buildchain.toml", sha256: sha256RepoFile(".buildchain/buildchain.toml") },
    { id: "alpha-contract-lock", path: ALPHA_CONTRACT_LOCK_PATH, sha256: sha256RepoFile(ALPHA_CONTRACT_LOCK_PATH) },
    { id: "stable-contract-lock", path: STABLE_CONTRACT_LOCK_PATH, sha256: sha256RepoFile(STABLE_CONTRACT_LOCK_PATH) },
  ];
  const machineEvidence = [
    {
      id: "kfd-1-contract-world-witness",
      path: ".buildchain/kfd/kfd-1/build-images-contract-world.witness.json",
      sha256: sha256RepoFile(".buildchain/kfd/kfd-1/build-images-contract-world.witness.json"),
      contract: "kfd-1",
      status: "passed",
    },
    {
      id: "kfd-upstream-aggregate",
      path: ".buildchain/kfd/kfd-2/kfd-upstream-aggregate.json",
      sha256: sha256RepoFile(".buildchain/kfd/kfd-2/kfd-upstream-aggregate.json"),
      contract: "kungfu-buildchain-kfd-upstream-aggregate",
      status: "passed",
    },
    {
      id: "kfd-3-surface-registry",
      path: KFD3_REGISTRY_PATH,
      sha256: sha256RepoFile(KFD3_REGISTRY_PATH),
      contract: "kungfu-buildchain-kfd-3-surface-registry",
      status: "passed",
    },
  ];
  const claim = {
    id: "claim:build-images-release-trust",
    public: true,
    claim: "Build Images releases are governed by declared image contracts, Buildchain version-state promotion, channel-specific locked Buildchain runtime contracts, and KFD upstream evidence.",
    sourceBindings,
    machineEvidence,
    hashes: {
      kfd1WitnessSha256: sha256FileForValue(kfd1Witness),
      upstreamAggregateSha256: sha256FileForValue(upstreamFacts),
      sourceBindingsSha256: sha256Json(sourceBindings),
      machineEvidenceSha256: sha256Json(machineEvidence),
    },
    artifacts: [
      { id: "oci-image-lock", path: "images.lock.json", sha256: sha256RepoFile("images.lock.json") },
      { id: "publish-evidence-writer", path: "scripts/write-publish-evidence.py", sha256: sha256RepoFile("scripts/write-publish-evidence.py") },
    ],
    verification: {
      result: "passed",
      method: "pnpm run check:kfd",
    },
    auditBoundary: {
      mode: "machine-bound-release-contract",
      scope: "Build Images repository config, declared public surfaces, image lock, alpha/stable Buildchain runtime contract locks, and publish evidence writer.",
    },
    responsibility: {
      owner: "Kungfu Build Images maintainers",
      sourceOwner: "Kungfu Build Images maintainers",
      releasePassportProofOwner: "Buildchain",
    },
    residualRisk: [],
  };
  validateKfd2TrustTaxonomyEntries(claim.residualRisk, "residualRisk");
  writeJson(".buildchain/kfd/kfd-2/release-claims.json", claim);
  return claim;
}

function sha256FileForValue(value) {
  return sha256Json(value);
}

function validateKfd2TrustTaxonomyEntries(entries, kind) {
  for (const [index, entry] of entries.entries()) {
    validateKfd2TrustTaxonomyEntry(entry, { kind, label: `${kind}[${index}]` });
  }
}

function writeKfd3Witnesses() {
  const registryPath = KFD3_REGISTRY_PATH;
  const audit = kfd3.auditSurfaces({ cwd, registryPath });
  assertPassed(audit.ok, `KFD-3 audit failed: ${JSON.stringify(audit.issues || audit, null, 2)}`);
  assertPassed(audit.status === "passed", `KFD-3 audit status is ${audit.status}`);
  const prebuild = stableGeneratedEvidence(kfd3.createSurfaceWitness({ cwd, registryPath, kind: "prebuild", sourceSha: "" }));
  const shippedSurfaces = prebuild.collaborationInterface.surfaces.map((surface) => ({
    ...surface,
    state: "shipped",
    availability: "shipped",
  }));
  prebuild.collaborationInterface = {
    ...prebuild.collaborationInterface,
    surfaces: shippedSurfaces,
  };
  prebuild.collaborationInterfaceDigest = `sha256:${sha256Json(prebuild.collaborationInterface)}`;
  const artifact = {
    ...stableGeneratedEvidence(kfd3.createSurfaceWitness({ cwd, registryPath, kind: "artifact", sourceSha: "" })),
    artifact: {
      name: KFD3_RELEASE_ARTIFACT_NAME,
    },
    collaborationInterface: prebuild.collaborationInterface,
    collaborationInterfaceDigest: prebuild.collaborationInterfaceDigest,
    exposedSurfaces: shippedSurfaces,
  };
  writeJson(".buildchain/kfd/kfd-3/collaboration-interface.prebuild.json", prebuild);
  writeJson(".buildchain/kfd/kfd-3/collaboration-interface.artifact.json", artifact);
  const gate = kfd3.createReleaseGateEvidence({
    prebuildWitnesses: [prebuild],
    artifactWitnesses: [artifact],
    verifiedAt: STABLE_EVIDENCE_TIME,
  });
  kfd3.validateReleaseGateEvidence(gate);
  writeJson(".buildchain/kfd/kfd-3/release-gate.json", gate);
  return { audit, prebuild, artifact, gate };
}

function runReleasePassportSmoke() {
  const packageJson = readJson("package.json");
  const smokeSha = process.env.GITHUB_SHA || execFileSync("git", ["rev-parse", "HEAD"], { cwd, encoding: "utf8" }).trim();
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "build-images-kfd-passport-"));
  try {
    const outputDir = path.join(tmpDir, "release-passport");
    const publishEvidencePath = path.join(tmpDir, "publish-evidence.json");
    const publishEvidence = {
      schema: 1,
      version: packageJson.version,
      channel: "release",
      source_sha: smokeSha,
      release_sha: smokeSha,
      target_ref: "release/v1/v1.3",
      release_material_sha: smokeSha,
      publish_tooling_sha: smokeSha,
      artifacts: [
        {
          group: "image",
          kind: "oci",
          name: KFD3_RELEASE_ARTIFACT_NAME,
          ref: `v${packageJson.version}`,
          digest: readJson("images.lock.json").images[0].digest,
        },
      ],
    };
    fs.writeFileSync(publishEvidencePath, `${JSON.stringify(publishEvidence, null, 2)}\n`);
    runJsonCommand([
      "exec",
      "buildchain",
      "collect",
      "github-release",
      "--tag",
      `v${packageJson.version}`,
      "--repository",
      "kungfu-systems/build-images",
      "--product-name",
      "Kungfu Build Images",
      "--package-name",
      packageJson.name,
      "--package-version",
      packageJson.version,
      "--publish-evidence-json",
      publishEvidencePath,
      "--impact-json",
      ".buildchain/release-impact.json",
      "--kfd-1-witness-json",
      ".buildchain/kfd/kfd-1/build-images-contract-world.witness.json",
      "--kfd-2-claim-json",
      ".buildchain/kfd/kfd-2/release-claims.json",
      "--kfd-3-prebuild-witness-json",
      ".buildchain/kfd/kfd-3/collaboration-interface.prebuild.json",
      "--kfd-3-artifact-witness-json",
      ".buildchain/kfd/kfd-3/collaboration-interface.artifact.json",
      "--output-dir",
      outputDir,
      "--json",
    ]);
    const verifyOutput = runJsonCommand([
      "exec",
      "buildchain",
      "verify",
      "release-passport",
      path.join(outputDir, "buildchain.release.json"),
      "--json",
    ]);
    const verify = JSON.parse(verifyOutput);
    assertPassed(verify.ok, `release passport smoke failed: ${JSON.stringify(verify.issues || verify, null, 2)}`);
    return { ok: true };
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function runJsonCommand(args) {
  try {
    return execFileSync("pnpm", args, { cwd, stdio: "pipe", encoding: "utf8" });
  } catch (error) {
    const stdout = error.stdout ? String(error.stdout) : "";
    const stderr = error.stderr ? String(error.stderr) : "";
    throw new Error(`pnpm ${args.join(" ")} failed\nstdout:\n${stdout}\nstderr:\n${stderr}`);
  }
}

function main() {
  refreshKfd3Registry();
  const layout = checkLayout();
  const contractLocks = checkContractLocks();
  const upstream = checkUpstream();
  const kfd1 = writeKfd1Witness();
  const kfd2Claim = writeKfd2Claim({ kfd1Witness: kfd1.witness, upstreamFacts: upstream.facts });
  const kfd3Evidence = writeKfd3Witnesses();
  const releasePassportSmoke = runReleasePassportSmoke();
  const summary = {
    schemaVersion: 1,
    contract: "kungfu-build-images-kfd123-check",
    status: "passed",
    buildchain: {
      configPath: layout.paths.config.path,
      alphaContractLockRef: contractLocks.alphaLock.buildchain.ref,
      alphaContractLockStatus: contractLocks.alphaEvaluation.status,
      alphaContractDrift: Boolean(contractLocks.alphaEvaluation.drift),
      alphaContractDigest: contractLocks.alphaLock.buildchain.contractDigest,
      stableContractLockRef: contractLocks.stableLock.buildchain.ref,
      stableContractLockStatus: contractLocks.stableEvaluation.status,
      stableContractDrift: Boolean(contractLocks.stableEvaluation.drift),
      stableContractDigest: contractLocks.stableLock.buildchain.contractDigest,
    },
    kfd1: {
      witness: ".buildchain/kfd/kfd-1/build-images-contract-world.witness.json",
      surfaceCount: kfd1.witness.surfaces.length,
    },
    kfd2: {
      claim: ".buildchain/kfd/kfd-2/release-claims.json",
      upstreamCount: upstream.facts.summary?.upstreamCount ?? 0,
      claimStatus: kfd2Claim.status || "passed",
    },
    kfd3: {
      registry: KFD3_REGISTRY_PATH,
      auditStatus: kfd3Evidence.audit.status,
      declared: kfd3Evidence.audit.summary.declared,
      prebuildWitness: ".buildchain/kfd/kfd-3/collaboration-interface.prebuild.json",
      artifactWitness: ".buildchain/kfd/kfd-3/collaboration-interface.artifact.json",
    },
    releasePassportSmoke,
  };
  writeJson(".buildchain/kfd/kfd123-summary.json", summary);
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

try {
  main();
} catch (error) {
  console.error(`check-kfd123: ${error.message}`);
  process.exitCode = 1;
}
