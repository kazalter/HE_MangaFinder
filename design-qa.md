# MangaFinder Logo 方案二 Design QA

- source visual truth: `docs/designs/logo-option-2.png`
- implementation screenshots: `docs/designs/qa-logo/expanded.png`, `docs/designs/qa-logo/collapsed.png`
- viewport: 1552 × 1014
- states: desktop sidebar expanded and collapsed
- full-view comparison evidence: `docs/designs/qa-logo/reference-vs-implementation.png`
- focused comparison evidence: `docs/designs/qa-logo/focused-reference-vs-asset.png`

## Findings

No actionable P0, P1, or P2 findings remain.

- Fonts and typography: the 20 px DM Sans wordmark retains the selected concept's confident sans-serif character without changing the established sidebar type system. The 10 px descriptor uses aged-paper gold and wider tracking to match the visual target while remaining readable at production scale.
- Spacing and layout rhythm: the expanded lockup uses a 50 px mark track, 12 px gap, and centered 46 px mark. The collapsed rail uses a 42 px mark inside the existing 76 px rail. Both states align with the navigation controls and preserve the current sidebar proportions.
- Colors and visual tokens: the mark uses warm ivory, aged-paper gold, and a single cinnabar locator point over the existing forest-green surface. These map directly to the selected concept and MangaFinder's current palette.
- Image quality and asset fidelity: the selected book-and-radar emblem is implemented as a dedicated 256 px transparent PNG generated from the chosen visual target, not as CSS art, a text glyph, or an approximate inline SVG. The transparent edge is clean on the production green surface, with no visible chroma halo at 42 px or 46 px.
- Copy and content: `MangaFinder` and `作者作品雷达` remain exact. No navigation or surrounding product copy changed.
- Accessibility and behavior: the expanded mark is decorative, while the collapsed mark retains the accessible `MangaFinder` label. Existing focus states and expand/collapse controls remain unchanged.

## Comparison history

### Pass 1

- The first rendered implementation preserved the selected open-page silhouette, two radar arcs, and cinnabar point in both required production sizes.
- The generated concept's oversized presentation was intentionally normalized to the existing 46 px brand slot rather than enlarging the sidebar header.
- No actionable P0/P1/P2 differences were found. The focused comparison confirms subject geometry, palette, transparency, and optical balance.

## Browser verification

Google Chrome rendered the local app at 1552 × 1014. Primary interactions checked:

- the sidebar rendered the new lockup in its expanded state;
- the collapse control switched to the 76 px rail and retained a legible standalone mark;
- the expand control restored the full wordmark and descriptor;
- no application console errors were recorded during reload and state changes.

Automated verification: the TypeScript/Vite production build passed; 7 frontend test files and 19 tests passed.

## Follow-up polish

- P3: if the brand is later used for large-format print, a separately mastered vector source would be useful. The current 256 px asset is intentionally optimized for the 42–46 px product UI slots.

final result: passed

---

# 紧凑列表封面原位展开 Design QA

- source visual truth: `docs/designs/qa-cover-preview/implementation.png` as the existing product baseline, plus the user's annotation that the cover should enlarge at its original thumbnail position and temporarily expand the whole row
- implementation screenshot: `docs/designs/qa-inline-cover/implementation.png`
- viewport: 1440 × 900
- state: desktop compact list, fourth available cover hovered, current row expanded in place
- full-view comparison evidence: `docs/designs/qa-inline-cover/comparison.png` (top row)
- focused comparison evidence: `docs/designs/qa-inline-cover/comparison.png` (bottom row)

## Findings

No actionable P0, P1, or P2 findings remain.

- Typography and copy: all existing title, author, tag, status, source, version, and date styles remain unchanged. The interaction adds no new copy or competing type hierarchy.
- Spacing and layout rhythm: the active row grows from the compact 48 px cover track to a 150 × 202.5 px cover with 14 px vertical row padding. The measured row height is 231.5 px, keeping the cover fully contained while pushing later rows down as one coherent list interaction.
- Colors and surfaces: the active row reuses the existing warm-paper hover surface, terracotta inset selection rule, neutral cover border, and restrained shadow. No new palette tokens were introduced.
- Image quality and asset fidelity: the enlarged cover is the same `CoverImage` instance and URL used by the thumbnail, so there is no duplicate floating image, loading mismatch, or placeholder substitution.
- Interaction and accessibility: pointer hover enlarges the thumbnail in place; pointer leave and blur close it; focus-visible exposes the same preview for keyboard users; clicking closes the preview before opening work detail. Rows without a cover remain compact. Reduced-motion users inherit the existing motion override.
- Viewport resilience: at the 1440 px desktop target the measured row bounds are fully inside the viewport. Tablet sizing reduces the enlarged cover to 116 px; the mobile breakpoint deliberately retains the normal 48 px row because touch devices do not have a reliable hover state.
- Icons: no iconography was added or changed.

## Comparison history

### Previous direction

- The earlier right-docked preview kept list rows compact but split attention between the left-side hover target and a separate preview on the far right.
- User feedback identified this gaze travel as visually disconnected.

### Current pass

- Removed the portal-based right preview, title footer, connector line, and reserved right preview rail.
- Enlarged the existing cover at the pointer location and expanded only its owning row, preserving the relationship among image, title, metadata, status, sources, and update information.
- Combined before/after evidence shows the catalog returning to full width while the hovered item becomes the single local focal point.
- Browser measurements confirm row `1021.4 × 231.5`, cover `150 × 202.5`, complete in-row containment, and no viewport clipping.

## Browser verification

Google Chrome rendered the local app at 1440 × 900. Primary interactions checked:

- switched into compact-list mode and hovered a real cover;
- confirmed the hovered cover enlarged at its original left-side position and the whole row expanded around it;
- confirmed the old floating preview element was absent;
- clicked the enlarged cover, confirmed work detail opened, and confirmed the expanded state closed;
- checked browser runtime and log events; no console errors were recorded.

Automated verification: all 8 frontend test files and 22 tests passed; the TypeScript/Vite production build and whitespace validation passed.

## Follow-up polish

- P3: expansion currently uses a restrained 200 ms transition. This can be tuned after judging the feel against a larger real catalog, without changing the interaction model.

final result: passed

---

# 紧凑列表悬浮封面方案三 Design QA

- source visual truth: `docs/designs/qa-cover-preview/source-option-3.png`
- implementation screenshot: `docs/designs/qa-cover-preview/implementation.png`
- viewport: 1440 × 900
- state: desktop compact list, fourth available cover hovered, enlarged preview visible in the right preview rail
- full-view comparison evidence: `docs/designs/qa-cover-preview/comparison.png` (top row)
- focused comparison evidence: `docs/designs/qa-cover-preview/comparison.png` (bottom row)

## Findings

No actionable P0, P1, or P2 findings remain.

- Fonts and typography: the preview footer uses the existing Georgia / Noto Serif SC editorial display stack at 14 px and truncates long dynamic titles to one line. The list keeps the established DM Sans / Noto Sans SC hierarchy, so the new state does not introduce a competing type system.
- Spacing and layout rhythm: at the 1440 px target viewport, the compact list reserves a 256 px preview rail. The 230 × 337.5 px preview sits 24 px from the viewport edge and remains entirely visible. The selected row, short connector, preview frame, 2 px radius, and restrained shadow reproduce the selected option without moving individual rows.
- Colors and visual tokens: warm white `#fffdf8`, neutral paper border `#cbbfb0`, terracotta `#c65d3d`, and the existing ivory list surface map directly to the selected design and current MangaFinder palette.
- Image quality and asset fidelity: the enlarged preview reuses the exact existing cover URL at the source aspect ratio with `object-fit: cover`; it is not a placeholder, CSS drawing, or generated substitute. Failed covers receive a readable fallback and works without covers do not open an empty preview.
- Copy and content: the footer uses the exact dynamic work title. No catalog labels, status values, source badges, or existing navigation copy were changed.
- Interaction and accessibility: hovering the small cover opens the preview; pointer events pass through the floating visual; keyboard focus-visible can expose the same state; blur closes it; opening the work detail closes the preview first. The duplicate enlarged image is `aria-hidden` to prevent repetitive screen-reader output, and the existing reduced-motion rule covers both entrance animations.
- Viewport resilience: preview width drops to 180 px below 1100 px and is additionally constrained by available viewport height. Positioning clamps to all viewport edges. The desktop reserved rail only applies from 1360 px upward, avoiding compact-grid overflow at narrower breakpoints.
- Icons: the interaction adds no iconography, so the existing Phosphor-based icon system remains unchanged.

## Comparison history

### Pass 1

- [P2] The first browser capture placed the preview correctly inside the viewport but overlaid the list's version and date columns.
- Fix: reserved a 256 px right-side preview rail at wide desktop sizes and anchored the terracotta connector from the selected row's outer edge instead of drawing across list content.
- [P1] Interaction review found that a mouse click could leave a focus-triggered preview above the work-detail modal.
- Fix: focus now opens the preview only for `:focus-visible`, and the row closes the preview synchronously before calling the detail action.

### Pass 2

- Post-fix evidence: `docs/designs/qa-cover-preview/implementation.png` and the bottom-right panel of `docs/designs/qa-cover-preview/comparison.png` show the preview outside all data columns, aligned to the highlighted row, with the connector visible and no viewport clipping.
- Browser measurements confirm `left: 1186`, `right: 1416`, `top: 442.86`, and `bottom: 780.36` inside the 1440 × 900 viewport.
- No actionable P0/P1/P2 differences remain.

## Browser verification

Google Chrome rendered the local app at 1440 × 900. Primary interactions checked:

- switched from the persisted catalog view into compact-list mode;
- hovered a real small cover and observed the highlighted row, connector, enlarged cover, and dynamic title;
- verified the preview stayed fully within the viewport;
- clicked the hovered cover, confirmed the work-detail view opened, and confirmed the preview closed first;
- checked browser runtime and log events; no console errors were recorded.

Automated verification: all 8 frontend test files and 22 tests passed; the TypeScript/Vite production build passed.

## Follow-up polish

- P3: a low-resolution source cover may look softer when enlarged. The component intentionally preserves the existing cached source instead of inventing a higher-resolution asset.

final result: passed
