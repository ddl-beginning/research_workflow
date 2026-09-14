import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const fixtureDir = path.dirname(fileURLToPath(import.meta.url));
const encoded = await fs.readFile(path.join(fixtureDir, 'tiny.png.b64'), 'utf8');
await fs.writeFile(path.join(fixtureDir, 'tiny.png'), Buffer.from(encoded.trim(), 'base64'));
