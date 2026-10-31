// Shared by the views: the bridge to the host (MCP Apps, JSON-RPC over postMessage) and a few
// helpers. A view calls App.start(name, texts, render): render(data) receives the tool's
// structured result, again when the host's language changes.
var App = (function () {
  "use strict";
  var texts = {}, lang = "en", nextId = 1, waiting = {}, handlers = {}, data = null, draw = null;

  function send(message) { message.jsonrpc = "2.0"; window.parent.postMessage(message, "*"); }
  function request(method, params) {
    var id = nextId++;
    send({ id: id, method: method, params: params || {} });
    return new Promise(function (resolve, reject) { waiting[id] = { resolve: resolve, reject: reject }; });
  }
  function notify(method, params) { send({ method: method, params: params || {} }); }
  window.addEventListener("message", function (event) {
    var message = event.data;
    if (event.source !== window.parent || !message || message.jsonrpc !== "2.0") return;
    if (message.id != null && !message.method) {
      var pending = waiting[message.id];
      delete waiting[message.id];
      if (!pending) return;
      if (message.error) pending.reject(new Error(message.error.message || "error"));
      else pending.resolve(message.result);
      return;
    }
    if (message.method === "ui/resource-teardown" || message.method === "ping") {
      send({ id: message.id, result: {} });
      return;
    }
    var handler = handlers[message.method];
    if (handler) handler(message.params || {});
  });

  function applyContext(context) {
    if (!context) return;
    if (context.theme) document.documentElement.setAttribute("data-theme", context.theme);
    var before = lang;
    if (context.locale) lang = context.locale.toLowerCase().indexOf("fr") === 0 ? "fr" : "en";
    document.documentElement.lang = lang;
    if (lang !== before && data && draw) draw(data);  // in the new language
  }

  function t(key, values) {
    var text = (texts[lang] || texts.en)[key];
    for (var name in values || {}) text = text.split("{" + name + "}").join(String(values[name]));
    return text;
  }
  function locale() { return lang === "fr" ? "fr-FR" : "en-GB"; }
  function day(iso) {
    if (!iso) return "";
    var date = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
    if (isNaN(date)) return iso;
    return date.toLocaleDateString(locale(), { day: "numeric", month: "long", year: "numeric" });
  }
  function moment(iso) {
    var date = new Date(iso);
    if (isNaN(date)) return iso || "";
    return day(iso) + ", " + date.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
  }
  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;  // never HTML: titles and paths are untrusted
    return node;
  }
  function button(label, primary, onClick) {
    var node = element("button", primary ? "primary" : null, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }
  function textOf(result) {
    return ((result && result.content) || []).map(function (c) { return c.text || ""; }).join(" ");
  }
  // A tool called from the view; its error (isError) becomes a rejection.
  function callTool(name, args) {
    return request("tools/call", { name: name, arguments: args }).then(function (result) {
      if (result && result.isError) throw new Error(textOf(result));
      return result;
    });
  }
  function tellModel(text) {
    // The host may not keep it: what the view did is done anyway.
    request("ui/update-model-context", { content: [{ type: "text", text: text }] }).catch(function () {});
  }
  function ask(text) { return request("ui/message", { role: "user", content: [{ type: "text", text: text }] }); }
  function open(url) {
    request("ui/open-link", { url: url }).catch(function () { window.open(url, "_blank", "noopener"); });
  }
  function showError(node, e) {
    node.hidden = false;
    node.textContent = t("failed", { error: (e && e.message) || e });
  }

  function start(name, viewTexts, render) {
    texts = viewTexts;
    draw = render;
    handlers["ui/notifications/host-context-changed"] = applyContext;
    handlers["ui/notifications/tool-result"] = function (result) {
      data = result.structuredContent || {};
      render(data);
    };
    var lastHeight = 0;  // the host sizes the frame to the view
    new ResizeObserver(function () {
      var height = Math.ceil(document.documentElement.getBoundingClientRect().height);
      if (height !== lastHeight) {
        lastHeight = height;
        notify("ui/notifications/size-changed", { height: height });
      }
    }).observe(document.body);
    request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appInfo: { name: "synaptane-" + name, version: "1" },
      appCapabilities: { availableDisplayModes: ["inline"] }
    }).then(function (result) {
      applyContext((result && result.hostContext) || { locale: navigator.language });
      notify("ui/notifications/initialized", {});
    }, function () {
      applyContext({ locale: navigator.language });
      notify("ui/notifications/initialized", {});
    });
  }

  return {
    start: start, t: t, day: day, moment: moment, element: element, button: button,
    callTool: callTool, tellModel: tellModel, ask: ask, open: open, showError: showError
  };
})();
