# Project image provenance

## Gardenia rosette mark

[gardenia.svg](gardenia.svg) is gardenia's project mark, published in the repository
README and documentation homepage. It depicts a symmetrical eight-petal pinwheel
gardenia rosette centered inside a subtle open-cornered square garden border. The
source specifies exactly two colors: solid black (`#000000`) for all strokes and solid
white (`#ffffff`) for the background. It contains no shading, gradients, texture,
text, fonts, embedded raster images, or external dependencies.

The SVG was authored directly for [Issue #1](https://github.com/xu-hao/gardenia/issues/1)
on 2026-11-22 in the same geometric monochrome style as `howhard` (`spiral.svg`) and
`agroute` (`route.svg`). No third-party artwork was used.

### Mathematical construction

- Canvas and viewBox: `512 × 512`, centered at `(256, 256)`.
- Uniform line weight: `stroke-width="17"`, `stroke-linecap="round"`,
  `stroke-linejoin="round"`.
- Open-cornered garden border: four equal straight wall segments along `x = 52`,
  `x = 460`, `y = 52`, and `y = 460`, each spanning from `108` to `404` (leaving
  symmetric `56`-unit open corner gaps).
- Central ring: circle centered at `(256, 256)` with radius `r0 = 34`.
- Eight-petal pinwheel rosette (`C8` rotational symmetry, `θ_k = k·π/4 − π/2` for
  `k = 0..7`):
  - Outward cubic Bézier for petal `k` from `(r0 = 34, θ_k)` through polar control
    points `(78, θ_k − 10°)` and `(136, θ_k + 4°)` to outer petal crest
    `(152, θ_k + 24°)`.
  - Return cubic Bézier from the crest through polar control points `(160, θ_k + 42°)`
    and `(124, θ_k + 52°)` to the exact analytic join point `C_{k+1}(0.48)` on the
    neighboring petal's outward curve.
  - Resulting rosette bounding box is `[109.37, 402.63] × [109.37, 402.63]`, leaving
    `57.37` units of uniform clearance to every border edge.

Browsers may antialias edges; the vector source itself defines only `#000000` and
`#ffffff`.

### Checksum

The SHA-256 digest of `docs/assets/gardenia.svg` is:

`67d91d31b967746dc3bc402df54575e9fc3954f695179a4066a222c5b4819be4`

### Tracking

[Issue #1](https://github.com/xu-hao/gardenia/issues/1) tracks authoring `gardenia.svg`
for the `0.1.0` CLI and documentation release.
