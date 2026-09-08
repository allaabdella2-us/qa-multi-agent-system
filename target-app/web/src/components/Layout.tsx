import type { ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { clearSession, getSession } from "../api";
import Button from "./Button";

interface LayoutProps {
  children: ReactNode;
}

export default function Layout({ children }: LayoutProps) {
  const navigate = useNavigate();
  const session = getSession();

  function handleSignOut() {
    clearSession();
    navigate("/login", { replace: true });
  }

  const navClass = ({ isActive }: { isActive: boolean }) =>
    isActive ? "nav-link nav-link-active" : "nav-link";

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-inner">
          <span className="app-brand">Corvid Orders</span>
          <nav className="app-nav" aria-label="Main">
            <NavLink to="/orders" className={navClass} data-testid="nav-orders">
              Orders
            </NavLink>
            <NavLink to="/orders/new" className={navClass} data-testid="nav-new-order">
              New order
            </NavLink>
          </nav>
          <div className="app-account">
            {session?.email ? (
              <span className="app-account-email" data-testid="session-email">
                {session.email}
                {session.role ? <span className="app-account-role">{session.role}</span> : null}
              </span>
            ) : null}
            <Button variant="secondary" onClick={handleSignOut} data-testid="sign-out-button">
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="app-main" id="main-content">
        {children}
      </main>
    </div>
  );
}
