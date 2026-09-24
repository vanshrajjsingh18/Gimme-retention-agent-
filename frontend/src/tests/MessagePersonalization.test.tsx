import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import MessagePersonalization from '../features/MessagePersonalization';

const post = vi.fn();

vi.mock('../api/client', () => ({
  api: { post: (path: string, body: unknown) => post(path, body) },
  ApiError: class extends Error {},
}));

const FIELDS = [
  {
    token: 'first_name',
    label: 'First name',
    group: 'Customer',
    example: 'Sarah',
    fallback: 'there',
    description: '',
    tag: '#first_name#',
  },
  {
    token: 'brand',
    label: 'Favourite brand',
    group: 'Ordering',
    example: 'Steinlager',
    fallback: 'your favourites',
    description: '',
    tag: '#brand#',
  },
];

vi.mock('../hooks/useApi', () => ({
  // Pass-through: the debounce exists to spare the server, not to make tests
  // wait out a timer.
  useDebounced: <T,>(value: T) => value,
  useQuery: () => ({
    data: {
      fields: FIELDS,
      groups: ['Customer', 'Ordering'],
      syntax: '#field_name#',
      sample_customers: [{ id: 7, name: 'Aroha Ngata' }],
    },
    loading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

/** The composer, reduced to the two pieces that have to cooperate. */
function Composer({ initial = '' }: { initial?: string }) {
  const [body, setBody] = useState(initial);
  const ref = useRef<HTMLTextAreaElement | null>(null);
  return (
    <>
      <textarea aria-label="Body" ref={ref} value={body} onChange={(e) => setBody(e.target.value)} />
      <MessagePersonalization value={body} onChange={setBody} textareaRef={ref} />
    </>
  );
}

/**
 * Inserting a merge tag, and seeing what it will say.
 *
 * The insert landing at the cursor is the whole difference between a feature
 * somebody uses and one they work around by typing the customer's name in by
 * hand: appending to the end means every tag has to be cut and re-pasted into
 * the sentence it belongs in.
 */
describe('Message personalization', () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({
      text: 'Kia ora Aroha, welcome back',
      template: '',
      missing_fields: [],
      fallbacks_used: [],
      unknown_tags: [],
      customer_id: 7,
      customer_name: 'Aroha Ngata',
      characters: 27,
    });
  });

  it('inserts the tag where the cursor is, not at the end', async () => {
    render(<Composer initial="Kia ora , welcome back" />);
    const body = screen.getByLabelText('Body') as HTMLTextAreaElement;
    body.setSelectionRange(8, 8); // just before the comma

    await userEvent.selectOptions(
      screen.getByLabelText('Insert a merge tag'),
      '#first_name#',
    );

    expect(body).toHaveValue('Kia ora #first_name#, welcome back');
  });

  it('replaces the selection rather than pushing it aside', async () => {
    render(<Composer initial="Kia ora NAME, welcome back" />);
    const body = screen.getByLabelText('Body') as HTMLTextAreaElement;
    body.setSelectionRange(8, 12); // "NAME"

    await userEvent.click(screen.getByRole('button', { name: '#first_name#' }));

    expect(body).toHaveValue('Kia ora #first_name#, welcome back');
  });

  it('leaves the cursor after what it inserted, ready to keep typing', async () => {
    render(<Composer initial="Kia ora , welcome back" />);
    const body = screen.getByLabelText('Body') as HTMLTextAreaElement;
    body.setSelectionRange(8, 8);

    await userEvent.click(screen.getByRole('button', { name: '#first_name#' }));

    await waitFor(() => expect(body.selectionStart).toBe(8 + '#first_name#'.length));
  });

  it('offers every field, grouped as somebody reaches for them', () => {
    render(<Composer />);
    const menu = screen.getByLabelText('Insert a merge tag');
    expect(menu).toHaveTextContent('First name');
    expect(menu).toHaveTextContent('Favourite brand');
    expect(menu.querySelectorAll('optgroup')).toHaveLength(2);
  });

  it('shows what the message will say, resolved by the server', async () => {
    render(<Composer initial="Kia ora #first_name#, welcome back" />);

    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][0]).toBe('/api/v1/message-fields/preview');
    expect(post.mock.calls[0][1]).toMatchObject({
      template: 'Kia ora #first_name#, welcome back',
    });
    expect(await screen.findByText('Kia ora Aroha, welcome back')).toBeInTheDocument();
  });

  it('previews against the customer the operator picked', async () => {
    render(<Composer initial="Kia ora #first_name#" />);
    await waitFor(() => expect(post).toHaveBeenCalled());

    await userEvent.selectOptions(screen.getByLabelText('Preview as customer'), '7');

    await waitFor(() =>
      expect(post.mock.calls.at(-1)![1]).toMatchObject({ customer_id: 7 }),
    );
  });

  it('says plainly that an unknown tag blocks the campaign', async () => {
    post.mockResolvedValue({
      text: 'Kia ora Aroha, use #discont_code#',
      template: '',
      missing_fields: [],
      fallbacks_used: [],
      unknown_tags: ['discont_code'],
      customer_id: null,
      customer_name: 'Sample customer',
      characters: 33,
    });
    render(<Composer initial="Kia ora #first_name#, use #discont_code#" />);

    expect(await screen.findByText(/cannot be approved/i)).toHaveTextContent('#discont_code#');
  });

  it('says when a fallback stood in for a missing detail', async () => {
    post.mockResolvedValue({
      text: 'Kia ora there',
      template: '',
      missing_fields: ['first_name'],
      fallbacks_used: ['first_name'],
      unknown_tags: [],
      customer_id: 7,
      customer_name: 'Aroha Ngata',
      characters: 13,
    });
    render(<Composer initial="Kia ora #first_name#" />);

    expect(await screen.findByText(/a fallback was used/i)).toBeInTheDocument();
  });

  it('does not preview an empty message', () => {
    render(<Composer />);
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByText(/write a message above/i)).toBeInTheDocument();
  });
});
