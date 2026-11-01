import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MeetingListView } from "./MeetingListView";

function mockListFetch(items: unknown[]): void {
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => items,
  })) as unknown as typeof fetch;
}

function renderList() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<MeetingListView />} />
        <Route path="/live" element={<div>LIVE ROUTE</div>} />
        <Route path="/upload" element={<div>UPLOAD ROUTE</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.setItem("mm_token", "tok-123");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("New_Meeting_Entry (Req 1.1-1.4)", () => {
  it("is shown even when the meeting list is empty", async () => {
    mockListFetch([]);
    renderList();
    expect(await screen.findByTestId("new-meeting-entry")).toBeInTheDocument();
  });

  it("offers exactly two options: start live and upload", async () => {
    mockListFetch([]);
    renderList();
    await userEvent.click(await screen.findByTestId("new-meeting-entry"));
    expect(screen.getByTestId("new-meeting-live")).toBeInTheDocument();
    expect(screen.getByTestId("new-meeting-upload")).toBeInTheDocument();
    const menuItems = screen.getAllByRole("menuitem");
    expect(menuItems).toHaveLength(2);
  });

  it("navigates to the live route when 开始实时会议 is chosen", async () => {
    mockListFetch([]);
    renderList();
    await userEvent.click(await screen.findByTestId("new-meeting-entry"));
    await userEvent.click(screen.getByTestId("new-meeting-live"));
    await waitFor(() => expect(screen.getByText("LIVE ROUTE")).toBeInTheDocument());
  });

  it("navigates to the existing upload flow when 上传音频 is chosen", async () => {
    mockListFetch([]);
    renderList();
    await userEvent.click(await screen.findByTestId("new-meeting-entry"));
    await userEvent.click(screen.getByTestId("new-meeting-upload"));
    await waitFor(() => expect(screen.getByText("UPLOAD ROUTE")).toBeInTheDocument());
  });
});
