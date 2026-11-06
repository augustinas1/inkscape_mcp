# MCP workflow reference

Operational guide for driving `inkscape_mcp` against a running (or headless) Inkscape. Originally captured in the `svg-design` skill; consolidated here so it ships with the repo and is reachable independent of any per-client skill installation.

When the `inkscape_mcp` server is reachable and Inkscape is running, **prefer driving edits through the MCP** instead of hand-writing SVG. The MCP gives you precise geometric ops, live canvas feedback, and avoids the "I rewrote the whole file" failure mode for small tweaks.

Hand-author SVG when: the document doesn't exist yet, no Inkscape session is active, or you need a single-shot generation (icon, logo). Drive via MCP when: the file is already open, you're iterating, or you need an op Inkscape provides natively.

## Compose procedurally — the default design loop

Building a drawing as one blob of hand-written SVG is the failure mode this server exists to prevent: brittle hand-computed coordinates, no reuse, and a document the user can't edit afterwards. Build it up the way a designer would instead.

| Instead of… | Do this |
|---|---|
| Computing aligned/evenly-spaced coordinates by hand | `inkscape_vector` `align` (`operation_type="top"`, `"hcenter vcenter"`, `"top page"`) and `distribute` (`"hgap"`, `"vgap"`) |
| Writing one path that is really several shapes merged | Place the shapes, then `apply_boolean` (`union` / `difference` / `intersection` / `exclusion`), `path_combine`, `path_division`, `path_cut` |
| Copy-pasting the same geometry N times | `clone`, `tile_clone`, `object_to_marker`, `object_to_pattern`, or `<symbol>`/`<use>` — one edit updates every instance |
| Hand-authoring filter primitives | `inkscape_extension(operation="run_live", target=<id>)` with **no params**, after selecting the target; or `lpe_*` for path effects. Read the rendering-pitfalls section below before writing filters yourself |
| Outlining a stroke by hand | `stroke_to_path`; grow/shrink with `path_inset_outset` |
| Resizing the canvas by editing width/height | `fit_canvas_to_drawing`, `page_fit_to_selection` |

Two habits that make everything above work:

1. **Give every meaningful element a stable `id` as you create it.** Ids are the handle for every later edit — unaddressable geometry has to be rebuilt rather than adjusted.
2. **Work incrementally.** Each call is one Inkscape undo entry, so the user can step back through your work and co-edit alongside you.

## Look at your work

`rasterize` gives you eyes — render the document, a single element, or a region to PNG and read the image back. After any visual change, LOOK rather than assuming the SVG renders as intended: occlusion, clipped filter regions, invisible elements and colour mistakes only show up in the raster. When you need numbers instead of pixels, `inspect_element` returns the post-transform bbox and the fully-cascaded computed style.

## Work with the person at the keyboard

The canvas is shared, so collaborate rather than guess.

- **Ask them to select.** For an ambiguous target ("fix the spacing", "recolor this"), ask the user to select the object or group on canvas and read it back with `get_selection` / `inspect_selection`. Faster and far less error-prone than inferring intent from the XML.
- **Show them your candidate.** `set_selection` highlights what you're about to change on their canvas before you change it.
- **Open the UI for them.** When they ask where a setting lives, open it rather than describing a menu path — `apply_action("dialog-open", payload=<name>)`. Valid dialog names: `AlignDistribute`, `Clonetiler`, `DocumentProperties`, `Export`, `FillStroke`, `FilterEffects`, `Find`, `Glyphs`, `IconPreview`, `Input`, `LivePathEffect`, `Memory`, `Messages`, `ObjectAttributes`, `ObjectProperties`, `Objects`, `PaintServers`, `Selectors`, `Spellcheck`, `SVGFonts`, `Swatches`, `Symbols`, `Text`, `Trace`, `Transform`, `UndoHistory`, `XMLEditor`. Tools via `apply_action("tool-switch", payload=<name>)`: `Arc`, `Booleans`, `Calligraphic`, `Connector`, `Dropper`, `Eraser`, `Gradient`, `LPETool`, `Measure`, `Mesh`, `Node`, `PaintBucket`, `Pen`, `Pencil`, `Rect`, `Select`, `Spiral`, `Spray`, `Star`, `Text`, `Tweak`, `Zoom`. Both are read-only — they never modify the document.

## Picking the right operation

| You want to… | Use |
|---|---|
| Fire any of Inkscape's ~1244 verbs (selection, transforms, booleans, attribute edits, tool/dialog opening, layer ops, page ops, snap toggles, zoom/view) | `inkscape_live(operation="apply_action", target="<verb>", payload="<arg>")` |
| Discover the verb you need + its description | `inkscape_live(operation="list_actions", target="<substring>")` — returns `{app[{name, description}], window[{name, description}]}`. Filter is case-insensitive, matches name or description. Use `target="layer-"` for layer ops, `"snap"` for snapping, etc. |
| Insert new SVG geometry into the canvas | `inkscape_live(operation="insert_svg", payload="<svg fragment>")` |
| Structurally edit the XML (add to `<defs>`, restructure, rewrite `d` data, set arbitrary attribute on an arbitrary node) | `inkscape_live(operation="edit_xml", target="<xpath>", payload="<json>")` |
| Per-node bezier edits on a path (move node, set handle, smooth/cusp, insert node, subdivide) | `inkscape_live(operation="path_edit", target="<path-id>", payload="<json>")` |
| Read what the user has selected | `inkscape_live(operation="get_selection")` → `{count, ids, objects[{id,type,bbox}], text}` |
| Read the full document XML | `inkscape_live(operation="get_document_xml")` |
| Select something by id or CSS selector | `inkscape_live(operation="set_selection", target="<id-or-selector>")` |
| Check the bridge is live | `inkscape_live(operation="ping")` |

Default to `apply_action`. Use `edit_xml` when no Inkscape verb covers the edit (defs additions, structural rewrites, complex `d` math).

## Gotchas when firing actions

1. **`select-by-id` and `select-by-selector` are additive.** They don't replace the prior selection — they *add* to it. A subsequent `object-set-attribute` then mutates every still-selected element, not just the latest one. **Fire `apply_action("select-clear")` before each new selection.**
2. **`object-set-attribute` payload is `"attr,value"` (comma separator).** The value itself may contain commas — the action splits on the first comma only. So `points,900,216 892,212 892,220` parses as `attr=points`, `value=900,216 892,212 892,220`.
3. **There is no `delete-attribute` action.** Once you set a junk attribute via `apply_action`, it's there. Use `edit_xml` with `set_attr` to overwrite, or accept the cosmetic clutter (e.g. a stray `points` on a `<line>` is inert).
4. **Selection by CSS comma-list works.** `set_selection target="#a, #b, #c"` selects all three at once — useful for applying a common attribute to multiple elements in one call.
5. **Pattern-match on the selector heuristic.** Targets containing `#.[ >:,*` route through `select-by-selector` (CSS); plain identifiers route through `select-by-id`. Match the form to your intent.
6. **Make elements addressable.** When hand-authoring an SVG you plan to edit via MCP later, **give every meaningful element a stable `id`**. CSS attribute selectors (`polygon[points="..."]`) work too but are brittle.

## Structural XML edits via `edit_xml`

`edit_xml` wraps the entire save → mutate → reload sequence in one call. Handles dirty-doc detection, namespace propagation, and reload sequencing internally.

```
inkscape_live(
  operation="edit_xml",
  target='//svg:defs',                          # xpath; svg:/inkscape:/sodipodi:/xlink:/rdf:/dc: prefixes available
  payload='{"action":"append",
            "xml":"<filter id=\"shadow\">…</filter>"}'
)
```

Actions inside the JSON payload: `append` · `insert_before` · `insert_after` · `replace` · `remove` · `set_attr` (name, value) · `set_text` (text). Fragments inherit SVG/Inkscape namespaces automatically — no need to declare `xmlns=` per fragment.

**Batch several edits into one undo step** — use this whenever a change touches more than one
element (restyling a set, moving a group and its label, re-spacing arrows):

```
inkscape_live(operation="edit_xml", payload='{"ops": [
  {"xpath": "//*[@id=\'arrow-on\']",  "action": "set_attr", "name": "d", "value": "M 53,48 V 74.8"},
  {"xpath": "//*[@id=\'text65\']",    "action": "set_attr", "name": "x", "value": "50"},
  {"xpath": "//*[@id=\'layer1\']",    "action": "append",   "xml": "<g id=\'new\'/>"}
]}')
```

The batch is all-or-nothing: it is rehearsed on a copy first, and if any op fails nothing is
applied and the error names the op (`op 1 (remove): xpath matched no nodes`). Later ops see
earlier ones. An op without `xpath` uses `target`.

Large edits need not be pasted inline: `{"ops_path": "/path/ops.json"}` reads the batch from a
file (`{"ops": [...]}` or a bare list), and any op (or a single edit) may give `"xml_path"`
instead of `"xml"`. Read large XML the same way: `get_document_xml` with `target=<id>` returns
one subtree, and anything over 40 KB comes back as a file path, not inline.

### Selection, saving and tidying in one call each

- `selection` — what the user selected: ids, a short outline of each and one render (`png`).
  When the server launched Inkscape it reads Inkscape's `select-list` output: no undo entry.
  The prompt hook then also lists the selection on every message ("Selected in Inkscape now").
- `measure` — exact sizes at print size (type in pt by family/weight/colour, colours with
  near-duplicates and hue families, stroke widths, alignment near-misses of 0.02-0.5 mm) and
  checks P02/P03/P04/P07 of the style rules. `target` limits it to a subtree; payload
  `{"path": "/abs/file.svg"}` measures a file without Inkscape; `{"rules": {...}}` overrides rules.
- `edit_xml` / `transplant` with `"render": true` return `render`: before|after, side by side,
  framed on the ids the edit names — no separate rasterize and crop.
- `save` — writes the active document to its file even with several windows open (Inkscape
  only saves from a window). The file is remembered from `open_file`; otherwise pass
  `{"path": "/abs/file.svg"}`. The previous file goes to `~/.cache/inkscape_mcp/backups/`.
- `label` — plain-word labels: texts by content, outlined text, arrows by what they connect
  (`arrow: Translation → Protein (k)`, a text beside the middle in brackets), groups by their main
  text. Proximity-based, so `check` first; `relabel: ["arrow"]` renames generic labels.
- `organize` — `{"layers": [{"label": "Panel A", "area": [x0, y0, x1, y1]}, …]}` sorts top-level
  objects into layers by where their centre falls (Inkscape's measured boxes), flattens
  single-child wrapper groups and labels; applied only if the page renders pixel-identical.
- `parked` — objects entirely off the page; `{"delete": true}` removes them.
- `component` kind `math_label` writes maths in the figure convention: `"σ_off = 3"`, `"G_on"`,
  `"N_{0}+1"` → italic variables, upright subscripts, Arial at the style's maths size.

Edits to these modules take effect on the next call (they reload when their files change);
`ping` says when a restart is still needed for other parts of the server.

### Swapping in a re-plotted figure part (`transplant`)

When a plot's data changes, paste the new export anywhere (off the page is fine) and let
`transplant` put it in place of the old, hand-restyled one, as one undo step:

```
inkscape_live(operation="transplant", payload='{"source": "g17", "target": "g64", "check": true}')
inkscape_live(operation="transplant", payload='{"source": "g17", "target": "g64", "preview": true}')
inkscape_live(operation="transplant", payload='{"source": "g17", "target": "g64"}')
```

- **Lines up** the axes (cairo exports: the plot origin every mark shares; matplotlib's own
  SVG backend: the axes frame).
- **Restyles** each mark like its counterpart in the old plot. Counterparts are matched by what
  matplotlib wrote, which hand edits leave alone: presentation attributes (cairo) or the mark's
  role (`axes_1/line2d`, `matplotlib.axis_1/xtick/text`; matplotlib-svg).
- **Drops** marks the old plot no longer has (a white background, outlined legend text you
  replaced). When the old plot has fewer of a kind, only marks sharing a row or column with one
  are kept, so extra ticks survive and stray legend swatches go.
- **Carries over** your own objects inside the old plot (real text, legend boxes, extra lines)
  unchanged. They are data-blind: a label or marker placed at a data position (a band edge, a
  value) keeps the OLD position, so check the report's `copied` list and move or `skip` those.
- **Parks** the old plot where the new one was (`"park": false` deletes it).
- **Outlined text** (cairo glyph runs, listed as `outlined_text`) becomes real text with
  `"glyph_text": {"<run id>": "20"}` and a `"text_style"`.

Mixed hand styles among marks of one kind (say an arrowhead only on infinite values) are
data-specific; the commonest style wins and the report says so.

Avoid most of this at the source: export plots with the figure's style sheet,
`python -m inkscape_mcp.mplstyle figures/style.json --font Calibri > figures/figure.mplstyle`,
then `plt.style.use(...)` and `savefig("plot.svg")`. Its `svg.fonttype: none` keeps text as
real `<text>`, and the colours, weights and fonts already match the figure.

**What `edit_xml` covers that `apply_action` can't:**

- Adding `<filter>`, `<marker>`, `<symbol>`, `<pattern>`, `<clipPath>` to `<defs>`.
- Editing path `d` data when the math is more complex than a single attribute set.
- Restructuring `<defs>` / `<metadata>` / Inkscape namespace blocks.
- Setting attributes on elements that lack ids and don't match a clean CSS selector.

### Leftover artefacts (unused definitions, empty groups)

Documents accumulate definitions nothing uses (copy-paste between files is the usual source; a
figure cropped from a master can carry thousands). Three places handle them, all with the same
rules (svg_cleanup: keep what the drawing reaches through url(#…), href and path-effect chains):

- live document: `inkscape_live(operation="cleanup")` — unused definitions (down to single unused
  glyphs of a pasted plot's font), empty groups and empty texts, as one undo step;
  `payload='{"check": true}'` only reports. Inkscape's own "Clean up document" is no substitute:
  it removes only definitions Inkscape marked collectable, so pasted clip paths, path effects
  and fonts all survive it (on a 1.6 MB figure it saved nothing; this cut it to 0.45 MB);
- files and folders: `python -m inkscape_mcp.svg_cleanup --verify --backup DIR PATH ...`
  (`--verify` renders before/after and refuses to write a file whose rendering would change);
- imports: `import_svg` always prunes what it brings in.

### Components and style tokens

For schematic figures, build parts with `inkscape_live(operation="component")` instead of
hand-writing geometry — they share one set of style tokens, so parts drawn on different days match:

```
inkscape_live(operation="component", payload='{"kind": "rna_strand",
  "params": {"id": "mrna2", "x": 104, "y": 82, "waves": 1.5}, "style": "/abs/figures/style.json"}')
```

`kind` is any registered generator — `{"kind": "catalogue"}` lists them all with their parameter
schemas. Categories: **schematic** (`arrow`, `gene`, `rna_strand`, `protein`, `decay`),
**scatter** (`scatter_even` Poisson-disc, `scatter_grid`, `scatter_clusters`), **organic**
(`blob`, `chain` with over/under crossings, `wobble_line`), **flow** (`flow_lines`: swirl,
vortex, waves, parallel, source), **tiling** (grids, Truchet, radial, isometric). Every part is a
`<g>` with stable ids and `inkscape:label`s; `seed` makes variants reproducible. Coordinates are
document units (as `outline` reports), whichever group the result lands in. Colours accept
`#rrggbb` or a style-token path (`ink`, `species.rna.color`).

Without `into`/`replace`/`apply.to` the fragment is returned — `preview` it, then insert.

**Import an outside SVG** (a downloaded pattern, icon or drawing) into the house style:

```
inkscape_live(operation="component", payload='{"kind": "import_svg", "params": {"id": "bg",
  "path": "/abs/pattern.svg", "x": 0, "y": 0, "width": 60, "recolor": "mono", "color": "ink"},
  "into": "layer1"}')
```

It is sanitised (scripts, event handlers, foreignObject, external links, metadata removed), its
ids are prefixed with the import's id, it is scaled to `width`, and `recolor` maps its colours:
`mono` = tints of one colour by lightness (best for backgrounds), `palette` = nearest style
colour, `none` = untouched. Combine with `apply` to clip it into a shape. Only use files the user
is entitled to use; never store imported files in this repo.

**Style presets.** `style` takes a preset name — `"nature"` (default; muted, Nature-Reviews-like)
or `"reference"` (the user's published palette: sky/cyan, coral, periwinkle) — or a style.json
path; a style.json can build on a preset with `{"extends": "reference", ...overrides}`. Pick one
per figure.

**Motifs from the user's figures:** `hex_tissue` (cells with nuclei, seeded highlighted patch),
`zoom_callout` (magnifier: put the detail inside with apply clip to `<id>-lens`), `panel`,
`label_pill`, `trajectory_fan`, `histogram_fit`, `ridge_plot`, and `gene_box` (the compact
box gene — the default gene; `gene` is the promoter + body version).

**Asset library** (local, never committed): illustrations from the user's own figures in
`reference_figures/` (or $INKSCAPE_MCP_ASSETS), indexed in its `assets.json`:

```
inkscape_live(operation="component", payload='{"kind": "asset",
  "params": {"id": "ill", "name": "microscope", "x": 10, "y": 10, "width": 30, "height": 25},
  "into": "layer1"}')
```

`element` takes one part of a figure by id. Re-index after adding files:
`python -m inkscape_mcp.generators.assets index` (keeps curated entries; measures each asset's
visible box with Inkscape, so clipped content does not shrink it).

**Show the options visually:** `{"kind": "catalogue", "render": true}` (optional `category`)
renders a contact sheet — every generator, three seeds or variants each, captioned with the call
that reproduces the cell — and returns its PNG. Read it, or point the user at it, so choices can
be made by look ("chain seed 2", "truchet seed 3").

**Apply modes** (`apply`):

- `{"mode": "clip", "to": "cell"}` — the output sits inside the shape, clipped by a live link to
  it (reshape the shape and the texture follows); region params default to the shape's box.
- `{"mode": "pattern", "tile": [12, 12], "to": "box"}` — rendered once into a seamless
  `<pattern>` (scatters switch to periodic) and set as `box`'s fill, editable in Fill & Stroke.
  Without `to` the pattern is just added to `<defs>`.

### Arrows

Draw an arrow as a `<path>` stroke with an Inkscape stock marker, exactly as Inkscape writes one
when a marker is picked in Fill & Stroke. A `<line>` has no nodes for the node tool, and a
hand-made `<marker>` is invisible to Fill & Stroke → Markers and keeps its own colour.

```xml
<!-- in <defs>: Inkscape 1.4 stock "Triangle arrow" (share/inkscape/markers/markers.svg) -->
<marker style="overflow:visible" id="Triangle" refX="0" refY="0" orient="auto-start-reverse"
        inkscape:stockid="Triangle arrow" markerWidth="1" markerHeight="1" viewBox="0 0 1 1"
        inkscape:isstock="true" inkscape:collect="always" preserveAspectRatio="xMidYMid">
  <path transform="scale(0.5)" d="M 5.77,0 -2.88,5 V -5 Z"
        style="fill:context-stroke;fill-rule:evenodd;stroke:context-stroke;stroke-width:1pt"/>
</marker>

<!-- the arrow -->
<path id="arrow-a-b" d="M 10,20 H 40"
      style="fill:none;stroke:#333333;stroke-width:0.6;marker-end:url(#Triangle)"/>
```

- `context-stroke` makes the head take each arrow's stroke colour, so one marker serves every colour.
- The head scales with `stroke-width` and is centred on the endpoint: its tip lands about 3×
  stroke-width past it, so end the path that much short of the target.
- **Add the marker after the paths that reference it.** `inkscape:collect="always"` makes
  Inkscape delete an unreferenced marker on the next document update, so a marker appended
  first silently disappears and the arrows render without heads.

> **Undo guarantee:** `edit_xml` routes through an inkex extension under the hood. Inkscape records each call as one undoable command, labeled **"MCP: Edit"** in the undo history. Ctrl+Z reverses the edit cleanly.

## Save semantics

Every MCP-driven mutation auto-saves to disk by default. That covers `apply_action` (mutating verbs), `insert_svg`, `delete_selected`, and `edit_xml`. Read-only verbs (selection changes, zoom/view, dialog/tool opening, `export-*`/`list-*`/`help-*`/`about-*`/`dialog-*` prefixes) don't trigger save.

Kill switch: `INKSCAPE_MCP_AUTO_SAVE=0` for batched-save behavior.

## Untitled docs

For Untitled documents (no saved path) the MCP auto-promotes them by saving to `~/.cache/inkscape_mcp/live-session-<pid>.svg` and re-opening. The original Untitled window stays open — close it manually.

## Operation cheat sheet

| Task | Call |
|---|---|
| Recolor selection | `apply_action(target="object-set-attribute", payload="fill,#cc3333")` |
| Rotate selection | `apply_action(target="transform-rotate", payload="45")` |
| Page rotate | `apply_action(target="page-rotate", payload="1")` — integer steps |
| Switch drawing tool | `apply_action(target="tool-switch", payload="Pen")` |
| Open a dialog | `apply_action(target="dialog-open", payload="DocumentProperties")` |
| Select by id | `set_selection(target="my-id")` |
| Select by CSS | `set_selection(target="#a, .b")` |
| Clear selection | `apply_action(target="select-clear")` |
| Insert geometry | `insert_svg(payload="<svg>…</svg>")` |
| Add filter to defs | `edit_xml(target="//svg:defs", payload='{"action":"append","xml":"<filter …/>"}')` |
| Edit path `d` directly | `edit_xml(target='//svg:path[@id="p1"]', payload='{"action":"set_attr","name":"d","value":"M 0 0 L 10 10"}')` |
| Replace an element | `edit_xml(target='//svg:rect[@id="r1"]', payload='{"action":"replace","xml":"<circle …/>"}')` |
| Move a path node | `path_edit(target="p1", payload='{"op":"move_node","node":[0,1],"to":[35,15]}')` |
| Set a node's in/out handle | `path_edit(target="p1", payload='{"op":"set_handle_in","node":[0,1],"to":[22,12]}')` |
| Insert a node mid-segment (t∈(0,1)) | `path_edit(target="p1", payload='{"op":"insert_node","segment":[0,0],"t":0.5}')` |
| Smooth / cusp a node (updates handles + `sodipodi:nodetypes`) | `path_edit(target="p1", payload='{"op":"smooth","node":[0,1]}')` |
| Subdivide every segment of a subpath (default: all subpaths) | `path_edit(target="p1", payload='{"op":"subdivide"}')` |
| Snapshot to scratch | `save_snapshot()` returns the scratch path |
| Read selection | `get_selection()` → `{count, ids, objects[{id,type,bbox}], text}` |
| Inspect one element (bbox + computed style + ancestors) | `inkscape_live(operation="inspect_element", target="<id>")` — bbox is post-transform document coords; `computed_style` is full CSS-cascade resolution (what the renderer actually uses); `ancestors` includes `is_layer` markers |
| Run arbitrary inkex Python | `inkscape_live(operation="execute_inkex", payload="<python>")` — globals: `svg`, `doc`, `inkex`, `etree`, `json`, `math`, `set_result(x)`. Assign to `result` or define `main()` returning a value. The broadest analytical + manipulation primitive — use for multi-element queries, geometric math, complex mutations not expressible via `apply_action`/`edit_xml`. One `MCP: Execute` undo entry (or `[unchanged]` if read-only). |
| Rasterize the doc / an element / an area to PNG | `inkscape_live(operation="rasterize", target="<id-or-empty>", payload='{"area":"x:y:w:h","dpi":192,"filename":"<abs>"}')` — defaults to a scratch path under `~/.cache/inkscape_mcp/`. Returns `{path, bytes}`. Gives the agent "eyes" for visual diff, occlusion, color sampling. |
| Discover installed extensions (system + user) | `inkscape_extension(operation="list")` — `target` is an optional substring filter |
| Inspect one extension's params | `inkscape_extension(operation="describe", target="<id>")` — returns the INX-declared param schema |
| Run an extension headlessly with custom params | `inkscape_extension(operation="run", target="<id>", params='<json>', input_path="<abs>", output_path?="<abs>")` |
| Apply an extension to the live doc | `inkscape_extension(operation="run_live", target="<id>", params='<json>')` — empty params route via D-Bus `.noprefs`; non-empty params render the extension on an **empty canvas** and append its output (wrapped in `<g id="mcp-ext-…">`) via a single `mcp_edit_xml` call. One `MCP: Edit` undo entry either way. The wrapper id comes back in the response — pass it to follow-up `edit_xml` calls to move/recolor/transform the inserted block. Only suits *generator* extensions (pdflatex, calendar, barcode, qr, gears) — transformer extensions on an empty canvas produce nothing and fail with a hint. |
| **Re-render** an extension in place (preserves CSS hooks / style rules referencing the id) | `inkscape_extension(operation="run_live", target="<ext-id>", params='<json>', wrapper_id="<existing-or-fresh-id>")` — when `wrapper_id` is supplied, any prior element with that id is removed and the new render uses the same id. Response surfaces `replaced_existing: true/false`. `wrapper_id` must match `^[A-Za-z][A-Za-z0-9_.\-]{0,63}$`. |

## Recipes for tools that used to be specialised

These were standalone MCP tools; folded into `apply_action` over the native Inkscape verb after the Phase 8 conservative prune. The agent composes the same outcomes from primitives.

### Clip / mask ops

| Operation | Recipe |
|---|---|
| Set clip on selection | `apply_action(target="object-set-clip")` |
| Release clip | `apply_action(target="object-release-clip")` |
| Inverse clip | `apply_action(target="object-set-inverse-clip")` |
| Set mask | `apply_action(target="object-set-mask")` |
| Release mask | `apply_action(target="object-release-mask")` |
| Inverse mask | `apply_action(target="object-set-inverse-mask")` |

Typical sequence: `set_selection(target="<clip-shape-id>, <target-id>")` then `apply_action(target="object-set-clip")`.

### Layer & selection refinement

| Operation | Recipe |
|---|---|
| Select by id | `set_selection(target="<id>")` |
| Select by CSS selector | `set_selection(target="<selector>")` |
| Select by element type | `apply_action(target="select-by-element", payload="<tag>")` |
| Select by class | `apply_action(target="select-by-class", payload="<class>")` |
| Invert selection | `apply_action(target="select-invert")` |
| Select all | `apply_action(target="select-all")` |
| Hide / unhide selection | `apply_action(target="selection-hide")` / `selection-unhide` |
| Lock / unlock selection | `apply_action(target="selection-lock")` / `selection-unlock` |

### Render generators

| Operation | Recipe |
|---|---|
| Calendar | `inkscape_extension(operation="run_live", target="org.inkscape.render.calendar")` (defaults) — or with params for year/month |
| Barcode | `apply_action(target="org.inkscape.render.barcode.noprefs")` |
| Data matrix | `apply_action(target="org.inkscape.render.data-matrix.noprefs")` |
| Foldable box | `apply_action(target="org.inkscape.render.foldable-box.noprefs")` |
| Cartesian grid | `apply_action(target="org.inkscape.render.grid-cartesian.noprefs")` |
| Isometric grid | `apply_action(target="org.inkscape.render.grid-isometric.noprefs")` |
| 3D polyhedron | `apply_action(target="org.inkscape.render.poly-3d.noprefs")` |
| Rack gear | `apply_action(target="org.inkscape.render.rack-gear.noprefs")` |

For custom params (e.g. calendar year/month): use `inkscape_extension(operation="describe", target="org.inkscape.render.calendar")` to see param names, then `run_live(target=..., params='{"year":2026,...}')`.

## Inkscape rendering pitfalls (MCP-relevant)

These are Inkscape-specific quirks that bite MCP-authored content. The output files are *valid SVG* and render correctly in browsers / image viewers; only Inkscape's editor preview shows the symptoms.

### `feDropShadow` is silently broken in Inkscape 1.4 preview

`<feDropShadow>` is valid SVG 2. Inkscape 1.4.4 (Ubuntu PPA build, 2026-05-06) **silently hides** any element with a filter that contains it. No error, no fallback to the unfiltered source.

**Workaround — use the classic SVG 1.1 drop-shadow recipe instead:**

```xml
<filter id="dropshadow" x="-0.3" y="-0.3" width="1.6" height="1.6" inkscape:auto-region="false">
  <feGaussianBlur in="SourceAlpha" stdDeviation="3"/>
  <feOffset dx="0" dy="4"/>
  <feComponentTransfer><feFuncA type="linear" slope="0.5"/></feComponentTransfer>
  <feMerge>
    <feMergeNode/>
    <feMergeNode in="SourceGraphic"/>
  </feMerge>
</filter>
```

`stdDeviation` = blur radius, `dy` = shadow offset, `slope` = shadow opacity.

### `inkscape:auto-region` silently clamps filter bounds

Inkscape auto-rewrites filter `x`/`y`/`width`/`height` to fit the source bbox precisely — so `x="-0.3" width="1.6"` becomes `x="-0.01" width="1.02"`. The shadow then renders outside the now-tiny filter region and gets clipped.

**Always set `inkscape:auto-region="false"` on filters authored via the MCP.** Required when shadows or blurs need margin around the source.

### Empty-string attribute values delete the attribute (since `PLUGINS_VERSION=8`)

`edit_xml(set_attr, value="")` deletes the named attribute rather than writing `attr=""`. This is the canonical "absent" form and avoids the Inkscape 1.4 invisibility bug where empty-string `filter=""` hid the element. To explicitly set `value="none"` (e.g. for `filter="none"`), pass that literal string. Pre-v8 behaviour wrote the empty string and tripped the renderer — upgrade if you're on v7 or earlier.

### Recoloring extension output via CSS needs to discriminate fg from bg

Generator extensions don't all paint the same way. Two patterns that need different CSS selectors:

**Pdflatex / text-heavy output** — glyphs have explicit `fill="#000000"` on inner groups; some use `stroke` for fraction bars / integral decorations. Set both, but limit stroke to elements that already have one (so unstroked glyphs don't visually thicken):

```xml
<style>
  #my-equation * { fill: #f472b6 !important; }
  #my-equation [stroke] { stroke: #f472b6 !important; }
</style>
```

**QR codes / barcodes / similar 2-color generators** — modules are paths, background is a white `<rect>`. `* { fill }` recolors **both** → solid block. Use element-specific selectors:

```xml
<style>
  /* recolours the QR modules but leaves the white background alone */
  #my-qr path { fill: #cc3333 !important; }
</style>
```

If unsure, `rasterize` the result to see what the recoloring actually did — then narrow the selector based on what looks wrong.

### `//svg:defs` matches every `<defs>` element, including nested ones

`edit_xml(target="//svg:defs", action="append", ...)` appends the fragment to **all** matching defs — and SVG docs frequently have a nested `<defs>` inside any extension-rendered wrapper (pdflatex emits `<defs id="defs1">` containing glyph paths). Inkscape silently renames any duplicate ids in the second copy, leaving a leftover element you didn't intend.

Use a precise target instead:

| Goal | xpath |
|---|---|
| Append to the root doc's defs only | `//*[@id='defs110']` (or whatever its id is — `inspect_defs` reports it) |
| Append to the SVG-root-level defs without relying on id | `/svg:svg/svg:defs` |
| Append to a specific wrapper's defs | `//*[@id='<wrapper-id>']/svg:defs` |

### Multi-window auto-detect (since `PLUGINS_VERSION=8`)

`inkscape_live` now auto-detects the active window. If you pass `window_id=1` (the default) but window 1 isn't open, the tool falls back to the first available window from `bus.list_windows()`. The earlier failure mode — `Object does not exist at path /org/inkscape/Inkscape/window/1` when the user closes a doc — is gone.

You can still pass `window_id` explicitly to target a specific window when multiple are open.

## When the bridge is down

`ping` returns `success: False` if no GUI is running; other live ops return `bridge unavailable`. Fall back to the headless tools — `inkscape_file`, `inkscape_vector`, `inkscape_extension(operation="run", ...)` — which work without the GUI.
