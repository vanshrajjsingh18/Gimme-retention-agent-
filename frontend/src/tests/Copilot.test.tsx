import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import ActionCard from '../features/copilot/ActionCard';
import Markdown from '../features/copilot/Markdown';
import type { CopilotActionView } from '../features/copilot/types';

function action(overrides: Partial<CopilotActionView> = {}): CopilotActionView {
  return {
    id: 7,
    conversation_id: 1,
    tool_name: 'update_coupon_allocation',
    risk: 'WRITE',
    summary: 'Change coupon codes / allocation',
    arguments: {},
    preview: {
      summary: 'Change coupon codes / allocation',
      risk: 'WRITE',
      target: { type: 'automation', id: 3 },
      before: { coupons: [{ code: 'FIRST7', allocation: 33.33, enabled: true }] },
      after: { coupons: [{ code: 'FIRST7', allocation: 50, enabled: true }] },
      result: {},
    },
    status: 'PENDING',
    error: null,
    created_at: null,
    expires_at: null,
    resolved_at: null,
    execution_id: null,
    ...overrides,
  };
}

describe('Copilot Markdown', () => {
  it('keeps merge tags intact instead of reading underscores as italics', () => {
    render(<Markdown text={'Message: "Hi #first_name#, code #coupon_code#." and _really_ sure'} />);
    expect(screen.getByText(/#first_name#, code #coupon_code#/)).toBeInTheDocument();
    expect(screen.getByText('really').tagName).toBe('EM');
  });

  it('renders model text as text, never as HTML', () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> **bold**'} />);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText('bold').tagName).toBe('STRONG');
  });

  it('renders pipe tables', () => {
    render(<Markdown text={'| Customer | Coupon |\n|---|---|\n| Sarah | FIRST7 |'} />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('FIRST7')).toBeInTheDocument();
  });
});

describe('Copilot ActionCard', () => {
  it('confirms exactly this action id', async () => {
    const onConfirm = vi.fn();
    render(<ActionCard action={action()} busy={false} onConfirm={onConfirm} onCancel={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText('Action required')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(onConfirm).toHaveBeenCalledWith(7);
  });

  it('marks high-risk actions and labels the button as going live', () => {
    render(
      <ActionCard
        action={action({ risk: 'HIGH_RISK_WRITE', summary: 'ACTIVATE Smart Reorder campaign' })}
        busy={false}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText('High risk')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Confirm — go live/ })).toBeInTheDocument();
    expect(screen.getByText(/affects real customers/)).toBeInTheDocument();
  });

  it('offers no confirm button once executed, and retry once failed', () => {
    const { rerender } = render(
      <ActionCard action={action({ status: 'EXECUTED', execution_id: 4 })} busy={false} onConfirm={vi.fn()} onCancel={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull();
    expect(screen.getByText(/receipt #4/)).toBeInTheDocument();
    rerender(
      <ActionCard action={action({ status: 'FAILED', error: 'Coupon service error' })} busy={false} onConfirm={vi.fn()} onCancel={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.getByText('Coupon service error')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('shows what changes, before and after', () => {
    render(<ActionCard action={action()} busy={false} onConfirm={vi.fn()} onCancel={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText('FIRST7 33.33%')).toBeInTheDocument();
    expect(screen.getByText('FIRST7 50%')).toBeInTheDocument();
  });
});
