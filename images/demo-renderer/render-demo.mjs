#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { chromium } from 'playwright';
import xtermHeadless from '@xterm/headless';

const { Terminal } = xtermHeadless;

const VERSION = '1.1.0';
const MEDIA = ['demo.mp4', 'demo.webm', 'demo.gif', 'poster.png'];
const UTF8 = new TextDecoder('utf-8', { fatal: true });
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const MAX_CAPTURE_BYTES = 4 * 1024 * 1024;
const MAX_CAPTURE_EVENTS = 10_000;
const CAPTURE_NON_AUTHORITIES = [
  'first-party-identity',
  'system-identity',
  'kfd-compliance',
  'product-system-metadata',
  'package-metadata',
  'registry-history',
  'scan-output',
  'standalone-generation',
];

function fail(message) {
  process.stderr.write(`demo-renderer: ${message}\n`);
  process.exit(1);
}

function parseArguments(argv) {
  if (argv.length === 1 && argv[0] === '--version') {
    process.stdout.write(`${VERSION}\n`);
    process.exit(0);
  }
  if (argv.length === 1 && argv[0] === '--help') {
    process.stdout.write(
      'Usage: demo-renderer --scene FILE --transcript FILE --projection FILE [--terminal-capture FILE] --output DIR --renderer-image IMAGE@sha256:DIGEST\n',
    );
    process.exit(0);
  }
  const allowed = new Set([
    '--scene',
    '--transcript',
    '--projection',
    '--output',
    '--renderer-image',
    '--terminal-capture',
  ]);
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!allowed.has(flag) || !value) fail(`unknown or incomplete argument: ${flag || '<empty>'}`);
    if (values[flag]) fail(`duplicate argument: ${flag}`);
    values[flag] = value;
  }
  for (const flag of ['--scene', '--transcript', '--projection', '--output', '--renderer-image']) {
    if (!values[flag]) fail(`${flag} is required`);
  }
  if (!/@sha256:[0-9a-f]{64}$/.test(values['--renderer-image'])) {
    fail('--renderer-image must be an immutable image@sha256:digest coordinate');
  }
  return {
    scenePath: values['--scene'],
    transcriptPath: values['--transcript'],
    projectionPath: values['--projection'],
    outputPath: values['--output'],
    rendererImage: values['--renderer-image'],
    terminalCapturePath: values['--terminal-capture'] || '',
  };
}

function readRegularFile(filePath, label) {
  let metadata;
  try {
    metadata = fs.lstatSync(filePath);
  } catch {
    fail(`${label} does not exist`);
  }
  if (metadata.isSymbolicLink() || !metadata.isFile()) fail(`${label} must be a regular non-symlink file`);
  if (metadata.size > 4 * 1024 * 1024) fail(`${label} exceeds the 4 MiB input bound`);
  return fs.readFileSync(filePath);
}

function parseJson(bytes, label) {
  try {
    return JSON.parse(UTF8.decode(bytes));
  } catch {
    fail(`${label} must be valid UTF-8 JSON`);
  }
}

function exactKeys(value, required, optional, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`);
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${label}.${key} is not declared`);
  }
  for (const key of required) {
    if (!(key in value)) fail(`${label}.${key} is required`);
  }
}

function integer(value, minimum, maximum, label) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    fail(`${label} must be an integer from ${minimum} through ${maximum}`);
  }
  return value;
}

function text(value, minimum, maximum, label) {
  if (typeof value !== 'string' || value.length < minimum || value.length > maximum) {
    fail(`${label} must contain ${minimum} through ${maximum} characters`);
  }
  return value;
}

function color(value, fallback, label) {
  if (value === undefined) return fallback;
  if (typeof value !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(value)) {
    fail(`${label} must be a six-digit hex color`);
  }
  return value.toLowerCase();
}

function validateScene(value) {
  exactKeys(
    value,
    ['schema', 'id', 'width', 'height', 'fps', 'durationMs', 'title'],
    ['commandLabel', 'background', 'accent'],
    'scene',
  );
  if (value.schema !== 'build-images.demo-scene/v1') fail('unsupported scene schema');
  if (typeof value.id !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,63}$/.test(value.id)) {
    fail('scene.id must be a stable lowercase identifier');
  }
  return {
    schema: value.schema,
    id: value.id,
    width: integer(value.width, 640, 1920, 'scene.width'),
    height: integer(value.height, 360, 1080, 'scene.height'),
    fps: integer(value.fps, 1, 30, 'scene.fps'),
    durationMs: integer(value.durationMs, 500, 60000, 'scene.durationMs'),
    title: text(value.title, 1, 120, 'scene.title'),
    commandLabel: text(value.commandLabel ?? '', 0, 160, 'scene.commandLabel'),
    background: color(value.background, '#10151f', 'scene.background'),
    accent: color(value.accent, '#67e8a5', 'scene.accent'),
  };
}

function validateProjection(value, scene, transcriptLines) {
  exactKeys(value, ['schema', 'evidenceClass', 'claimBoundary', 'cues'], [], 'projection');
  if (value.schema !== 'build-images.demo-projection/v1') fail('unsupported projection schema');
  const evidenceClass = text(value.evidenceClass, 1, 120, 'projection.evidenceClass');
  const claimBoundary = text(value.claimBoundary, 1, 500, 'projection.claimBoundary');
  if (!Array.isArray(value.cues) || value.cues.length < 1 || value.cues.length > 240) {
    fail('projection.cues must contain 1 through 240 cues');
  }
  const cues = value.cues.map((cue, index) => {
    exactKeys(cue, ['startMs', 'endMs', 'transcriptLines'], ['annotation'], `projection.cues[${index}]`);
    const startMs = integer(cue.startMs, 0, scene.durationMs - 1, `projection.cues[${index}].startMs`);
    const endMs = integer(cue.endMs, 1, scene.durationMs, `projection.cues[${index}].endMs`);
    if (endMs <= startMs) fail(`projection.cues[${index}] must have endMs after startMs`);
    if (!Array.isArray(cue.transcriptLines) || cue.transcriptLines.length < 1 || cue.transcriptLines.length > 80) {
      fail(`projection.cues[${index}].transcriptLines must contain 1 through 80 line references`);
    }
    const lineSet = new Set();
    const lines = cue.transcriptLines.map((line, lineIndex) => {
      const checked = integer(
        line,
        1,
        transcriptLines.length,
        `projection.cues[${index}].transcriptLines[${lineIndex}]`,
      );
      if (lineSet.has(checked)) fail(`projection.cues[${index}] repeats transcript line ${checked}`);
      lineSet.add(checked);
      return checked;
    });
    return {
      startMs,
      endMs,
      transcriptLines: lines,
      annotation: text(cue.annotation ?? '', 0, 200, `projection.cues[${index}].annotation`),
    };
  });
  return { schema: value.schema, evidenceClass, claimBoundary, cues };
}

function decodeBase64(value, label) {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length > MAX_CAPTURE_BYTES * 2
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    fail(`${label} must be canonical base64`);
  }
  const decoded = Buffer.from(value, 'base64');
  if (decoded.toString('base64') !== value) fail(`${label} must be canonical base64`);
  return decoded;
}

function validateTerminalCapture(value, scene) {
  exactKeys(
    value,
    [
      'schema',
      'command',
      'dimensions',
      'durationMs',
      'encoding',
      'events',
      'completion',
      'exitCode',
      'authority',
    ],
    [],
    'terminalCapture',
  );
  if (value.schema !== 'kungfu.terminal-capture/v1') fail('unsupported terminal capture schema');
  const command = text(value.command, 1, 160, 'terminalCapture.command');
  exactKeys(value.dimensions, ['columns', 'rows'], [], 'terminalCapture.dimensions');
  const dimensions = {
    columns: integer(value.dimensions.columns, 80, 200, 'terminalCapture.dimensions.columns'),
    rows: integer(value.dimensions.rows, 24, 80, 'terminalCapture.dimensions.rows'),
  };
  const durationMs = integer(value.durationMs, 500, 60_000, 'terminalCapture.durationMs');
  if (durationMs > scene.durationMs || scene.durationMs - durationMs > 2_000) {
    fail('terminal capture duration must end within two seconds of the scene');
  }
  if (value.encoding !== 'base64') fail('terminalCapture.encoding must be base64');
  if (!Array.isArray(value.events) || value.events.length < 1 || value.events.length > MAX_CAPTURE_EVENTS) {
    fail(`terminalCapture.events must contain 1 through ${MAX_CAPTURE_EVENTS} events`);
  }
  let previousAtMs = -1;
  let totalBytes = 0;
  const events = value.events.map((event, index) => {
    exactKeys(event, ['atMs', 'data'], [], `terminalCapture.events[${index}]`);
    const atMs = integer(event.atMs, 0, durationMs - 1, `terminalCapture.events[${index}].atMs`);
    if (atMs < previousAtMs) fail('terminal capture event timestamps must be monotonic');
    if (index === 0 && atMs !== 0) fail('the first terminal capture event must start at zero');
    previousAtMs = atMs;
    const data = decodeBase64(event.data, `terminalCapture.events[${index}].data`);
    totalBytes += data.length;
    if (totalBytes > MAX_CAPTURE_BYTES) fail('terminal capture exceeds the 4 MiB byte bound');
    return { atMs, data, encoded: event.data };
  });
  exactKeys(
    value.completion,
    ['schema', 'status', 'reportRoot', 'eventCount'],
    [],
    'terminalCapture.completion',
  );
  if (
    value.completion.schema !== 'kungfu.agent-work-lab.tui-autoplay/v1'
    || value.completion.status !== 'passed'
    || !DIGEST_PATTERN.test(value.completion.reportRoot)
  ) {
    fail('terminal capture completion sentinel is not a passed Agent Work Lab autoplay');
  }
  integer(value.completion.eventCount, 1, 100_000, 'terminalCapture.completion.eventCount');
  if (value.exitCode !== 0) fail('terminal capture exitCode must be zero');
  exactKeys(value.authority, ['classification', 'grants', 'nonAuthorities'], [], 'terminalCapture.authority');
  if (value.authority.classification !== 'volatile-terminal-observation') {
    fail('terminal capture authority classification must remain observation-only');
  }
  if (!Array.isArray(value.authority.grants) || value.authority.grants.length !== 0) {
    fail('terminal capture must not grant authority');
  }
  if (
    JSON.stringify(value.authority.nonAuthorities) !== JSON.stringify(CAPTURE_NON_AUTHORITIES)
  ) {
    fail('terminal capture must declare every identity and metadata non-authority');
  }
  return {
    schema: value.schema,
    command,
    dimensions,
    durationMs,
    encoding: value.encoding,
    events,
    completion: value.completion,
    exitCode: value.exitCode,
    authority: value.authority,
    totalBytes,
  };
}

function writeTerminal(terminal, bytes) {
  return new Promise((resolve) => terminal.write(bytes, resolve));
}

function terminalScreen(terminal, rows) {
  const lines = [];
  for (let row = 0; row < rows; row += 1) {
    const line = terminal.buffer.active.getLine(row);
    lines.push(line ? line.translateToString(true) : '');
  }
  while (lines.length > 1 && lines.at(-1) === '') lines.pop();
  return lines.join('\n');
}

function stableJson(value) {
  const canonical = (item) => {
    if (Array.isArray(item)) return item.map(canonical);
    if (!item || typeof item !== 'object') return item;
    return Object.fromEntries(Object.keys(item).sort().map((key) => [key, canonical(item[key])]));
  };
  return `${JSON.stringify(canonical(value), null, 2)}\n`;
}

function sha256(bytes) {
  return `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`;
}

function run(command, args, label) {
  const result = spawnSync(command, args, { encoding: 'utf8' });
  if (result.error || result.status !== 0) {
    fail(`${label} failed: ${(result.stderr || result.error?.message || '').trim()}`);
  }
  return result.stdout.trim();
}

function writeFile(output, name, bytes) {
  const target = path.join(output, name);
  fs.writeFileSync(target, bytes);
  return target;
}

function probeMedia(output, scene) {
  const media = MEDIA.map((name) => {
    const target = path.join(output, name);
    const metadata = fs.statSync(target);
    if (name === 'poster.png') {
      const raw = run(
        'ffprobe',
        ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name,width,height', '-of', 'json', target],
        `probe ${name}`,
      );
      const stream = JSON.parse(raw).streams[0];
      return { name, bytes: metadata.size, codec: stream.codec_name, width: stream.width, height: stream.height };
    }
    const raw = run(
      'ffprobe',
      [
        '-v',
        'error',
        '-select_streams',
        'v:0',
        '-show_entries',
        'stream=codec_name,width,height,avg_frame_rate:format=duration',
        '-of',
        'json',
        target,
      ],
      `probe ${name}`,
    );
    const parsed = JSON.parse(raw);
    const stream = parsed.streams[0];
    return {
      name,
      bytes: metadata.size,
      codec: stream.codec_name,
      width: stream.width,
      height: stream.height,
      durationMs: Math.round(Number(parsed.format.duration) * 1000),
      frameRate: stream.avg_frame_rate,
    };
  });
  const errors = [];
  for (const item of media) {
    if (item.width !== scene.width || item.height !== scene.height) errors.push(`${item.name} dimensions drifted`);
    if (item.bytes < 100) errors.push(`${item.name} is unexpectedly small`);
    if ('durationMs' in item && Math.abs(item.durationMs - scene.durationMs) > 600) {
      errors.push(`${item.name} duration drifted`);
    }
  }
  return { schema: 'build-images.demo-media-probe/v1', passed: errors.length === 0, errors, media };
}

function checksums(output, names) {
  return names
    .slice()
    .sort()
    .map((name) => `${sha256(fs.readFileSync(path.join(output, name))).slice(7)}  ${name}`)
    .join('\n') + '\n';
}

async function render(options) {
  const sceneBytes = readRegularFile(options.scenePath, 'scene');
  const transcriptBytes = readRegularFile(options.transcriptPath, 'transcript');
  const projectionBytes = readRegularFile(options.projectionPath, 'projection');
  if (transcriptBytes.includes(0)) fail('transcript must be UTF-8 text without NUL bytes');
  let transcript;
  try {
    transcript = UTF8.decode(transcriptBytes).replace(/\r\n/g, '\n');
  } catch {
    fail('transcript must be valid UTF-8 text');
  }
  if (!transcript.trim()) fail('transcript must not be empty');
  const transcriptLines = transcript.endsWith('\n')
    ? transcript.slice(0, -1).split('\n')
    : transcript.split('\n');
  const scene = validateScene(parseJson(sceneBytes, 'scene'));
  const projection = validateProjection(parseJson(projectionBytes, 'projection'), scene, transcriptLines);
  const terminalCaptureBytes = options.terminalCapturePath
    ? readRegularFile(options.terminalCapturePath, 'terminal capture')
    : null;
  const terminalCapture = terminalCaptureBytes
    ? validateTerminalCapture(parseJson(terminalCaptureBytes, 'terminal capture'), scene)
    : null;

  const outputMetadata = fs.lstatSync(options.outputPath);
  if (outputMetadata.isSymbolicLink() || !outputMetadata.isDirectory()) fail('output must be a non-symlink directory');
  if (fs.readdirSync(options.outputPath).length !== 0) fail('output directory must be initially empty');

  const normalizedTranscript = `${transcript.replace(/\n*$/, '')}\n`;
  writeFile(options.outputPath, 'complete-transcript.txt', normalizedTranscript);
  writeFile(options.outputPath, 'scene.json', stableJson(scene));
  writeFile(options.outputPath, 'public-projection.json', stableJson(projection));

  const frames = fs.mkdtempSync(path.join(os.tmpdir(), 'demo-renderer-frames-'));
  try {
    const terminal = terminalCapture
      ? new Terminal({
        cols: terminalCapture.dimensions.columns,
        rows: terminalCapture.dimensions.rows,
        scrollback: 0,
        convertEol: false,
        cursorBlink: false,
        disableStdin: true,
        logLevel: 'off',
      })
      : null;
    let terminalEventIndex = 0;
    const browser = await chromium.launch({
      headless: true,
      args: ['--disable-gpu', '--font-render-hinting=none', '--force-color-profile=srgb'],
    });
    const page = await browser.newPage({
      viewport: { width: scene.width, height: scene.height },
      deviceScaleFactor: 1,
      locale: 'en-US',
      timezoneId: 'UTC',
    });
    await page.route('**/*', (route) => route.abort('blockedbyclient'));
    await page.setContent(`<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden}
body{background:${scene.background};color:#e8edf5;font-family:"DejaVu Sans Mono",monospace;padding:24px}
.window{height:100%;border:1px solid #344154;border-radius:14px;background:#0b1018;box-shadow:0 22px 70px #0008;overflow:hidden}
.bar{height:48px;border-bottom:1px solid #283446;display:flex;align-items:center;padding:0 16px;gap:8px;background:#121a26}
.dot{width:11px;height:11px;border-radius:50%;background:#65738a}.title{font:600 14px system-ui,sans-serif;margin-left:8px;color:#cbd5e1}
.badge{margin-left:auto;font:600 11px system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:${scene.accent};border:1px solid ${scene.accent}66;border-radius:999px;padding:5px 9px}
.terminal{height:calc(100% - 48px);padding:18px 22px;display:flex;flex-direction:column}
.command{color:${scene.accent};font-size:14px;min-height:22px}.runtime-label,.annotation-label{font:600 10px system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#8290a6;margin:14px 0 8px}
pre{font:14px/1.52 "DejaVu Sans Mono",monospace;white-space:pre-wrap;word-break:break-word;margin:0;color:#e5edf7}
.capture pre{font:13px/1.24 "DejaVu Sans Mono",monospace;white-space:pre;word-break:normal}
.capture .runtime-label{margin-top:7px}.capture .annotation{min-height:38px;padding-top:8px}
.annotation{margin-top:auto;border-top:1px solid #283446;padding-top:11px;color:#9facbf;font:12px/1.4 system-ui,sans-serif;min-height:48px}
.cursor{display:inline-block;width:8px;height:15px;background:${scene.accent};vertical-align:-2px;margin-left:3px;opacity:.9}
</style></head><body>
<section class="window"><header class="bar"><i class="dot"></i><i class="dot"></i><i class="dot"></i><span class="title"></span><span class="badge">presentation, not screen capture</span></header>
<main class="terminal"><div class="command"></div><div class="runtime-label">traceable runtime transcript</div><pre></pre><div class="annotation"><div class="annotation-label">presentation annotation</div><span></span><i class="cursor"></i></div></main></section>
</body></html>`);
    const frameCount = Math.ceil((scene.durationMs / 1000) * scene.fps);
    for (let frame = 0; frame < frameCount; frame += 1) {
      const atMs = Math.floor((frame * 1000) / scene.fps);
      const active = projection.cues.filter((cue) => cue.startMs <= atMs && atMs < cue.endMs).at(-1)
        ?? projection.cues.filter((cue) => cue.startMs <= atMs).at(-1)
        ?? projection.cues[0];
      if (terminalCapture && terminal) {
        while (
          terminalEventIndex < terminalCapture.events.length
          && terminalCapture.events[terminalEventIndex].atMs <= atMs
        ) {
          await writeTerminal(terminal, terminalCapture.events[terminalEventIndex].data);
          terminalEventIndex += 1;
        }
      }
      const runtime = terminal && terminalCapture
        ? terminalScreen(terminal, terminalCapture.dimensions.rows)
        : active.transcriptLines.map((line) => transcriptLines[line - 1]).join('\n');
      const annotation = terminalCapture
        ? `${terminalCapture.dimensions.columns}x${terminalCapture.dimensions.rows} bounded PTY replay · captured bytes grant no authority`
        : active.annotation;
      await page.evaluate(
        ({ title, commandLabel, runtime, annotation, frame, captureMode }) => {
          document.querySelector('.window').classList.toggle('capture', captureMode);
          document.querySelector('.title').textContent = title;
          document.querySelector('.command').textContent = commandLabel;
          document.querySelector('pre').textContent = runtime;
          document.querySelector('.annotation span').textContent = annotation;
          document.querySelector('.runtime-label').textContent = captureMode
            ? 'exact bounded terminal capture'
            : 'traceable runtime transcript';
          document.querySelector('.annotation-label').textContent = captureMode
            ? 'authority boundary'
            : 'presentation annotation';
          document.querySelector('.badge').textContent = captureMode
            ? 'captured PTY replay'
            : 'presentation, not screen capture';
          document.querySelector('.cursor').style.opacity = captureMode
            ? '0'
            : frame % 2 === 0 ? '0.9' : '0.25';
        },
        {
          title: scene.title,
          commandLabel: scene.commandLabel,
          runtime,
          annotation,
          frame,
          captureMode: Boolean(terminalCapture),
        },
      );
      await page.screenshot({
        path: path.join(frames, `frame-${String(frame + 1).padStart(6, '0')}.png`),
        animations: 'disabled',
        caret: 'hide',
      });
    }
    await browser.close();
    terminal?.dispose();

    const posterFrame = terminalCapture
      ? Math.min(frameCount, Math.max(1, Math.floor(frameCount * 0.55)))
      : 1;
    fs.copyFileSync(
      path.join(frames, `frame-${String(posterFrame).padStart(6, '0')}.png`),
      path.join(options.outputPath, 'poster.png'),
    );
    const input = path.join(frames, 'frame-%06d.png');
    run(
      'ffmpeg',
      ['-hide_banner', '-loglevel', 'error', '-y', '-framerate', String(scene.fps), '-i', input,
        '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', '-map_metadata', '-1', '-fflags', '+bitexact', '-flags:v', '+bitexact',
        '-threads', '1', path.join(options.outputPath, 'demo.mp4')],
      'MP4 encoding',
    );
    run(
      'ffmpeg',
      ['-hide_banner', '-loglevel', 'error', '-y', '-framerate', String(scene.fps), '-i', input,
        '-an', '-c:v', 'libvpx-vp9', '-deadline', 'good', '-cpu-used', '2', '-crf', '32', '-b:v', '0',
        '-pix_fmt', 'yuv420p', '-row-mt', '0', '-map_metadata', '-1', '-fflags', '+bitexact',
        '-threads', '1', path.join(options.outputPath, 'demo.webm')],
      'WebM encoding',
    );
    run(
      'ffmpeg',
      ['-hide_banner', '-loglevel', 'error', '-y', '-framerate', String(scene.fps), '-i', input,
        '-filter_complex', `fps=${Math.min(scene.fps, 12)},scale=${scene.width}:${scene.height}:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3`,
        '-loop', '0', '-map_metadata', '-1', '-threads', '1', path.join(options.outputPath, 'demo.gif')],
      'GIF encoding',
    );
  } finally {
    fs.rmSync(frames, { recursive: true, force: true });
  }

  const probe = probeMedia(options.outputPath, scene);
  writeFile(options.outputPath, 'media-probe.json', stableJson(probe));
  if (!probe.passed) fail(`media probe failed: ${probe.errors.join('; ')}`);

  const fontInventory = JSON.parse(
    fs.readFileSync('/opt/kungfu/demo-renderer/font-inventory.json', 'utf8'),
  );
  const terminalRuntimeInventoryBytes = fs.readFileSync(
    '/opt/kungfu/demo-renderer/terminal-runtime-inventory.json',
  );
  const terminalRuntimeInventory = JSON.parse(terminalRuntimeInventoryBytes.toString('utf8'));
  const xtermVersion = JSON.parse(
    fs.readFileSync('/opt/kungfu/demo-renderer/node_modules/@xterm/headless/package.json', 'utf8'),
  ).version;
  if (
    terminalRuntimeInventory.schema !== 'build-images.demo-renderer-terminal-runtime-inventory/v1'
    || terminalRuntimeInventory.packages?.length !== 1
    || terminalRuntimeInventory.packages[0]?.name !== '@xterm/headless'
    || terminalRuntimeInventory.packages[0]?.version !== xtermVersion
  ) {
    fail('terminal runtime inventory does not match the installed state machine');
  }
  const manifest = {
    schema: 'build-images.auditable-demo-render/v1',
    renderer: {
      contractVersion: VERSION,
      image: options.rendererImage,
      architecture: `${process.platform}/${process.arch === 'x64' ? 'amd64' : process.arch}`,
      playwright: JSON.parse(
        fs.readFileSync('/opt/kungfu/demo-renderer/node_modules/playwright/package.json', 'utf8'),
      ).version,
      chromium: await chromium.launch({ headless: true }).then(async (browser) => {
        const version = browser.version();
        await browser.close();
        return version;
      }),
      node: process.version,
      ffmpeg: run('ffmpeg', ['-hide_banner', '-version'], 'ffmpeg version').split('\n')[0],
      fonts: fontInventory,
      ...(terminalCapture
        ? {
          terminal: {
            engine: '@xterm/headless',
            version: xtermVersion,
            inventoryRoot: sha256(terminalRuntimeInventoryBytes),
          },
        }
        : {}),
    },
    policy: {
      locale: 'C.UTF-8',
      timezone: 'UTC',
      sourceDateEpoch: '0',
      network: 'caller-disabled-and-browser-requests-blocked',
      runtimeTextAuthority: terminalCapture ? 'terminal-capture.json' : 'complete-transcript.txt',
      visualClassification: terminalCapture ? 'bounded-pty-replay' : 'styled-presentation-not-literal-screen-capture',
      evidenceClass: projection.evidenceClass,
      claimBoundary: projection.claimBoundary,
    },
    inputs: {
      scene: { path: 'scene.json', root: sha256(fs.readFileSync(path.join(options.outputPath, 'scene.json'))) },
      transcript: {
        path: 'complete-transcript.txt',
        root: sha256(fs.readFileSync(path.join(options.outputPath, 'complete-transcript.txt'))),
        lines: transcriptLines.length,
      },
      projection: {
        path: 'public-projection.json',
        root: sha256(fs.readFileSync(path.join(options.outputPath, 'public-projection.json'))),
      },
      ...(terminalCaptureBytes
        ? {
          terminalCapture: {
            path: 'terminal-capture.json',
            root: sha256(terminalCaptureBytes),
            schema: terminalCapture.schema,
            events: terminalCapture.events.length,
            bytes: terminalCapture.totalBytes,
            dimensions: terminalCapture.dimensions,
            durationMs: terminalCapture.durationMs,
          },
        }
        : {}),
    },
    traceability: projection.cues.map((cue) => ({
      startMs: cue.startMs,
      endMs: cue.endMs,
      transcriptLines: cue.transcriptLines,
      visualAnnotation: cue.annotation,
    })),
    outputs: Object.fromEntries(
      [...MEDIA, 'media-probe.json'].sort().map((name) => [
        name,
        { root: sha256(fs.readFileSync(path.join(options.outputPath, name))), bytes: fs.statSync(path.join(options.outputPath, name)).size },
      ]),
    ),
  };
  writeFile(options.outputPath, 'manifest.json', stableJson(manifest));
  writeFile(
    options.outputPath,
    'checksums.sha256',
    checksums(options.outputPath, [
      'complete-transcript.txt',
      'public-projection.json',
      'scene.json',
      ...MEDIA,
      'media-probe.json',
      'manifest.json',
    ]),
  );
}

const options = parseArguments(process.argv.slice(2));
render(options).catch((error) => fail(error instanceof Error ? error.message : String(error)));
