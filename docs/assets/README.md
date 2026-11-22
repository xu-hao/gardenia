# Project image provenance

## Gardenia fractal mark

[gardenia.svg](gardenia.svg) is gardenia's project mark, published in the repository
README and documentation homepage. It depicts four non-overlapping imbricate
*Gardenia jasminoides* blossoms sized in consecutive Fibonacci proportions
(`89 : 55 : 34 : 21`) along a Golden-Rectangle spiral with zero enclosing border,
wall, box, or ring. The four blossoms represent the four ecosystem packages unified by
`gardenia`: `agrein` (`89`), `agcoord` (`55`), `howhard` (`34`), and `agroute` (`21`).

The source specifies exactly two colors: solid black (`#000000`) for all strokes and
solid white (`#ffffff`) for the background and inner-whorl occlusion masks. It contains
no shading, gradients, texture, `<text>`, fonts, embedded raster images, or external
dependencies.

The SVG was authored directly for [Issue #10](https://github.com/xu-hao/gardenia/issues/10)
on 2026-11-22, replacing the single five-fold fractal bloom from
[Issue #7](https://github.com/xu-hao/gardenia/issues/7) (which earlier replaced the
original walled eight-petal rosette from
[Issue #1](https://github.com/xu-hao/gardenia/issues/1)), in the same geometric
monochrome style as `howhard` (`spiral.svg`) and `agroute` (`route.svg`). No
third-party artwork was used.

### Mathematical construction

- Canvas and viewBox: `512 × 512` (`role="img"`), on a solid `#ffffff` background
  `<rect width="512" height="512" fill="#ffffff"/>`.
- Fibonacci Golden-Rectangle layout: starting from four square entries
  `(cx_0, cy_0, side, rot_0, sw_0)`:
  1. `agrein`: `(44.5, 44.5, 89.0, −π/2, 10.2)`
  2. `agcoord`: `(116.5, 27.5, 55.0, −π/2 + 0.28, 8.4)`
  3. `howhard`: `(127.0, 72.0, 34.0, −π/2 − 0.22, 6.8)`
  4. `agroute`: `(97.0, 75.0, 21.0, −π/2 + 0.15, 5.6)`
  Each center `(cx_0, cy_0)` is rotated by `ang_r = −24°` (`radians(-24.0)`) and scaled
  by `sc = 2.94`, with outer blossom radius `R = 0.415 × side × sc`. Translating all four
  centers by `(−10.412311, 234.831010)` centers the bounding rectangle
  `[min(rx − R), max(rx + R)] × [min(ry − R), max(ry + R)]` at `(256.0, 256.0)`:

  | Package | Fibonacci side | Centered `(cx, cy)` | Radius `R` | Base stroke `sw_0` | Whorl layers |
  | --- | --- | --- | --- | --- | --- |
  | `agrein` | `89` | `(162.32, 301.14)` | `108.59` | `10.20` | 3 whorls (6, 6, 5) + spiral heart |
  | `agcoord` | `55` | `(335.37, 169.38)` | `67.11` | `8.40` | 2 whorls (6, 6) + spiral heart |
  | `howhard` | `34` | `(416.79, 276.34)` | `41.48` | `6.80` | 2 whorls (6, 6) + spiral heart |
  | `agroute` | `21` | `(339.80, 320.27)` | `25.62` | `5.60` | 1 whorl (6) + spiral heart |

- Imbricate cubic-Bézier whorls (`draw_whorl(cx, cy, r_base, r_tip, rot, n_petals, sw, twist_deg, join_t)`):
  with `polar(cx, cy, r, a) = (cx + r·cos(a), cy + r·sin(a))`, `tw = radians(twist_deg)`,
  and `step_ang = 2π / n_petals`, each petal `k = 0 .. n_petals − 1` at `th = rot + k·step_ang`
  uses a leading cubic Bézier `L_k` from `p0 = polar(cx, cy, r_base, th)` through
  `p1 = polar(cx, cy, r_base + 0.36·(r_tip − r_base), th − 0.16·step_ang)` and
  `p2 = polar(cx, cy, r_base + 0.84·(r_tip − r_base), th + 0.12·tw)` to
  `p3 = polar(cx, cy, r_tip, th + tw)`. Exact de Casteljau subdivision of
  `L_{(k+1) mod n}` at `t = join_t` gives the tuck join point `jpt` and upper sub-Bézier
  `(jpt, u1, u2, p3_next)`. With normalized angular span `span_a = j_ang − (th + tw) > 0`,
  the trailing cubic Bézier `T_k` runs from `p3` to `jpt` via
  `q1 = polar(cx, cy, 0.98·r_tip, tip_ang + 0.42·span_a)` and
  `q2 = polar(cx, cy, j_rad + 0.46·(r_tip − j_rad), tip_ang + 0.86·span_a)`, keeping the
  petal strictly within `[tip_ang, j_ang]` with zero figure-8 self-intersections. Each whorl
  emits a `#ffffff` closed outer silhouette loop `(u1, u2, p3_next)` + `T_{(k+1) mod n}`
  to cleanly occlude underlying layers, followed by `#000000` rounded-cap/join petal paths:
  - Outer whorl (all 4 blossoms): `r_base = 0.22·R`, `r_tip = 0.97·R`, `rot`, `n_petals = 6`,
    `sw = sw_base`, `twist_deg = 22.0`, `join_t = 0.58`.
  - Middle whorl (`R >= 36.0`): `r_base = 0.14·R`, `r_tip = 0.61·R`, `rot + π/6`,
    `n_petals = 6`, `sw = max(4.2, 0.84·sw_base)`, `twist_deg = 24.0`, `join_t = 0.56`.
  - Inner whorl (`R >= 68.0`): `r_base = 0.09·R`, `r_tip = 0.36·R`, `rot + π/3`,
    `n_petals = 5`, `sw = max(3.6, 0.72·sw_base)`, `twist_deg = 26.0`, `join_t = 0.54`.
  - Furled spiral bud heart: white occlusion disc of radius `1.05·r_heart` where
    `r_heart = R × (0.11 if R >= 68.0 else 0.16 if R >= 36.0 else 0.24)`, topped by a
    25-point Archimedean spiral polyline (`i = 0..24`) at
    `polar(cx, cy, r_heart·(0.18 + 0.82·(i/24)), rot + (i/24)·1.25·2π)` with
    stroke width `max(3.0, 0.66·sw_base)`.
- Resulting bounding boxes and clearances (computed from the exact serialized geometry
  and rounded to two decimal places):
  - Centerline bounding box: `[58.01, 454.73] × [108.38, 398.80]`.
  - Stroke-expanded bounding box (accounting for rounded half-stroke widths):
    `[52.91, 458.13] × [104.18, 403.90]`, leaving at least `52.91` units of open white
    margin to every canvas edge.
  - Minimum pairwise stroke-edge clearance between any two blossoms: `20.32` units
    (`howhard` ↔ `agroute`).

Browsers may antialias edges; the vector source itself defines only `#000000` and
`#ffffff`.

### Checksum

The SHA-256 digest of `docs/assets/gardenia.svg` is:

`ac94a2e831ecadc8f75cad9c4c9a5431fbb849deaeb76ec4fb423aa81d7c7492`

### Tracking

[Issue #10](https://github.com/xu-hao/gardenia/issues/10) tracks replacing the
single five-fold fractal bloom from [Issue #7](https://github.com/xu-hao/gardenia/issues/7)
with this four-blossom Fibonacci Golden-Rectangle composition.
