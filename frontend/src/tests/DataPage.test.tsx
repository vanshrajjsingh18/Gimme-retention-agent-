import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import DataPage from '../pages/DataPage';

const downloadFile = vi.fn().mockResolvedValue(undefined);

vi.mock('../api/client', () => ({
  downloadFile: (path: string, filename: string) => downloadFile(path, filename),
  api: {
    url: (path: string) => path,
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue({}),
    upload: vi.fn().mockResolvedValue({}),
    baseUrl: '',
  },
  ApiError: class extends Error {},
}));

vi.mock('../hooks/useApi', () => ({
  useQuery: () => ({ data: [], loading: false, error: null, refetch: vi.fn() }),
  useMutation: (fn: (...args: unknown[]) => unknown) => ({
    run: fn,
    loading: false,
    error: null,
  }),
}));

/**
 * Which template the page hands over before anybody touches it.
 *
 * The combined file is the way in — one row per order line, customers, orders
 * and lines together. The page offered it in the list while still defaulting
 * to the customers-only template, so the download button kept producing the
 * wrong file and the one-file format looked like it had never shipped. The
 * default is read off the list now, and this is what says so.
 */
describe('Data & imports', () => {
  beforeEach(() => {
    downloadFile.mockClear();
  });

  it('offers the one-file template before anything is chosen', () => {
    render(<DataPage />);
    expect(
      screen.getByRole('button', { name: /download everything in one file template/i }),
    ).toBeInTheDocument();
  });

  it('downloads the combined template, not the customers one', async () => {
    render(<DataPage />);
    await userEvent.click(
      screen.getByRole('button', { name: /download everything in one file template/i }),
    );
    expect(downloadFile).toHaveBeenCalledWith(
      '/api/v1/uploads/templates/combined.csv',
      'combined-template.csv',
    );
  });

  it('still lets a single entity be picked', async () => {
    render(<DataPage />);
    await userEvent.click(screen.getByRole('button', { name: 'Customers' }));
    await userEvent.click(
      screen.getByRole('button', { name: /download customers template/i }),
    );
    expect(downloadFile).toHaveBeenCalledWith(
      '/api/v1/uploads/templates/customers.csv',
      'customers-template.csv',
    );
  });
});
