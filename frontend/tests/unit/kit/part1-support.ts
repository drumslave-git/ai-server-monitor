// Browser behaviour that happy-dom lacks and that the first half of the kit relies on. Installed per test file
// with the functions below (nothing here touches tests/setup.ts).
import { vi } from "vitest";

/** happy-dom's <dialog> only toggles `open`. This adds what a browser does around it, as far as the kit needs:
 *  showModal() is recorded (`modalOpened`), a submit button of a <form method="dialog"> closes its dialog with
 *  the button's value as returnValue, and Escape on the page cancels and closes the open modal dialog. */
export function installDialogBehaviour() {
  const proto = HTMLDialogElement.prototype;
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
    this.dataset.modal = "true";
  });
  vi.spyOn(proto, "showModal").mockImplementation(showModal);

  const onClick = (e: MouseEvent) => {
    const button = (e.target as Element).closest("button");
    const form = button?.closest("form");
    const dialog = button?.closest("dialog");
    if (!button || !form || !dialog || form.getAttribute("method") !== "dialog") return;
    if (button.type !== "submit") return;
    e.preventDefault();
    dialog.close(button.value);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== "Escape") return;
    const dialogs = [...document.querySelectorAll("dialog[open]")];
    const top = dialogs[dialogs.length - 1] as HTMLDialogElement | undefined;
    if (!top) return;
    const cancel = new Event("cancel", { cancelable: true });
    top.dispatchEvent(cancel);
    if (!cancel.defaultPrevented) top.close();
  };
  document.addEventListener("click", onClick);
  document.addEventListener("keydown", onKey);
  return {
    showModal,
    remove() {
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
    },
  };
}
