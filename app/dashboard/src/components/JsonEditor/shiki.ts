import { createHighlighterCore } from "shiki/core";
import { createOnigurumaEngine } from "shiki/engine/oniguruma";

export const highlighter = createHighlighterCore({
  themes: [
    import("@shikijs/themes/one-light"),
    import("@shikijs/themes/one-dark-pro"),
  ],
  langs: [import("@shikijs/langs/json")],
  engine: createOnigurumaEngine(import("shiki/wasm")),
});
