import { useMemo } from 'react';

import type { FieldDefinition, RuleCondition, RuleGroup, RuleNode } from '../types';
import { humanize } from '../utils/format';

/**
 * Visual builder for nested AND/OR segment rules.
 *
 * Rules are edited immutably: every change rebuilds the tree from the root, so
 * the parent always holds the single source of truth and undo is a matter of
 * restoring a previous value.
 */

const OPERATOR_LABELS: Record<string, string> = {
  eq: 'is',
  neq: 'is not',
  gt: 'is greater than',
  gte: 'is at least',
  lt: 'is less than',
  lte: 'is at most',
  between: 'is between',
  contains: 'contains',
  not_contains: 'does not contain',
  starts_with: 'starts with',
  ends_with: 'ends with',
  in: 'is one of',
  not_in: 'is not one of',
  is_null: 'is not set',
  is_not_null: 'is set',
  is_true: 'is true',
  is_false: 'is false',
  before: 'is before',
  after: 'is after',
  on: 'is on',
  in_last_days: 'is within the last (days)',
  not_in_last_days: 'is not within the last (days)',
  contains_any: 'includes any of',
  contains_all: 'includes all of',
  is_empty: 'is empty',
  is_not_empty: 'is not empty',
};

const NO_VALUE_OPERATORS = new Set([
  'is_null',
  'is_not_null',
  'is_true',
  'is_false',
  'is_empty',
  'is_not_empty',
]);

const MULTI_VALUE_OPERATORS = new Set(['in', 'not_in', 'contains_any', 'contains_all']);

export function isGroup(node: RuleNode): node is RuleGroup {
  return typeof node === 'object' && node !== null && 'conditions' in node;
}

function isCondition(node: RuleNode): node is RuleCondition {
  return typeof node === 'object' && node !== null && 'field' in node;
}

export function emptyGroup(): RuleGroup {
  return { op: 'AND', conditions: [] };
}

export default function RuleBuilder({
  rule,
  fields,
  onChange,
}: {
  rule: RuleNode;
  fields: FieldDefinition[];
  onChange: (rule: RuleNode) => void;
}) {
  const fieldMap = useMemo(
    () => new Map(fields.map((f) => [f.field, f])),
    [fields],
  );

  // Normalise a bare condition or empty object into a group so the builder
  // always has somewhere to add siblings.
  const root: RuleGroup = isGroup(rule)
    ? rule
    : isCondition(rule)
      ? { op: 'AND', conditions: [rule] }
      : emptyGroup();

  if (fields.length === 0) {
    return <p className="text-sm text-slate-500">Loading fields…</p>;
  }

  return (
    <GroupEditor
      group={root}
      fields={fields}
      fieldMap={fieldMap}
      depth={0}
      onChange={onChange}
      onRemove={undefined}
    />
  );
}

function GroupEditor({
  group,
  fields,
  fieldMap,
  depth,
  onChange,
  onRemove,
}: {
  group: RuleGroup;
  fields: FieldDefinition[];
  fieldMap: Map<string, FieldDefinition>;
  depth: number;
  onChange: (node: RuleNode) => void;
  onRemove?: () => void;
}) {
  function updateChild(index: number, child: RuleNode) {
    const conditions = [...group.conditions];
    conditions[index] = child;
    onChange({ ...group, conditions });
  }

  function removeChild(index: number) {
    onChange({ ...group, conditions: group.conditions.filter((_, i) => i !== index) });
  }

  function addCondition() {
    const first = fields[0];
    onChange({
      ...group,
      conditions: [
        ...group.conditions,
        { field: first.field, operator: first.operators[0], value: '' },
      ],
    });
  }

  function addGroup() {
    onChange({ ...group, conditions: [...group.conditions, emptyGroup()] });
  }

  const grouped = useMemo(() => {
    const byGroup = new Map<string, FieldDefinition[]>();
    fields.forEach((f) => {
      const list = byGroup.get(f.group) ?? [];
      list.push(f);
      byGroup.set(f.group, list);
    });
    return [...byGroup.entries()];
  }, [fields]);

  return (
    <div
      className={
        depth === 0
          ? ''
          : 'rounded-lg border border-slate-200 bg-slate-50/60 p-3'
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="inline-flex overflow-hidden rounded-lg border border-slate-300">
          {(['AND', 'OR'] as const).map((op) => (
            <button
              key={op}
              type="button"
              onClick={() => onChange({ ...group, op })}
              aria-pressed={group.op === op}
              className={`px-3 py-1 text-xs font-medium transition-colors ${
                group.op === op
                  ? 'bg-brand-600 text-white'
                  : 'bg-white text-slate-600 hover:bg-slate-50'
              }`}
            >
              {op}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500">
          {group.op === 'AND' ? 'Match every condition below' : 'Match any condition below'}
        </span>
        <div className="flex-1" />
        {onRemove && (
          <button
            type="button"
            className="btn-ghost px-2 py-1 text-xs text-red-600 hover:bg-red-50"
            onClick={onRemove}
          >
            Remove group
          </button>
        )}
      </div>

      {group.conditions.length === 0 ? (
        <p className="mb-3 rounded-lg border border-dashed border-slate-300 px-3 py-4 text-center text-xs text-slate-500">
          No conditions yet. An empty rule matches every customer.
        </p>
      ) : (
        <ul className="mb-3 space-y-2">
          {group.conditions.map((child, index) => (
            <li key={index} className="relative">
              {index > 0 && (
                <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-400">
                  {group.op}
                </span>
              )}
              {isGroup(child) ? (
                <GroupEditor
                  group={child}
                  fields={fields}
                  fieldMap={fieldMap}
                  depth={depth + 1}
                  onChange={(node) => updateChild(index, node)}
                  onRemove={() => removeChild(index)}
                />
              ) : (
                <ConditionEditor
                  condition={child as RuleCondition}
                  grouped={grouped}
                  fieldMap={fieldMap}
                  onChange={(node) => updateChild(index, node)}
                  onRemove={() => removeChild(index)}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-secondary px-2.5 py-1 text-xs" onClick={addCondition}>
          + Condition
        </button>
        {depth < 4 && (
          <button type="button" className="btn-secondary px-2.5 py-1 text-xs" onClick={addGroup}>
            + Nested group
          </button>
        )}
      </div>
    </div>
  );
}

function ConditionEditor({
  condition,
  grouped,
  fieldMap,
  onChange,
  onRemove,
}: {
  condition: RuleCondition;
  grouped: [string, FieldDefinition[]][];
  fieldMap: Map<string, FieldDefinition>;
  onChange: (node: RuleNode) => void;
  onRemove: () => void;
}) {
  const definition = fieldMap.get(condition.field);
  const operators = definition?.operators ?? [];
  const needsValue = !NO_VALUE_OPERATORS.has(condition.operator);
  const isMulti = MULTI_VALUE_OPERATORS.has(condition.operator);
  const isBetween = condition.operator === 'between';

  function changeField(field: string) {
    const next = fieldMap.get(field);
    if (!next) return;
    // Operators are per-type, so a field change resets the operator and value.
    onChange({ field, operator: next.operators[0], value: '' });
  }

  function changeOperator(operator: string) {
    const nextValue = MULTI_VALUE_OPERATORS.has(operator)
      ? []
      : operator === 'between'
        ? ['', '']
        : '';
    onChange({ ...condition, operator, value: nextValue });
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.2fr)_auto]">
        <select
          className="input text-sm"
          value={condition.field}
          onChange={(e) => changeField(e.target.value)}
          aria-label="Field"
        >
          {grouped.map(([groupName, groupFields]) => (
            <optgroup key={groupName} label={groupName}>
              {groupFields.map((f) => (
                <option key={f.field} value={f.field}>
                  {f.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>

        <select
          className="input text-sm"
          value={condition.operator}
          onChange={(e) => changeOperator(e.target.value)}
          aria-label="Operator"
        >
          {operators.map((op) => (
            <option key={op} value={op}>
              {OPERATOR_LABELS[op] ?? humanize(op)}
            </option>
          ))}
        </select>

        {needsValue ? (
          <ValueEditor
            definition={definition}
            operator={condition.operator}
            value={condition.value}
            isMulti={isMulti}
            isBetween={isBetween}
            onChange={(value) => onChange({ ...condition, value })}
          />
        ) : (
          <span className="self-center text-xs text-slate-400">No value needed</span>
        )}

        <button
          type="button"
          className="btn-ghost self-center px-2 py-1 text-xs text-red-600 hover:bg-red-50"
          onClick={onRemove}
          aria-label="Remove condition"
        >
          Remove
        </button>
      </div>
    </div>
  );
}

function ValueEditor({
  definition,
  operator,
  value,
  isMulti,
  isBetween,
  onChange,
}: {
  definition: FieldDefinition | undefined;
  operator: string;
  value: unknown;
  isMulti: boolean;
  isBetween: boolean;
  onChange: (value: unknown) => void;
}) {
  const type = definition?.type ?? 'string';

  if (isBetween) {
    const pair = Array.isArray(value) ? value : ['', ''];
    return (
      <div className="flex items-center gap-1.5">
        <input
          className="input text-sm"
          type={type === 'date' ? 'date' : 'number'}
          value={String(pair[0] ?? '')}
          onChange={(e) => onChange([e.target.value, pair[1] ?? ''])}
          aria-label="From"
        />
        <span className="text-xs text-slate-400">and</span>
        <input
          className="input text-sm"
          type={type === 'date' ? 'date' : 'number'}
          value={String(pair[1] ?? '')}
          onChange={(e) => onChange([pair[0] ?? '', e.target.value])}
          aria-label="To"
        />
      </div>
    );
  }

  if (isMulti) {
    const selected = Array.isArray(value) ? value.map(String) : [];
    if (definition?.choices?.length) {
      return (
        <div className="flex flex-wrap gap-1">
          {definition.choices.map((choice) => {
            const active = selected.includes(choice);
            return (
              <button
                key={choice}
                type="button"
                onClick={() =>
                  onChange(
                    active ? selected.filter((v) => v !== choice) : [...selected, choice],
                  )
                }
                aria-pressed={active}
                className={`rounded px-2 py-0.5 text-xs ring-1 ring-inset transition-colors ${
                  active
                    ? 'bg-brand-50 text-brand-700 ring-brand-300'
                    : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50'
                }`}
              >
                {humanize(choice)}
              </button>
            );
          })}
        </div>
      );
    }
    return (
      <input
        className="input text-sm"
        placeholder="Comma-separated values"
        value={selected.join(', ')}
        onChange={(e) =>
          onChange(
            e.target.value
              .split(',')
              .map((v) => v.trim())
              .filter(Boolean),
          )
        }
        aria-label="Values"
      />
    );
  }

  if (definition?.choices?.length) {
    return (
      <select
        className="input text-sm"
        value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Value"
      >
        <option value="">Select…</option>
        {definition.choices.map((choice) => (
          <option key={choice} value={choice}>
            {humanize(choice)}
          </option>
        ))}
      </select>
    );
  }

  const inputType =
    type === 'number' || operator.includes('_days')
      ? 'number'
      : type === 'date'
        ? 'date'
        : 'text';

  return (
    <input
      className="input text-sm"
      type={inputType}
      value={String(value ?? '')}
      onChange={(e) => onChange(e.target.value)}
      placeholder={type === 'number' ? '0' : 'Value'}
      aria-label="Value"
    />
  );
}
