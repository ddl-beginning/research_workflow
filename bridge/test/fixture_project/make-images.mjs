import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';

const root = path.dirname(fileURLToPath(import.meta.url));
const crcTable = Array.from({ length: 256 }, (_, index) => {
  let value = index;
  for (let bit = 0; bit < 8; bit += 1) value = (value & 1) ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  return value >>> 0;
});

function crc32(buffer) {
  let value = 0xffffffff;
  for (const byte of buffer) value = crcTable[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const typeBuffer = Buffer.from(type, 'ascii');
  const checksumInput = Buffer.concat([typeBuffer, data]);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(checksumInput));
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  return Buffer.concat([length, checksumInput, checksum]);
}

function makePng(width, height, pixels) {
  const rows = [];
  for (let row = 0; row < height; row += 1) {
    rows.push(Buffer.from([0]));
    rows.push(pixels.subarray(row * width * 4, (row + 1) * width * 4));
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(Buffer.concat(rows))),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

function shorelineScene(variant) {
  const width = 16;
  const height = 16;
  const water = [35, 104, 154, 255];
  const land = [220, 214, 166, 255];
  const boundary = [184, 112, 54, 255];
  const speckle = [238, 176, 48, 255];
  const pixels = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    // A simple diagonal shoreline makes the fixture interpretable at a glance.
    // The after result erodes the upper-right boundary by one pixel and removes
    // the fixed set of bright speckles, matching the metrics in metrics.json.
    const shoreline = 7 + Math.floor(y / 4) + (variant === 'after' && y < 9 ? 1 : 0);
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4;
      const color = x >= shoreline ? land : water;
      pixels.set(color, offset);
      if (x === shoreline - 1 || x === shoreline) pixels.set(boundary, offset);
    }
  }
  if (variant === 'before') {
    for (const [x, y] of [[2, 3], [12, 5], [4, 12], [14, 13]]) {
      pixels.set(speckle, (y * width + x) * 4);
    }
  }
  return pixels;
}

const images = {
  result_before: shorelineScene('before'),
  result_after: shorelineScene('after'),
};
for (const [name, pixels] of Object.entries(images)) {
  const png = await makePng(16, 16, pixels);
  await fs.writeFile(path.join(root, `${name}.png`), png);
  await fs.writeFile(path.join(root, `${name}.png.b64`), `${png.toString('base64')}\n`);
}
