import React from "react";
import ReactDOM from "react-dom/client";
import {
  BrowserRouter,
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
} from "react-router-dom";
import Layout from "./components/Layout";
import Login from "./routes/Login";
import OrdersList from "./routes/OrdersList";
import NewOrder from "./routes/NewOrder";
import CheckoutReview from "./routes/CheckoutReview";
import OrderDetail from "./routes/OrderDetail";
import { getToken } from "./api";
import "./styles.css";

if (import.meta.env.DEV) {
  console.warn(
    "Corvid web is running against the seeded development dataset. Orders you create here are not real.",
  );
}

function RequireAuth() {
  if (!getToken()) {
    return <Navigate to="/login" replace />;
  }

  return (
    <Layout>
      <Outlet />
    </Layout>
  );
}

function NotFound() {
  return (
    <section className="page">
      <div className="page-header">
        <h1 className="page-title">Page not found</h1>
      </div>
      <p className="muted">That page does not exist.</p>
      <Link className="link" to="/orders">
        Back to orders
      </Link>
    </section>
  );
}

function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route path="/" element={<Navigate to="/orders" replace />} />
        <Route path="/orders" element={<OrdersList />} />
        <Route path="/orders/new" element={<NewOrder />} />
        <Route path="/orders/:id" element={<OrderDetail />} />
        <Route path="/checkout/review" element={<CheckoutReview />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
