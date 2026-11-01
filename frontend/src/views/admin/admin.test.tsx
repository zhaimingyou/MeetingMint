import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppLayout } from "../../components/AppLayout";
import { AuthProvider } from "../../auth/AuthContext";
import { AdminGuard } from "../../auth/AdminGuard";
import { ThemeProvider } from "../../theme/ThemeContext";
import { AdminConfigView } from "./AdminConfigView";
import { AdminUsersView } from "./AdminUsersView";
import { AdminJobsView } from "./AdminJobsView";
import { ChangePasswordView } from "../ChangePasswordView";

interface Call {
  url: string;
  method: string;
  body: unknown;
}

let calls: Call[] = [];

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => new Blob([JSON.stringify(body)]),
  } as unknown as Response;
}

type Handler = (url: string, method: string, body: unknown) => Response;

function installFetch(handler: Handler): void {
  calls = [];
  globalThis.fetch = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    let body: unknown = undefined;
    if (init?.body && typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    calls.push({ url, method, body });
    return handler(url, method, body);
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  localStorage.setItem("mm_token", "tok-123");
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

// --- nav gating (Req 1.6) ----------------------------------------------------

function renderLayout() {
  return render(
    <ThemeProvider>
      <AuthProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<AppLayout />}>
              <Route index element={<div>HOME</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </ThemeProvider>,
  );
}

describe("admin nav gating", () => {
  it("shows the admin link for an administrator", async () => {
    localStorage.setItem("mm_is_admin", "1");
    installFetch(() => jsonResponse(200, { username: "admin", is_admin: true }));
    renderLayout();
    expect(await screen.findByTestId("nav-admin")).toBeInTheDocument();
  });

  it("hides the admin link for a non-administrator", async () => {
    localStorage.setItem("mm_is_admin", "0");
    installFetch(() => jsonResponse(200, { username: "bob", is_admin: false }));
    renderLayout();
    await screen.findByText("HOME");
    expect(screen.queryByTestId("nav-admin")).not.toBeInTheDocument();
  });
});

// --- AdminGuard --------------------------------------------------------------

function renderGuard() {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={["/admin"]}>
        <Routes>
          <Route path="/admin" element={<AdminGuard />}>
            <Route index element={<div>ADMIN OK</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe("AdminGuard", () => {
  it("renders an access-denied panel for a non-administrator", async () => {
    localStorage.setItem("mm_is_admin", "0");
    installFetch(() => jsonResponse(200, { username: "bob", is_admin: false }));
    renderGuard();
    expect(await screen.findByTestId("admin-access-denied")).toBeInTheDocument();
  });

  it("renders the guarded content for an administrator", async () => {
    localStorage.setItem("mm_is_admin", "1");
    installFetch(() => jsonResponse(200, { username: "admin", is_admin: true }));
    renderGuard();
    expect(await screen.findByText("ADMIN OK")).toBeInTheDocument();
  });
});

// --- AdminConfigView ---------------------------------------------------------

const CONFIG_BODY = {
  editable: [
    {
      key: "asr_model_name",
      secret: false,
      value: "sensevoice",
      source: "env",
      editable: true,
    },
    {
      key: "llm_api_key",
      secret: true,
      configured: false,
      source: "env",
      editable: true,
    },
  ],
  restart_only: [{ key: "database_url", value: "sqlite://", editable: false }],
};

describe("AdminConfigView", () => {
  it("renders source badges and secret state, and runs the connectivity test", async () => {
    installFetch((url, method) => {
      if (url.endsWith("/api/admin/config") && method === "GET")
        return jsonResponse(200, CONFIG_BODY);
      if (url.endsWith("/api/admin/config/test"))
        return jsonResponse(200, {
          asr: { status: "skip", message: "mock" },
          llm: { status: "skip", message: "mock" },
        });
      return jsonResponse(404, {});
    });
    render(
      <MemoryRouter>
        <AdminConfigView />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("source-asr_model_name")).toHaveTextContent(
      "环境默认",
    );
    expect(screen.getByTestId("secret-state-llm_api_key")).toHaveTextContent(
      "未配置",
    );
    await userEvent.click(screen.getByTestId("config-test"));
    expect(await screen.findByTestId("conn-asr")).toHaveTextContent("skip");
  });

  it("PUTs only the changed fields on save", async () => {
    installFetch((url, method) => {
      if (url.endsWith("/api/admin/config") && method === "GET")
        return jsonResponse(200, CONFIG_BODY);
      if (url.endsWith("/api/admin/config") && method === "PUT")
        return jsonResponse(200, CONFIG_BODY);
      return jsonResponse(404, {});
    });
    render(
      <MemoryRouter>
        <AdminConfigView />
      </MemoryRouter>,
    );
    const input = await screen.findByTestId("input-asr_model_name");
    await userEvent.clear(input);
    await userEvent.type(input, "paraformer");
    await userEvent.click(screen.getByTestId("config-save"));
    await waitFor(() => {
      const put = calls.find(
        (c) => c.method === "PUT" && c.url.endsWith("/api/admin/config"),
      );
      expect(put).toBeTruthy();
      expect(put!.body).toEqual({ asr_model_name: "paraformer" });
    });
  });
});

// --- AdminUsersView ----------------------------------------------------------

describe("AdminUsersView", () => {
  it("toggles a user's disabled flag via PUT", async () => {
    const users = [
      {
        id: "u1",
        username: "bob",
        is_admin: false,
        disabled: false,
        created_at: "2026-06-19T00:00:00+00:00",
      },
    ];
    installFetch((url, method) => {
      if (url.endsWith("/api/admin/users") && method === "GET")
        return jsonResponse(200, users);
      if (url.includes("/api/admin/users/u1") && method === "PUT")
        return jsonResponse(200, { ...users[0], disabled: true });
      return jsonResponse(404, {});
    });
    render(
      <MemoryRouter>
        <AdminUsersView />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByTestId("toggle-disabled-bob"));
    await waitFor(() => {
      const put = calls.find(
        (c) => c.method === "PUT" && c.url.includes("/api/admin/users/u1"),
      );
      expect(put).toBeTruthy();
      expect(put!.body).toEqual({ disabled: true });
    });
  });

  it("creates a user via POST", async () => {
    installFetch((url, method) => {
      if (url.endsWith("/api/admin/users") && method === "GET")
        return jsonResponse(200, []);
      if (url.endsWith("/api/admin/users") && method === "POST")
        return jsonResponse(201, {
          id: "new",
          username: "carol",
          is_admin: false,
          disabled: false,
          created_at: "x",
        });
      return jsonResponse(404, {});
    });
    render(
      <MemoryRouter>
        <AdminUsersView />
      </MemoryRouter>,
    );
    await userEvent.type(await screen.findByTestId("new-username"), "carol");
    await userEvent.type(screen.getByTestId("new-password"), "carol-pass");
    await userEvent.click(screen.getByTestId("create-user"));
    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.url.endsWith("/api/admin/users"),
      );
      expect(post!.body).toMatchObject({
        username: "carol",
        password: "carol-pass",
        is_admin: false,
      });
    });
  });
});

// --- AdminJobsView -----------------------------------------------------------

describe("AdminJobsView", () => {
  it("resubmits a failed job via POST", async () => {
    const page = {
      items: [
        {
          meeting_id: "m1",
          title: "t",
          owner_username: "alice",
          status: "failed",
          created_at: "2026-06-19T00:00:00+00:00",
          error_description: "boom",
        },
      ],
      page: 1,
      page_size: 20,
      total: 1,
    };
    installFetch((url, method) => {
      if (url.includes("/api/admin/jobs/m1/resubmit") && method === "POST")
        return jsonResponse(200, { id: "m1", status: "queued" });
      if (url.includes("/api/admin/jobs") && method === "GET")
        return jsonResponse(200, page);
      return jsonResponse(404, {});
    });
    render(
      <MemoryRouter>
        <AdminJobsView />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByTestId("resubmit-m1"));
    await waitFor(() => {
      expect(
        calls.find(
          (c) =>
            c.method === "POST" && c.url.includes("/api/admin/jobs/m1/resubmit"),
        ),
      ).toBeTruthy();
    });
  });
});

// --- ChangePasswordView ------------------------------------------------------

describe("ChangePasswordView", () => {
  it("POSTs the password change when the confirmation matches", async () => {
    installFetch((url, method) => {
      if (url.endsWith("/api/auth/password") && method === "POST")
        return jsonResponse(200, { changed: true });
      return jsonResponse(404, {});
    });
    render(
      <MemoryRouter>
        <ChangePasswordView />
      </MemoryRouter>,
    );
    await userEvent.type(screen.getByTestId("current-password"), "old-pass");
    await userEvent.type(screen.getByTestId("new-password"), "new-pass-1");
    await userEvent.type(screen.getByTestId("confirm-password"), "new-pass-1");
    await userEvent.click(screen.getByTestId("submit-change-password"));
    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.url.endsWith("/api/auth/password"),
      );
      expect(post!.body).toEqual({
        current_password: "old-pass",
        new_password: "new-pass-1",
      });
    });
  });

  it("does not submit when the passwords do not match", async () => {
    installFetch(() => jsonResponse(404, {}));
    render(
      <MemoryRouter>
        <ChangePasswordView />
      </MemoryRouter>,
    );
    await userEvent.type(screen.getByTestId("current-password"), "old-pass");
    await userEvent.type(screen.getByTestId("new-password"), "aaa");
    await userEvent.type(screen.getByTestId("confirm-password"), "bbb");
    await userEvent.click(screen.getByTestId("submit-change-password"));
    expect(screen.getByRole("alert")).toHaveTextContent("不一致");
    expect(
      calls.find((c) => c.url.endsWith("/api/auth/password")),
    ).toBeUndefined();
  });
});
