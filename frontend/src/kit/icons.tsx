// The kit's icons: Carbon (IBM, @carbon/icons-react). Pages name an icon; they never import the package.
// The package has one module per icon and no side effects, so only the icons named here reach the bundle.
import {
  Add,
  Calendar,
  Catalog,
  Chat,
  Checkbox,
  CheckboxChecked,
  Checkmark,
  ChevronDown,
  CollapseAll,
  Copy,
  Dashboard,
  Edit,
  ExpandAll,
  Filter,
  FolderMoveTo,
  Hourglass,
  Misuse,
  Play,
  RuleTest,
  Save,
  SendFilled,
  Settings,
  StopFilledAlt,
  TaskApproved,
  Tools,
  TrashCan,
  Undo,
} from "@carbon/icons-react";

export const GLYPHS = {
  // pages (rail and bottom bar)
  dashboard: Dashboard,
  jobs: Calendar,
  approvals: TaskApproved,
  chat: Chat,
  mcp: Tools,
  logs: Catalog,
  settings: Settings,
  // actions
  run: Play,
  copy: Copy,
  copied: Checkmark,
  failed: Misuse,
  running: Hourglass,
  send: SendFilled,
  stop: StopFilledAlt,
  add: Add,
  remove: TrashCan,
  test: RuleTest,
  rename: Edit,
  selectAll: CheckboxChecked,
  deselectAll: Checkbox,
  collapseAll: CollapseAll,
  expandAll: ExpandAll,
  filter: Filter,
  save: Save,
  discard: Undo,
  chevron: ChevronDown,
  sort: FolderMoveTo,
};

export type IconName = keyof typeof GLYPHS;

/** The icons that HTML written as a string (Prose: the buttons of a command) may use. The Shell draws
 *  them once as symbols; Prose puts a use of the symbol into each placeholder. */
export const SPRITE: IconName[] = ["run", "copy", "copied", "failed", "running"];
