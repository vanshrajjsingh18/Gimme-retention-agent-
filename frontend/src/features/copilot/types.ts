export type Risk = 'READ' | 'WRITE' | 'HIGH_RISK_WRITE';

export type ActionStatus =
  | 'PENDING'
  | 'EXECUTING'
  | 'EXECUTED'
  | 'FAILED'
  | 'CANCELLED'
  | 'EXPIRED'
  | 'SUPERSEDED'
  | 'BLOCKED';

export interface DryRun {
  campaign: {
    id: number;
    name: string;
    status: string;
    channel: string;
    audience: Record<string, unknown>;
    timing?: { minutes_before_predicted_order: number; min_confidence: number; min_gap_days: number };
  };
  analysed: number;
  eligible_before_consent: number;
  excluded: Record<string, number>;
  final_audience: number;
  messages_today: number;
  messages_tonight: number;
  today_label: string;
  horizon_days: number;
  estimated_touchpoints: number;
  coupon_allocation: Record<string, number>;
  schedule_sample: {
    moved_for_send_window?: boolean;
    customer_id: number;
    customer: string;
    send_at_local: string | null;
    predicted_order_local: string | null;
    minutes_before: number | null;
    confidence: number | null;
    product: string | null;
    coupon_code: string | null;
    message: string | null;
  }[];
  message_check: {
    valid: boolean;
    sendable_without_review: boolean;
    blocking: string[];
    needs_confirmation: string[];
    warnings: string[];
    sms?: { characters: number; segments: number; encoding: string } | null;
  };
}

export interface ActionPreview {
  summary: string;
  risk: Risk;
  target: { type: string | null; id: string | number | null };
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  result: unknown;
  dry_run?: DryRun;
}

export interface CopilotActionView {
  id: number;
  conversation_id: number;
  tool_name: string;
  risk: Risk;
  summary: string;
  arguments: Record<string, unknown>;
  preview: ActionPreview;
  status: ActionStatus;
  error: string | null;
  created_at: string | null;
  expires_at: string | null;
  resolved_at: string | null;
  execution_id: number | null;
}

export interface ExecutionView {
  id: number;
  action_id: number | null;
  tool_name: string;
  action_type: Risk;
  target_type: string | null;
  target_id: string | null;
  success: boolean;
  error: string | null;
  timestamp: string | null;
  before_state: Record<string, unknown>;
  after_state: Record<string, unknown>;
}

export interface ToolInfo {
  name: string;
  risk: Risk;
  arguments: Record<string, unknown>;
  success: boolean;
  errors: string[] | null;
  metadata: Record<string, unknown> | null;
  data: unknown;
}

export interface CopilotMessageView {
  id: number;
  role: 'user' | 'assistant' | 'tool' | 'event';
  content: string;
  created_at: string | null;
  tool_calls?: { id: string; name: string; arguments: Record<string, unknown> }[];
  tool?: ToolInfo;
  meta?: { provider?: string; model?: string; latency_ms?: number; error?: string };
  action?: CopilotActionView;
}

export interface ActiveEntity {
  type: 'automation' | 'campaign' | 'segment' | 'customer';
  id: number;
  name?: string;
  kind_label?: string;
  status?: string;
  channel?: string;
  audience?: { segment?: string; members?: number; manual_customers?: number };
  coupons?: { code: string; allocation: number; enabled: boolean }[];
  timing?: { minutes_before_predicted_order: number; min_confidence: number };
  message_template?: string;
  approved?: boolean;
  members?: number;
  lifecycle_stage?: string;
}

export interface ConversationView {
  id: number;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  context: {
    active: ActiveEntity | null;
    pending_action: CopilotActionView | null;
    last_result_set: { kind: string; count: number; description: string } | null;
    now_local: string;
  };
  messages: CopilotMessageView[];
  recent_actions: ExecutionView[];
  provider: { provider: string; model: string; mode: string; note?: string };
  new_message_ids?: number[];
}

export interface ConversationSummary {
  id: number;
  title: string;
  updated_at: string | null;
  active_entity_type: string | null;
  active_entity_id: number | null;
}

export interface ActionOutcome {
  action: CopilotActionView;
  execution: ExecutionView | null;
  duplicate: boolean;
  conversation: ConversationView;
}
