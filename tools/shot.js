const fs = require('fs');

const [, , url, out, w = '390', h = '844', dpr = '2', afterJsArg = ''] = process.argv;
const afterJs = afterJsArg.startsWith('@') ? fs.readFileSync(afterJsArg.slice(1), 'utf8') : afterJsArg;

async function main() {
  const list = await (await fetch('http://127.0.0.1:9222/json/list')).json();
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('nessuna pagina disponibile in Chrome');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const pending = new Map();
  let seq = 0;
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  };
  await new Promise((resolve) => { ws.onopen = resolve; });
  const send = (method, params = {}) => new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', {
    width: Number(w), height: Number(h), deviceScaleFactor: Number(dpr), mobile: true,
  });
  await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  await send('Page.navigate', { url });
  await new Promise((r) => setTimeout(r, 2500));
  if (afterJs) {
    const res = await send('Runtime.evaluate', { expression: afterJs, awaitPromise: true, returnByValue: true });
    if (res.result && res.result.exceptionDetails) console.error('JS: ' + JSON.stringify(res.result.exceptionDetails.exception.description));
    await new Promise((r) => setTimeout(r, 1200));
  }
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  ws.close();
  console.log('salvato ' + out);
}

main().catch((err) => { console.error(err); process.exit(1); });
