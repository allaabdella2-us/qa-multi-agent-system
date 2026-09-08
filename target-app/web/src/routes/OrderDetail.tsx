import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import {
  ApiError,
  formatDate,
  formatMoney,
  getOrder,
  type Order,
  type OrderItem,
} from "../api";

function lineTotal(item: OrderItem): number {
  return item.quantity * item.unit_price_cents;
}

export default function OrderDetail() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  const justPlaced = Boolean((location.state as { placed?: boolean } | null)?.placed);

  const [order, setOrder] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const orderId = Number(id);
    if (!Number.isInteger(orderId) || orderId <= 0) {
      setOrder(null);
      setError("That order reference is not valid.");
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError(null);

    getOrder(orderId, controller.signal)
      .then((result) => {
        setOrder(result);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof ApiError && err.status === 404) {
          setError("We could not find that order.");
        } else if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Could not reach the server. Check your connection and try again.");
        }
        setOrder(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [id]);

  if (loading) {
    return (
      <section className="page">
        <p className="muted" role="status" data-testid="order-detail-loading">
          Loading order…
        </p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="page">
        <div className="page-header">
          <h1 className="page-title">Order</h1>
        </div>
        <p className="alert alert-error" role="alert" data-testid="order-detail-error">
          {error}
        </p>
        <Link className="link" to="/orders">
          Back to orders
        </Link>
      </section>
    );
  }

  if (!order) {
    return (
      <section className="page">
        <div className="page-header">
          <h1 className="page-title">Order</h1>
        </div>
        <div className="card empty-state" data-testid="order-detail-empty">
          <p>This order is no longer available.</p>
          <Link className="link" to="/orders">
            Back to orders
          </Link>
        </div>
      </section>
    );
  }

  const items = order.items ?? [];
  const currency = order.currency || "USD";

  return (
    <section className="page" data-testid="order-detail">
      <div className="page-header">
        <h1 className="page-title" data-testid="order-detail-reference">
          {order.reference}
        </h1>
        <span className={`badge badge-${order.status}`} data-testid="order-detail-status">
          {order.status}
        </span>
      </div>

      {justPlaced ? (
        <p className="alert alert-success" role="status" data-testid="order-placed-notice">
          Order placed.
        </p>
      ) : null}

      <div className="card">
        <dl className="summary">
          <div className="summary-row">
            <dt>Order id</dt>
            <dd>{order.id}</dd>
          </div>
          <div className="summary-row">
            <dt>Created</dt>
            <dd>{formatDate(order.created_at)}</dd>
          </div>
          <div className="summary-row">
            <dt>Total</dt>
            <dd data-testid="order-detail-total">
              {formatMoney(order.total_cents, currency)}
            </dd>
          </div>
        </dl>

        <h2 className="section-title">Items</h2>
        {items.length === 0 ? (
          <p className="muted" data-testid="order-detail-no-items">
            This order has no items.
          </p>
        ) : (
          <table className="table" data-testid="order-items-table">
            <thead>
              <tr>
                <th scope="col">SKU</th>
                <th scope="col">Description</th>
                <th scope="col">Qty</th>
                <th scope="col">Unit price</th>
                <th scope="col">Line total</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={`${item.sku}-${index}`} data-testid={`order-item-${index}`}>
                  <td>{item.sku}</td>
                  <td>{item.description}</td>
                  <td>{item.quantity}</td>
                  <td>{formatMoney(item.unit_price_cents, currency)}</td>
                  <td>{formatMoney(lineTotal(item), currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Link className="link" to="/orders" data-testid="order-detail-back">
        Back to orders
      </Link>
    </section>
  );
}
