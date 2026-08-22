import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import AutomationForm from '../features/AutomationForm';
import type { Automation } from '../types';

const SEGMENTS = [
  { id: 7, name: 'At Risk', member_count: 41 },
  { id: 9, name: 'Dormant', member_count: 128 },
];

function stubAutomation(overrides: Partial<Automation> = {}): Automation {
  return {
    id: 1,
    name: 'Test',
    description: '',
    kind: 'COHORT_BULK',
    status: 'DRAFT',
    channel: 'SMS',
    objective: 'RETENTION',
    segment_id: 7,
    segment_name: 'At Risk',
    manual_customer_ids: [],
    enrollment_mode: 'ROLLING',
    recurrence: 'ONCE',
    recurrence_day: null,
    send_time_local: '10:00',
    starts_at: null,
    ends_at: null,
    message_template: '',
    template_overrides: {},
    config: {},
    campaign_id: 2,
    stop_on_order: true,
    require_approval: true,
    approved_at: null,
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

beforeEach(() => {
  // The form loads segments through the shared query hook, which calls fetch.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => SEGMENTS,
    })),
  );
});

describe('AutomationForm', () => {
  it('sends the chosen segment rather than a snapshot of its members', async () => {
    const submit = vi.fn(async (_payload: Record<string, unknown>) => stubAutomation());
    render(
      <AutomationForm kind="COHORT_BULK" onCreated={vi.fn()} onCancel={vi.fn()} submit={submit} />,
    );

    await screen.findByRole('option', { name: /At Risk/ });
    await userEvent.type(screen.getByLabelText('Name'), 'Winter reorder');
    await userEvent.selectOptions(screen.getByLabelText('Audience'), '7');
    await userEvent.click(screen.getByRole('button', { name: /create as draft/i }));

    await waitFor(() => expect(submit).toHaveBeenCalled());
    const payload = submit.mock.calls[0][0];
    expect(payload.segment_id).toBe(7);
    expect(payload.name).toBe('Winter reorder');
    expect(payload.kind).toBe('COHORT_BULK');
  });

  it('offers weekday choice only for a weekly cohort send', async () => {
    render(
      <AutomationForm
        kind="COHORT_BULK"
        onCreated={vi.fn()}
        onCancel={vi.fn()}
        submit={vi.fn(async () => stubAutomation())}
      />,
    );

    expect(screen.queryByLabelText('On')).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Repeats'), 'WEEKLY');
    expect(screen.getByLabelText('On')).toBeInTheDocument();
  });

  it('collects sequence steps as day offsets, not calendar dates', async () => {
    const submit = vi.fn(async (_payload: Record<string, unknown>) =>
      stubAutomation({ kind: 'SEQUENCE' }),
    );
    render(
      <AutomationForm kind="SEQUENCE" onCreated={vi.fn()} onCancel={vi.fn()} submit={submit} />,
    );

    await screen.findByRole('option', { name: /At Risk/ });
    await userEvent.type(screen.getByLabelText('Name'), 'Win-back series');
    await userEvent.selectOptions(screen.getByLabelText('Audience'), '9');
    await userEvent.type(screen.getByLabelText('Message', { selector: '#step-body-0' }), 'Day zero.');
    await userEvent.click(screen.getByRole('button', { name: /create as draft/i }));

    await waitFor(() => expect(submit).toHaveBeenCalled());
    const payload = submit.mock.calls[0][0] as unknown as {
      steps: { offset_days: number }[];
    };
    expect(payload.steps.map((step) => step.offset_days)).toEqual([0, 7]);
  });

  it('adds a step seven days after the last one', async () => {
    render(
      <AutomationForm
        kind="SEQUENCE"
        onCreated={vi.fn()}
        onCancel={vi.fn()}
        submit={vi.fn(async () => stubAutomation())}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /add step/i }));
    const offsets = screen
      .getAllByLabelText('Day')
      .map((input) => (input as HTMLInputElement).value);
    expect(offsets).toEqual(['0', '7', '14']);
  });

  it('keeps the last step from being removed', async () => {
    render(
      <AutomationForm
        kind="SEQUENCE"
        onCreated={vi.fn()}
        onCancel={vi.fn()}
        submit={vi.fn(async () => stubAutomation())}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Remove step 2' }));
    expect(screen.getByRole('button', { name: 'Remove step 1' })).toBeDisabled();
  });

  it('surfaces a rejected create instead of pretending it worked', async () => {
    const onCreated = vi.fn();
    const submit = vi.fn(async () => {
      throw new Error("An automation named 'Winter reorder' already exists.");
    });
    render(
      <AutomationForm
        kind="COHORT_BULK"
        onCreated={onCreated}
        onCancel={vi.fn()}
        submit={submit}
      />,
    );

    await screen.findByRole('option', { name: /At Risk/ });
    await userEvent.type(screen.getByLabelText('Name'), 'Winter reorder');
    await userEvent.selectOptions(screen.getByLabelText('Audience'), '7');
    await userEvent.click(screen.getByRole('button', { name: /create as draft/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('already exists');
    expect(onCreated).not.toHaveBeenCalled();
  });
});
