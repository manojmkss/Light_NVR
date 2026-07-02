import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="page">
      <div className="empty-state">
        <h2>Page not found</h2>
        <Link to="/">Back to dashboard</Link>
      </div>
    </div>
  );
}
