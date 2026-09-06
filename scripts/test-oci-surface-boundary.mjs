import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { kfd3 } from "@kungfu-tech/buildchain/kfd";

const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "build-images-oci-surface-"));
const registryPath = ".buildchain/kfd/kfd-3/surfaces.json";
try {
  for (const entry of ["docs", "README.md", registryPath]) {
    fs.mkdirSync(path.dirname(path.join(cwd, entry)), { recursive: true });
    fs.cpSync(entry, path.join(cwd, entry), { recursive: true });
  }
  const write = (name, body) => {
    fs.mkdirSync(path.dirname(path.join(cwd, name)), { recursive: true });
    fs.writeFileSync(path.join(cwd, name), body);
  };
  write("build/oci-candidate/oci/oci-layout", '{"imageLayoutVersion":"1.0.0"}');
  write(`build/oci-candidate/oci/blobs/sha256/${"a".repeat(64)}`, "fixture blob");
  const audit = () => kfd3.auditSurfaces({ cwd, registryPath });
  const qualified = audit();
  assert.equal(qualified.status, "passed");
  assert.equal(qualified.comparison.distributionBindings.length, 2);
  assert.ok(qualified.comparison.distributionBindings.every(
    (binding) => binding.declaredSurfaceId === "doc:docs/image-contract.md",
  ));
  write("build/unregistered-tool", "undeclared executable");
  const unexpected = audit();
  assert.equal(unexpected.status, "partial");
  assert.deepEqual(unexpected.comparison.detectedButUnregistered.map((s) => s.artifactPath),
    ["build/unregistered-tool"]);
  console.log("OCI distribution boundary passes; unrelated binaries remain blocked");
} finally {
  fs.rmSync(cwd, { recursive: true, force: true });
}
