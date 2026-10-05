import { autocompletion, type CompletionSource } from "@codemirror/autocomplete";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { linter, lintGutter } from "@codemirror/lint";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import CodeMirror from "@uiw/react-codemirror";
import { useMemo } from "react";

// Colours come from CSS variables, so the editor follows light/dark mode automatically.
const theme = EditorView.theme({
  "&": { backgroundColor: "var(--surface)", color: "var(--text)" },
  ".cm-content": { caretColor: "var(--text)", padding: "10px 0" },
  ".cm-cursor": { borderLeftColor: "var(--text)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--accent) 6%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text)" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in srgb, var(--accent) 22%, transparent) !important",
  },
  ".cm-tooltip": { backgroundColor: "var(--surface)", border: "1px solid var(--border)", color: "var(--text)" },
  ".cm-diagnostic-error": { borderLeftColor: "var(--danger-strong)" },
});

const highlight = HighlightStyle.define([
  { tag: t.propertyName, color: "var(--accent-text)" },
  { tag: t.string, color: "var(--success)" },
  { tag: [t.number, t.bool, t.null], color: "var(--warn)" },
  { tag: t.invalid, color: "var(--danger)" },
]);

export function JsonEditor({ value, onChange, height = "280px", readOnly, label, completions, lint = true, extraKeys }: {
  value: string;
  onChange?: (v: string) => void;
  height?: string;
  readOnly?: boolean;
  label: string;
  completions?: CompletionSource;
  lint?: boolean;
  extraKeys?: Parameters<typeof EditorView.domEventHandlers>[0];
}) {
  const extensions = useMemo(
    () => [json(), ...(lint ? [linter(jsonParseLinter(), { delay: 300 }), lintGutter()] : []), theme, syntaxHighlighting(highlight),
      EditorView.contentAttributes.of({ "aria-label": label }), EditorView.lineWrapping,
      ...(completions ? [autocompletion({ override: [completions], activateOnTyping: true })] : []),
      ...(extraKeys ? [EditorView.domEventHandlers(extraKeys)] : [])],
    [label, completions, lint, extraKeys],
  );
  return (
    <div className="editor">
      <CodeMirror
        value={value}
        height={height}
        theme="none"
        editable={!readOnly}
        readOnly={readOnly}
        basicSetup={{ foldGutter: true, highlightActiveLine: !readOnly, autocompletion: false, searchKeymap: true, tabSize: 2 }}
        extensions={extensions}
        onChange={onChange}
      />
    </div>
  );
}

/** Parse editor text; returns [value, error message]. */
export function parseJson(text: string): [unknown, string | null] {
  if (!text.trim()) return [undefined, "Enter a JSON value"];
  try {
    return [JSON.parse(text), null];
  } catch (e) {
    return [undefined, `Invalid JSON: ${(e as Error).message}`];
  }
}
