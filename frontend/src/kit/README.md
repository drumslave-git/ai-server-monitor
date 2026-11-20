# Flight Deck UI kit

The look of AI Server Monitor: an instrument console for one server. Square, flat, dense, lowercase mono
labels, one lime that means "fine". Every screen is built from the components in `index.tsx`.

## The rule

A page never defines its own colours, type, borders or component styles. If a part is missing, add it
to the kit first (a component in `index.tsx`, its rules in `kit.css`, a token in `tokens.css` if it is a
new value), then use it. A page's own `style` is for placement only: flex, grid, gap, width.

## Files

- `tokens.css`: every colour (dark by default, light when the system asks for light), the three typefaces
  and their text styles, spacing steps and sizes.
- `kit.css`: the rules of every component, written against the tokens only.
- `index.tsx`: the components. Each has a doc comment saying what it is for and what it takes.

The design kit with live previews of every component: https://claude.ai/artifact/CHS5xnv6x2P4zWmhEg6pFH

## Building a screen

- `Shell` > `Rail` + `Main` (done once, in `App.tsx`). A page starts with `PageHeader`, then `Panel`s.
  Panels side by side go in a `Split` and take `flex`; fields in columns go in a `FormGrid`.
- Controls: `Button`, `LinkButton`, `Switch`, `Checkbox`, `Field` with `Input` / `Textarea` / `Select` /
  `Segmented`, `Tabs`.
- Status: `Status` (a lamp plus a word; never colour alone), `Lamp`, `Tag`, `Count`, `Counter`.
- Data: `Schedule` and `Lane` (time as cells), `Meter` (an amount out of a maximum shared by the list),
  `Bar` (a span on a shared axis), `Axis`, `Cells`, `Code`, `Command`, `Table`, `KeyValue`.
- Content: `ListItem`, `Finding`, `Message`, `Approval`, `Fold`, `Notice`, `Empty`, `Hero`.

## Tones

`ok` = fine or on, `warn` = warning or waiting, `crit` = critical or failed, `info` = info or running,
`off` = nothing to say. Each has a fill (lamps, cells, tags) and a text colour that stays readable in
both themes. Lime is both the brand and OK: when the server is fine the screen is lime.

The hazard stripe (`Hazard`, `hazard=` on `Panel`, `Approval`) is only for things that wait on the
owner: a pending approval, a critical summary.

## Words

Buttons, nav, field labels and status words are lowercase mono: "run summary now", "approve all".
Page titles are written in normal case (the header uppercases them). Proper names keep their case.
No icons and no emoji: a lamp, a number or a word does the job.
