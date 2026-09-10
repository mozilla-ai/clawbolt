import { render, screen, waitFor } from '@testing-library/react';
import { LLMModelField } from './llm-picker';

// The model field is the surface an operator picks a model from once an
// endpoint is selected. Its branches decide between a usable dropdown and a
// text box, and getting that wrong is silent: the form still submits.

vi.mock('./admin-api', () => ({
  listEndpointModels: vi.fn(),
  listProviders: vi.fn().mockResolvedValue([]),
  listProviderModels: vi.fn(),
  invalidateProviderModels: vi.fn(),
  listLLMEndpoints: vi.fn().mockResolvedValue([]),
}));

const noop = () => {};

beforeEach(async () => {
  const api = await import('./admin-api');
  vi.mocked(api.listEndpointModels).mockReset();
});

describe('LLMModelField with an endpoint selected', () => {
  it('offers what the endpoint says it serves', async () => {
    const api = await import('./admin-api');
    vi.mocked(api.listEndpointModels).mockResolvedValue({
      provider: 'anthropic',
      models: ['gw-sonnet', 'gw-haiku'],
      supports_listing: true,
      error: null,
    });
    render(<LLMModelField endpoint="otari" provider="" value="" onChange={noop} />);

    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());
    expect(
      Array.from(screen.getByRole('combobox').querySelectorAll('option')).map(o => o.textContent),
    ).toEqual(['gw-sonnet', 'gw-haiku']);
  });

  it('falls back to typing when the endpoint cannot be asked', async () => {
    // A dropdown with nothing in it would be a dead end, so this is the one
    // case where free text is the right answer rather than a cop-out.
    const api = await import('./admin-api');
    vi.mocked(api.listEndpointModels).mockResolvedValue({
      provider: 'anthropic',
      models: [],
      supports_listing: false,
      error: 'no listing here',
    });
    render(<LLMModelField endpoint="otari" provider="" value="" onChange={noop} />);

    await waitFor(() => expect(screen.getByPlaceholderText('model id')).toBeInTheDocument());
    expect(screen.getByText(/no listing here/)).toBeInTheDocument();
  });

  it('keeps a saved model the endpoint no longer lists', async () => {
    // Dropping it would silently rewrite a working config on first render.
    const api = await import('./admin-api');
    vi.mocked(api.listEndpointModels).mockResolvedValue({
      provider: 'anthropic',
      models: ['gw-sonnet'],
      supports_listing: true,
      error: null,
    });
    render(<LLMModelField endpoint="otari" provider="" value="retired-model" onChange={noop} />);

    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());
    expect(screen.getByText(/retired-model \(saved, not in list\)/)).toBeInTheDocument();
  });

  it('ignores an answer that arrives after the endpoint changed', async () => {
    // The slow first response must not fill the dropdown under the second
    // endpoint's label, which would offer models it does not serve.
    const api = await import('./admin-api');
    let releaseFirst: (v: never) => void = () => {};
    vi.mocked(api.listEndpointModels)
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            releaseFirst = resolve as never;
          }),
      )
      .mockResolvedValueOnce({
        provider: 'anthropic',
        models: ['second-model'],
        supports_listing: true,
        error: null,
      });

    const { rerender } = render(
      <LLMModelField endpoint="first" provider="" value="" onChange={noop} />,
    );
    rerender(<LLMModelField endpoint="second" provider="" value="" onChange={noop} />);
    await waitFor(() => expect(screen.getByText('second-model')).toBeInTheDocument());

    releaseFirst({
      provider: 'anthropic',
      models: ['first-model'],
      supports_listing: true,
      error: null,
    } as never);

    await waitFor(() => expect(screen.getByText('second-model')).toBeInTheDocument());
    expect(screen.queryByText('first-model')).not.toBeInTheDocument();
  });
});
