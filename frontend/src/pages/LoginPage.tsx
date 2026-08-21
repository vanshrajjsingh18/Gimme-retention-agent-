import { useState } from 'react';

import { ApiError } from '../api/client';
import { Spinner } from '../components/ui';
import { useAuth } from '../hooks/useAuth';

export default function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState('admin@gimmedelivery.co.nz');
  const [password, setPassword] = useState('GimmeAdmin123!');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError('Enter your email address and password.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : 'Could not sign in. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-base font-bold text-white">
            G
          </div>
          <div>
            <p className="text-base font-semibold text-slate-900">GIMME</p>
            <p className="text-xs text-slate-500">Retention Engine</p>
          </div>
        </div>

        <form onSubmit={onSubmit} className="card px-6 py-6" noValidate>
          <h1 className="text-lg font-semibold text-slate-900">Sign in</h1>
          <p className="mt-1 text-sm text-slate-500">
            Use your GIMME retention team account.
          </p>

          {error && (
            <div
              role="alert"
              className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
            >
              {error}
            </div>
          )}

          <div className="mt-5">
            <label className="label" htmlFor="email">
              Email address
            </label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              className="input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>

          <div className="mt-4">
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          <button type="submit" className="btn-primary mt-6 w-full" disabled={busy}>
            {busy && <Spinner className="h-4 w-4 text-white" />}
            {busy ? 'Signing in…' : 'Sign in'}
          </button>

          <p className="mt-5 border-t border-slate-200 pt-4 text-xs text-slate-500">
            <span className="font-medium text-slate-600">Local development credentials</span> are
            pre-filled. Change <code className="font-mono">ADMIN_PASSWORD</code> in your{' '}
            <code className="font-mono">.env</code> before using this anywhere real.
          </p>
        </form>
      </div>
    </div>
  );
}
