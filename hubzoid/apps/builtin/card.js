// Hubzoid built-in cards: the MCP Apps app side (stable 2026-01-26) in the
// few messages these cards use: ui/initialize, the tool input and result
// notifications, host context, size changes, tools/call and ui/open-link.
// Everything shown is set with textContent: a value is never markup.
var HZ = (function () {
  "use strict";
  var seq = 0;
  var pending = {};
  var handlers = {};
  var state = { input: null, result: null, meta: null };

  function send(msg) { window.parent.postMessage(msg, "*"); }

  function request(method, params) {
    var id = ++seq;
    send({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      setTimeout(function () {
        if (pending[id]) { delete pending[id]; reject(new Error("The host did not answer.")); }
      }, 15000);
    });
  }

  function notify(method, params) { send({ jsonrpc: "2.0", method: method, params: params || {} }); }

  function applyContext(ctx) {
    if (ctx && (ctx.theme === "dark" || ctx.theme === "light")) {
      document.documentElement.setAttribute("data-theme", ctx.theme);
    }
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var m = event.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined && m.method === undefined) {
      var p = pending[m.id];
      if (!p) return;
      delete pending[m.id];
      if (m.error) p.reject(new Error(m.error.message || "The host refused this."));
      else p.resolve(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-input") {
      state.input = (m.params && m.params.arguments) || {};
      if (handlers.input) handlers.input(state.input);
    } else if (m.method === "ui/notifications/tool-result") {
      state.result = m.params || {};
      state.meta = (state.result._meta && state.result._meta["hubzoid/card"]) || state.meta;
      if (handlers.result) handlers.result(state.result);
    } else if (m.method === "ui/notifications/host-context-changed") {
      applyContext(m.params);
    } else if (m.method === "ui/resource-teardown" && m.id !== undefined) {
      send({ jsonrpc: "2.0", id: m.id, result: {} });
    } else if (m.id !== undefined && m.method) {
      send({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: "Not supported by this card." } });
    }
  });

  function resize() {
    var h = Math.ceil(document.documentElement.getBoundingClientRect().height);
    notify("ui/notifications/size-changed", { height: h });
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "onclick") node.addEventListener("click", attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function textOf(result) {
    var c = (result && result.content) || [];
    for (var i = 0; i < c.length; i++) if (c[i] && c[i].type === "text") return c[i].text;
    return "";
  }

  function mount(node) {
    var root = document.getElementById("card");
    root.textContent = "";
    root.appendChild(node);
    resize();
  }

  function showStatus(message, kind) {
    mount(el("p", { "class": "note " + (kind || ""), role: kind === "error" ? "alert" : "status", text: message }));
  }

  function call(name, args) {
    return request("tools/call", { name: name, arguments: args || {} }).then(function (r) {
      if (r && r.isError) throw new Error(textOf(r) || "The tool refused this.");
      return r;
    });
  }

  function openLink(url) {
    return request("ui/open-link", { url: url });
  }

  function sources(list) {
    if (!list || !list.length) return null;
    var items = list.map(function (s) {
      if (s.url && /^https:\/\//.test(s.url)) {
        return el("li", {}, [el("button", { type: "button", "class": "link", text: s.label,
          onclick: function () { openLink(s.url); } })]);
      }
      return el("li", { text: s.label });
    });
    return el("div", { "class": "sources" }, [el("span", { "class": "muted", text: "Sources" }), el("ul", {}, items)]);
  }

  function start(opts) {
    handlers.input = opts.onInput || null;
    handlers.result = function (result) {
      if (result.isError) { showStatus(textOf(result) || "The tool failed.", "error"); return; }
      if (!result.structuredContent) { showStatus(textOf(result) || "No result.", ""); return; }
      try { opts.onResult(result.structuredContent, result); }
      catch (e) { showStatus(textOf(result) || "This card could not show the result.", "error"); }
    };
    showStatus("Loading " + (opts.label || "card") + "…", "");
    if (window.ResizeObserver) new ResizeObserver(resize).observe(document.documentElement);
    request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appInfo: { name: "hubzoid-" + (opts.name || "card"), version: "1" },
      appCapabilities: {}
    }).then(function (res) {
      applyContext(res && res.hostContext);
      notify("ui/notifications/initialized", {});
    }).catch(function () { showStatus("This host did not start the card. The text answer above is complete.", "error"); });
  }

  return { start: start, call: call, openLink: openLink, el: el, mount: mount, showStatus: showStatus,
           sources: sources, textOf: textOf, resize: resize, state: state };
})();
