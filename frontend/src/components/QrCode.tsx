import qrcode from "qrcode-generator";

/**
 * A QR code rendered as React elements.
 *
 * The library offers `createImgTag` and `createSvgTag`, which return markup
 * strings — using either would mean `dangerouslySetInnerHTML` on a page that
 * displays a one-time secret, and the app's CSP has no `unsafe-inline` for
 * exactly that kind of reason. So only the module *matrix* is taken from the
 * library and the SVG is built here as ordinary elements. Nothing is injected.
 */
export default function QrCode({ value, size = 180 }: { value: string; size?: number }) {
  // Type 0 selects the smallest version that fits; 'M' tolerates ~15% damage,
  // which is the usual choice for something read off a screen.
  const qr = qrcode(0, "M");
  qr.addData(value);
  qr.make();

  const count = qr.getModuleCount();
  const margin = 2;
  const extent = count + margin * 2;
  const cells: { x: number; y: number }[] = [];
  for (let row = 0; row < count; row += 1) {
    for (let column = 0; column < count; column += 1) {
      if (qr.isDark(row, column)) cells.push({ x: column + margin, y: row + margin });
    }
  }

  return (
    <svg
      role="img"
      aria-label="Two-factor setup QR code"
      width={size}
      height={size}
      viewBox={`0 0 ${extent} ${extent}`}
      shapeRendering="crispEdges"
      style={{ background: "#fff", borderRadius: 6 }}
    >
      {cells.map((cell) => (
        <rect key={`${cell.x}-${cell.y}`} x={cell.x} y={cell.y} width={1} height={1} fill="#000" />
      ))}
    </svg>
  );
}
