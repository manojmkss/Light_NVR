import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { SettingsProvider } from "./context/SettingsContext";
import { CamerasPage } from "./pages/CamerasPage";
import { DashboardPage } from "./pages/DashboardPage";
import { KioskPage } from "./pages/KioskPage";
import { LiveViewPage } from "./pages/LiveViewPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaybackPage } from "./pages/PlaybackPage";
import { RecordingsPage } from "./pages/RecordingsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SetupPage } from "./pages/SetupPage";

function AppRoutes() {
  const { user, setupWizardActive } = useAuth();

  if (setupWizardActive) {
    return <SetupPage />;
  }

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/kiosk/:token" element={<KioskPage />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <Layout>
              <DashboardPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/live"
        element={
          <ProtectedRoute>
            <Layout>
              <LiveViewPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/playback"
        element={
          <ProtectedRoute>
            <Layout>
              <PlaybackPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/cameras"
        element={
          <ProtectedRoute>
            <Layout>
              <CamerasPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/recordings"
        element={
          <ProtectedRoute>
            <Layout>
              <RecordingsPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedRoute>
            <Layout>
              <SettingsPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route path="*" element={user ? <NotFoundPage /> : <Navigate to="/login" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <SettingsProvider>
        <AppRoutes />
      </SettingsProvider>
    </AuthProvider>
  );
}
