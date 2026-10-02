import { expect, test, type Page } from '@playwright/test';

/**
 * AI Copilot, end to end in the browser, on the offline planner.
 *
 * Walks the acceptance conversation: plan a Smart Reorder campaign, confirm
 * it as a draft, re-split coupons, rewrite the copy, dry-run it, and check
 * that activation waits for its own explicit confirmation (it is cancelled
 * here so the run leaves nothing live).
 */

async function login(page: Page) {
  await page.goto('/login');
  await page.getByLabel('Email address').fill('admin@gimmedelivery.co.nz');
  await page.getByLabel('Password').fill('GimmeAdmin123!');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Retention overview' })).toBeVisible();
}

async function send(page: Page, text: string) {
  const input = page.locator('#copilot-input');
  await input.fill(text);
  await input.press('Enter');
  await expect(input).toBeEnabled({ timeout: 120_000 });
}

function lastCard(page: Page) {
  return page.locator('[data-testid^="action-card-"]').last();
}

async function confirmLast(page: Page) {
  await lastCard(page).getByRole('button', { name: /^Confirm/ }).click();
  await expect(lastCard(page).getByText('Done', { exact: true })).toBeVisible({ timeout: 60_000 });
}

test('copilot plans, confirms, edits, dry-runs and guards activation', async ({ page }) => {
  test.setTimeout(240_000);
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));

  await login(page);
  await page.getByRole('button', { name: 'AI Copilot' }).click();
  await expect(page.getByRole('dialog', { name: 'AI Copilot' })).toBeVisible();
  await page.getByRole('link', { name: 'Open full console' }).click();
  await expect(page).toHaveURL(/\/ai-copilot$/);
  await page.getByRole('button', { name: 'New conversation' }).click();

  const name = `E2E Beer Lapsed ${Date.now() % 1_000_000}`;
  await send(
    page,
    `Create a Smart Reorder campaign called ${name} for customers who ordered beer at least twice and haven't ` +
      'ordered in 14 days. Send SMS 30 minutes before their predicted reorder time. Use three coupon codes FIRST7, ' +
      'LUCKY7 and COMEAGAIN7 with equal allocation. Write a short cheeky message using #first_name#, #product# and #coupon_code#.',
  );
  await expect(lastCard(page).getByText('Action required')).toBeVisible();
  await expect(lastCard(page).getByText('Final audience')).toBeVisible();
  await expect(page.getByText('#first_name#', { exact: false }).first()).toBeVisible();
  await confirmLast(page);

  await send(page, 'Change the coupon split to 50%, 25%, 25%.');
  await confirmLast(page);
  await send(page, 'Change the message to be more Kiwi and dry.');
  await confirmLast(page);
  await send(page, 'Show me a dry run.');
  await expect(page.getByText(/DRY RUN/).last()).toBeVisible();

  await send(page, 'Activate it.');
  await expect(lastCard(page).getByText('High risk')).toBeVisible();
  await expect(lastCard(page).getByRole('button', { name: /Confirm — go live/ })).toBeVisible();
  await send(page, 'yes go ahead');
  await expect(page.getByText(/can't act on a typed approval/).last()).toBeVisible();
  await lastCard(page).getByRole('button', { name: 'Cancel' }).click();
  await expect(lastCard(page).getByText('Cancelled', { exact: true })).toBeVisible();

  expect(errors).toEqual([]);
});
