import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import SearchInput from "../components/SearchInput";
import {
  ORDER_STATUSES,
  formatDate,
  formatMoney,
  listOrders,
  type Order,
  type OrderStatus,
} from "../api";

export default function OrdersList() {
  const navigate = useNavigate();
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<OrderStatus | "">("");

  const load = useCallback(() => {
    setLoading(true);
    listOrders({ status })
      .then((page) => setOrders(page.items))
      .finally(() => setLoading(false));
  }, [status]);

  useEffect(() => {
    load();
  }, [load]);

  // Order status changes out of band (payments, cancellations from other
  // sessions), so bring the list up to date whenever the tab regains focus.
  useEffect(() => {
    window.addEventListener("focus", load);
    return () => {
      window.removeEventListener("focus", load);
    };
  }, [load]);

  const visibleOrders = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return orders;
    return orders.filter((order) => order.reference.toLowerCase().includes(needle));
  }, [orders, query]);

  function openOrder(id: number) {
    navigate(`/orders/${id}`);
  }

  return (
    <section className="page">
      <div className="page-header">
        <h1 className="page-title">Orders</h1>
      </div>

      <div className="toolbar">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder="Search by reference"
          testId="orders-search"
        />
        <div className="field field-inline">
          <label className="field-label" htmlFor="orders-status-filter">
            Status
          </label>
          <select
            id="orders-status-filter"
            className="field-input"
            value={status}
            onChange={(event) => setStatus(event.target.value as OrderStatus | "")}
            data-testid="orders-status-filter"
          >
            <option value="">All statuses</option>
            {ORDER_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading ? (
        <p className="muted" role="status" data-testid="orders-loading">
          Loading orders…
        </p>
      ) : (
        <table className="table" data-testid="orders-table">
          <thead>
            <tr>
              <th scope="col">Reference</th>
              <th scope="col">Status</th>
              <th scope="col">Total</th>
              <th scope="col">Created</th>
            </tr>
          </thead>
          <tbody>
            {visibleOrders.map((order) => (
              <tr
                key={order.id}
                className="table-row-clickable"
                tabIndex={0}
                onClick={() => openOrder(order.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openOrder(order.id);
                  }
                }}
                data-testid={`order-row-${order.id}`}
              >
                <td>{order.reference}</td>
                <td>
                  <span className={`badge badge-${order.status}`}>{order.status}</span>
                </td>
                <td>{formatMoney(order.total_cents, order.currency)}</td>
                <td>{formatDate(order.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
