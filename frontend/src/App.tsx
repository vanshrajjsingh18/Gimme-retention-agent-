import { Navigate, Route, Routes } from 'react-router-dom';

import { LoadingState, ToastHost } from './components/ui';
import { useAuth } from './hooks/useAuth';
import AppLayout from './layouts/AppLayout';
import BrandPage from './pages/BrandPage';
import CampaignAnalyticsPage from './pages/CampaignAnalyticsPage';
import CampaignDetailPage from './pages/CampaignDetailPage';
import CampaignsPage from './pages/CampaignsPage';
import ChurnAnalyticsPage from './pages/ChurnAnalyticsPage';
import CohortsPage from './pages/CohortsPage';
import CompliancePage from './pages/CompliancePage';
import CustomerAnalyticsPage from './pages/CustomerAnalyticsPage';
import CustomerDetailPage from './pages/CustomerDetailPage';
import CustomersPage from './pages/CustomersPage';
import DataPage from './pages/DataPage';
import IntegrationsPage from './pages/IntegrationsPage';
import JourneysPage from './pages/JourneysPage';
import LoginPage from './pages/LoginPage';
import MessageStudioPage from './pages/MessageStudioPage';
import NotFoundPage from './pages/NotFoundPage';
import OverviewPage from './pages/OverviewPage';
import SegmentsPage from './pages/SegmentsPage';
import SettingsPage from './pages/SettingsPage';

export default function App() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <LoadingState label="Starting GIMME Retention Engine…" />
      </div>
    );
  }

  return (
    <>
      <Routes>
        {!user ? (
          <>
            <Route path="/login" element={<LoginPage />} />
            <Route path="*" element={<Navigate to="/login" replace />} />
          </>
        ) : (
          <>
            <Route path="/login" element={<Navigate to="/" replace />} />
            <Route element={<AppLayout />}>
              <Route index element={<OverviewPage />} />
              <Route path="analytics/customers" element={<CustomerAnalyticsPage />} />
              <Route path="analytics/churn" element={<ChurnAnalyticsPage />} />
              <Route path="analytics/campaigns" element={<CampaignAnalyticsPage />} />
              <Route path="analytics/cohorts" element={<CohortsPage />} />
              <Route path="customers" element={<CustomersPage />} />
              <Route path="customers/:id" element={<CustomerDetailPage />} />
              <Route path="segments" element={<SegmentsPage />} />
              <Route path="campaigns" element={<CampaignsPage />} />
              <Route path="campaigns/:id" element={<CampaignDetailPage />} />
              <Route path="studio" element={<MessageStudioPage />} />
              <Route path="journeys" element={<JourneysPage />} />
              <Route path="data" element={<DataPage />} />
              <Route path="brand" element={<BrandPage />} />
              <Route path="compliance" element={<CompliancePage />} />
              <Route path="integrations" element={<IntegrationsPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </>
        )}
      </Routes>
      <ToastHost />
    </>
  );
}
