# Smart Reorder Campaign Expansion - Implementation Plan

## Phase 0: Foundation (Current State Analysis)

### Existing Components to Reuse
- ✅ `Automation` model with segment_id, config, message_template
- ✅ `AutomationEnrollment` for tracking customer enrollment
- ✅ `AutomationSend` for message delivery tracking
- ✅ `nudge.run()` for Smart Reorder prediction + scheduling
- ✅ Segment evaluation via `evaluate_segment()`
- ✅ Merge tag system via `merge_tags.unknown_tags()`
- ✅ Campaign model linking for analytics

### Key Observations
- Smart Reorder is a NUDGE automation type
- Current flow: segment audience → predict order → schedule message
- Needs extension: coupon assignment, frequency gating, multi-touch rules

---

## Phase 1: Database Schema Extensions

### New Models Required

#### 1. CouponVariant
```python
class CouponVariant(Base, TimestampMixin):
    automation_id: int (FK)
    position: int
    coupon_code: str
    enabled: bool
    allocation_percentage: float
```

#### 2. CustomerCouponAssignment
```python
class CustomerCouponAssignment(Base):
    customer_id: int (FK)
    automation_id: int (FK)
    coupon_code: str
    variant_id: int
    assigned_at: datetime
```

#### 3. FrequencyRule
Extend Automation.config to store:
```json
{
  "frequency_rules": {
    "max_frequency_days": 7,
    "max_messages_per_window": 1,
    "window_days": 30,
    "max_total_touchpoints": 3,
    "quiet_hours": {"start": "19:00", "end": "09:00"}
  }
}
```

#### 4. TouchpointRule
Extend Automation.config:
```json
{
  "touchpoints": [
    {
      "position": 1,
      "timing_minutes_before": 30,
      "condition": "none"
    },
    {
      "position": 2,
      "timing_days_after": 7,
      "condition": "not_ordered"
    }
  ]
}
```

#### 5. StopRule
Extend Automation.config:
```json
{
  "stop_rules": [
    "ORDERED",
    "OPTED_OUT",
    "SUPPRESSED",
    "CAMPAIGN_ENDED",
    "MAX_MESSAGES_REACHED"
  ]
}
```

### Migration Steps
1. Add new tables to `app/models/entities.py`
2. Create Alembic migration
3. Update `bootstrap()` to create tables
4. Add relationships to `Automation`

---

## Phase 2: Campaign Configuration API

### Endpoints to Add/Extend

#### GET `/api/v1/automations/{id}/coupon-variants`
List coupon variants for automation

#### POST `/api/v1/automations/{id}/coupon-variants`
Add new coupon variant

#### PUT `/api/v1/automations/{id}/coupon-variants/{variant_id}`
Update coupon variant

#### DELETE `/api/v1/automations/{id}/coupon-variants/{variant_id}`
Remove coupon variant

#### POST `/api/v1/automations/{id}/audience-preview`
Return detailed audience breakdown:
```json
{
  "segment_matched": 1245,
  "eligible_reorder": 842,
  "insufficient_history": 143,
  "low_confidence": 98,
  "no_consent": 72,
  "suppressed": 44,
  "no_phone": 21,
  "final_eligible": 439
}
```

---

## Phase 3: Coupon Assignment Logic

### Create `app/services/coupon_assignment.py`

Core function:
```python
def assign_coupon_for_customer(
    db: Session,
    customer_id: int,
    automation_id: int,
    variants: list[CouponVariant],
) -> str:
    """Assign or retrieve existing coupon for customer/automation combo."""
```

Logic:
1. Check if customer already assigned to this automation
2. If yes, return existing coupon
3. If no, use allocation method to select variant
4. Store assignment in `CustomerCouponAssignment`
5. Return coupon code

---

## Phase 4: Frequency Gating

### Create `app/services/frequency_gating.py`

Core function:
```python
def should_send_message(
    db: Session,
    customer_id: int,
    automation_id: int,
    touchpoint_position: int,
) -> tuple[bool, str]:
    """Check if customer is eligible for message based on frequency rules."""
```

Logic:
1. Check max messages in time window
2. Check min days between messages
3. Check quiet hours
4. Check max total touchpoints reached
5. Check stop conditions
6. Return (should_send, reason)

---

## Phase 5: Multi-Touch Orchestration

### Extend `app/automations/nudge.py`

Modify `build_candidates()` to:
1. Determine current touchpoint position (0, 1, 2, etc.)
2. Apply touchpoint-specific timing offsets
3. Check if customer ordered since last message
4. Apply frequency gating
5. Only return candidates that pass all gates

---

## Phase 6: Message Rendering Pipeline

### Extend `app/automations/templates.py`

Add to `build_context()`:
```python
def build_context(customer, brand, automation, coupon, *, now=None):
    context = {
        "first_name": customer.first_name,
        "product": ...,
        "coupon_code": coupon,  # NEW
        ...
    }
    return context
```

Merge tag resolver automatically fills `#coupon_code#` from assignment.

---

## Phase 7: Audience Preview Implementation

### Extend `app/api/v1/automations.py`

Add endpoint:
```python
@router.get("/automations/{id}/audience-preview")
def audience_preview(automation_id: int, db: Session = Depends(get_db)):
    """Show detailed breakdown of eligible audience."""
```

Logic reuses existing `nudge._prospective_enrollments()` logic:
- Run segment evaluation
- Run reorder eligibility checks
- Count at each filtering step
- Return breakdown

---

## Phase 8: Frontend Campaign Builder

### New Pages/Components

#### AutomationCouponVariants
- List coupon codes
- Add/edit/remove variants
- Allocation percentage inputs
- Validation: total = 100%

#### AutomationFrequencyConfig
- Max messages per customer
- Time window
- Min days between messages
- Quiet hours picker

#### AutomationTouchpointConfig
- Position (1, 2, 3...)
- Timing (minutes before / days after)
- Conditions (none, "not_ordered", etc.)

#### AudiencePreview
- Show breakdown numbers
- Pie chart of exclusion reasons
- Final eligible count

---

## Phase 9: Analytics & Reporting

### Extend `app/api/v1/automations.py`

Add endpoint:
```python
@router.get("/automations/{id}/coupon-analytics")
def coupon_analytics(automation_id: int, db: Session = Depends(get_db)):
```

Return:
```json
{
  "coupons": [
    {
      "code": "FIRST7",
      "customers_assigned": 150,
      "messages_sent": 145,
      "orders": 24,
      "conversion_rate": 0.1655,
      "revenue": 1850.00
    }
  ]
}
```

---

## Phase 10: Integration & Testing

### Test Dataset
- 3 sample customers (Sam, John, Sarah)
- 3 coupon variants (FIRST7, LUCKY7, COMEAGAIN7)
- Equal allocation (33/33/34)
- Max 3 messages per 30 days

### Test Scenarios
1. Dry run shows audience correctly
2. Each customer gets assigned distinct coupon
3. Coupon persists across runs
4. Frequency gating prevents over-messaging
5. Multi-touch progression works
6. Stop-on-order takes effect
7. Analytics show coupon performance

---

## Implementation Order

1. **Week 1**: Phase 0 → Phase 2 (Database + API foundation)
2. **Week 2**: Phase 3 → Phase 4 (Coupon logic + frequency gating)
3. **Week 3**: Phase 5 → Phase 6 (Orchestration + message rendering)
4. **Week 4**: Phase 7 → Phase 10 (Audience preview, UI, analytics, testing)

---

## Success Criteria

✅ Dry run shows individual customers with assigned coupons
✅ Coupon assignment is deterministic and persistent
✅ Frequency rules prevent over-messaging
✅ Stop-on-order removes customer from future messages
✅ Analytics show coupon performance with real data
✅ Existing Smart Reorder prediction continues working
✅ All 42 acceptance tests pass
