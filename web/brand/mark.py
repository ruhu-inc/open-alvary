"""The Open Alvary mark: an arrowhead, drawn as an outline.

The mark is three shapes on a 64-unit square: a kite over two legs, split by a V-shaped gap. An
outline is drawn inside each shape, so it keeps the mark's outer edge and gaps at every line weight.
Running this rewrites the icon and share-image sources and prints the shapes for `BrandMark` in
src/site.tsx. Render the PNGs on macOS with `qlmanage -t -s <size> -o <dir> <svg>`, cropping the
share image to its middle 1200x630 with `sips --cropToHeightWidth 630 1200`.
"""

from pathlib import Path

SHAPES = {
    "kite": [(32, 4), (18.19, 33.44), (32, 42.32), (45.81, 33.44)],
    "left leg": [(17.09, 35.79), (30.9, 44.3), (5.73, 60)],
    "right leg": [(46.91, 35.79), (58.27, 60), (33.1, 44.3)],
}
GREEN, CREAM = "#214b3b", "#fcfbf7"
HERE = Path(__file__).parent


def inset(shape, distance):
    """The shape moved inward by `distance` on every side."""
    n = len(shape)
    sides = list(zip(shape, shape[1:] + shape[:1], strict=True))
    turn = 1 if sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in sides) > 0 else -1
    edges = []
    for (x1, y1), (x2, y2) in sides:
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        nx, ny = -dy * turn / length, dx * turn / length
        edges.append((x1 + nx * distance, y1 + ny * distance, dx, dy))
    corners = []
    for i in range(n):
        ax, ay, adx, ady = edges[i - 1]
        bx, by, bdx, bdy = edges[i]
        t = ((bx - ax) * bdy - (by - ay) * bdx) / (adx * bdy - ady * bdx)
        corners.append((round(ax + adx * t, 2), round(ay + ady * t, 2)))
    return corners


def points(shape):
    return " ".join(f"{x:g},{y:g}" for x, y in shape)


def outline(weight, colour, transform=""):
    """The mark as SVG, its line `weight` units wide."""
    shapes = "".join(f'<polygon points="{points(inset(s, weight / 2))}"/>' for s in SHAPES.values())
    move = f' transform="{transform}"' if transform else ""
    return f'<g{move} fill="none" stroke="{colour}" stroke-width="{weight}">{shapes}</g>'


def main():
    (HERE.parent / "public" / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{CREAM}"/>'
        f"{outline(4.4, GREEN, 'translate(5.76,5.76) scale(0.82)')}</svg>\n"
    )
    (HERE / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">\n'
        f'  <rect width="512" height="512" fill="{GREEN}"/>\n'
        f"  {outline(3.6, CREAM, 'translate(96,96) scale(5)')}\n</svg>\n"
    )
    (HERE / "og-image.svg").write_text(
        # Drawn on a square canvas because Quick Look renders squares; the PNG is the middle 1200x630.
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1200" viewBox="0 0 1200 1200">\n'
        f'  <rect width="1200" height="1200" fill="{CREAM}"/>\n'
        '  <g transform="translate(0,285)">\n'
        f'  <rect x="0" y="600" width="1200" height="30" fill="{GREEN}"/>\n'
        f"  {outline(2.4, GREEN, 'translate(80,70) scale(1.5)')}\n"
        f'  <text x="192" y="136" fill="{GREEN}" font-family="Helvetica Neue, Arial, sans-serif" '
        'font-weight="500" font-size="44">open alvary</text>\n'
        '  <text x="80" y="300" fill="#22372f" font-family="Georgia, serif" font-size="84">A shared foundation</text>\n'
        '  <text x="80" y="400" fill="#22372f" font-family="Georgia, serif" font-size="84">'
        'for <tspan fill="#5f7d6c">African law.</tspan></text>\n'
        '  <text x="80" y="484" fill="#4a5a50" font-family="Helvetica Neue, Arial, sans-serif" font-size="32">'
        "Public legal information with clear rights and traceable sources.</text>\n"
        f'  <text x="80" y="548" fill="{GREEN}" font-family="Helvetica Neue, Arial, sans-serif" '
        'font-weight="500" font-size="28">open.alvary.ai</text>\n'
        "  </g>\n</svg>\n"
    )
    for weight in (2.2, 3.2):
        print(f"weight {weight}:")
        for name, shape in SHAPES.items():
            print(f"  {name}: {points(inset(shape, weight / 2))}")


if __name__ == "__main__":
    main()
