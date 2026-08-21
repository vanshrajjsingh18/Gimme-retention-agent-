import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import RuleBuilder, { emptyGroup, isGroup } from '../features/RuleBuilder';
import type { FieldDefinition, RuleGroup, RuleNode } from '../types';

const FIELDS: FieldDefinition[] = [
  {
    field: 'lifecycle_stage',
    label: 'Lifecycle stage',
    type: 'enum',
    group: 'Profile',
    choices: ['AT_RISK', 'DORMANT', 'VIP'],
    operators: ['eq', 'neq', 'in', 'not_in'],
  },
  {
    field: 'lifetime_revenue',
    label: 'Lifetime revenue',
    type: 'number',
    group: 'Behaviour',
    choices: [],
    operators: ['eq', 'gt', 'gte', 'lt', 'lte', 'between', 'is_null'],
  },
  {
    field: 'is_suppressed',
    label: 'Suppressed',
    type: 'boolean',
    group: 'Consent',
    choices: [],
    operators: ['is_true', 'is_false'],
  },
];

/** Renders the builder as a controlled component and exposes the latest rule. */
function Harness({ initial, onRule }: { initial: RuleNode; onRule: (rule: RuleNode) => void }) {
  const [rule, setRule] = useState<RuleNode>(initial);
  return (
    <RuleBuilder
      rule={rule}
      fields={FIELDS}
      onChange={(next) => {
        setRule(next);
        onRule(next);
      }}
    />
  );
}

describe('RuleBuilder', () => {
  it('starts empty and explains that an empty rule matches everyone', () => {
    render(<Harness initial={emptyGroup()} onRule={vi.fn()} />);
    expect(screen.getByText(/An empty rule matches every customer/)).toBeInTheDocument();
  });

  it('adds a condition seeded with the first field and its first operator', async () => {
    const onRule = vi.fn();
    render(<Harness initial={emptyGroup()} onRule={onRule} />);

    await userEvent.click(screen.getByRole('button', { name: '+ Condition' }));

    const rule = onRule.mock.lastCall![0] as RuleGroup;
    expect(rule.op).toBe('AND');
    expect(rule.conditions).toHaveLength(1);
    expect(rule.conditions[0]).toMatchObject({
      field: 'lifecycle_stage',
      operator: 'eq',
    });
  });

  it('toggles the group operator between AND and OR', async () => {
    const onRule = vi.fn();
    render(<Harness initial={emptyGroup()} onRule={onRule} />);

    await userEvent.click(screen.getByRole('button', { name: 'OR' }));
    expect((onRule.mock.lastCall![0] as RuleGroup).op).toBe('OR');

    await userEvent.click(screen.getByRole('button', { name: 'AND' }));
    expect((onRule.mock.lastCall![0] as RuleGroup).op).toBe('AND');
  });

  it('resets the operator and value when the field changes type', async () => {
    const onRule = vi.fn();
    const initial: RuleGroup = {
      op: 'AND',
      conditions: [{ field: 'lifetime_revenue', operator: 'gte', value: '500' }],
    };
    render(<Harness initial={initial} onRule={onRule} />);

    // Switching to an enum field must not leave the number operator behind.
    await userEvent.selectOptions(screen.getByLabelText('Field'), 'lifecycle_stage');

    const rule = onRule.mock.lastCall![0] as RuleGroup;
    expect(rule.conditions[0]).toEqual({
      field: 'lifecycle_stage',
      operator: 'eq',
      value: '',
    });
  });

  it('offers only the operators valid for the selected field type', async () => {
    render(
      <Harness
        initial={{ op: 'AND', conditions: [{ field: 'is_suppressed', operator: 'is_true' }] }}
        onRule={vi.fn()}
      />,
    );

    const operatorSelect = screen.getByLabelText('Operator') as HTMLSelectElement;
    const options = [...operatorSelect.options].map((o) => o.value);
    expect(options).toEqual(['is_true', 'is_false']);
  });

  it('hides the value input for operators that take no value', () => {
    render(
      <Harness
        initial={{ op: 'AND', conditions: [{ field: 'is_suppressed', operator: 'is_true' }] }}
        onRule={vi.fn()}
      />,
    );
    expect(screen.getByText('No value needed')).toBeInTheDocument();
    expect(screen.queryByLabelText('Value')).not.toBeInTheDocument();
  });

  it('renders a two-part input for between', () => {
    render(
      <Harness
        initial={{
          op: 'AND',
          conditions: [{ field: 'lifetime_revenue', operator: 'between', value: ['100', '500'] }],
        }}
        onRule={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('From')).toHaveValue(100);
    expect(screen.getByLabelText('To')).toHaveValue(500);
  });

  it('renders enum choices as toggles for multi-value operators', async () => {
    const onRule = vi.fn();
    render(
      <Harness
        initial={{
          op: 'AND',
          conditions: [{ field: 'lifecycle_stage', operator: 'in', value: [] }],
        }}
        onRule={onRule}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'At Risk' }));
    expect((onRule.mock.lastCall![0] as RuleGroup).conditions[0]).toMatchObject({
      value: ['AT_RISK'],
    });

    await userEvent.click(screen.getByRole('button', { name: 'Dormant' }));
    expect((onRule.mock.lastCall![0] as RuleGroup).conditions[0]).toMatchObject({
      value: ['AT_RISK', 'DORMANT'],
    });

    // Clicking an active choice removes it.
    await userEvent.click(screen.getByRole('button', { name: 'At Risk' }));
    expect((onRule.mock.lastCall![0] as RuleGroup).conditions[0]).toMatchObject({
      value: ['DORMANT'],
    });
  });

  it('adds and removes a nested group', async () => {
    const onRule = vi.fn();
    render(<Harness initial={emptyGroup()} onRule={onRule} />);

    await userEvent.click(screen.getByRole('button', { name: '+ Nested group' }));
    let rule = onRule.mock.lastCall![0] as RuleGroup;
    expect(rule.conditions).toHaveLength(1);
    expect(isGroup(rule.conditions[0])).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: 'Remove group' }));
    rule = onRule.mock.lastCall![0] as RuleGroup;
    expect(rule.conditions).toHaveLength(0);
  });

  it('removes a condition', async () => {
    const onRule = vi.fn();
    render(
      <Harness
        initial={{
          op: 'AND',
          conditions: [
            { field: 'lifecycle_stage', operator: 'eq', value: 'VIP' },
            { field: 'lifetime_revenue', operator: 'gte', value: '500' },
          ],
        }}
        onRule={onRule}
      />,
    );

    await userEvent.click(screen.getAllByRole('button', { name: 'Remove condition' })[0]);
    const rule = onRule.mock.lastCall![0] as RuleGroup;
    expect(rule.conditions).toHaveLength(1);
    expect(rule.conditions[0]).toMatchObject({ field: 'lifetime_revenue' });
  });

  it('normalises a bare condition into a group so siblings can be added', () => {
    render(
      <Harness
        initial={{ field: 'lifecycle_stage', operator: 'eq', value: 'VIP' }}
        onRule={vi.fn()}
      />,
    );
    // The condition is shown, and the group controls are available around it.
    expect(screen.getByLabelText('Field')).toHaveValue('lifecycle_stage');
    expect(screen.getByRole('button', { name: '+ Condition' })).toBeInTheDocument();
  });

  it('shows the joining operator between sibling conditions', () => {
    render(
      <Harness
        initial={{
          op: 'OR',
          conditions: [
            { field: 'lifecycle_stage', operator: 'eq', value: 'VIP' },
            { field: 'lifetime_revenue', operator: 'gte', value: '500' },
          ],
        }}
        onRule={vi.fn()}
      />,
    );
    // One separator label between the two conditions.
    expect(screen.getAllByText('OR')).toHaveLength(2); // the toggle plus the separator
  });
});
