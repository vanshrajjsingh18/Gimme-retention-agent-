import { Link } from 'react-router-dom';

import { EmptyState } from '../components/ui';

export default function NotFoundPage() {
  return (
    <div className="card">
      <EmptyState
        title="Page not found"
        description="That page does not exist in the GIMME Retention Engine."
        action={
          <Link to="/" className="btn-primary">
            Back to overview
          </Link>
        }
      />
    </div>
  );
}
