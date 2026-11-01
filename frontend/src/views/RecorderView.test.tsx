import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import * as meetingsApi from "../api/meetings";
import { SUPPORTED_AUDIO_FORMATS } from "../api/types";
import {
  AUDIO_INPUT_DEVICE,
  VIDEO_INPUT_DEVICE,
  enumerateDevicesMock,
  getUserMediaMock,
  makeFakeStream,
  makeMediaError,
  mediaRecorderInstances,
} from "../test/mediaMocks";
import { RecorderView } from "./RecorderView";

vi.mock("../api/meetings");
const mockedCreateMeeting = vi.mocked(meetingsApi.createMeeting);

function renderRecorder() {
  return render(
    <MemoryRouter>
      <RecorderView />
    </MemoryRouter>,
  );
}

/** Drive a recording from idle through start -> stop -> "recorded". */
async function recordOnce(user: ReturnType<typeof userEvent.setup>) {
  enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
  getUserMediaMock.mockResolvedValue(makeFakeStream());
  renderRecorder();
  await user.click(screen.getByRole("button", { name: "开始录音" }));
  await user.click(await screen.findByRole("button", { name: "停止录音" }));
  await screen.findByTestId("recording-ready");
}

describe("RecorderView", () => {
  // Req 2.1
  it("starts capture via getUserMedia + MediaRecorder when a device is present", async () => {
    const user = userEvent.setup();
    enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
    getUserMediaMock.mockResolvedValue(makeFakeStream());

    renderRecorder();
    await user.click(screen.getByRole("button", { name: "开始录音" }));

    await waitFor(() => expect(getUserMediaMock).toHaveBeenCalledTimes(1));
    expect(getUserMediaMock).toHaveBeenCalledWith({ audio: true });

    await waitFor(() => expect(mediaRecorderInstances).toHaveLength(1));
    expect(mediaRecorderInstances[0].start).toHaveBeenCalledTimes(1);

    expect(
      screen.getByRole("button", { name: "停止录音" }),
    ).toBeInTheDocument();
    expect(screen.getByText("录音中…")).toBeInTheDocument();
  });

  // Req 2.3
  it("produces a Supported_Audio_Format blob when stopped", async () => {
    const user = userEvent.setup();
    mockedCreateMeeting.mockResolvedValue({
      id: "m1",
      status: "queued",
      title: "会议",
    });

    await recordOnce(user);

    // The recorded-clip caption surfaces the produced container type.
    expect(screen.getByTestId("recording-ready")).toHaveTextContent(
      "audio/webm",
    );

    // Submitting hands a Blob of a supported type to the API.
    await user.click(screen.getByRole("button", { name: "提交录音" }));
    await waitFor(() => expect(mockedCreateMeeting).toHaveBeenCalledTimes(1));

    const [blobArg, filename] = mockedCreateMeeting.mock.calls[0];
    expect(blobArg).toBeInstanceOf(Blob);
    expect(blobArg.type).toBe("audio/webm");
    const container = blobArg.type.split("/")[1];
    expect(SUPPORTED_AUDIO_FORMATS as readonly string[]).toContain(container);
    expect(filename).toMatch(/\.webm$/);
  });

  // Req 2.5
  it("shows the permission message and does not start when access is denied", async () => {
    const user = userEvent.setup();
    enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
    getUserMediaMock.mockRejectedValue(makeMediaError("NotAllowedError"));

    renderRecorder();
    await user.click(screen.getByRole("button", { name: "开始录音" }));

    expect(
      await screen.findByText("需要麦克风权限才能录音"),
    ).toBeInTheDocument();
    expect(getUserMediaMock).toHaveBeenCalledTimes(1);
    expect(mediaRecorderInstances).toHaveLength(0);
    // recorder not started: the start button is still shown
    expect(screen.getByRole("button", { name: "开始录音" })).toBeInTheDocument();
  });

  // Req 2.6
  it("shows the no-device message and does not start when no audio input exists", async () => {
    const user = userEvent.setup();
    enumerateDevicesMock.mockResolvedValue([VIDEO_INPUT_DEVICE]);
    getUserMediaMock.mockResolvedValue(makeFakeStream());

    renderRecorder();
    await user.click(screen.getByRole("button", { name: "开始录音" }));

    expect(await screen.findByText("未找到音频输入设备")).toBeInTheDocument();
    expect(getUserMediaMock).not.toHaveBeenCalled();
    expect(mediaRecorderInstances).toHaveLength(0);
    expect(screen.getByRole("button", { name: "开始录音" })).toBeInTheDocument();
  });

  // Req 2.7
  it("retains the recorded blob and shows a failure message when submission fails", async () => {
    const user = userEvent.setup();
    mockedCreateMeeting.mockRejectedValue(new Error("网络异常"));

    await recordOnce(user);
    await user.click(screen.getByRole("button", { name: "提交录音" }));

    expect(await screen.findByText(/提交失败/)).toBeInTheDocument();
    expect(mockedCreateMeeting).toHaveBeenCalledTimes(1);

    // blob retained -> recorded caption still visible + a resubmit affordance
    expect(screen.getByTestId("recording-ready")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新提交" }),
    ).toBeInTheDocument();
  });
});
