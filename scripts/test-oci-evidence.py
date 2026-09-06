#!/usr/bin/env python3
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


projection = module("project-oci-evidence")
accept = module("accept-image-summary")


class OciEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.manifests = accept.load_manifests()
        digest = lambda name: "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        self.family = {"schema": "kungfu-buildchain-oci-family/v1", "repository": "kungfu-systems/build-images",
                       "root": digest("family"), "sourceSha": "a" * 40, "version": "1.3.0-alpha.28",
                       "expectedImages": list(self.manifests), "images": []}
        for name, manifest in self.manifests.items():
            parent = manifest.get("base", {}).get("image")
            self.family["images"].append({"name": name, "repository": accept.REPOSITORY_PREFIX + name,
                "digest": digest(name), "platform": accept.PLATFORMS[manifest["platform"]], "action": "built",
                "contractMajor": manifest["contract_major"], "parentDigest": digest(parent) if parent else None,
                "content": {"sourceSha": "a" * 40, "materialSha": "a" * 40, "version": self.family["version"]},
                "smoke": {"path": name + "-smoke.json", "sha256": digest(name + "smoke")}})
        self.readback = {"schema": "kungfu-buildchain-oci-publication-readback/v1", "familyRoot": self.family["root"],
                         "candidateSourceSha": "a" * 40, "sourceSha": "b" * 40, "version": self.family["version"],
                         "images": [dict(i, ref="v" + self.family["version"], anonymous=True) for i in self.family["images"]]}

    def project(self, readback=None):
        return projection.project(readback or self.readback, self.family, "b" * 40, "c" * 40, "alpha/v1/v1.3")

    def test_complete_readback_round_trips_existing_lock_acceptance(self):
        summary = accept.summary_from_evidence(self.project(), self.manifests)
        lock = accept.accepted_lock(summary, self.manifests, "https://github.com/kungfu-systems/build-images/actions/runs/123")
        self.assertEqual(len(lock["images"]), len(self.manifests))

    def test_missing_private_or_conflicting_image_cannot_be_projected(self):
        for mutate in [lambda r: r["images"].pop(), lambda r: r["images"][0].update(anonymous=False),
                       lambda r: r["images"][0].update(digest="sha256:" + "f" * 64),
                       lambda r: r["images"][0].update(content={"version": "wrong"})]:
            readback = copy.deepcopy(self.readback)
            mutate(readback)
            with self.assertRaises(ValueError):
                self.project(readback)


if __name__ == "__main__":
    unittest.main()
