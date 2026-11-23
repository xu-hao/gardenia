# Project image provenance

## Gardenia fractal hexagon mark

[gardenia.svg](gardenia.svg) is gardenia's project mark, published in the repository
README and documentation homepage. It is an abstract geometric composition of five
parallel-oriented regular hexagons stepping down in fractal proportions along an open
counter-clockwise-to-clockwise face-to-face arc with uniform stroke width and rounded
joins, with zero enclosing border, box, or frame.

The source specifies exactly two colors: solid black (`#000000`) for all strokes and
solid white (`#ffffff`) for the background. It contains no shading, gradients, texture,
`<text>`, fonts, embedded raster images, or external dependencies.

The SVG was authored directly for [Issue #16](https://github.com/xu-hao/gardenia/issues/16)
on 2026-11-22 in the same bold monochrome vector style as `howhard` (`spiral.svg`) and
`agroute` (`route.svg`), replacing the earlier floral marks from
[Issue #10](https://github.com/xu-hao/gardenia/issues/10),
[Issue #7](https://github.com/xu-hao/gardenia/issues/7), and
[Issue #1](https://github.com/xu-hao/gardenia/issues/1). No third-party artwork was used.

### Mathematical construction

- Canvas and viewBox: `512 × 512` (`role="img"`), on a solid `#ffffff` background
  `<rect width="512" height="512" fill="#ffffff"/>`.
- Each hexagon is a pointy-top regular hexagon (`vertices at 30° + 60°·k` for
  `k = 0 .. 5`) rendered as `<polygon ... fill="none" stroke="#000000" stroke-width="16" stroke-linejoin="round"/>`.
- Starting from unscaled circumradii `[118, 80, 55, 38, 26]`, stroke `16`, and uniform
  parallel-face channel gap `20`, each subsequent hexagon `i` (`i = 2 .. 5`) steps off
  the previous hexagon `i − 1` along face-normal angles `[−60°, 0°, +60°, +120°]` at
  center distance `(R_{i-1} + R_i)·(√3 / 2) + 36`, then the entire group is centered
  and scaled to span `428` units around `(256, 256)`:

  | Hexagon | Centered `(cx, cy)` | Circumradius `R` | Face-normal step |
  | --- | --- | --- | --- |
  | `1` | `(143.20, 321.06)` | `108.37` | anchor |
  | `2` | `(238.47, 156.04)` | `73.47` | `-60°` off Hex 1 |
  | `3` | `(378.91, 156.04)` | `50.51` | `+0°` off Hex 2 |
  | `4` | `(432.43, 248.73)` | `34.90` | `+60°` off Hex 3 |
  | `5` | `(390.45, 321.45)` | `23.88` | `+120°` off Hex 4 |

- Resulting bounding boxes (computed from the exact serialized vertices):
  - Centerline bounding box: `[49.35, 462.65] × [82.57, 429.43]`.
  - Stroke-expanded bounding box (half-stroke `8.00`): `[41.35, 470.65] × [74.57, 437.43]`, leaving at least
    `41.35` units of open white margin to every canvas edge.

Browsers may antialias edges; the vector source itself defines only `#000000` and
`#ffffff`.

### Checksum

The SHA-256 digest of `docs/assets/gardenia.svg` is:

`0e0fb691faa40e1e2a9f477cb590b6e73e4c9b46c1b7f167ea8b7017a427b147`

### Tracking

[Issue #16](https://github.com/xu-hao/gardenia/issues/16) tracks replacing the
four-blossom mark from [Issue #10](https://github.com/xu-hao/gardenia/issues/10)
with this five-hexagon open fractal arc.
