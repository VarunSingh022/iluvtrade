import styles from "./styles.css?inline";

const primaryInteractionRule = styles.match(
  /button\.primary:hover:not\(:disabled\),\s*\.btn\.primary:hover:not\(:disabled\),\s*button\.primary:focus-visible,\s*\.btn\.primary:focus-visible,\s*button\.primary:active:not\(:disabled\),\s*\.btn\.primary:active:not\(:disabled\)\s*\{([^}]*)\}/s,
)?.[1];

describe("shared button styles", () => {
  it("keeps primary actions blue with readable white labels in interactive states", () => {
    expect(primaryInteractionRule).toBeDefined();
    expect(primaryInteractionRule).toContain("background: var(--accent-button);");
    expect(primaryInteractionRule).toContain("color: #fff;");
    expect(styles).toContain("--accent-button: #2f5bd4;");
  });

  it("provides distinct hover and pressed feedback plus a visible keyboard focus ring", () => {
    expect(styles).toContain("button:hover:not(:disabled), .btn:hover:not(:disabled) { background: var(--surface-2); text-decoration: none; }");
    expect(styles).toContain("button.primary:hover:not(:disabled), .btn.primary:hover:not(:disabled) { filter: brightness(0.92); }");
    expect(styles).toContain("button.primary:active:not(:disabled), .btn.primary:active:not(:disabled) { filter: brightness(0.84); }");
    expect(styles).toContain("button:focus-visible, .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }");
  });

  it("preserves disabled primary button opacity, cursor, and default colors without hover or pressed effects", () => {
    expect(styles).toContain("button:disabled { opacity: 0.45; cursor: not-allowed; }");
    expect(styles).toContain("button.primary, .btn.primary { background: var(--accent-button); border-color: var(--accent-button); color: #fff; }");
    expect(styles).not.toContain(".btn:hover {");
    expect(styles).not.toContain(".btn.primary:hover,");
    expect(styles).not.toContain(".btn.primary:active {");
  });
});
