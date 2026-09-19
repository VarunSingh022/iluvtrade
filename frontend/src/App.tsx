import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import Shell from "./components/Shell";
import { useAuth } from "./lib/auth";
import AuditPage from "./pages/Audit";
import BacktestDetailPage from "./pages/BacktestDetail";
import BacktestsPage from "./pages/Backtests";
import BrokersPage from "./pages/Brokers";
import DashboardPage from "./pages/Dashboard";
import DatasetDetailPage from "./pages/DatasetDetail";
import DatasetsPage from "./pages/Datasets";
import LoginPage from "./pages/Login";
import OrdersPage from "./pages/Orders";
import PortfolioPage from "./pages/Portfolio";
import RedDeskPage from "./pages/RedDesk";
import ResearchPage from "./pages/Research";
import SessionDetailPage from "./pages/SessionDetail";
import SessionsPage from "./pages/Sessions";
import SettingsPage from "./pages/Settings";
import StrategiesPage from "./pages/Strategies";

export default function App() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="auth-shell">
        <span className="spinner" />
      </div>
    );
  }

  if (!user) {
    return (
      <BrowserRouter>
        <Routes>
          <Route path="*" element={<LoginPage />} />
        </Routes>
      </BrowserRouter>
    );
  }

  return (
    <BrowserRouter>
      <Shell>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/datasets" element={<DatasetsPage />} />
          <Route path="/datasets/:versionId" element={<DatasetDetailPage />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/backtests" element={<BacktestsPage />} />
          <Route path="/backtests/:jobId" element={<BacktestDetailPage />} />
          <Route path="/reddesk" element={<RedDeskPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:sessionId" element={<SessionDetailPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/orders" element={<OrdersPage />} />
          <Route path="/brokers" element={<BrokersPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
