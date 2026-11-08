"use strict";
// Inside the sandboxed frame of /diagram (no cookies, no network, no access to the page): draws
// the Mermaid text the page sends. The text is what anyone wrote in a page: Mermaid runs here,
// away from Synaptane, and only the height of the drawing goes back. The editor's preview may
// send a new text to the same frame: drawings are made one after the other, and a text that
// cannot be drawn leaves the previous drawing in place.
(() => {
  const output = document.getElementById("diagram");
  const report = message => window.parent.postMessage(message, "*");
  const height = () => report({ height: Math.ceil(output.getBoundingClientRect().height) });
  let observed = false;
  let drawings = 0;  // a new id each time: Mermaid removes an element with the id it renders
  let queue = Promise.resolve();
  const draw = async (text, dark) => {
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: dark ? "dark" : "neutral",
      fontFamily: "system-ui, sans-serif",
    });
    try {
      drawings += 1;
      const { svg } = await window.mermaid.render(`drawing-${drawings}`, text);
      output.innerHTML = svg;
      height();
      if (!observed) { new ResizeObserver(height).observe(output); observed = true; }
    } catch {
      report({ failed: true });
    } finally {
      // What Mermaid may leave behind after an error.
      [...document.body.children].forEach(element => {
        if (element !== output && element.tagName !== "SCRIPT") element.remove();
      });
    }
  };
  window.addEventListener("message", event => {
    if (event.source !== window.parent || typeof event.data?.text !== "string") return;
    const { text, dark } = event.data;
    queue = queue.then(() => draw(text, Boolean(dark)));
  });
  report({ ready: true });
})();
