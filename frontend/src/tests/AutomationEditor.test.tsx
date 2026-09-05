import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import AutomationEditor from '../features/AutomationEditor';
import type { Automation } from '../types';

function stubAutomation(overrides: Partial<Automation> = {}): Automation {
  return {
    id: 4,
    name: 'Weekly win-back',
    description: 'Every Monday.',
    kind: 'COHORT_BULK',
    status: 'ACTIVE',
    channel: 'SMS',
    objective: 'REACTIVATION',
    segment_id: 9,
    segment_name: 'Dormant',
    manual_customer_ids: [],
    enrollment_mode: 'ROLLING',
    recurrence: 'WEEKLY',
    recurrence_day: 0,
    send_time_local: '10:00',
    starts_at: null,
    ends_at: null,
    trigger_type: 'SEGMENT_ENTRY',
    message_variants: [],
    message_template: 'Hi {first_name}, it has been a while. Reply STOP to opt out.',
    template_overrides: {},
    config: {},
    campaign_id: 2,
    stop_on_order: true,
    require_approval: true,
    approved_at: '2026-08-20T10:00:00',
    last_run_at: null,
    next_run_at: null,
    total_sent: 0,
    total_skipped: 0,
    total_failed: 0,
    created_at: '2026-06-15T00:00:00',
    steps: [],
    ...overrides,
  };
}

const calls: { method: string; path: string; body: unknown }[] = [];

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        path: String(url),
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => stubAutomation(),
      };
    }),
  );
});

describe('AutomationEditor', () => {
  it('warns that changing the copy withdraws approval', async () => {
    render(
      <AutomationEditor automation={stubAutomation()} onSaved={vi.fn()} onCancel={vi.fn()} />,
    );

    expect(screen.queryByText(/withdraws approval/)).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Message'), ' Extra.');

    expect(screen.getByText(/withdraws approval/)).toBeInTheDocument();
  });

  it('does not warn when only the name changes', async () => {
    render(
      <AutomationEditor automation={stubAutomation()} onSaved={vi.fn()} onCancel={vi.fn()} />,
    );

    await userEvent.type(screen.getByLabelText('Name'), ' v2');

    expect(screen.queryByText(/withdraws approval/)).not.toBeInTheDocument();
  });

  it('does not warn for an automation that never required approval', async () => {
    render(
      <AutomationEditor
        automation={stubAutomation({ require_approval: false, approved_at: null })}
        onSaved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(screen.getByLabelText('Message'), ' Extra.');

    expect(screen.queryByText(/withdraws approval/)).not.toBeInTheDocument();
  });

  it('saves the edited copy', async () => {
    const onSaved = vi.fn();
    render(
      <AutomationEditor automation={stubAutomation()} onSaved={onSaved} onCancel={vi.fn()} />,
    );

    await userEvent.clear(screen.getByLabelText('Message'));
    await userEvent.type(screen.getByLabelText('Message'), 'Fresh copy.');
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const patch = calls.find((call) => call.method === 'PATCH');
    expect(patch?.path).toContain('/api/v1/automations/4');
    expect((patch?.body as { message_template: string }).message_template).toBe('Fresh copy.');
  });

  it('writes sequence steps to the steps endpoint, not the automation body', async () => {
    const sequence = stubAutomation({
      kind: 'SEQUENCE',
      steps: [
        { id: 1, position: 0, name: 'Day 0', offset_days: 0, send_time_local: null, message_template: 'First.', use_llm: false },
        { id: 2, position: 1, name: 'Day 7', offset_days: 7, send_time_local: null, message_template: 'Second.', use_llm: false },
      ],
    });
    render(<AutomationEditor automation={sequence} onSaved={vi.fn()} onCancel={vi.fn()} />);

    const firstStep = screen.getByLabelText('Message', { selector: '#edit-step-body-0' });
    await userEvent.clear(firstStep);
    await userEvent.type(firstStep, 'Rewritten first step.');
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true));
    const put = calls.find((call) => call.method === 'PUT');
    expect(put?.path).toContain('/steps');
    expect((put?.body as { message_template: string }[])[0].message_template).toBe(
      'Rewritten first step.',
    );
  });

  it('leaves the steps endpoint alone when the steps did not change', async () => {
    const sequence = stubAutomation({
      kind: 'SEQUENCE',
      steps: [
        { id: 1, position: 0, name: 'Day 0', offset_days: 0, send_time_local: null, message_template: 'First.', use_llm: false },
      ],
    });
    render(<AutomationEditor automation={sequence} onSaved={vi.fn()} onCancel={vi.fn()} />);

    await userEvent.type(screen.getByLabelText('Name'), ' v2');
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(calls.some((call) => call.method === 'PATCH')).toBe(true));
    expect(calls.some((call) => call.method === 'PUT')).toBe(false);
  });

  it('surfaces a rejected save rather than closing as if it worked', async () => {
    const onSaved = vi.fn();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 409,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          detail: 'Steps cannot be changed once customers are enrolled.',
        }),
      })),
    );
    render(
      <AutomationEditor automation={stubAutomation()} onSaved={onSaved} onCancel={vi.fn()} />,
    );

    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('cannot be changed');
    expect(onSaved).not.toHaveBeenCalled();
  });
});
