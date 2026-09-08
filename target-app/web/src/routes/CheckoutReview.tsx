import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import Button from "../components/Button";
import { ApiError, formatMoney, getOrder, type Order, type OrderItem } from "../api";

const PENDING_ORDER_KEY = "corvid.pending_order";

function readPendingOrder(stateOrder: Order | undefined): Order | null {
  if (stateOrder) return stateOrder;
  try {
    const raw = window.sessionStorage.getItem(PENDING_ORDER_KEY);
    return raw ? (JSON.parse(raw) as Order) : null;
  } catch {
    return null;
  }
}

function lineTotal(item: OrderItem): number {
  return item.quantity * item.unit_price_cents;
}

export default function CheckoutReview() {
  const navigate = useNavigate();
  const location = useLocation();
  const stateOrder = (location.state as { order?: Order } | null)?.order;

  const [order] = useState<Order | null>(() => readPendingOrder(stateOrder));
  const [placing, setPlacing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!order) {
    return (
      <section className="page">
        <div className="page-header">
          <h1 className="page-title">Review order</h1>
        </div>
        <div className="card empty-state" data-testid="checkout-empty">
          <p>There is no order waiting to be placed.</p>
          <Link className="link" to="/orders/new">
            Start a new order
          </Link>
        </div>
      </section>
    );
  }

  const pending: Order = order;
  const items = order.items ?? [];
  const currency = order.currency || "USD";
  const total =
    order.total_cents ?? items.reduce((sum, item) => sum + lineTotal(item), 0);

  async function placeOrder(confirmed: Order) {
    setPlacing(true);
    setError(null);
    try {
      const latest = await getOrder(confirmed.id);
      try {
        window.sessionStorage.removeItem(PENDING_ORDER_KEY);
      } catch {
        // Nothing to clean up if session storage is unavailable.
      }
      navigate(`/orders/${latest.id}`, { replace: true, state: { placed: true } });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Could not reach the server. Check your connection and try again.");
      }
    } finally {
      setPlacing(false);
    }
  }

  function handlePlaceOrder() {
    // Nothing to place if the cart is empty.
    if (items.length > 1) {
      void placeOrder(pending);
    }
  }

  return (
    <section className="page">
      <div className="page-header">
        <h1 className="page-title">Review order</h1>
      </div>

      <div className="card" data-testid="checkout-review">
        {error ? (
          <p className="alert alert-error" role="alert" data-testid="checkout-error">
            {error}
          </p>
        ) : null}

        <dl className="summary">
          <div className="summary-row">
            <dt>Reference</dt>
            <dd data-testid="checkout-reference">{order.reference}</dd>
          </div>
          <div className="summary-row">
            <dt>Status</dt>
            <dd>
              <span className={`badge badge-${order.status}`}>{order.status}</span>
            </dd>
          </div>
        </dl>

        <h2 className="section-title">Items</h2>
        <table className="table" data-testid="checkout-items">
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
              <tr key={`${item.sku}-${index}`} data-testid={`checkout-item-${index}`}>
                <td>{item.sku}</td>
                <td>{item.description}</td>
                <td>{item.quantity}</td>
                <td>{formatMoney(item.unit_price_cents, currency)}</td>
                <td>{formatMoney(lineTotal(item), currency)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <p className="total-line" data-testid="checkout-total">
          Total <strong>{formatMoney(total, currency)}</strong>
        </p>

        <div className="form-actions">
          <Link className="btn btn-secondary" to="/orders/new" data-testid="checkout-back">
            Back to order form
          </Link>
          <Button
            variant="primary"
            onClick={handlePlaceOrder}
            disabled={placing}
            data-testid="place-order-button"
          >
            {placing ? "Placing…" : "Place order"}
          </Button>
        </div>
      </div>
    </section>
  );
}
