"use strict";
// Inside the sandboxed frame of /diagram (no cookies, no network, no access to the page): draws
// the Mermaid text the page sends. The text is what anyone wrote in a page: Mermaid runs here,
// away from Synaptane, and only the height of the drawing goes back.
(() => {
  const output = document.getElementById("diagram");
  const report = message => window.parent.postMessage(message, "*");
  const height = () => report({ height: Math.ceil(output.getBoundingClientRect().height) });
  window.addEventListener("message", async event => {
    if (event.source !== window.parent || typeof event.data?.text !== "string") return;
    document.documentElement.style.colorScheme = event.data.dark ? "dark" : "light";
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: event.data.dark ? "dark" : "neutral",
      fontFamily: "system-ui, sans-serif",
    });
    try {
      const { svg } = await window.mermaid.render("drawing", event.data.text);
      output.innerHTML = svg;
      height();
      new ResizeObserver(height).observe(output);
    } catch {
      report({ failed: true });
    }
  });
  report({ ready: true });
})();
