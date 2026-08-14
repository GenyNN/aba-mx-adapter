(() => {
  if (globalThis.__MAX_WS_SNIFFER_INSTALLED__) return;
  globalThis.__MAX_WS_SNIFFER_INSTALLED__ = true;

  const NativeWebSocket = globalThis.WebSocket;
  const emitFnName = "__max_ws_sniffer_emit";

  const toBase64 = (arrayBuffer) => {
    const bytes = new Uint8Array(arrayBuffer);
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  };

  const safeEmit = (data) => {
    try {
      const fn = globalThis[emitFnName];
      if (typeof fn === "function") fn(data);
    } catch (_) {}
  };

  const wrap = (ws, url) => {
    const wsId = `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
    try {
      ws.binaryType = "arraybuffer";
    } catch (_) {}

    safeEmit({ type: "ws_opened", wsId, url, ts: Date.now() });

    const origSend = ws.send;
    ws.send = function (data) {
      try {
        if (typeof data === "string") {
          safeEmit({ type: "frame", dir: "out", wsId, url, opcode: 1, payload: data, ts: Date.now() });
        } else if (data instanceof ArrayBuffer) {
          safeEmit({ type: "frame", dir: "out", wsId, url, opcode: 2, payload: toBase64(data), ts: Date.now() });
        } else if (ArrayBuffer.isView(data)) {
          safeEmit({
            type: "frame",
            dir: "out",
            wsId,
            url,
            opcode: 2,
            payload: toBase64(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength)),
            ts: Date.now(),
          });
        }
      } catch (_) {}
      return origSend.call(this, data);
    };

    ws.addEventListener("message", (ev) => {
      try {
        const data = ev.data;
        if (typeof data === "string") {
          safeEmit({ type: "frame", dir: "in", wsId, url, opcode: 1, payload: data, ts: Date.now() });
        } else if (data instanceof ArrayBuffer) {
          safeEmit({ type: "frame", dir: "in", wsId, url, opcode: 2, payload: toBase64(data), ts: Date.now() });
        }
      } catch (_) {}
    });

    ws.addEventListener("close", (ev) => {
      safeEmit({ type: "ws_closed", wsId, url, code: ev.code, reason: ev.reason, ts: Date.now() });
    });

    ws.addEventListener("error", () => {
      safeEmit({ type: "ws_error", wsId, url, ts: Date.now() });
    });

    return ws;
  };

  function WebSocketProxy(url, protocols) {
    const ws = protocols !== undefined ? new NativeWebSocket(url, protocols) : new NativeWebSocket(url);
    return wrap(ws, url);
  }

  WebSocketProxy.prototype = NativeWebSocket.prototype;
  WebSocketProxy.OPEN = NativeWebSocket.OPEN;
  WebSocketProxy.CLOSING = NativeWebSocket.CLOSING;
  WebSocketProxy.CLOSED = NativeWebSocket.CLOSED;
  WebSocketProxy.CONNECTING = NativeWebSocket.CONNECTING;

  globalThis.WebSocket = WebSocketProxy;
})();
