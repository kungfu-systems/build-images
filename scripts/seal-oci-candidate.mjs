import fs from "node:fs";
import { sealOciPublicationBundle } from "@kungfu-tech/buildchain/oci-publication";

const bundleRoot = "build/oci-candidate";
const body = JSON.parse(fs.readFileSync(`${bundleRoot}/oci-family.input.json`));
const manifest = sealOciPublicationBundle({ bundleRoot, body });
fs.writeFileSync(`${bundleRoot}/oci-family.json`, `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Qualified ${manifest.images.length} OCI images: ${manifest.root}`);
