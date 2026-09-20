import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import CampaignDetailPage from '../pages/CampaignDetailPage';
import type { Campaign, CopyMode } from '../types';

const patch = vi.fn().mockImplementation(async (_path: string, body: unknown) => body);
const post = vi.fn().mockResolvedValue({ sent: 1, skipped_ineligible: 0, failed: 0, is_mock: true });

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useParams: () => ({ id: '4' }), useNavigate: () => vi.fn() };
});

vi.mock('../api/client', () => ({
  api: {
    url: (path: string) => path,
    // The page loads the audience through a mutation, so this is what
    // `Refresh` and the run dialog's count read.
    get: vi.fn().mockResolvedValue({
      audience_size: 10,
      eligible_count: 6,
      excluded_count: 4,
      excluded_by_reason: {},
      exclusion_samples: {},
      sample_recipients: [],
    }),
    post: (path: string, body: unknown) => post(path, body),
    patch: (path: string, body: unknown) => patch(path, body),
    del: vi.fn().mockResolvedValue({}),
    baseUrl: '',
  },
  ApiError: class extends Error {},
}));

const COPY_MODES = [
  { value: 'WRITTEN', label: 'Send the copy I write', description: 'Everyone gets the message below.' },
  { value: 'DRAFTED', label: 'Draft each message with AI', description: 'Written at send time.' },
];

let campaign: Campaign;

function stubCampaign(copyMode: CopyMode, status = 'DRAFT'): Campaign {
  return {
    id: 4,
    name: 'Win back',
    description: '',
    objective: 'REACTIVATION',
    channel: 'SMS',
    status,
    segment_id: 2,
    segment_name: 'At Risk',
    sending_strategy: 'IMMEDIATE',
    scheduled_at: null,
    attribution_window_hours: 72,
    subject: '',
    body: 'Kia ora #name#, we are still delivering. Reply STOP to opt out.',
    copy_mode: copyMode,
    audience_snapshot: {},
    compliance_result: { passed: true, blocking_count: 0, findings: [], checked_at: null },
    approved_at: null,
    started_at: null,
    completed_at: null,
    total_recipients: 0,
    messages_sent: 0,
    messages_delivered: 0,
    messages_opened: 0,
    messages_clicked: 0,
    messages_replied: 0,
    messages_failed: 0,
    unsubscribes: 0,
    conversions: 0,
    attributed_revenue: 0,
    created_at: '2026-09-01T00:00:00',
    updated_at: '2026-09-01T00:00:00',
  } as unknown as Campaign;
}

vi.mock('../hooks/useApi', () => ({
  useQuery: (path: string | null) => {
    const data =
      path === null
        ? null
        : path.includes('/options')
          ? { merge_tags: [{ token: 'name', label: 'First name', example: 'Sarah' }], copy_modes: COPY_MODES }
          : path.includes('/audience')
            ? {
                audience_size: 10,
                eligible_count: 6,
                excluded_count: 4,
                excluded_by_reason: {},
                exclusion_samples: {},
                sample_recipients: [],
              }
            : path.includes('/recipients')
              ? { recipients: [] }
              : campaign;
    return { data, loading: false, error: null, refetch: vi.fn() };
  },
  useMutation: (fn: (...args: unknown[]) => unknown) => ({ run: fn, loading: false, error: null }),
}));

function renderPage() {
  return render(
    <MemoryRouter>
      <CampaignDetailPage />
    </MemoryRouter>,
  );
}

/**
 * Where a campaign's copy comes from is the campaign's, and the page has to
 * say which. It used to send `generate_per_customer: true` on every run, so
 * copy somebody wrote and approved was replaced by a generated message — with
 * the merge-tag helper sitting right above it promising the opposite.
 */
describe('Campaign copy mode', () => {
  beforeEach(() => {
    patch.mockClear();
    post.mockClear();
    campaign = stubCampaign('WRITTEN');
  });

  it('shows which way this campaign writes its copy', () => {
    renderPage();
    expect(screen.getByRole('radio', { name: /send the copy i write/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /draft each message with ai/i })).not.toBeChecked();
  });

  it('offers merge tags for written copy', () => {
    renderPage();
    expect(screen.getByRole('button', { name: '#name#' })).toBeInTheDocument();
  });

  it('does not offer merge tags when every message is drafted', () => {
    campaign = stubCampaign('DRAFTED');
    renderPage();
    expect(screen.queryByRole('button', { name: '#name#' })).not.toBeInTheDocument();
    expect(screen.getByText(/fallback body/i)).toBeInTheDocument();
  });

  it('saves the mode with the copy', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('radio', { name: /draft each message with ai/i }));
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0][1]).toMatchObject({ copy_mode: 'DRAFTED' });
  });

  it('sends without asking for drafting at send time', async () => {
    campaign = stubCampaign('WRITTEN', 'APPROVED');
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: /run campaign/i }));
    const confirm = screen.getAllByRole('button', { name: /run campaign/i }).pop()!;
    await userEvent.click(confirm);

    await waitFor(() => expect(post).toHaveBeenCalled());
    const runCall = post.mock.calls.find(([path]) => String(path).endsWith('/run'));
    expect(runCall).toBeDefined();
    expect(runCall![1]).not.toHaveProperty('generate_per_customer');
  });

  it('says what the send will do, in the words of the mode', async () => {
    campaign = stubCampaign('DRAFTED', 'APPROVED');
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: /run campaign/i }));
    expect(screen.getByText(/a different message to each/i)).toBeInTheDocument();
  });
});
