/**
 * Minimal deterministic ZIP support for custom stem archives (roadmap A3).
 *
 * The reader extracts one known entry (manifest.json) from the worker's
 * results ZIP with positioned reads — only the central directory tail and the
 * entry's compressed bytes touch memory. The writer produces the same layout
 * as the worker (deflate, zeroed timestamps, entries in given order). No
 * third-party dependency.
 */
import { createReadStream, createWriteStream } from "node:fs";
import { open, stat, unlink } from "node:fs/promises";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { FileHandle } from "node:fs/promises";
import { createDeflateRaw, inflateRawSync } from "node:zlib";

const LOCAL_SIGNATURE = 0x04034b50;
const CENTRAL_SIGNATURE = 0x02014b50;
const EOCD_SIGNATURE = 0x06054b50;

/** DOS date/time for 1980-01-01 00:00:00, the ZIP epoch (matches the worker). */
const DOS_TIME = 0;
const DOS_DATE = (1 << 5) | 1; // month 1, day 1, year 0 (= 1980)
const METHOD_DEFLATE = 8;

const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[i] = c >>> 0;
  }
  return table;
})();

/** Streaming CRC-32 for archives assembled entry by entry. */
class IncrementalCrc {
  private state = 0xffffffff;

  update(chunk: Buffer): void {
    for (let i = 0; i < chunk.length; i++) {
      this.state = CRC32_TABLE[(this.state ^ chunk[i]) & 0xff] ^ (this.state >>> 8);
    }
  }

  digest(): number {
    return (this.state ^ 0xffffffff) >>> 0;
  }
}

type CentralEntry = {
  name: string;
  method: number;
  compressedSize: number;
  localHeaderOffset: number;
};

function parseCentralDirectory(buffer: Buffer, count: number): CentralEntry[] {
  const entries: CentralEntry[] = [];
  let offset = 0;
  for (let i = 0; i < count; i++) {
    if (offset + 46 > buffer.length || buffer.readUInt32LE(offset) !== CENTRAL_SIGNATURE) {
      throw new Error("corrupt central directory");
    }
    const method = buffer.readUInt16LE(offset + 10);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const localHeaderOffset = buffer.readUInt32LE(offset + 42);
    entries.push({
      name: buffer.toString("utf8", offset + 46, offset + 46 + nameLength),
      method,
      compressedSize,
      localHeaderOffset,
    });
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

/**
 * Read one entry from a ZIP on disk with positioned reads: only the central
 * directory tail and the requested entry's compressed bytes touch memory, so
 * the worker's multi-hundred-MB results archive can be opened to pull out
 * manifest.json alone. Returns null when the archive has no such entry.
 */
export async function readZipEntryFromFile(zipPath: string, entryName: string): Promise<Buffer | null> {
  const handle: FileHandle = await open(zipPath, "r");
  try {
    const fileSize = (await handle.stat()).size;
    const tailLength = Math.min(fileSize, 22 + 65535);
    const tail = Buffer.alloc(tailLength);
    await handle.read(tail, 0, tailLength, fileSize - tailLength);

    let eocdRelative = -1;
    for (let i = tail.length - 22; i >= 0; i--) {
      if (tail.readUInt32LE(i) === EOCD_SIGNATURE) {
        eocdRelative = i;
        break;
      }
    }
    if (eocdRelative < 0) throw new Error("not a ZIP archive (no end-of-central-directory record)");
    const count = tail.readUInt16LE(eocdRelative + 10);
    const centralOffsetAbsolute = tail.readUInt32LE(eocdRelative + 16);
    const centralStartInTail = centralOffsetAbsolute - (fileSize - tailLength);
    if (centralStartInTail < 0) throw new Error("unsupported ZIP layout (spans beyond tail)");

    const entry = parseCentralDirectory(tail.subarray(centralStartInTail), count).find(
      (candidate) => candidate.name === entryName,
    );
    if (!entry) return null;

    const localHeader = Buffer.alloc(30);
    await handle.read(localHeader, 0, 30, entry.localHeaderOffset);
    if (localHeader.readUInt32LE(0) !== LOCAL_SIGNATURE) throw new Error("corrupt local header");
    const localNameLength = localHeader.readUInt16LE(26);
    const localExtraLength = localHeader.readUInt16LE(28);
    const dataStart = entry.localHeaderOffset + 30 + localNameLength + localExtraLength;

    const compressed = Buffer.alloc(entry.compressedSize);
    await handle.read(compressed, 0, entry.compressedSize, dataStart);
    if (entry.method === 0) return compressed;
    if (entry.method === METHOD_DEFLATE) return inflateRawSync(compressed);
    throw new Error(`unsupported compression method ${entry.method}`);
  } finally {
    await handle.close();
  }
}

export type ZipEntrySource =
  | { name: string; kind: "buffer"; data: Buffer }
  | { name: string; kind: "file"; path: string };

function localHeaderBuffer(name: Buffer, crc: number, compressedSize: number, uncompressedSize: number): Buffer {
  const local = Buffer.alloc(30 + name.length);
  local.writeUInt32LE(LOCAL_SIGNATURE, 0);
  local.writeUInt16LE(20, 4); // version needed
  local.writeUInt16LE(0, 6); // flags: sizes are known, no data descriptor
  local.writeUInt16LE(METHOD_DEFLATE, 8);
  local.writeUInt16LE(DOS_TIME, 10);
  local.writeUInt16LE(DOS_DATE, 12);
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(compressedSize, 18);
  local.writeUInt32LE(uncompressedSize, 22);
  local.writeUInt16LE(name.length, 26);
  local.writeUInt16LE(0, 28); // extra length
  name.copy(local, 30);
  return local;
}

/**
 * Write a deterministic archive (worker layout: deflate level 6, zeroed
 * timestamps, rw-r--r--) processing one entry at a time. Each entry is
 * deflated into a reused temp file first so large stems never sit in memory
 * whole; the temp file is removed afterwards. Returns the archive size.
 */
export async function writeZipArchive(destPath: string, entries: ZipEntrySource[]): Promise<number> {
  const tempStaging = `${destPath}.deflate.tmp`;
  const output = createWriteStream(destPath, { flags: "w" });
  const centralParts: Buffer[] = [];
  let offset = 0;

  try {
    for (const entry of entries) {
      const name = Buffer.from(entry.name, "utf8");
      if (name.length > 0xffff) throw new Error("ZIP entry name too long");

      // Pass 1: deflate into the staging file. CRC-32 covers the
      // UNCOMPRESSED bytes; compressedSize counts the deflated bytes. The
      // source stream backpressures through zlib, so memory stays bounded.
      const crc = new IncrementalCrc();
      let compressedSize = 0;
      const source =
        entry.kind === "buffer"
          ? Readable.from(Buffer.from(entry.data))
          : createReadStream(entry.path);
      await pipeline(
        source,
        async function* (chunks: AsyncIterable<Buffer>) {
          for await (const chunk of chunks) {
            crc.update(chunk);
            yield chunk;
          }
        },
        createDeflateRaw({ level: 6 }),
        async function* (chunks: AsyncIterable<Buffer>) {
          for await (const chunk of chunks) {
            compressedSize += chunk.length;
            yield chunk;
          }
        },
        createWriteStream(tempStaging, { flags: "w" }),
      );
      const uncompressedSize =
        entry.kind === "buffer" ? entry.data.length : (await stat(entry.path)).size;

      // Pass 2: known-size local header, then the staged compressed bytes.
      const headerPosition = offset;
      const header = localHeaderBuffer(name, crc.digest(), compressedSize, uncompressedSize);
      await pipeline(Readable.from(Buffer.from(header)), output, { end: false });
      offset += header.length;
      await pipeline(createReadStream(tempStaging), output, { end: false });
      offset += compressedSize;

      const central = Buffer.alloc(46 + name.length);
      central.writeUInt32LE(CENTRAL_SIGNATURE, 0);
      central.writeUInt16LE(20, 4);
      central.writeUInt16LE(20, 6);
      central.writeUInt16LE(0, 8);
      central.writeUInt16LE(METHOD_DEFLATE, 10);
      central.writeUInt16LE(DOS_TIME, 12);
      central.writeUInt16LE(DOS_DATE, 14);
      central.writeUInt32LE(crc.digest(), 16);
      central.writeUInt32LE(compressedSize, 20);
      central.writeUInt32LE(uncompressedSize, 24);
      central.writeUInt16LE(name.length, 28);
      central.writeUInt16LE(0, 30);
      central.writeUInt16LE(0, 32);
      central.writeUInt16LE(0, 34);
      central.writeUInt16LE(0, 36);
      central.writeUInt32LE(0o644 << 16, 38);
      central.writeUInt32LE(headerPosition, 42);
      name.copy(central, 46);
      centralParts.push(central);
    }

    const centralDirectory = Buffer.concat(centralParts);
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(EOCD_SIGNATURE, 0);
    eocd.writeUInt16LE(0, 4);
    eocd.writeUInt16LE(0, 6);
    eocd.writeUInt16LE(entries.length, 8);
    eocd.writeUInt16LE(entries.length, 10);
    eocd.writeUInt32LE(centralDirectory.length, 12);
    eocd.writeUInt32LE(offset, 16); // start of central directory
    eocd.writeUInt16LE(0, 20);
    await pipeline(Readable.from(Buffer.concat([centralDirectory, eocd])), output, { end: true });
    offset += centralDirectory.length + 22;
  } catch (error) {
    output.destroy();
    await unlink(destPath).catch(() => {});
    throw error;
  } finally {
    await unlink(tempStaging).catch(() => {});
  }
  return offset;
}
