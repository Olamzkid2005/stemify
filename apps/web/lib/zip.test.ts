import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { describe, it } from "node:test";

import { readZipEntryFromFile, writeZipArchive } from "@/lib/zip";

/**
 * Round-trip smoke test (roadmap A3): the writer's archive must be readable
 * by readZipEntryFromFile AND by Python's zipfile — the worker-built archives
 * and the web-built custom archives must stay mutually compatible.
 */
describe("zip round-trip", () => {
  it("writes a deterministic archive that our reader and Python zipfile both read", async () => {
    const dir = await mkdtemp(path.join(tmpdir(), "stemify-zip-test-"));
    try {
      const stemA = Buffer.alloc(300_000, 7);
      const stemB = Buffer.from("manifest-like payload".repeat(100), "utf8");
      const fileA = path.join(dir, "vocals.mp3");
      const fileB = path.join(dir, "drums.mp3");
      const { writeFile } = await import("node:fs/promises");
      await writeFile(fileA, stemA);
      await writeFile(fileB, stemB);

      const dest = path.join(dir, "out.zip");
      const entries = [
        { name: "vocals.mp3", kind: "file" as const, path: fileA },
        { name: "drums.mp3", kind: "file" as const, path: fileB },
        { name: "manifest.json", kind: "buffer" as const, data: stemB },
      ];
      const size1 = await writeZipArchive(dest, entries);
      const first = await readFile(dest);

      // Determinism: same input, same bytes.
      const dest2 = path.join(dir, "out2.zip");
      const size2 = await writeZipArchive(dest2, entries);
      assert.equal(size2, size1);
      assert.ok(first.equals(await readFile(dest2)));

      // Our positioned reader recovers each entry.
      assert.ok(stemA.equals(await (await import("node:fs/promises")).readFile(fileA)));
      assert.ok(stemB.equals((await readZipEntryFromFile(dest, "manifest.json"))!));

      // Python (the worker's ecosystem) reads the archive too.
      const pythonScript = `
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    assert names == ["vocals.mp3", "drums.mp3", "manifest.json"], names
    assert archive.read("vocals.mp3") == open(sys.argv[2], "rb").read()
    assert archive.read("drums.mp3") == open(sys.argv[3], "rb").read()
    assert archive.read("manifest.json") == b"manifest-like payload" * 100
    info = archive.getinfo("vocals.mp3")
    assert info.date_time == (1980, 1, 1, 0, 0, 0), info.date_time
print("python-zipfile-ok")
`;
      const stdout = execFileSync(
        process.env.PYTHON ?? "python",
        ["-c", pythonScript, dest, fileA, fileB],
        { encoding: "utf8" },
      );
      assert.ok(stdout.includes("python-zipfile-ok"));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it("reads the manifest out of a Python-built worker archive", async () => {
    // Build a worker-style archive with Python (deflate, zeroed timestamps),
    // then read one entry back with positioned reads.
    const dir = await mkdtemp(path.join(tmpdir(), "stemify-zip-py-"));
    try {
      const manifestPayload = JSON.stringify({ output: { format: "mp3", stems: [1, 2, 3] } });
      const bigStem = Buffer.alloc(500_000, 3);
      const stemPath = path.join(dir, "big.mp3");
      const { writeFile } = await import("node:fs/promises");
      await writeFile(stemPath, bigStem);
      const pythonScript = `
import sys, zipfile, json
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as archive:
    archive.write(sys.argv[2], "vocals.mp3")
    archive.writestr("manifest.json", sys.argv[3])
print("ok")
`;
      const workerZip = path.join(dir, "worker.zip");
      execFileSync(process.env.PYTHON ?? "python", ["-c", pythonScript, workerZip, stemPath, manifestPayload], {
        encoding: "utf8",
      });

      const extracted = await readZipEntryFromFile(workerZip, "manifest.json");
      assert.ok(extracted);
      assert.deepEqual(JSON.parse(extracted.toString("utf8")), {
        output: { format: "mp3", stems: [1, 2, 3] },
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
