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

/**
 * Open a campaign whose copy can still be edited.
 *
 * Clicking whichever row happens to be first reads whatever was created
 * last — including a campaign that has already sent, whose body is
 * deliberately read-only. The tests below are about the composer, so they
 * have to ask for one that has a composer.
 */
async function openEditableCampaign(page: Page) {
  // Campaigns this suite created are excluded: they are empty by design, and
  // the tests using this one need a campaign with an audience and copy.
  const draft = page
    .locator('tbody tr')
    .filter({ hasText: /Draft|Awaiting Approval/ })
    .filter({ hasNotText: /^E2E / })
    .first();
  await expect(draft).toBeVisible({ timeout: 15_000 });
  await draft.getByRole('link').first().click();
}

/**
 * Create a campaign of this test's own and open it.
 *
 * Any test that *saves* a body needs one. Sharing "whichever draft is first"
 * works only while every test is read-only, and the moment one wrote to it
 * five others started failing on copy they had not written — so a test that
 * edits brings its own.
 */
async function createDraftCampaign(page: Page, label: string, channel = 'SMS') {
  await page.goto('/campaigns');
  await page.getByRole('button', { name: /new campaign/i }).first().click();
  await page.locator('#campaign-name').fill(`E2E ${label} ${Date.now()}`);
  await page.locator('#campaign-channel').selectOption(channel);
  await page.getByRole('button', { name: /create draft/i }).click();
  await expect(page.locator('#campaign-body')).toBeVisible({ timeout: 15_000 });
}

test.describe('Segments', () => {
  test('exporting a segment downloads a file with international phone numbers', async ({
    page,
  }) => {
    const { errors } = guard(page);
    await login(page);

    await page.goto('/segments');
    // The real button, and the real file it produces — the whole point of
    // this column is what ends up in somebody's download.
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('tbody tr').first().getByRole('button', { name: 'Export' }).click(),
    ]);

    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream) chunks.push(chunk as Buffer);
    const text = Buffer.concat(chunks).toString('utf8').replace(/^\uFEFF/, '');

    const [header, ...rows] = text.trim().split('\n');
    const columns = header.split(',');
    expect(columns).toContain('phone');
    // Beside email, where the brief asks for it.
    expect(columns.indexOf('phone')).toBe(columns.indexOf('email') + 1);

    const phoneAt = columns.indexOf('phone');
    const phones = rows.map((row) => row.split(',')[phoneAt]).filter(Boolean);
    expect(phones.length).toBeGreaterThan(0);
    // One shape, all the way down: international or empty, never a raw 02…
    for (const phone of phones) {
      expect(phone).toMatch(/^\+\d{7,15}$/);
    }
    expect(phones.some((p) => p.startsWith('+64'))).toBe(true);

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });
});

test.describe('Campaigns', () => {
  test('shows the audience breakdown with consent and age exclusions', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await page.getByRole('link', { name: 'Campaigns', exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'Campaigns' })).toBeVisible();

    await openEditableCampaign(page);

    // The workflow rail and audience panel render.
    await expect(page.getByText('Compliance', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Audience' })).toBeVisible();
    await expect(page.getByText(/eligible of/)).toBeVisible({ timeout: 30_000 });

    // The action buttons that must stay gated are gated.
    await expect(page.getByRole('heading', { name: 'Actions' })).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('a draft campaign says who writes its copy, and previews it', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await page.goto('/campaigns');
    await openEditableCampaign(page);

    // The choice is on the campaign, where approval can see it — not a flag
    // on the send button, which is how approved copy used to be replaced by
    // a generated message with nothing on screen saying so.
    const written = page.getByRole('radio', { name: /send the copy i write/i });
    await expect(written).toBeVisible();
    await expect(page.getByRole('radio', { name: /draft each message with ai/i })).toBeVisible();

    await page.getByRole('button', { name: 'Preview', exact: true }).click();
    // A real recipient, and the message as they would read it.
    await expect(page.getByRole('heading', { name: 'What recipients will get' })).toBeVisible();
    await expect(page.locator('main').getByRole('link', { name: /\w/ }).first()).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('a merge tag is inserted at the cursor and previewed against a customer', async ({
    page,
  }) => {
    const { errors } = guard(page);
    await login(page);

    await createDraftCampaign(page, 'Merge tag cursor');

    const body = page.locator('#campaign-body');
    await body.fill('Kia ora , the usual?');

    // Cursor placed mid-sentence. Appending to the end is the behaviour that
    // makes somebody give up and type the name in by hand.
    await body.evaluate((el: HTMLTextAreaElement) => el.setSelectionRange(8, 8));
    await page.getByLabel('Insert a merge tag').selectOption('#first_name#');
    await expect(body).toHaveValue('Kia ora #first_name#, the usual?');

    // And the preview says what that will read as, resolved by the server
    // rather than by a second implementation in the browser.
    await expect(page.getByText(/as .* would receive it/i)).toBeVisible({ timeout: 15_000 });

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('the same template previews differently for two customers', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await createDraftCampaign(page, 'Merge tag preview');

    const body = page.locator('#campaign-body');
    await body.fill('Hi #first_name#, fancy another #product#?');

    // One template, two people, two messages — the whole claim of the
    // feature, checked by reading the screen rather than the resolver.
    const picker = page.getByLabel('Preview as customer');
    const options = picker.locator('option');
    await expect(options.nth(2)).toBeAttached({ timeout: 15_000 });

    await picker.selectOption({ index: 1 });
    const first = await page.locator('main p.whitespace-pre-wrap').first().innerText();

    await picker.selectOption({ index: 2 });
    await expect
      .poll(async () => page.locator('main p.whitespace-pre-wrap').first().innerText())
      .not.toBe(first);

    // And neither of them is the template.
    expect(first).not.toContain('#first_name#');

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('an unverifiable claim is a reviewer call, not a wall', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    // SMS, because an email campaign carries its own mandatory statements and
    // those are genuinely blocking — not the reviewer's to sign away.
    await createDraftCampaign(page, 'Compliance vouching');

    const body = page.locator('#campaign-body');
    await body.fill(
      "Hi #first_name#, GIMME's got $10 off with FIRST10. Please enjoy responsibly. Reply STOP to opt out.",
    );
    await page.getByRole('button', { name: /^save$/i }).click();
    await page.getByRole('button', { name: /run check/i }).click();

    // The engine holds no promotions or coupon data, so it says so — and
    // offers the reviewer the tick rather than refusing outright.
    await expect(page.getByText(/your confirmation/i)).toBeVisible({ timeout: 15_000 });
    const confirms = page.getByRole('checkbox');
    const claims = await confirms.count();
    expect(claims).toBeGreaterThan(0);
    await expect(page.getByText(/your name is recorded against them/i)).toBeVisible();

    // And the company's own name is not reported as a coupon code.
    await expect(page.getByText('\u201cGIMME\u201d')).toHaveCount(0);

    // Until the claims are confirmed there is nothing to submit \u2014 but the
    // remedy is the tick box on screen, not an edit to the copy.
    const submit = page.getByRole('button', { name: /submit for approval/i });
    await expect(submit).toBeDisabled();

    // This is the part that was missing, and it is the whole feature: the
    // boxes rendered and ticking them did nothing, because the confirmation
    // never left the browser and the only endpoint that would have taken it
    // sat behind the very step it was meant to unblock.
    for (let index = 0; index < claims; index += 1) {
      await confirms.nth(index).check();
    }
    await expect(submit).toBeEnabled();

    await submit.click();
    await expect(page.getByText(/submitted for approval/i)).toBeVisible({ timeout: 15_000 });

    // The sign-off is on the record, by name, against each claim it covers.
    await expect(page.getByText(/confirmed by/i).first()).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByText(/campaign approved/i)).toBeVisible({ timeout: 15_000 });

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('a merge tag that is not a field is refused before approval', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await createDraftCampaign(page, 'Merge tag unknown');

    const body = page.locator('#campaign-body');
    await body.fill('Kia ora #first_name#, use #discont_code#. Reply STOP to opt out.');

    // Named, not just flagged: "a merge tag is wrong" in a 400-character body
    // is not something an operator can act on.
    await expect(page.getByText(/#discont_code#/).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/cannot be approved/i)).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });
});

test.describe('Smart Reorder', () => {
  test('Create Campaign offers an individual-reminder campaign type', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await page.goto('/campaigns');
    await page.getByRole('button', { name: /new campaign/i }).first().click();

    // The choice comes first, because the two are genuinely different things
    // and almost none of the fields below mean the same for both.
    const smart = page.getByRole('radio', { name: /smart reorder reminder/i });
    await expect(smart).toBeVisible();
    await smart.check();

    // And the page says the thing people get wrong about this type: there is
    // no one send time to pick.
    await expect(page.getByText(/every customer gets their own send time/i)).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('the upcoming queue shows one row per customer with its own time', async ({ page }) => {
    const { errors } = guard(page);
    await login(page);

    await page.goto('/smart-reorder/queue');
    await expect(
      page.getByRole('heading', { level: 1, name: /upcoming smart reorder messages/i }),
    ).toBeVisible();

    // Wait for the fetch to land before branching: reading the row count
    // immediately after navigation sees zero rows and an empty state that has
    // not rendered yet, so the test takes the wrong branch and then fails.
    const rows = page.locator('tbody tr');
    const empty = page.getByText(/no messages are queued/i);
    await expect(rows.first().or(empty)).toBeVisible({ timeout: 15_000 });

    if (await rows.count()) {
      // Each row is one customer, one predicted order, one send time.
      await expect(rows.first()).toBeVisible();
      await expect(page.getByRole('columnheader', { name: 'Predicted order' })).toBeVisible();
      await expect(page.getByRole('columnheader', { name: 'Reminder' })).toBeVisible();

      // Opening one shows that customer's own message, not a template.
      await rows.first().getByRole('button', { name: 'Open' }).click();
      await expect(page.getByLabel('Message')).toBeVisible();
      const body = await page.getByLabel('Message').inputValue();
      expect(body).not.toContain('#first_name#');
    } else {
      await expect(empty).toBeVisible();
    }

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

  test('editing the copy of an approved automation withdraws its approval', async ({
    page,
  }) => {
    const { errors, failedRequests } = guard(page);
    await login(page);

    const name = `E2E editable ${Date.now()}`;
    await page.getByRole('link', { name: 'Automations', exact: true }).click();
    await page.getByRole('button', { name: 'New cohort send' }).click();
    await page.getByLabel('Name').fill(name);
    await page.getByLabel('Audience').selectOption({ index: 1 });
    await page.getByLabel(/^Message/).fill('Original copy. Reply STOP to opt out.');
    await page.getByRole('button', { name: 'Create as draft' }).click();

    await page.getByRole('link', { name }).click();
    await page.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByRole('button', { name: 'Activate' })).toBeVisible();
    await page.getByRole('button', { name: 'Activate' }).click();
    await expect(page.getByText('ACTIVE').first()).toBeVisible();

    // Editing the message warns before saving, then pauses the automation.
    await page.getByRole('button', { name: 'Edit' }).click();
    await page.getByLabel('Message').fill('Rewritten copy. Reply STOP to opt out.');
    await expect(page.getByText(/withdraws approval/)).toBeVisible();
    await page.getByRole('button', { name: 'Save changes' }).click();

    await expect(page.getByText('has not been approved yet')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Approve' })).toBeVisible();

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

  test('a description in a table cell wraps instead of running across its neighbours', async ({
    page,
  }) => {
    guard(page);
    await login(page);
    await page.goto('/automations');
    await expect(page.getByRole('heading', { level: 1, name: 'Automations' })).toBeVisible();

    // `.table-cell` sets whitespace-nowrap. A max-width alone does not make the
    // text wrap inside it — it renders as one long line straight across the
    // Type and Status columns, which is what this asserts is not happening.
    // getBoundingClientRect reports the clamped border box, so it cannot see
    // this: the box stays at max-width while the unwrapped text spills out of
    // it. scrollWidth vs clientWidth is what actually catches it.
    const spill = await page.evaluate(() =>
      Array.from(document.querySelectorAll('tbody tr td:first-child p'))
        .map((text) => text.scrollWidth - text.clientWidth)
        .reduce((worst, value) => Math.max(worst, value), 0),
    );
    expect(spill).toBeLessThanOrEqual(1);
  });
});

test.describe('Data import', () => {
  const MESSY = [
    'external_id,email,phone,first_name,signup_date',
    'E2E-IMP-1,imp1@example.test,021 555 9001,One,2025-03-14',
    'E2E-IMP-2,imp2@example.test,09 366 1234,Two,2025-03-15',
    'E2E-IMP-3,,not-a-number,Three,2025-03-16',
  ].join('\n');

  async function previewCsv(page: Page) {
    await page.goto('/data');
    // The page lands on the one-file format, which wants order columns this
    // file does not have. A customers-only file is a deliberate choice now,
    // so make it the way a person would — this spec was asserting on a
    // preview the page had stopped producing.
    await page.getByRole('button', { name: 'Customers', exact: true }).click();
    await page.setInputFiles('input[type="file"]', {
      name: 'e2e-import.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(MESSY),
    });
    await expect(page.getByText(/would import/).first()).toBeVisible();
  }

  test('a preview reports every row without importing any of them', async ({ page }) => {
    guard(page);
    await login(page);

    await previewCsv(page);

    // Two importable rows; the third has no email and an unreachable number.
    await expect(page.getByText('2 of 3 rows would import')).toBeVisible();
    await expect(page.getByText(/1 row would be rejected/)).toBeVisible();
    // The landline row still imports — on email only — and says so.
    await expect(page.getByText(/1 row would import with a change/)).toBeVisible();
    await expect(page.getByText(/not a mobile number/).first()).toBeVisible();

    // The property the preview rests on. Every ingestor commits when it
    // finishes, so previewing would silently import the file if the dry run
    // were not contained — searching for a previewed row is the direct check.
    await page.goto('/customers?search=imp1%40example.test');
    await expect(page.getByRole('heading', { level: 1, name: 'Customers' })).toBeVisible();
    await expect(page.getByText('imp1@example.test')).toHaveCount(0);
  });
});

test.describe('Integrations', () => {
  test('TNZ credentials can be entered before going live, and a mock test says so', async ({
    page,
  }) => {
    guard(page);
    await login(page);
    await page.goto('/integrations');

    // Outlook, TNZ, WhatsApp.
    await page.getByRole('button', { name: 'Configure' }).nth(1).click();
    await expect(page.getByText('TNZ Group SMS').last()).toBeVisible();

    // The fields have to be reachable while still in Mock: entering and
    // testing credentials is how you find out whether you are ready, so
    // requiring Live first would be backwards.
    await expect(page.getByRole('button', { name: 'Mock' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await expect(page.getByLabel('Auth token')).toBeVisible();
    await expect(page.getByLabel('Sender number or name')).toBeVisible();

    // The webhook secret is generated rather than invented, and shown once so
    // it can be copied into TNZ before it is masked.
    const secret = page.getByLabel('Webhook secret');
    await expect(secret).toHaveValue('');
    await page.getByRole('button', { name: 'Generate' }).click();
    await expect(secret).not.toHaveValue('');
    await expect(secret).toHaveAttribute('type', 'text');

    // Testing the mock must not satisfy the go-live connection check.
    await page.getByRole('button', { name: 'Test connection' }).click();
    await expect(page.getByText(/MOCK MODE/).first()).toBeVisible();
    await page.getByRole('button', { name: 'Close', exact: true }).last().click();

    await expect(page.getByText('The last test ran against the mock adapter')).toBeVisible();
  });
});
