/**
 * Test bootstrap: MUST be the first import in any test file that renders a
 * client component through `react-dom/client`.
 *
 * Node has no DOM. Installing the jsdom globals here — rather than inside a test
 * body — relies on the same property `test-env.ts` uses for the database: static
 * imports are evaluated in document order, so a first-line import of this module
 * runs before `react-dom` and the component graph are evaluated.
 *
 * No production module imports this file, so none of it reaches the app bundle.
 */
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://stemify.test/",
  pretendToBeVisual: true,
});

// Only the globals the renderer and the components actually touch. jsdom's own
// `crypto` and `fetch` are deliberately left uninstalled: the components need
// Node's `crypto.randomUUID`, and every test stubs `fetch` for itself.
//
// defineProperty, not Object.assign: Node ships some of these as getter-only
// globals (`navigator` on Node 26), and a plain assignment to those throws.
const globals: Record<string, unknown> = {
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
};
for (const [name, value] of Object.entries(globals)) {
  Object.defineProperty(globalThis, name, {
    value,
    writable: true,
    configurable: true,
    enumerable: true,
  });
}

// Without this React warns on every state update under `act`, and effects do not
// flush synchronously.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
