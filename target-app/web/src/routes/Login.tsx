import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import Button from "../components/Button";
import { ApiError, getToken, login, saveSession } from "../api";

export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (getToken()) {
    return <Navigate to="/orders" replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (!email.trim() || !password) {
      setError("Enter your email address and password.");
      return;
    }

    setSubmitting(true);
    try {
      const result = await login(email.trim(), password);
      saveSession(result.access_token, result.role, email.trim());
      navigate("/orders", { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("That email and password combination was not recognised.");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Could not reach the server. Check your connection and try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <form className="card login-card" onSubmit={handleSubmit} data-testid="login-form">
        <h1 className="login-title">Sign in to Corvid</h1>
        <p className="login-subtitle">Order and invoice management for your organization.</p>

        {error ? (
          <p className="alert alert-error" role="alert" data-testid="login-error">
            {error}
          </p>
        ) : null}

        <div className="field">
          <label className="field-label" htmlFor="login-email">
            Email address
          </label>
          <input
            id="login-email"
            className="field-input"
            type="email"
            name="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            data-testid="login-email"
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="login-password">
            Password
          </label>
          <input
            id="login-password"
            className="field-input"
            type="password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            data-testid="login-password"
          />
        </div>

        <Button
          type="submit"
          variant="primary"
          disabled={submitting}
          data-testid="login-submit"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
