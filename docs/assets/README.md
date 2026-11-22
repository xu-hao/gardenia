# Project image provenance

## Gardenia fractal mark

[gardenia.svg](gardenia.svg) is gardenia's project mark, published in the repository
README and documentation homepage. It depicts an open, borderless five-fold (`C_5`)
golden-ratio recursive fractal bloom radiating freely from an open inner pentagon with
zero enclosing border, wall, box, or ring. The source specifies exactly two colors:
solid black (`#000000`) for all strokes and solid white (`#ffffff`) for the background.
It contains no shading, gradients, texture, text, fonts, embedded raster images, or
external dependencies.

The SVG was authored directly for [Issue #7](https://github.com/xu-hao/gardenia/issues/7)
on 2026-11-22, replacing the original walled eight-petal rosette from
[Issue #1](https://github.com/xu-hao/gardenia/issues/1), in the same geometric
monochrome style as `howhard` (`spiral.svg`) and `agroute` (`route.svg`). No
third-party artwork was used.

### Mathematical construction

- Canvas and viewBox: `512 × 512`, centered at `(256.0, 256.0)`, on a solid `#ffffff`
  background `<rect width="512" height="512" fill="#ffffff"/>`.
- Open inner pentagon and `C_5` symmetry: `5` root branches begin at radius `r_0 = 18.0`
  along the five-fold angles `a_k = −π/2 + 2πk/5` (`k = 0..4`), starting at
  `(256 + 18·cos(a_k), 256 + 18·sin(a_k))`.
- Self-similar recursion: `5` generations (`g = 0..4`), yielding
  `5 × (2^5 − 1) = 155` branches total (`5, 10, 20, 40, 80` paths per generation):
  - Length scaling: initial branch length `L_0 = 88.0`, scaling by `s = 0.585` per
    generation (`L_g = 88.0 × 0.585^g`: `88.00`, `51.48`, `30.12`, `17.62`, `10.31`),
    approaching the golden-ratio conjugate `φ⁻¹ ≈ 0.618` while preserving open negative
    space between neighboring lobes.
  - Stroke weight taper: initial stroke width `w_0 = 13.5`, tapering by `0.71` per
    generation and rounded half-up to two decimal places (`13.5`, `9.59`, `6.81`,
    `4.83`, `3.43`). Paths are grouped into five `<g stroke-width="...">` layers inside
    an outer `<g fill="none" stroke="#000000" stroke-linecap="round" stroke-linejoin="round">`.
  - Spiral curl: each branch at generation `g` advances in `3` equal substeps of length
    `sl = L_g / 3`; at each substep `i = 0..2`, the point steps by
    `(sl·cos(ca), sl·sin(ca))` and the heading angle increments by `+0.045 rad` (total
    curl `+0.135 rad` over the three substeps), emitting a 4-point polyline
    `<path d="M x0 y0 L x1 y1 L x2 y2 L x3 y3"/>` with coordinates formatted to two
    decimal places.
  - Bifurcation: after the third substep, every branch with `g < 4` bifurcates
    symmetrically from its tip `(cx, cy)` at headings `ca − 32°` and `ca + 32°`.
- Resulting bounding boxes (computed from the exact geometry and rounded to two decimal
  places):
  - Centerline bounding box: `[51.34, 460.85] × [47.35, 450.36]` (maximum centerline
    radius `209.04` from `(256, 256)`).
  - Stroke-expanded bounding box (accounting for rounded half-stroke caps):
    `[49.62, 462.57] × [45.64, 452.08]` (maximum stroke radius `210.76`, leaving at
    least `45.64` units of open white margin to every canvas edge).

Browsers may antialias edges; the vector source itself defines only `#000000` and
`#ffffff`.

### Checksum

The SHA-256 digest of `docs/assets/gardenia.svg` is:

`ae66a55136da04197307ef0b2a8b9f7d85dd5e10a23c5e520e43dcd2d9e793d0`

### Tracking

[Issue #7](https://github.com/xu-hao/gardenia/issues/7) tracks replacing the original
walled rosette mark from [Issue #1](https://github.com/xu-hao/gardenia/issues/1) with
this open five-fold golden-ratio fractal bloom.
