const fs = require('fs');

function readStdin() {
  return fs.readFileSync(0, 'utf8');
}

function lineText(line) {
  if (!line) return '';
  return line.translateToString(true);
}

async function main() {
  const input = JSON.parse(readStdin() || '{}');
  const { Terminal } = require(require.resolve('@xterm/xterm', { paths: [process.cwd()] }));
  const terminal = new Terminal({
    cols: Math.max(1, Number(input.columns || 120)),
    rows: Math.max(1, Number(input.rows || 30)),
    scrollback: Math.max(0, Number(input.scrollback || 10000)),
    convertEol: false,
    windowsPty: { backend: 'conpty' },
  });
  for (const chunk of input.chunks || []) {
    if (!chunk) continue;
    await new Promise((resolve) => terminal.write(String(chunk), resolve));
  }
  const buffer = terminal.buffer.active;
  const lines = [];
  for (let index = 0; index < buffer.length; index += 1) {
    const line = buffer.getLine(index);
    const text = lineText(line);
    if (line && line.isWrapped && lines.length > 0) {
      lines[lines.length - 1] += text;
    } else {
      lines.push(text);
    }
  }
  while (lines.length > 0 && lines[0] === '') lines.shift();
  while (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  process.stdout.write(JSON.stringify({ text: lines.join('\n'), line_count: lines.length }));
  terminal.dispose();
}

main().catch((error) => {
  process.stderr.write(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
