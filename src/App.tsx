import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from './context/AuthContext';
import { AuthModalProvider } from './context/AuthModalContext';
import { ProtectedRoute } from './components/auth/ProtectedRoute';
import { AppLayout } from './components/layout/AppLayout';
import StudentDashboard from './pages/StudentDashboard';
import ReportIssue from './pages/ReportIssue';
import ComplaintsPage from './pages/ComplaintsPage';
import ComplaintDetailPage from './pages/ComplaintDetailPage';
import AdminDashboard from './pages/AdminDashboard';
import AnalyticsPage from './pages/AnalyticsPage';
import { ensureSeeded } from './utils/storage';

// Seed localStorage with demo data on first load
ensureSeeded();

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuthModalProvider>
          <AppLayout>
            <Routes>
              <Route
                path="/"
                element={
                  <ProtectedRoute allowedRoles={['student', 'admin']}>
                    <StudentDashboard />
                  </ProtectedRoute>
                }
              />
              <Route
                path="/report"
                element={
                  <ProtectedRoute allowedRoles={['student', 'admin']}>
                    <ReportIssue />
                  </ProtectedRoute>
                }
              />
              <Route
                path="/complaints"
                element={
                  <ProtectedRoute allowedRoles={['student', 'maintenance', 'admin']}>
                    <ComplaintsPage />
                  </ProtectedRoute>
                }
              />
              <Route
                path="/complaints/:id"
                element={
                  <ProtectedRoute allowedRoles={['student', 'maintenance', 'admin']}>
                    <ComplaintDetailPage />
                  </ProtectedRoute>
                }
              />
              <Route
                path="/admin"
                element={
                  <ProtectedRoute allowedRoles={['maintenance', 'admin']}>
                    <AdminDashboard />
                  </ProtectedRoute>
                }
              />
              <Route
                path="/analytics"
                element={
                  <ProtectedRoute allowedRoles={['admin']}>
                    <AnalyticsPage />
                  </ProtectedRoute>
                }
              />
            </Routes>
          </AppLayout>
        </AuthModalProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
