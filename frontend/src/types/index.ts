export type LifecycleStage =
  | 'NEW'
  | 'ACTIVATING'
  | 'REGULAR'
  | 'HIGH_VALUE'
  | 'VIP'
  | 'AT_RISK'
  | 'DORMANT'
  | 'CHURNED'
  | 'REACTIVATED';

export type ChurnRiskBand = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type Channel = 'EMAIL' | 'SMS' | 'WHATSAPP' | 'PUSH';

export type CampaignStatus =
  | 'DRAFT'
  | 'AI_GENERATED'
  | 'VALIDATED'
  | 'COMPLIANCE_CHECKED'
  | 'AWAITING_APPROVAL'
  | 'APPROVED'
  | 'SCHEDULED'
  | 'RUNNING'
  | 'PAUSED'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED';

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in_minutes: number;
  user: User;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface CustomerSummary {
  id: number;
  external_id: string;
  full_name: string;
  email: string | null;
  phone: string | null;
  city: string | null;
  lifecycle_stage: LifecycleStage;
  total_orders: number;
  completed_orders: number;
  lifetime_revenue: number;
  average_order_value: number;
  days_since_last_order: number | null;
  last_order_at: string | null;
  churn_score: number;
  churn_risk_band: ChurnRiskBand;
  rfm_segment: string | null;
  rfm_cell: string | null;
  estimated_ltv: number;
  engagement_score: number;
  recommended_action: string;
  marketing_consent: boolean;
  is_suppressed: boolean;
}

export interface ChurnFactor {
  code: string;
  label: string;
  severity: number;
  points: number;
  detail: string;
}

export interface CustomerProfile extends CustomerSummary {
  first_name: string;
  last_name: string;
  region: string | null;
  postcode: string | null;
  country: string;
  signup_date: string | null;
  acquisition_source: string | null;
  preferred_channel: Channel;
  age_verified: boolean;
  date_of_birth: string | null;
  email_consent: boolean;
  sms_consent: boolean;
  whatsapp_consent: boolean;
  lifecycle_updated_at: string | null;
  cancelled_orders: number;
  total_units: number;
  first_order_at: string | null;
  days_since_first_order: number | null;
  average_purchase_interval_days: number | null;
  median_purchase_interval_days: number | null;
  purchase_frequency_per_month: number;
  discount_dependency: number;
  orders_last_30d: number;
  orders_last_90d: number;
  revenue_last_90d: number;
  spend_trend: number;
  frequency_trend: number;
  preferred_categories: string[];
  preferred_brands: string[];
  top_products: { product_name: string; quantity: number }[];
  typical_order_weekday: string | null;
  typical_order_hour: number | null;
  churn_explanation: string;
  churn_factors: ChurnFactor[];
  revenue_at_risk: number;
  rfm_total: number | null;
  recency_score: number | null;
  frequency_score: number | null;
  monetary_score: number | null;
  recommendation_explanation: string;
  recommendation_reason_codes: string[];
  recommended_channel: Channel;
  suggested_products: { product_name: string; quantity: number }[];
  expected_cycle_days: number | null;
  cadence_source: string | null;
  days_overdue: number | null;
  suppressed_channels: string[];
  consent_history: {
    consent_type: string;
    granted: boolean;
    source: string;
    occurred_at: string;
  }[];
}

export interface OrderItem {
  id: number;
  sku: string;
  product_name: string;
  category: string;
  brand: string;
  quantity: number;
  unit_price: number;
  line_total: number;
}

export interface Order {
  id: number;
  external_id: string;
  ordered_at: string;
  status: string;
  total_amount: number;
  discount_amount: number;
  delivery_fee: number;
  coupon_code: string | null;
  channel: string | null;
  items: OrderItem[];
}

export interface CustomerDetail {
  profile: CustomerProfile;
  orders: Order[];
  lifecycle_history: {
    from_stage: string | null;
    to_stage: string;
    reason: string;
    changed_at: string;
  }[];
  communication_events: {
    id: number;
    event_type: string;
    channel: string;
    provider: string;
    campaign_id: number | null;
    message_id: number | null;
    occurred_at: string;
    is_simulated: boolean;
  }[];
  messages: Message[];
  campaigns: {
    campaign_id: number;
    name: string;
    objective: string;
    channel: string;
    status: string;
    exclusion_reason: string | null;
    sent_at: string | null;
    opened_at: string | null;
    clicked_at: string | null;
    converted_at: string | null;
  }[];
  segments: { id: number; name: string; segment_type: string }[];
  attribution: {
    order_external_id: string;
    ordered_at: string;
    campaign_id: number;
    campaign_name: string;
    revenue: number;
    hours_since_touch: number;
    is_reactivation: boolean;
  }[];
}

export interface ValidationFinding {
  code: string;
  message: string;
  severity: string;
  blocks_send: boolean;
  excerpt: string;
}

export interface Message {
  id: number;
  customer_id: number | null;
  campaign_id: number | null;
  channel: Channel;
  objective: string;
  subject: string;
  body: string;
  status: string;
  provider: string;
  is_test: boolean;
  llm_provider: string;
  llm_model: string;
  prompt_version: string;
  generated_at: string | null;
  validation_result: {
    valid: boolean;
    errors: ValidationFinding[];
    warnings: ValidationFinding[];
    subject_length?: number;
    body_length?: number;
    body_word_count?: number;
  };
  was_edited: boolean;
  approved_at: string | null;
  sent_at: string | null;
  error_message: string | null;
  created_at: string;
}

export interface Segment {
  id: number;
  name: string;
  description: string;
  segment_type: 'DYNAMIC' | 'MANUAL';
  status: 'ACTIVE' | 'ARCHIVED';
  is_system: boolean;
  rule_definition: RuleNode;
  member_count: number;
  last_evaluated_at: string | null;
  created_at: string;
  updated_at: string;
  rule_description: string;
}

export type RuleCondition = {
  field: string;
  operator: string;
  value?: unknown;
};

export type RuleGroup = {
  op: 'AND' | 'OR';
  conditions: RuleNode[];
};

export type RuleNode = RuleGroup | RuleCondition | Record<string, never>;

export interface FieldDefinition {
  field: string;
  label: string;
  type: 'number' | 'string' | 'enum' | 'boolean' | 'date' | 'list';
  group: string;
  choices: string[];
  operators: string[];
}

export interface SegmentPreview {
  total_customers: number;
  matched_customers: number;
  match_rate: number;
  sample: {
    id: number;
    external_id: string;
    full_name: string;
    email: string | null;
    lifecycle_stage: string;
    lifetime_revenue: number;
    days_since_last_order: number | null;
    churn_score: number;
    churn_risk_band: string;
  }[];
}

export interface Campaign {
  id: number;
  name: string;
  description: string;
  objective: string;
  channel: Channel;
  status: CampaignStatus;
  segment_id: number | null;
  segment_name: string | null;
  sending_strategy: string;
  scheduled_at: string | null;
  attribution_window_hours: number;
  subject: string;
  body: string;
  audience_snapshot: AudiencePreview | Record<string, never>;
  compliance_result: ComplianceReport | Record<string, never>;
  approved_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  total_recipients: number;
  messages_sent: number;
  messages_delivered: number;
  messages_opened: number;
  messages_clicked: number;
  messages_replied: number;
  messages_failed: number;
  unsubscribes: number;
  conversions: number;
  attributed_revenue: number;
  created_at: string;
  updated_at: string;
}

export interface AudiencePreview {
  audience_size: number;
  eligible_count: number;
  excluded_count: number;
  excluded_by_reason: Record<string, number>;
  exclusion_samples: Record<string, { id: number; full_name: string; reason: string }[]>;
  sample_recipients: {
    id: number;
    external_id: string;
    full_name: string;
    email: string | null;
    phone: string | null;
    lifecycle_stage: string;
  }[];
  channel: string;
  evaluated_at: string;
}

export interface ComplianceReport {
  passed: boolean;
  blocking_count: number;
  findings: ValidationFinding[];
  checked_at?: string;
}

export interface BrandSettings {
  id: number;
  company_name: string;
  company_description: string;
  brand_voice: string;
  tone: string;
  communication_principles: string[];
  preferred_vocabulary: string[];
  words_to_avoid: string[];
  emoji_usage: string;
  max_email_words: number;
  max_sms_characters: number;
  max_whatsapp_characters: number;
  email_signature: string;
  whatsapp_closing: string;
  sms_style: string;
  customer_service_phone: string;
  customer_service_email: string;
  website: string;
  delivery_areas: string[];
  delivery_promise: string;
  mission_statement: string;
  responsible_drinking_statement: string;
  legal_disclaimer: string;
  age_restriction_statement: string;
  prohibited_claims: string[];
  allowed_promotions: string[];
  active_coupon_codes: string[];
  verified_products: { product_name?: string; name?: string; price?: string }[];
  minimum_age: number;
  updated_at: string;
}

export interface Integration {
  id: number;
  provider: string;
  channel: string;
  display_name: string;
  mode: 'mock' | 'live';
  enabled: boolean;
  status: string;
  status_message: string;
  last_checked_at: string | null;
  config: Record<string, unknown>;
  credentials: Record<string, { configured: boolean; hint: string }>;
  required_credentials: string[];
}

export interface ComplianceRule {
  id: number;
  code: string;
  name: string;
  description: string;
  severity: string;
  blocks_send: boolean;
  enabled: boolean;
  config: Record<string, unknown>;
}

export interface OverviewAnalytics {
  generated_at: string;
  total_customers: number;
  active_customers: number;
  new_customers_30d: number;
  repeat_customers: number;
  reactivated_customers: number;
  total_reactivations: number;
  at_risk_customers: number;
  dormant_customers: number;
  churned_customers: number;
  total_orders: number;
  total_revenue: number;
  average_order_value: number;
  orders_30d: number;
  revenue_30d: number;
  orders_prev_30d: number;
  revenue_prev_30d: number;
  revenue_change_30d: number;
  repeat_purchase_rate: number;
  retention_rate_90d: number;
  reactivation_rate: number;
  estimated_ltv_total: number;
  revenue_at_risk: number;
  campaign_attributed_revenue: number;
  campaign_revenue_share: number;
  lifecycle_distribution: { stage: string; count: number }[];
}

export interface IngestionJob {
  id: number;
  source: string;
  entity_type: string;
  filename: string;
  status: string;
  total_rows: number;
  accepted_rows: number;
  updated_rows: number;
  rejected_rows: number;
  duplicate_rows: number;
  errors: { row: number; error: string; data: Record<string, string> }[];
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface Journey {
  id: number;
  name: string;
  description: string;
  status: string;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  allow_reentry: boolean;
  total_entered: number;
  total_completed: number;
  nodes: {
    id: number;
    position: number;
    node_type: string;
    subtype: string;
    config: Record<string, unknown>;
  }[];
  created_at: string;
  updated_at: string;
}

export interface SystemStatus {
  app_name: string;
  environment: string;
  llm: { provider: string; model: string; status: string; mode: string; message: string };
  integrations: { provider: string; channel: string; mode: string; status: string }[];
  mock_mode: boolean;
  scheduler: { running: boolean; jobs: { id: string; next_run_at: string | null }[] };
  data: Record<string, number>;
}

export type AutomationKind = 'SEQUENCE' | 'NUDGE' | 'COHORT_BULK';
export type AutomationStatus = 'DRAFT' | 'ACTIVE' | 'PAUSED' | 'COMPLETED';

export interface AutomationStep {
  id: number;
  position: number;
  name: string;
  /** Days after the customer's own enrollment, not a calendar date. */
  offset_days: number;
  send_time_local: string | null;
  message_template: string;
  use_llm: boolean;
}

export interface Automation {
  id: number;
  name: string;
  description: string;
  kind: AutomationKind;
  status: AutomationStatus;
  channel: string;
  objective: string;
  segment_id: number | null;
  segment_name: string | null;
  manual_customer_ids: number[];
  enrollment_mode: 'ROLLING' | 'FIXED_COHORT';
  recurrence: 'ONCE' | 'DAILY' | 'WEEKLY' | 'MONTHLY';
  recurrence_day: number | null;
  send_time_local: string;
  starts_at: string | null;
  ends_at: string | null;
  message_template: string;
  template_overrides: Record<string, string>;
  config: Record<string, unknown>;
  campaign_id: number | null;
  stop_on_order: boolean;
  require_approval: boolean;
  approved_at: string | null;
  last_run_at: string | null;
  next_run_at: string | null;
  total_sent: number;
  total_skipped: number;
  total_failed: number;
  created_at: string;
  steps: AutomationStep[];
}

export interface AutomationRecipientPreview {
  customer_id: number;
  customer_name: string;
  /** Partially redacted, so a preview is safe to screenshot. */
  to: string | null;
  status: string;
  scheduled_for: string;
  scheduled_for_local: string;
  local_date: string;
  skip_reason: string | null;
  skip_detail: string | null;
  body: string;
  context: Record<string, unknown>;
}

export interface AutomationRunReport {
  automation_id: number;
  automation_name: string;
  kind: AutomationKind;
  dry_run: boolean;
  ran_at: string;
  candidates: number;
  sent: number;
  failed: number;
  skipped: number;
  previewed: number;
  skips_by_reason: Record<string, number>;
  provider: string;
  is_mock: boolean;
  recipients: AutomationRecipientPreview[];
  truncated: boolean;
}

export interface AutomationStats {
  automation_id: number;
  name: string;
  kind: AutomationKind;
  status: AutomationStatus;
  sends_by_status: Record<string, number>;
  skips_by_reason: Record<string, number>;
  enrollments: Record<string, number>;
  active_enrollments: number;
  total_sent: number;
  total_failed: number;
  total_skipped: number;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface AutomationSend {
  id: number;
  automation_id: number;
  customer_id: number;
  step_id: number | null;
  status: string;
  skip_reason: string | null;
  skip_detail: string | null;
  scheduled_for: string;
  local_date: string;
  sent_at: string | null;
  delivered_at: string | null;
  body: string;
  provider: string;
  provider_message_id: string | null;
  error_message: string | null;
  is_dry_run: boolean;
  priority: number;
}

export interface AutomationEnrollment {
  id: number;
  automation_id: number;
  customer_id: number;
  status: string;
  enrolled_at: string;
  current_step: number;
  last_sent_at: string | null;
  next_due_at: string | null;
  stopped_at: string | null;
  stop_reason: string | null;
  pattern: Record<string, unknown>;
}
