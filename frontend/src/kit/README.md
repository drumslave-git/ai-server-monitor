# Flight Deck UI kit

The look of AI Server Monitor: an instrument console for one server. Square, flat, dense, lowercase mono
labels, one lime that means "fine". Every screen is built from the components in `index.tsx`.

## The rule

A page never defines its own colours, type, borders or component styles, and writes no `className` and
no `style`. If a part is missing, add it to the kit first (a component in `index.tsx`, its rules in
`kit.css`, a token in `tokens.css` if it is a new value), then use it. Where a block sits in its parent
is said with the placement props the kit's blocks take: `flex`, `width`, `span`, `toEnd`.

Both of these must print nothing:

    grep -rn "className=" frontend/src --include=*.tsx | grep -v src/kit/
    grep -rn "CSSProperties\|style=" frontend/src --include=*.tsx | grep -v src/kit/

## Files

- `tokens.css`: every colour (dark by default, light when the system asks for light), the three typefaces
  and their text styles, spacing steps and sizes.
- `kit.css`: the rules of every component, written against the tokens only.
- `index.tsx`: the components. Each has a doc comment saying what it is for and what it takes.

The design kit with live previews of every component: https://claude.ai/artifact/CHS5xnv6x2P4zWmhEg6pFH

## Building a screen

- `Shell` > `Rail` + `Main` (done once, in `App.tsx`). A page starts with `PageHeader`, then `Panel`s.
- Layout: `Split` (blocks side by side that wrap; `stretch`), `Column` (blocks under each other),
  `Inline` (small things on one wrapping line: `gap`, `align`, `between`, `nowrap`), `FormGrid` (fields
  in columns), `Toolbar` (filters above a list), `Actions` (buttons side by side), `Scroll` (a height
  limit), `SectionHead` (a section's title, note and buttons), `Labeled` (a small heading over what it
  names). `gap` is a step of the spacing scale (1 = 4px … 6 = 24px). `Split`, `Column` and `Row` given
  `onSubmit` are a `<form>`.
- Inside a `Panel`: ruled lines (`Row`, `Table`, `Fold`, `Finding`, `List`, `Empty`, `TestLine`) and
  padded parts (`Pad`; `pad` on the panel when the whole body is one), in any order. `Row` takes
  `column`, `sticky` (the composer of a thread) and `current`.
- Controls: `Button` (`primary`, `warn` for what changes the server, `danger` for what deletes,
  `link`), `LinkButton`, `Switch`, `Checkbox` (`some` = partly selected; `CheckboxBlank` keeps its
  room), `Field` with `Input` / `Textarea` / `Select` / `Segmented`, `Joined` (an input with its button
  attached), `FieldValue` (a value that cannot be changed here), `Chip` (a chosen thing with
  "remove"), `Tabs`, `Dialog`, `Confirm` (a yes/no question before something that cannot be undone;
  `variant` colours the yes button), `Picker` (a dialog that lists groups of things to choose one
  from, with a filter), `SortableList` (rows put in order by dragging the handle, by keyboard on the
  handle — space, arrows, space — or by the up/down buttons; `onRemove` adds "remove").
- Status: `Status` (a lamp plus a word; never colour alone), `Lamp`, `Tag`, `Count`, `Counter`.
- Data: `Schedule` and `Lane` (time as cells), `Meter` (an amount out of a maximum shared by the list;
  `fluid`), `Bar` (a span on a shared axis: queued, first part, rest), `Axis`, `Cells`, `Code`
  (`maxHeight` scrolls inside), `Command`, `KeyValue`, and `Table` with `Tr` and `Td`: `head` names the
  columns, `stack` turns each row into a block of labelled cells where the table is narrower than
  640px (otherwise it scrolls sideways inside itself).
- Content: `List` and `ListItem` (a link, or with `onClick` alone a button that says `expanded`;
  `action` is a control of its own beside the row), `Finding`, `Message` (`typed`, `says`, `running`,
  `wide`), `Approval` (`decided`, `actions`), `Fold`, `Notice` (`tone`), `Empty`, `Hero` (`compact`: one of several side by side, e.g. a card per summary), `Prose`
  (Markdown already made safe HTML), `Clamp` (three lines, then "show more").
- Words: `Text` (`kind="data"` or `"label"`, `dim`, `tone`, `wrap`, `nowrap`, `keep`, `block`), `Meta`,
  `Heading`, `TitleLink`, `ErrorText`.

A chosen thing looks the same everywhere: the raised ground in a one-pixel lime frame (a selected
`ListItem`, a `Row current`, a `Tr selected`).

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
