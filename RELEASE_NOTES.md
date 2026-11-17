KiCad Project Manager v1.3.0

- Add an Exclude JLC checkbox as the last BOM & Stock column. Save ticks per
  project and omit checked items from the JLCPCB assembly CSV while retaining
  purchasing costs.
- Add the BOM & Stock tab: quantity-based JLCPCB USD
  pricing, stock and library classification, sorting, caching and saved paid prices.
- Detect LCSC assignments per component in dedicated fields and primary or
  alternate supplier part-number fields. Prefer the primary LCSC part, then the
  first LCSC alternative. Show the detected source fields and report conflicts.
- Exclude test points from schematic/CSV BOM imports, stock lookups, costing and
  assembly exports, including test-point references inside grouped CSV rows.
- Calculate USD totals from the components required for the selected board count.
  Keep MOQ for supplier price-tier selection and Needed / buy information without
  charging unused reel or minimum-order components to the board cost estimate.
- Export a per-board JLCPCB assembly CSV with Comment, Designator, Footprint,
  Quantity and LCSC Part #. Comment preserves the source Value; Quantity counts
  the row's designators on one board. Preserve values and footprints independently
  of cost grouping and build quantity. Keep uncoded parts for manual matching;
  refuse ambiguous codes and incomplete assembly data.
- Include BOM regressions in native Linux and bundled macOS GUI checks.
- Keep installation instructions and source package metadata independent of any
  fixed organization or personal repository. Resolve the release destination
  from CI, the checkout's origin, or an explicit packaging option.

KiCad Project Manager v1.2.0

- Remember the explicit release version, including Unreleased, in the project's
  Project Manager records. Default to blank instead of guessing from copied YAML
  history entries, and preserve the choice through reopening and template upgrades.
- Clear the unsaved-setup flag after Apply reports no changes, including when a
  CHECKED release version already has its history commands enabled.
- Use the same source-folder dialog for templates and standards, showing the
  current path. Size Git settings to fit all controls, fill remaining Overview
  space with Activity, and remove the redundant template Details window.
- Prepare board releases in Project Setup when CHECKED is selected. Edit only
  the Unreleased changelog notes and enter a version to enable the matching
  KiBot release title/body commands. Preserve earlier history and custom commands.
- Preview and back up changelog, variable and setup changes together; reject
  external edits before saving. Leave dated release sections to release-tag CI.
- Inspect the saved schematic hierarchy for the selected version's visible
  RELEASE_TITLE and RELEASE_BODY placeholders. Report missing blocks and remind
  designers to inspect the final schematic PDF without changing schematic layout.
- Add a fifth Checklist page with bundled generic PCB review items, reviewer
  names, comments and dates. Save answers in kibot_resources/project_manager/review.json,
  independently of Git, standards repositories and online services.
- Keep checklist ticks through iterative design changes. Optionally record a
  completed review against local saved-file fingerprints; show one notice for
  later design changes while preserving every answer and excluding editor preferences.
- Compact Project Setup into two columns with single-line authors and multiple
  reviewers. Keep Apply/Preview visible, prevent accidental dropdown wheel changes,
  and show schematic-history results beside the inspection button.
- Explain stackup row differences, including dielectric constant and loss tangent.
- Update all shared template files directly from Overview or template review,
  preserving project documents, metadata, workflow choices and release history.
- Optionally configure source Git URLs for automatic background checks and explicit
  fast-forward pulls. Refuse overlapping local edits and refresh sources after pulling.
- Show checklist comments in the table, autosave feedback and completed-review
  confirmation. Add Tick all/Untick all; leave old root review records untouched.

KiCad Project Manager v1.1.0

- Separate Project documents from shared Template files in template review. Use
  a resizable file list and editor/diff workspace with search and bulk actions.
- Open editable documents directly in a large editor. Keep drafts and pending
  choices across filters, selections and partial applies. Remove the redundant
  README shortcut and previous-baseline tab; retain stored merge baselines.
- Distinguish reference comparisons from action-specific planned changes. Color
  both diff modes and Apply previews red/green; shared files stay diff-only.
- Refresh the open review from disk without losing compatible drafts. Confirm
  before discarding edits to externally changed files; recheck changed actions.
- Keep review open after confirming Preview Apply, just as with direct Apply.
  Return .gitignore to green after custom patterns are removed; comments and
  blank lines alone no longer count as custom rules.
- Give shared/project ignore sections a draggable horizontal 50/50 split.
- Edit standard impedance CSV documents in a cell table with Add/Delete row and
  Text fallback. Import missing guidance only from a saved-board construction
  match, retaining manual rows/tolerances and reporting width/gap conflicts.
- Simplify Overview by removing the redundant Board file path row.
- Edit only the project section of a managed .gitignore; protect shared rules,
  preserve project overrides and show custom project rules in blue.
- Remember accepted document customizations in the project review record. Keep
  the overall status green and request review when that template default changes.
- Review template-provided LICENSE files through Keep project / Use template,
  with overwrite confirmation and local backups. Older records remain supported.
- Allow template manifests to declare additional plain documents under their
  templates resource folder without enabling editing for automation or binaries.

KiCad Project Manager v1.0.1

- Prevent four GTK allocation warnings when opening Design Rules by sizing its
  nested notebooks before adding their pages. Settings and project files are unchanged.

KiCad Project Manager v1.0.0

- First stable PCM release, supporting KiCad 9 and 10 on Linux, macOS and Windows.
- Support KiCad 9 and 10 IPC, workflows and their explicitly recognized board formats.
- Preserve a project's selected KiCad major during template updates.
- Let Project Setup select the CI KiCad major and suggest upgrading it for files
  opened in KiCad 10. Show persistent success/failure feedback after Apply.
- Require standards to declare compatibility before applying them to another major.
- Read KiCad 10 net names and retain the source major when capturing stackup presets.
- Select template/install paths for the intended major, including documents-root overrides.
- Detect and restore missing template resources, including all nested text/binary
  logos, even when the template version already matches.
- Exclude template maintenance files and optional workflows from managed updates.
- Remove company-specific trial setup and companion-plugin CI dependencies;
  retain generic PCM and manual development installation.

KiCad Project Manager v0.7.0

- Review template files in batches with Shift/Ctrl selection and partial apply.
- Apply directly beside Preview Apply and continue in the open review window.
- Store each stackup preset in one self-contained JSON file under catalog schema 2.
- Save an unmatched saved-board construction as a preset, with metadata,
  impedance guides and duplicate/similarity checks.
- Use a compact README with native screenshots and linked operating/developer guides.
- Use neutral public wording and the runtime-valid package ID
  `org.kicadprojectmanager.plugin`. Validate the extra KiCad 9 identifier constraint
  during development installation, packaging and schema checks.

Uninstall a PCM copy with the previous identity before installing this package.
Existing project data, settings and backups keep their current locations.
Save and close PCB/schematic editors before applying standards; reopen afterward.
