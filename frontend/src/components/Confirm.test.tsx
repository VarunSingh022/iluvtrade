import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmButton } from "./Confirm";

describe("ConfirmButton", () => {
  it("does not act on the first click", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton
        label="Disconnect"
        confirmLabel="Really disconnect"
        consequence="The token is deleted."
        onConfirm={onConfirm}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("states the consequence before the second click", async () => {
    render(
      <ConfirmButton
        label="Disconnect"
        confirmLabel="Really disconnect"
        consequence="The stored access token is deleted and cannot be recovered."
        onConfirm={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(
      screen.getByText("The stored access token is deleted and cannot be recovered."),
    ).toBeInTheDocument();
  });

  it("acts only on the second, explicit click", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton
        label="Reject"
        confirmLabel="Reject this version"
        consequence="It can never be used."
        onConfirm={onConfirm}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await userEvent.click(screen.getByRole("button", { name: "Reject this version" }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("can be cancelled without acting", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton
        label="Reject"
        confirmLabel="Confirm"
        consequence="Irreversible."
        onConfirm={onConfirm}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });

  it("does nothing at all when disabled", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton
        label="Disconnect"
        confirmLabel="Confirm"
        consequence="Irreversible."
        onConfirm={onConfirm}
        disabled
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
