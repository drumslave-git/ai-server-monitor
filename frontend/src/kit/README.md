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

- `Shell` > `Rail` + `Main` (done once, in `App.tsx`; `Rail` takes `meters`, shown above the automation box
  beside the page and as a row of the top bar on a phone; on a phone its pages are the `BottomBar`, fixed
  at the foot, the current one marked with `aria-current` and its own look). `Main` `resetOn` scrolls back
  to the top when the value changes (the route). A page starts with `PageHeader`, then `Panel`s or `Panes`.
- The shell fills the window and the page scrolls inside `Main`, never the window. A page with a list
  beside its content uses `Panes` > `Pane side` (the list, `--pane-side` wide; `side="wide"`, `--pane-side-wide`, for rows that carry more) + `Pane`: each scrolls on
  its own. `Pane` takes `label`, `bare` (no panel frame), `narrow` (what stands in it keeps `--form-max`, the form's reading width, so no border runs
  past the fields in a wide window), `foot` (stays at the pane's foot: a composer, a
  save bar), `resetOn` (back to the top when it changes), `endOn` (to the end when it changes: a thread)
  and `onSubmit` (the pane is a `<form>`). On a phone the panes stack and the window scrolls; a `foot`
  sticks above the bottom bar.
- `PageHeader` sticks to the top of its scroller and turns compact while the page scrolls (the title keeps its
  size; the note and meta go and the padding tightens; fields keep their rows and wrap inside the head) (a scroll-driven animation; where the browser has none it simply stays full size).
  `tools` is a row under the title that stays with it (filters); `fold` names it, and on a phone it folds
  behind an icon of that name.
- Layout: `Split` (blocks side by side that wrap; `stretch`), `Column` (blocks under each other),
  `Inline` (small things on one wrapping line: `gap`, `align`, `between`, `nowrap`), `FormGrid` (fields
  in columns), `FormGroups` with `FormGroup` (`title`, `notes`: a form in named groups, each a titled flow of fields that share its rows, with no holes, at most 60rem wide, stacking to one column when its pane is narrow; `FormWide` puts a field on a row of its own), `ListPicker` (a list that is one choice, such as the sections of Settings: on a phone one select that names the current item instead of the list), `Toolbar` (filters above a list; `side` is the part over a side pane, exactly as wide), `Actions` (buttons side by side), `Scroll` (a height
  limit), `SectionHead` (a section's title, note and buttons), `Labeled` (a small heading over what it
  names). `gap` is a step of the spacing scale (1 = 4px … 6 = 24px). `Split`, `Column` and `Row` given
  `onSubmit` are a `<form>`.
- Inside a `Panel`: ruled lines (`Row`, `Table`, `Fold`, `Finding`, `List`, `Empty`, `TestLine`) and
  padded parts (`Pad`; `pad` on the panel when the whole body is one), in any order. `Row` takes
  `column`, `sticky` (the composer of a thread) and `current`.
- Controls: `Button` (`primary`, `warn` for what changes the server, `danger` for what deletes,
  `link`; `icon` puts an icon before the word), `LinkButton` (also `icon`), `IconButton` (an icon alone:
  `label` is its accessible name and its hint, `hint` a longer one, `count` a number beside the icon,
  `small` for repeated actions in rows, `variant`, `type`), `IconLink` (the same as a link: it goes
  somewhere or downloads a file), `Switch`, `Checkbox` (`some` = partly selected; `CheckboxBlank` keeps its
  room), `Radio` (one of several; the group shares `name`), `Field` with `Input` / `Textarea` / `Select` / `Segmented`, `Joined` (an input with its button
  attached), `FieldValue` (a value that cannot be changed here), `Chip` (a chosen thing with
  "remove"), `Tabs`, `Dialog`, `Confirm` (a yes/no question before something that cannot be undone;
  `variant` colours the yes button), `Picker` (a dialog that lists groups of things to choose one
  from, with a filter), `SortableList` (rows put in order by dragging the handle, by keyboard on the
  handle — space, arrows, space — or by the up/down icon buttons; `onRemove` adds a take-out icon).
- Status: `Status` (a lamp plus a word; never colour alone), `Lamp`, `Tag`, `Count`, `Counter`.
- Data: `Schedule` and `Lane` (time as cells), `Meter` (an amount out of a maximum shared by the list;
  `fluid`), `Gauge` (one reading now with its label and value, for `Rail` `meters`), `Bar` (a span on a
  shared axis: queued, first part, rest), `Axis`, `Cells`, `Code`
  (`maxHeight` scrolls inside), `Command` (`action`: its icon buttons at the right end of its first line; the code wraps before them), `KeyValue`, and `Table` with `Tr` and `Td`: `head` names the
  columns, `stack` turns each row into a block of labelled cells where the table is narrower than
  640px (otherwise it scrolls sideways inside itself), `stickyHead` keeps the column names at the top of
  the pane that scrolls (use it with `stack`).
- Content: `List` and `ListItem` (a link, or with `onClick` alone a button that says `expanded`;
  `icon` puts an icon before the title, `action` is a control of its own beside the row), `Finding`, `Message` (`typed`, `says`, `running`,
  `wide`), `Approval` (`decided`, `actions`), `Card` in `Cards` (one form among repeated ones: closed
  by default showing `title`, `status`, `meta` and `aside` (its buttons); `reveal` opens it, for one just
  added or one with an error; `focus` opens it and scrolls it into view, the card a link points at: on a phone,
  where the page head stays at the top of the page, it comes to rest below the head, as tall as it is compact
  with one row of title and buttons; `below` shows under it, open or closed), `Composer` (the message box with
  its send or stop button at the right end of a toolbar row under the text; grows to about twelve lines, then scrolls with a fade and shows an expand/collapse icon; `leading` for buttons on the left of that row), `Fold`, `Notice` (`tone`), `Empty`, `Hero` (`compact`: one of several side by side, e.g. a card per summary), `Prose`
  (Markdown already made safe HTML), `Clamp` (three lines, then "show more").
- Markup made outside React (Markdown in `Prose`) uses `commandHtml`, `iconButtonHtml` and
  `setIconButton` to write the same command block and icon buttons; `Prose` fills in their icons and hints.
- Words: `Text` (`kind="data"` or `"label"`, `dim`, `tone`, `wrap`, `nowrap`, `keep`, `block`), `Meta`,
  `Heading`, `TitleLink`, `ErrorText`, `EntityLink`.

`EntityLink` is how one thing names another: an approval's source chat, a run's job, a call's chat, a tool's
server. It is a link with the icon of what it names (`kind`: chat, job, summary, run, approval, call, server,
model, worker) and opens that thing where it already lives (`href`). Every place that names another entity uses
it, so such links look and act the same; a name that is not a thing of its own (the owner, a timeout) stays
plain text. A link is one line: a name wider than its room is cut and gets the tooltip of cut text (the name of a job or summary has any length and wraps instead, its icon staying at the first line); `mono` is for ids. `EntityList` puts several in one line of text ("a, b, c") and keeps each comma with its link, so a line never starts with one. `EntityTail` is a link with text after it ("server · tool") as one unit: the line never breaks between them, and when the unit is too wide the link is cut and the text stays whole.

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
## Icons

Icons are Carbon (`@carbon/icons-react`), and only the kit imports them: `icons.tsx` names each one the
kit uses, and pages pass a name (`icon="save"`) to `Icon`, `Button`, `LinkButton`, `IconButton` or `IconLink`. A new
icon is a new name in `icons.tsx`. An action that repeats (in a row, a toolbar, a list) is an icon alone,
with its word as the accessible name and the hint shown on hover or focus. The one main action of a page
is an icon and a word. The pages in the rail and the bottom bar each have an icon, and so does each section of
the settings page (`general`, `model`, `worker`, `storage`, `outputs`; chat, jobs, logs, approvals and mcp
use the icon of the page of that name). No emoji, and no letters or numbers standing in for icons.

## Type

Every `font-size` is in `rem`, never `px`, so one number scales all text: the root font size, 16px in
`tokens.css` and then set from the server setting `general.root_font_size` (`setRootFontSize(px)`, called by
the app once it has loaded the settings and by the settings page after a save). The display styles (the
logo, page titles, counters) are their size in px ÷ 16, so at 16px they are what they always were; the
other styles are their old px size ÷ 14, so body text is 16px. Spacing, widths and paddings stay in `px`.

## Text that is cut

A text cut by its width (`text-overflow: ellipsis`) shows its whole text in a tooltip when the mouse rests
on it for half a second or the keyboard focus lands on it (Escape, scrolling and clicking close it).
`TrimTips`, mounted once by `Shell`, does this for the whole page: it asks the browser whether the element
under the pointer is really cut, so a part only has to use the ellipsis (`ListItem` titles, `TitleLink
truncate`, `EntityLink`, the title of a `Fold`, the meta of a `Card`) and a text added later is covered the moment it is
cut. A touch screen has no pointer to rest, so it gets no tooltip. A field is covered too: an empty `Input`'s
placeholder and a `Select`'s chosen option are measured with the field's own font (the browser clips them without
an ellipsis the page can ask about; a select's arrow is allowed 24px). `Clamp` has its own "show more".
