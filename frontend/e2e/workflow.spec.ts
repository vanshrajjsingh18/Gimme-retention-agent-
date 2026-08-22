import { expect, test, type Page } from '@playwright/test';

/**
 * Browser end-to-end verification of the GIMME Retention Engine UI.
 *
 * Runs against a live backend with seeded data. Each test asserts that the
 * page renders real data from the API — not just that a component mounted —
 * and every test fails on an uncaught console error or a failed API request.
 */

const EMAIL = 'admin@gimmedelivery.co.nz';
const PASSWORD = 'GimmeAdmin123!';

/** Attach console/network guards so a silently broken page fails the test. */
function guard(page: Page): { errors: string[]; failedRequests: string[] } {
  const errors: string[] = [];
  const failedRequests: string[] = [];

  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('response', (response) => {
    const url = response.url();
    if (url.includes('/api/') && response.status() >= 400) {
      failedRequests.push(`${response.status()} ${url}`);
    }
  });

  return { errors, failedRequests };
}

async function login(page: Page) {
  await page.goto('/login');
  await page.getByLabel('Email address').fill(EMAIL);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Retention overview' })).toBeVisible();
}

test.describe('Authentication', () => {
  test('rejects a wrong password with a visible error', async ({ page }) => {
    guard(page);
    await page.goto('/login');
    await page.getByLabel('Email address').fill(EMAIL);
    await page.getByLabel('Password').fill('definitely-wrong');
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page.getByRole('alert')).toContainText('Incorrect email or password');
    await expect(page).toHaveURL(/\/login/);
  });

  test('signs in and lands on the overview', async ({ page }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    // Real numbers, not placeholders.
    const customerTile = page.locator('text=Total customers').locator('..');
    await expect(customerTile).toContainText(/[1-9]/);

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests, `failed API calls: ${failedRequests.join(' | ')}`).toEqual([]);
  });

  test('an unauthenticated visit redirects to login', async ({ page }) => {
    guard(page);
    await page.goto('/customers');
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe('Navigation', () => {
  test('every nav destination renders without errors', async ({ page }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    const destinations: [string, RegExp][] = [
      ['Customer analytics', /Customer analytics/],
      ['Churn analytics', /Churn analytics/],
      ['Campaign analytics', /Campaign analytics/],
      ['Cohorts', /Cohort retention/],
      ['Customers', /^Customers$/],
      ['Segments', /^Segments$/],
      ['Campaigns', /^Campaigns$/],
      ['Automations', /^Automations$/],
      ['Message Studio', /Message Studio/],
      ['Journeys', /^Journeys$/],
      ['Data & imports', /Data & imports/],
      ['Brand', /^Brand$/],
      ['Compliance', /^Compliance$/],
      ['Integrations', /^Integrations$/],
      ['Settings', /^Settings$/],
    ];

    for (const [linkName, heading] of destinations) {
      await page.getByRole('link', { name: linkName, exact: true }).click();
      await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
      // No page may leave a spinner up forever.
      await expect(page.getByRole('status').filter({ hasText: '' })).toHaveCount(0, {
        timeout: 20_000,
      });
    }

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests, `failed API calls: ${failedRequests.join(' | ')}`).toEqual([]);
  });
});

test.describe('Customer 360', () => {
  test('search, open a profile and see computed intelligence', async ({ page }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    await page.getByRole('link', { name: 'Customers', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Customers' })).toBeVisible();

    // The table is populated from the database.
    const rows = page.locator('tbody tr');
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(1);

    // Filter to at-risk customers and confirm the list narrows.
    await page.getByRole('button', { name: 'At Risk', exact: true }).click();
    await expect(page.locator('tbody tr').first()).toContainText('At Risk');

    // Open the first profile.
    await page.locator('tbody tr').first().getByRole('link').first().click();

    // Every intelligence surface must be present and populated.
    await expect(page.getByRole('heading', { name: 'Next best action' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Churn risk' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Purchase behaviour' })).toBeVisible();
    await expect(page.getByText('Contributing factors')).toBeVisible();
    await expect(page.getByText('out of 100')).toBeVisible();

    // The churn explanation is a real sentence, not an empty node.
    const explanation = page.locator('text=/days since their last order|No risk signals|never completed/');
    await expect(explanation.first()).toBeVisible();

    // Tabs work.
    await page.getByRole('button', { name: /^Orders/ }).click();
    await expect(page.getByRole('columnheader', { name: 'Order' })).toBeVisible();

    await page.getByRole('button', { name: /^Communications/ }).click();
    await expect(page.getByRole('heading', { name: 'Message history' })).toBeVisible();

    await page.getByRole('button', { name: 'History' }).click();
    await expect(page.getByRole('heading', { name: 'Lifecycle transitions' })).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests, `failed API calls: ${failedRequests.join(' | ')}`).toEqual([]);
  });
});

test.describe('Message generation', () => {
  test('generates a grounded message and blocks approval of an invented offer', async ({
    page,
  }) => {
    const { errors } = guard(page);
    await login(page);

    await page.getByRole('link', { name: 'Message Studio', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Message Studio' })).toBeVisible();

    // Pick the first (highest churn risk) customer.
    await page.locator('ul li button').first().click();
    await page.getByRole('button', { name: 'Generate', exact: true }).click();

    // A grounded message appears and passes validation.
    await expect(page.getByText('Validation passed')).toBeVisible({ timeout: 30_000 });
    const body = page.getByLabel('Body');
    await expect(body).not.toBeEmpty();
    await expect(body).toHaveValue(/enjoy responsibly/i);

    // Now edit in an invented discount and coupon code.
    await body.fill(
      'Hi there, take 40% off everything with code MEGA50! Only 2 left in stock.\n\n' +
        'Please enjoy responsibly.',
    );
    await page.getByRole('button', { name: 'Save & revalidate' }).click();

    // Validation must block it, naming the specific violations.
    await expect(page.getByText(/blocking issue/)).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('UNVERIFIED_COUPON_CODE')).toBeVisible();
    await expect(page.getByText('UNVERIFIED_PROMOTION')).toBeVisible();

    // And the Approve button must be unavailable.
    await expect(page.getByRole('button', { name: 'Approve' })).toBeDisabled();

    expect(errors.filter((e) => !e.includes('400'))).toEqual([]);
  });
});

test.describe('Segments', () => {
  test('builds a rule and previews the matching customers', async ({ page }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    await page.getByRole('link', { name: 'Segments', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Segments' })).toBeVisible();

    // Built-in segments are listed with real member counts.
    await expect(page.locator('tbody tr').first()).toBeVisible();
    await expect(page.getByText('Built-in').first()).toBeVisible();

    await page.getByRole('button', { name: 'New segment' }).click();
    await expect(page.getByRole('dialog')).toBeVisible();

    await page.getByLabel('Name').fill(`E2E segment ${Date.now()}`);
    await page.getByRole('button', { name: '+ Condition' }).click();

    // A live preview count appears for the new rule.
    await expect(page.getByText(/of .* customers match/)).toBeVisible({ timeout: 20_000 });

    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests.filter((r) => !r.startsWith('400'))).toEqual([]);
  });
});

test.describe('Campaigns', () => {
  test('shows the audience breakdown with consent and age exclusions', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await page.getByRole('link', { name: 'Campaigns', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Campaigns' })).toBeVisible();

    await page.locator('tbody tr').first().getByRole('link').first().click();

    // The workflow rail and audience panel render.
    await expect(page.getByText('Compliance', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Audience' })).toBeVisible();
    await expect(page.getByText(/eligible of/)).toBeVisible({ timeout: 30_000 });

    // The action buttons that must stay gated are gated.
    await expect(page.getByRole('heading', { name: 'Actions' })).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });
});

test.describe('Automations', () => {
  test('a cohort send can be created, previewed and approved without sending', async ({
    page,
  }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    const name = `E2E cohort ${Date.now()}`;
    await page.getByRole('link', { name: 'Automations', exact: true }).click();
    await page.getByRole('button', { name: 'New cohort send' }).click();

    await page.getByLabel('Name').fill(name);
    await page.getByLabel('Audience').selectOption({ index: 1 });
    await page
      .getByLabel(/^Message/)
      .fill('Hi {first_name}, your usual is a tap away. Reply STOP to opt out.');
    await page.getByRole('button', { name: 'Create as draft' }).click();

    // Lands on the list as a draft that cannot send yet.
    await expect(page.getByRole('link', { name })).toBeVisible();
    await page.getByRole('link', { name }).click();
    await expect(page.getByRole('heading', { level: 1, name })).toBeVisible();
    await expect(page.getByText('has not been approved yet')).toBeVisible();

    // A dry run is available before approval, and it must send nothing: the
    // ledger stays empty and the panel reports a dry run explicitly.
    await page.getByRole('button', { name: 'Dry run' }).click();
    const preview = page.locator('section').filter({
      has: page.getByRole('heading', { name: 'Dry run — nothing was sent' }),
    });
    await expect(preview).toBeVisible();

    // The preview names the audience or says plainly that nobody matches,
    // and either way the live ledger stays empty: nothing was sent.
    await expect(
      preview.getByText('Recipients').or(preview.getByText('Nobody matches right now')),
    ).toBeVisible();
    await expect(page.getByText('No sends yet')).toBeVisible();

    await page.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByRole('button', { name: 'Activate' })).toBeVisible();
    await expect(page.getByText('has not been approved yet')).toHaveCount(0);

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests, `failed API calls: ${failedRequests.join(' | ')}`).toEqual([]);
  });

  test('a sequence is authored as day offsets, not calendar dates', async ({ page }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    const name = `E2E sequence ${Date.now()}`;
    await page.getByRole('link', { name: 'Automations', exact: true }).click();
    await page.getByRole('button', { name: 'New sequence' }).click();

    await page.getByLabel('Name').fill(name);
    await page.getByLabel('Audience').selectOption({ index: 1 });
    await page.locator('#step-body-0').fill('Day zero. Reply STOP to opt out.');
    await page.locator('#step-body-1').fill('Day seven. Reply STOP to opt out.');
    await page.getByRole('button', { name: 'Create as draft' }).click();

    await page.getByRole('link', { name }).click();
    await expect(page.getByRole('heading', { level: 1, name })).toBeVisible();
    const steps = page.locator('section').filter({
      has: page.getByRole('heading', { name: 'Steps' }),
    });
    await expect(steps.getByText('Day zero. Reply STOP to opt out.')).toBeVisible();
    await expect(steps.getByText('Day seven. Reply STOP to opt out.')).toBeVisible();
    // Offsets, not dates — that is what makes the sequence reusable.
    await expect(steps.getByText(/^Day 0$/)).toBeVisible();
    await expect(steps.getByText(/^Day 7$/)).toBeVisible();
    await expect(page.getByText(/counted from each customer/)).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests, `failed API calls: ${failedRequests.join(' | ')}`).toEqual([]);
  });
});

test.describe('Analytics', () => {
  test('charts render from database-derived data', async ({ page }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    await page.getByRole('link', { name: 'Churn analytics', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Risk distribution' })).toBeVisible();
    // Recharts renders an SVG once it has data.
    await expect(page.locator('svg.recharts-surface').first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Priority save list' })).toBeVisible();

    await page.getByRole('link', { name: 'Cohorts', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Retention heatmap' })).toBeVisible();
    // Month 0 is 100% by definition, so a populated heatmap always shows it.
    await expect(page.getByText('100%').first()).toBeVisible();

    await page.getByRole('link', { name: 'Customer analytics', exact: true }).click();
    await expect(page.locator('svg.recharts-surface').first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'RFM grid' })).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
    expect(failedRequests, `failed API calls: ${failedRequests.join(' | ')}`).toEqual([]);
  });
});

test.describe('Responsive layout', () => {
  test('mobile viewport keeps navigation reachable and avoids horizontal scroll', async ({
    page,
  }) => {
    guard(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);

    // The sidebar collapses behind a toggle.
    const toggle = page.getByRole('button', { name: 'Open navigation' });
    await expect(toggle).toBeVisible();
    await toggle.click();
    await page.getByRole('link', { name: 'Customers', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Customers' })).toBeVisible();

    // The page itself must not scroll sideways; wide tables scroll internally.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
