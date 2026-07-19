# 方案二作者动态中心 Design QA

- source visual truth: `docs/designs/social-radar-author-rail-option-2.png`
- implementation screenshot: `docs/designs/qa-social-radar-author-rail/implementation-v3.png`
- desktop viewport: 1503 × 1046
- mobile viewport: 390 × 844
- state: mignon selected, account management collapsed, recent activity tab active
- full-view comparison evidence: `docs/designs/qa-social-radar-author-rail/reference-vs-implementation.png`
- focused comparison evidence: `docs/designs/qa-social-radar-author-rail/focused-reference-vs-implementation.png`

## Findings

No actionable P0, P1, or P2 findings remain.

- Fonts and typography: the Georgia/Noto Serif display heading and DM Sans/Noto Sans interface typography preserve the reference hierarchy. The author rail, tabs, and summary labels use the same compact optical weights.
- Spacing and layout rhythm: the 1168 × 876 modal, 290 px author rail, 28 px inter-column gap, author context row, tab baseline, digest origin, and rail footer align with the selected visual at the same viewport.
- Colors and tokens: the warm paper surface, forest-green sync states, burnt-orange selection rail, pale active background, and restrained borders follow the source and existing MangaFinder system.
- Image quality and assets: live confirmed X avatars replace the illustrative mock avatars intentionally. Missing imagery uses the existing Phosphor icon family rather than CSS drawings or text-symbol placeholders.
- Copy and content: navigation and account-management copy match the target. The right content preserves real MangaFinder summaries and evidence instead of the generated mock's invented counters and sample works.
- Responsiveness: at 390 × 844 the author rail becomes a horizontally scrollable selector, the selected-author context stacks safely, and the modal remains usable without horizontal page overflow.
- Accessibility: the search field is labeled, author rows are semantic buttons with current state, account management exposes expanded state, focus styling is visible, and real images retain non-text fallbacks.

## Comparison history

### Pass 1

- Earlier P2: the first implementation used the previous free-height modal, placing the frame against the viewport edges and misaligning the rail and digest vertically.
- Earlier P2: live API author order placed the selected author last, unlike the persistent selected context in the source.
- Fix: constrained the desktop modal to the target's 876 px height, made the rail and content independently usable, and keeps the selected radar author at the top of the rail.

### Pass 2

- Earlier P2: the author rail began 10 px too low, ended 9 px too high, and the tabs/digest started below the visual target.
- Fix: tuned workspace top spacing, bottom padding, and the content-tab margin.
- Post-fix evidence: the focused comparison shows aligned search, selected author row, profile context, tabs, and digest top edge.

### Pass 3

- No actionable P0/P1/P2 differences remain. Live avatar art and real digest content are intentional production-data differences.

## Browser verification

Google Chrome rendered the deployed app at the target desktop and mobile viewports. Primary interactions checked:

- the radar opened with `mignon` selected and three live author rows;
- clicking `ie` changed only the radar context and persisted radar author ID `3`;
- the underlying URL remained `?radar=&author=1`, so the work-library author selection was not changed;
- searching `ramanda` reduced the author rail to one result;
- “账号与同步” opened the correct author's account panel;
- the “原始动态” tab rendered its data view;
- the close control dismissed the modal;
- no application console errors were recorded.

Automated verification: 7 frontend test files and 19 tests passed; TypeScript and Vite production build passed. Both deployed containers reported healthy.

## Follow-up polish

- P3: the generated mock uses monochrome sample avatars, while production correctly prioritizes each author's live X avatar.
- P3: the mock's summary counters are not shown because they are invented data; the implementation retains factual summary, evidence, and source-post content.

final result: passed
