import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import * as meetingsApi from "../api/meetings";
import type { MeetingDetail } from "../api/types";
import { MeetingDetailView } from "./MeetingDetailView";

vi.mock("../api/meetings");
const mockedGetMeeting = vi.mocked(meetingsApi.getMeeting);
const mockedDeleteMeeting = vi.mocked(meetingsApi.deleteMeeting);

function renderDetail(id: string) {
  return render(
    <MemoryRouter initialEntries={[`/meetings/${id}`]}>
      <Routes>
        <Route path="/meetings/:id" element={<MeetingDetailView />} />
        <Route path="/" element={<div>列表页</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const completedMeeting: MeetingDetail = {
  id: "m1",
  title: "季度评审会",
  status: "completed",
  created_at: "2024-01-01T08:00:00Z",
  transcript: {
    segments: [
      { start: 0, end: 5, text: "你好，今天我们讨论季度目标", speaker: "张三" },
      { start: 5, end: 12, text: "同意，我们需要确定预算", speaker: "李四" },
    ],
  },
  minutes: {
    summary: "本次会议确定了季度目标与预算安排",
    topics: ["季度目标", "预算安排"],
    decisions: ["批准市场预算增加百分之十"],
    action_items: [{ description: "制定详细预算表", owner: "王五" }],
  },
};

describe("MeetingDetailView", () => {
  // Req 9.1
  it("renders transcript + editable minutes for a completed meeting", async () => {
    mockedGetMeeting.mockResolvedValue(completedMeeting);
    renderDetail("m1");

    // transcript section with segment text + speaker labels
    expect(await screen.findByTestId("transcript-section")).toBeInTheDocument();
    expect(
      screen.getByText("你好，今天我们讨论季度目标"),
    ).toBeInTheDocument();
    expect(screen.getByText("张三")).toBeInTheDocument();
    expect(screen.getByText("李四")).toBeInTheDocument();

    // minutes section: summary, topics, decisions, action items + owners
    expect(screen.getByTestId("minutes-section")).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("本次会议确定了季度目标与预算安排"),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("季度目标")).toBeInTheDocument();
    expect(screen.getByDisplayValue("预算安排")).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("批准市场预算增加百分之十"),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("制定详细预算表")).toBeInTheDocument();
    expect(screen.getByDisplayValue("王五")).toBeInTheDocument();

    // edit/export/delete affordances are present
    expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出 MD" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
  });

  // Req 9.5
  it("renders only the status and hides minutes for a non-completed meeting", async () => {
    const processing: MeetingDetail = {
      id: "m2",
      title: "周会",
      status: "transcribing",
      created_at: "2024-01-02T09:00:00Z",
      transcript: null,
      minutes: null,
    };
    mockedGetMeeting.mockResolvedValue(processing);
    renderDetail("m2");

    expect(await screen.findByTestId("status-section")).toBeInTheDocument();
    // Chinese status label is shown (badge + status line).
    expect(screen.getAllByText("转写中").length).toBeGreaterThanOrEqual(1);

    // minutes + transcript are NOT rendered
    expect(screen.queryByTestId("minutes-section")).toBeNull();
    expect(screen.queryByTestId("transcript-section")).toBeNull();
    expect(screen.queryByText("会议纪要")).toBeNull();
  });

  // Req 9.5 (failed): status + error + resubmit, still no minutes
  it("shows a resubmit action and no minutes for a failed meeting", async () => {
    const failed: MeetingDetail = {
      id: "m3",
      title: "失败会议",
      status: "failed",
      created_at: "2024-01-03T09:00:00Z",
      transcript: null,
      minutes: null,
    };
    mockedGetMeeting.mockResolvedValue(failed);
    renderDetail("m3");

    expect(await screen.findByTestId("status-section")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新提交" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("minutes-section")).toBeNull();
  });

  it("allows deleting a failed meeting", async () => {
    const failed: MeetingDetail = {
      id: "m3",
      title: "失败会议",
      status: "failed",
      created_at: "2024-01-03T09:00:00Z",
      transcript: null,
      minutes: null,
    };
    mockedGetMeeting.mockResolvedValue(failed);
    mockedDeleteMeeting.mockResolvedValue({ id: "m3", deleted: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderDetail("m3");

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "删除" }));

    expect(mockedDeleteMeeting).toHaveBeenCalledWith("m3");
    expect(await screen.findByText("列表页")).toBeInTheDocument();
  });
});
