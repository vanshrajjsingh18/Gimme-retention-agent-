import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';

import { useQuery } from '../hooks/useApi';
import { useAuth } from '../hooks/useAuth';
import type { SystemStatus } from '../types';
import { Badge } from '../components/ui';

const NAV_SECTIONS: { heading: string; items: { to: string; label: string; icon: string }[] }[] = [
  {
    heading: 'Analyse',
    items: [
      { to: '/', label: 'Overview', icon: 'M3 13h4v8H3v-8zm7-9h4v17h-4V4zm7 5h4v12h-4V9z' },
      {
        to: '/analytics/customers',
        label: 'Customer analytics',
        icon: 'M12 12a4 4 0 100-8 4 4 0 000 8zm-8 9a8 8 0 1116 0H4z',
      },
      {
        to: '/analytics/churn',
        label: 'Churn analytics',
        icon: 'M12 2l9 16H3L12 2zm0 6v5m0 3v.5',
      },
      {
        to: '/analytics/campaigns',
        label: 'Campaign analytics',
        icon: 'M3 10v4h4l5 5V5L7 10H3zm13.5 2a4.5 4.5 0 00-2.5-4v8a4.5 4.5 0 002.5-4z',
      },
      {
        to: '/analytics/cohorts',
        label: 'Cohorts',
        icon: 'M4 4h16v4H4V4zm0 6h16v4H4v-4zm0 6h16v4H4v-4z',
      },
    ],
  },
  {
    heading: 'Act',
    items: [
      {
        to: '/customers',
        label: 'Customers',
        icon: 'M12 12a4 4 0 100-8 4 4 0 000 8zm-8 9a8 8 0 1116 0H4z',
      },
      {
        to: '/segments',
        label: 'Segments',
        icon: 'M3 5h18M6 12h12M10 19h4',
      },
      {
        to: '/campaigns',
        label: 'Campaigns',
        icon: 'M3 10v4h4l5 5V5L7 10H3z',
      },
      {
        to: '/automations',
        label: 'Automations',
        icon: 'M12 2v4m0 12v4M2 12h4m12 0h4M5.6 5.6l2.8 2.8m7.2 7.2l2.8 2.8m0-12.8l-2.8 2.8m-7.2 7.2l-2.8 2.8',
      },
      {
        to: '/studio',
        label: 'Message Studio',
        icon: 'M4 4h16v12H5.17L4 17.17V4zm4 3h8v2H8V7zm0 4h5v2H8v-2z',
      },
      {
        to: '/journeys',
        label: 'Journeys',
        icon: 'M4 5h6v6H4V5zm10 8h6v6h-6v-6zM10 8h4v0m-4 0h2a2 2 0 012 2v4',
      },
    ],
  },
  {
    heading: 'Configure',
    items: [
      { to: '/data', label: 'Data & imports', icon: 'M4 6h16v4H4V6zm0 8h16v4H4v-4z' },
      { to: '/brand', label: 'Brand', icon: 'M12 2l3 6 6 1-4.5 4.5L18 20l-6-3-6 3 1.5-6.5L3 9l6-1 3-6z' },
      {
        to: '/compliance',
        label: 'Compliance',
        icon: 'M12 2l8 4v6c0 5-3.4 8.7-8 10-4.6-1.3-8-5-8-10V6l8-4z',
      },
      {
        to: '/integrations',
        label: 'Integrations',
        icon: 'M10 3v4H6v4H2v6h6v-4h4V9h4V3h-6z',
      },
      { to: '/settings', label: 'Settings', icon: 'M12 8a4 4 0 100 8 4 4 0 000-8z' },
    ],
  },
];

export default function AppLayout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { data: status } = useQuery<SystemStatus>('/api/v1/system/status');

  // Close the mobile drawer whenever the route changes.
  useEffect(() => setSidebarOpen(false), [location.pathname]);

  return (
    <div className="flex min-h-screen bg-slate-50">
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          className="fixed inset-0 z-30 bg-slate-900/40 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white transition-transform lg:static lg:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex h-16 items-center gap-2.5 border-b border-slate-200 px-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">
            G
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-slate-900">GIMME</p>
            <p className="truncate text-xs text-slate-500">Retention Engine</p>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4">
          {NAV_SECTIONS.map((section) => (
            <div key={section.heading} className="mb-5">
              <p className="px-2 pb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
                {section.heading}
              </p>
              <ul className="space-y-0.5">
                {section.items.map((item) => (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      end={item.to === '/'}
                      className={({ isActive }) =>
                        `flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors ${
                          isActive
                            ? 'bg-brand-50 font-medium text-brand-700'
                            : 'text-slate-600 hover:bg-slate-100'
                        }`
                      }
                    >
                      <svg
                        viewBox="0 0 24 24"
                        className="h-4 w-4 shrink-0"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path d={item.icon} />
                      </svg>
                      <span className="truncate">{item.label}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-slate-200 px-4 py-3">
          {status?.mock_mode && (
            <div className="mb-3 rounded-lg bg-amber-50 px-3 py-2 ring-1 ring-inset ring-amber-200">
              <p className="text-xs font-medium text-amber-800">Mock mode</p>
              <p className="mt-0.5 text-xs text-amber-700">
                Messages are recorded locally and never sent.
              </p>
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-xs font-medium text-slate-700">{user?.full_name}</p>
              <p className="truncate text-xs text-slate-500">{user?.email}</p>
            </div>
            <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={logout}>
              Sign out
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between gap-4 border-b border-slate-200 bg-white/95 px-4 backdrop-blur sm:px-6">
          <button
            type="button"
            className="btn-ghost px-2 py-1.5 lg:hidden"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5">
              <path d="M3 5h14v2H3V5zm0 4h14v2H3V9zm0 4h14v2H3v-2z" />
            </svg>
          </button>

          <div className="flex flex-1 items-center justify-end gap-3">
            {status && (
              <>
                <Badge
                  className="hidden bg-slate-100 text-slate-600 ring-slate-200 sm:inline-flex"
                  title={status.llm.message}
                >
                  LLM: {status.llm.provider}
                </Badge>
                <Badge className="hidden bg-slate-100 text-slate-600 ring-slate-200 md:inline-flex">
                  {status.data.customers?.toLocaleString() ?? 0} customers
                </Badge>
              </>
            )}
          </div>
        </header>

        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
