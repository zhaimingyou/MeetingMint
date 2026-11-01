import { Navigate, Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { AuthGuard } from "./auth/AuthGuard";
import { AdminGuard } from "./auth/AdminGuard";
import { SettingsView } from "./views/SettingsView";
import { LiveMeetingView } from "./views/LiveMeetingView";
import { LoginView } from "./views/LoginView";
import { MeetingDetailView } from "./views/MeetingDetailView";
import { MeetingListView } from "./views/MeetingListView";
import { RecorderView } from "./views/RecorderView";
import { RegisterView } from "./views/RegisterView";
import { UploadView } from "./views/UploadView";
import { AdminLayout } from "./views/admin/AdminLayout";
import { AdminAuditView } from "./views/admin/AdminAuditView";
import { AdminConfigView } from "./views/admin/AdminConfigView";
import { AdminJobsView } from "./views/admin/AdminJobsView";
import { AdminUsersView } from "./views/admin/AdminUsersView";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginView />} />
      <Route path="/register" element={<RegisterView />} />
      <Route element={<AuthGuard />}>
        <Route element={<AppLayout />}>
          <Route index element={<MeetingListView />} />
          <Route path="record" element={<RecorderView />} />
          <Route path="live" element={<LiveMeetingView />} />
          <Route path="upload" element={<UploadView />} />
          <Route path="meetings/:id" element={<MeetingDetailView />} />
          <Route path="settings" element={<SettingsView />} />
          <Route path="change-password" element={<Navigate to="/settings" replace />} />
          <Route path="admin" element={<AdminGuard />}>
            <Route element={<AdminLayout />}>
              <Route index element={<Navigate to="config" replace />} />
              <Route path="config" element={<AdminConfigView />} />
              <Route path="users" element={<AdminUsersView />} />
              <Route path="jobs" element={<AdminJobsView />} />
              <Route path="audit" element={<AdminAuditView />} />
            </Route>
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
