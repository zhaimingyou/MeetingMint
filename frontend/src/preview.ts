/* =========================================================================
   TEMPORARY preview scaffolding
   -------------------------------------------------------------------------
   Lets you view the inner app (list / detail / record / upload) WITHOUT a
   backend or login, purely for design review. It is active ONLY when running
   the Vite dev server (`npm run dev`); it is disabled in tests and in
   production builds. Delete this file and revert the two import sites in
   `auth/AuthGuard.tsx` and `api/client.ts` to restore the real behaviour.
   ========================================================================= */
import type { JobStatus, MeetingDetail, MeetingListItem } from "./api/types";

// Self-contained typing so we don't need to add `vite/client` to tsconfig.
declare global {
  interface ImportMeta {
    readonly env?: { readonly MODE?: string };
  }
}

/** True only under `npm run dev`. Tests run as "test", builds as "production". */
export const PREVIEW_ENABLED = import.meta.env?.MODE === "development";

const minutesAgo = (n: number): string =>
  new Date(Date.now() - n * 60_000).toISOString();

/** Sample list covering every status so all badge styles are visible. */
export const PREVIEW_MEETINGS: MeetingListItem[] = [
  {
    id: "demo-1",
    title: "产品周会 · 2026 Q2 规划",
    status: "completed",
    created_at: minutesAgo(45),
  },
  {
    id: "demo-2",
    title: "客户访谈 — 北区销售复盘",
    status: "transcribing",
    created_at: minutesAgo(180),
  },
  {
    id: "demo-3",
    title: "技术评审：液态玻璃前端改版",
    status: "generating",
    created_at: minutesAgo(320),
  },
  {
    id: "demo-4",
    title: "晨会纪要",
    status: "queued",
    created_at: minutesAgo(700),
  },
  {
    id: "demo-5",
    title: "录音上传失败示例",
    status: "failed",
    created_at: minutesAgo(1500),
  },
];

/** Rich, completed meeting so the detail editor renders fully populated. */
const COMPLETED_DETAIL: MeetingDetail = {
  id: "demo-1",
  title: "产品周会 · 2026 Q2 规划",
  status: "completed",
  created_at: minutesAgo(45),
  transcript: {
    segments: [
      {
        start: 0,
        end: 8,
        text: "今天我们对齐一下第二季度的产品重点，先看上一轮的遗留项。",
        speaker: "主持人",
      },
      {
        start: 8,
        end: 21,
        text: "前端的液态玻璃改版已经完成，整体观感更现代，接下来要确认深色模式的对比度。",
        speaker: "设计",
      },
      {
        start: 21,
        end: 36,
        text: "后端这边转写与纪要生成的吞吐没问题，建议把导出格式再加一个 PDF。",
        speaker: "工程",
      },
      {
        start: 36,
        end: 52,
        text: "好，那我们把 PDF 导出排进本季度，负责人定一下。",
        speaker: "主持人",
      },
    ],
  },
  minutes: {
    summary:
      "本次周会确认了第二季度的三个重点：完成前端液态玻璃改版的深色模式校验、上线 PDF 导出、以及优化转写排队体验。",
    topics: ["前端液态玻璃改版验收", "导出格式扩展（PDF）", "转写排队体验优化"],
    decisions: [
      "批准在本季度上线 PDF 导出",
      "深色模式对比度需通过可访问性校验后再发布",
    ],
    action_items: [
      { description: "补充 PDF 导出的后端接口与模板", owner: "工程 - 李雷" },
      { description: "完成深色模式对比度校验报告", owner: "设计 - 韩梅梅" },
      { description: "整理转写排队的埋点指标", owner: "数据 - 王芳" },
    ],
  },
};

const PREVIEW_DETAILS: Record<string, MeetingDetail> = {
  "demo-1": COMPLETED_DETAIL,
};

function statusOnlyDetail(item: MeetingListItem): MeetingDetail {
  return { ...item, transcript: null, minutes: null };
}

function getPreviewDetail(id: string): MeetingDetail {
  if (PREVIEW_DETAILS[id]) return PREVIEW_DETAILS[id];
  const item = PREVIEW_MEETINGS.find((m) => m.id === id);
  if (item) return statusOnlyDetail(item);
  // Unknown id (e.g. created during preview) → show the rich completed demo.
  return COMPLETED_DETAIL;
}

function statusFor(id: string): JobStatus {
  return PREVIEW_MEETINGS.find((m) => m.id === id)?.status ?? "completed";
}

/**
 * Returns mock data for a known meeting request, or `undefined` to let the
 * real network request proceed. Only consulted when PREVIEW_ENABLED is true.
 */
export function getPreviewResponse(
  path: string,
  method: string,
): unknown | undefined {
  if (method === "GET" && path === "/meetings") {
    return PREVIEW_MEETINGS;
  }

  const statusMatch = /^\/meetings\/([^/]+)\/status$/.exec(path);
  if (method === "GET" && statusMatch) {
    return { status: statusFor(decodeURIComponent(statusMatch[1])) };
  }

  const detailMatch = /^\/meetings\/([^/]+)$/.exec(path);
  if (method === "GET" && detailMatch) {
    return getPreviewDetail(decodeURIComponent(detailMatch[1]));
  }

  if (method === "PUT" && /^\/meetings\/[^/]+\/minutes$/.test(path)) {
    return { id: "preview", saved: true };
  }
  if (method === "POST" && /^\/meetings\/[^/]+\/resubmit$/.test(path)) {
    return { id: "preview", status: "queued" as JobStatus };
  }
  if (method === "DELETE" && /^\/meetings\/[^/]+$/.test(path)) {
    return { id: "preview", deleted: true };
  }
  if (method === "POST" && path === "/meetings") {
    // Created recordings/uploads route to the rich completed demo.
    return { id: "demo-1", status: "completed" as JobStatus, title: "新会议（预览）" };
  }

  return undefined;
}

/** Mock markdown export so the "导出" button works without a backend. */
export function getPreviewDownload(): { blob: Blob; filename: string } {
  const md =
    "# 会议纪要（预览导出）\n\n" +
    "这是预览模式下生成的示例 Markdown 文件，仅用于查看前端效果。\n";
  return {
    blob: new Blob([md], { type: "text/markdown;charset=utf-8" }),
    filename: "meeting-preview.md",
  };
}
